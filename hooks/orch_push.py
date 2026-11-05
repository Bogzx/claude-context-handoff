"""Pushes to the owner's phone, and the relay mailbox it answers through, for `orch serve`.

Down: FCM HTTP v1 data messages that this machine sends itself, authorised by the service
account key in ~/.config/orch/firebase-sa.json. Google sees only the envelope
{v, machine, nonce, ct}: the message is zlib-compressed JSON sealed with AES-256-GCM under the
pairing key (ORCH_KEY in ~/.config/orch/.env, in the QR code from `orch pair`).

Up: off the tailnet the phone writes sealed actions into its Firestore mailbox
(mailboxes/<its anonymous uid>/<machine>/<id>). `orch serve` polls it outbound, opens each one,
runs it through Gateway.act(..., source="relay") with the gateway's own rules (an id runs once,
actions older than 10 minutes and answers to closed questions are refused) and deletes it.

The app side is specified in orch-app docs/PUSH.md. The crypto uses the `cryptography`
package (Debian/Ubuntu: python3-cryptography; it works with 2.8, Ubuntu 20.04's). It is imported
only here and only when a push or the relay runs, so the rest of orch works without it.
Python 3.8 compatible.
"""
import base64
import copy
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlencode

import orch

V = "1"  # envelope version
SA_FILE = Path.home() / ".config" / "orch" / "firebase-sa.json"
PHONES = Path.home() / ".config" / "orch" / "phones.json"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/firebase.messaging https://www.googleapis.com/auth/datastore"
FIRESTORE = "https://firestore.googleapis.com/v1"
DATA_LIMIT = 4000  # FCM data messages hold 4096 bytes of keys and values
HIGH = {"question", "dialog"}  # pushes that post an alerting notification (and a failed start)
COLLAPSIBLE = {"state", "accounts", "heartbeat"}  # a newer value replaces one not yet sent
SAME_SEQ = COLLAPSIBLE | {"test"}  # carry the current seq: `test` reaches one phone, another
# paired phone would owe the number for good
ACTIONABLE = {"question", "dialog", "stall", "usage", "test"}  # an answer may follow soon
TTL = {"heartbeat": 900, "test": 300, "actions": 3600}
DEFAULT_TTL = 86400
STATE_EVERY = 60  # at most one state snapshot per orchestrator a minute
ACCOUNTS_EVERY = 300
HEARTBEAT_EVERY = 600  # the app marks a machine stale after 15 minutes without a push
DIALOG_AFTER = 30  # a dialog open this long is pushed (DESIGN.md 4: Blocked)
STALL_AFTER = 5 * 60  # nudged by the watchdog this long ago and still stalled
SAMPLES = 2 * 3600  # an accounts push carries the samples of the last 2 hours
BACKLOG = 3600  # older events are not pushed when the gateway catches up
KEEP_DONE = 8 * 86400  # what was pushed once is remembered longer than a week window
PUSH_EVERY = 15
RELAY_SLOW, RELAY_FAST, FAST_FOR = 30, 5, 600  # seconds; fast for 10 min after an actionable push
ENDED = {"run-ended", "run-stop"}
SYSTEM = {"respawn-failed", "account-failed", "respawn-dialog", "orch-missing", "orch-blocked",
          "orch-not-acting", "test"}


class Unavailable(Exception):
    """Pushes can't run on this machine; the text says why in one sentence."""


def crypto():
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise Unavailable("pushes need the Python package `cryptography` (Debian/Ubuntu: "
                          "python3-cryptography), which is not installed") from None
    return SimpleNamespace(AESGCM=AESGCM, backend=default_backend, hashes=hashes,
                           serialization=serialization, padding=padding)


# --- the sealed envelope --------------------------------------------------------------------

def b64e(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def dumps(obj):
    return json.dumps(obj, separators=(",", ":"))


def aad(direction, machine):
    """GCM associated data: binds the envelope's version, its direction ("push" from the machine,
    "relay" from the phone) and the machine, so none of them can be swapped."""
    return f"orch:{V}:{direction}:{machine}".encode()


def seal(key, direction, machine, obj):
    nonce = os.urandom(12)
    ct = crypto().AESGCM(key).encrypt(nonce, zlib.compress(dumps(obj).encode(), 9),
                                      aad(direction, machine))
    return {"v": V, "machine": machine, "nonce": b64e(nonce), "ct": b64e(ct)}


def unseal(key, direction, machine, envelope):
    """The object in an envelope; raises when it isn't one sealed with this key for this
    direction and machine."""
    if envelope.get("v") != V:
        raise ValueError(f"envelope version {envelope.get('v')!r}")
    plain = crypto().AESGCM(key).decrypt(b64d(envelope["nonce"]), b64d(envelope["ct"]),
                                          aad(direction, machine))
    inflate = zlib.decompressobj()
    data = inflate.decompress(plain, 1 << 20)
    if inflate.unconsumed_tail:
        raise ValueError("over 1 MB uncompressed")
    return json.loads(data)


def sealed_size(machine, obj):
    """FCM's count (keys and values) for the data of seal(..., obj), without encrypting it."""
    ct = len(zlib.compress(dumps(obj).encode(), 9)) + 16
    return len("vmachinenoncect") + len(V) + len(machine) + 16 + (ct * 4 + 2) // 3


# --- Google: OAuth from the service account, FCM, Firestore -------------------------------

def http(method, url, body=None, headers=None, form=False, timeout=20):
    """(HTTP status, JSON body); status 0 when the request didn't get an answer."""
    data = None
    if body is not None:
        data = urlencode(body).encode() if form else dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=dict(
        {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"},
        **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (OSError, ValueError) as e:  # URLError, timeouts
        return 0, {"error": {"status": "NETWORK", "message": str(e)}}
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, {"error": {"message": raw[:200].decode(errors="replace")}}


def error_code(status, out):
    """(code, message) of a Google API error: FCM's errorCode when given, else the status."""
    err = (out or {}).get("error") or {}
    codes = [d.get("errorCode") for d in err.get("details") or []
             if isinstance(d, dict) and d.get("errorCode")]
    return (codes[0] if codes else err.get("status") or str(status)), err.get("message") or ""


class Google:
    """The service account's OAuth token and the calls pushes and the relay make."""

    def __init__(self, sa):
        self.sa, self.project, self.access = sa, sa["project_id"], None

    @classmethod
    def load(cls, path=None):
        path = Path(path or SA_FILE)
        try:
            sa = json.loads(path.read_text())
        except OSError:
            raise Unavailable(f"no service account key at {path} (cloud/firebase_setup.py "
                              "writes it)") from None
        except ValueError:
            raise Unavailable(f"{path} is not JSON") from None
        missing = [k for k in ("client_email", "private_key", "private_key_id", "project_id")
                   if not sa.get(k)]
        if missing:
            raise Unavailable(f"{path} has no {', '.join(missing)}")
        return cls(sa)

    def assertion(self, at):
        c = crypto()
        head = {"alg": "RS256", "typ": "JWT", "kid": self.sa["private_key_id"]}
        claims = {"iss": self.sa["client_email"], "scope": SCOPES, "aud": TOKEN_URI,
                  "iat": int(at), "exp": int(at) + 3600}
        signing = f"{b64e(dumps(head).encode())}.{b64e(dumps(claims).encode())}"
        key = c.serialization.load_pem_private_key(self.sa["private_key"].encode(), None,
                                                   c.backend())
        return f"{signing}.{b64e(key.sign(signing.encode(), c.padding.PKCS1v15(), c.hashes.SHA256()))}"

    def token(self):
        at = time.time()
        if self.access and self.access[1] > at + 300:
            return self.access[0], None
        status, out = http("POST", TOKEN_URI, {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": self.assertion(at)}, form=True)
        if status != 200 or not out.get("access_token"):
            return None, (status or 0, out)
        self.access = (out["access_token"], at + float(out.get("expires_in") or 3600))
        return self.access[0], None

    def call(self, method, url, body=None):
        token, failed = self.token()
        if failed:
            return failed
        status, out = http(method, url, body, {"Authorization": f"Bearer {token}"})
        if status == 401:
            self.access = None
        return status, out

    def send(self, fcm_token, data, kind, collapse, high=None):
        android = {"priority": "HIGH" if (kind in HIGH if high is None else high) else "NORMAL",
                   "ttl": f"{TTL.get(kind, DEFAULT_TTL)}s"}
        if collapse:
            android["collapse_key"] = collapse
        return self.call("POST", f"https://fcm.googleapis.com/v1/projects/{self.project}"
                                 "/messages:send",
                         {"message": {"token": fcm_token, "data": data, "android": android}})

    def documents(self, path):
        return self.call("GET", f"{FIRESTORE}/projects/{self.project}/databases/(default)"
                                f"/documents/{path}?pageSize=50")

    def delete(self, name):
        return self.call("DELETE", f"{FIRESTORE}/{name}")


def outcome(status, out):
    """What a failed FCM send means: "token" (this phone's token is dead), "retry" (try again
    on the next pass) or "drop" (this message can't be sent)."""
    code, message = error_code(status, out)
    if code in ("UNREGISTERED", "SENDER_ID_MISMATCH") or (
            code == "INVALID_ARGUMENT" and "registration token" in message.lower()):
        return "token"
    if status in (0, 401, 429) or status >= 500 or code in ("UNAVAILABLE", "INTERNAL",
                                                             "QUOTA_EXCEEDED"):
        return "retry"
    return "drop"


def project():
    """The Firebase project of the service account key, or None."""
    try:
        return json.loads(SA_FILE.read_text()).get("project_id")
    except (OSError, ValueError):
        return None


# --- paired phones (~/.config/orch/phones.json, mode 600) --------------------------------------

def load_phones():
    return orch.load(PHONES)


def save_phones(phones):
    PHONES.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = PHONES.with_name(f".{PHONES.name}.{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(phones, f, indent=1)
    os.replace(tmp, PHONES)


# --- fitting a message into 4 KB -----------------------------------------------------------------

def urgent(message):
    """High priority: a question, a dialog, or a start that failed."""
    return message["kind"] in HIGH or (message["kind"] == "start"
                                       and (message.get("body") or {}).get("status") == "failed")


def _agents(o):
    return [o["agent"], *(o.get("workers") or [])] if isinstance(o.get("agent"), dict) else []


def _cut(text, n):
    return text if len(text or "") <= n else text[:n - 1] + "…"


def _orch_steps():
    def agent_report(o):  # it repeats the orchestrator's `report`
        o["agent"]["report"] = None

    def last(o):
        for a in _agents(o):
            a["last"] = _cut(orch.first_line(a.get("last") or "", 120), 120)

    def worker_reports(o):
        for w in o.get("workers") or []:
            if w.get("report"):
                w["report"] = {k: w["report"].get(k) for k in ("time", "summary", "status")}

    def dialog_text(o):
        for d in o.get("dialogs") or []:
            d["text"] = "\n".join((d.get("text") or "").split("\n")[-12:])

    def report(o):
        if o.get("report"):
            o["report"] = {k: o["report"].get(k) for k in
                           ("time", "summary", "state", "progress", "source")}

    def workers(o):
        for w in o.get("workers") or []:
            w.update(report=None, last="", background_tasks=[])

    def questions(o):
        for q in o.get("questions") or []:
            q.update(text=_cut(q.get("text") or "", 300), evidence=[])
    return [agent_report, last, worker_reports, dialog_text, report, workers, questions]


def _question_steps():
    def evidence(q):
        q["evidence"] = []

    def text(q):
        q["text"] = _cut(q.get("text") or "", 1200)
    return [evidence, text]


def _dialog_steps():
    def text(d):
        d["text"] = "\n".join((d.get("text") or "").split("\n")[-15:])

    def less(d):
        d["text"] = _cut("\n".join((d.get("text") or "").split("\n")[-5:]), 600)
    return [text, less]


def _samples_steps():
    def samples(body):  # an Account (usage) or {"1": Account, ...} (accounts)
        for a in [body] if "windows" in body else body.values():
            for w in (a.get("windows") or {}).values():
                w["samples"] = []
    return [samples]


STEPS = {"state": _orch_steps(), "ended": _orch_steps(), "question": _question_steps(),
         "dialog": _dialog_steps(), "accounts": _samples_steps(), "usage": _samples_steps()}


def fit(machine, message):
    """The message cut down until its sealed form fits an FCM data message: the steps applied
    are listed in its `trimmed`; ["all"] means the body was dropped (the app refreshes)."""
    message = dict(message, sent=orch.now())  # flush() sets the real one, of the same length
    if sealed_size(machine, message) <= DATA_LIMIT:
        return message
    message["body"] = copy.deepcopy(message["body"])
    message["trimmed"] = []
    for step in STEPS.get(message["kind"], []):
        step(message["body"])
        message["trimmed"].append(step.__name__)
        if sealed_size(machine, message) <= DATA_LIMIT:
            return message
    message.update(body=None, trimmed=["all"])
    return message


# --- the push loop and the relay ---------------------------------------------------------------

def digest(obj):
    return hashlib.sha256(dumps(obj).encode()).hexdigest()[:16]


class Pusher:
    """What the gateway pushes, when, and the relay poller. `orch serve` calls tick() every 5 s
    from its background loop; the HTTP side calls test() after a pairing."""

    def __init__(self, gateway, machine=None):
        self.gw = gateway
        self.fixed_machine = machine
        self.lock = threading.Lock()
        self.google = None
        self.error = None
        self.relay = {"last_poll": None, "error": None}
        self.next_push = self.next_poll = self.fast_until = 0.0

    @property
    def machine(self):
        """In the AAD, collapse keys and the mailbox path: the name the phone paired with."""
        return self.fixed_machine or self.gw.machine()

    @staticmethod
    def path():
        return orch.ROOT / "push-state.json"

    def setup(self):
        """(Google, key), or raises Unavailable."""
        key = self.gw.push_key()
        if not key:
            raise Unavailable("no push key: run `orch pair`")
        crypto()
        if self.google is None:
            self.google = Google.load()
        return self.google, key

    @staticmethod
    def targets():
        """Paired phones whose FCM token is not known to be dead."""
        return {pid: p for pid, p in load_phones().items()
                if p.get("fcm_token") and not p.get("token_error")}

    def tick(self):
        mono = time.monotonic()
        acted = False
        if mono >= self.next_poll:
            self.next_poll = mono + (RELAY_FAST if mono < self.fast_until else RELAY_SLOW)
            acted = self.poll()
        if acted or mono >= self.next_push:
            self.next_push = mono + PUSH_EVERY
            self.cycle()

    # pushes

    def cycle(self, at=None):
        """One pass: read what happened since the last one and push what the phone needs."""
        at = orch.now() if at is None else at
        with self.lock:
            st = orch.load(self.path())
            events = self.new_events(st)
            phones = self.targets()
            try:
                if not phones:
                    raise Unavailable("no phone has registered for pushes (POST /v1/pair)")
                google, key = self.setup()
            except Unavailable as e:
                self.error = str(e)
                orch.save(self.path(), st)
                return
            self.error = None
            status, fleet = self.gw.fleet()
            for kind, lead, body in self.plan(st, events, fleet if status == 200 else None, at):
                self.queue(st, phones, kind, lead, body, at)
            self.flush(st, google, key, phones)
            orch.save(self.path(), st)

    @staticmethod
    def new_events(st):
        """events.jsonl lines added since the last pass (none on the very first one)."""
        path = orch.ROOT / "events.jsonl"
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        start = st.get("offset")
        if start is None or start == size:
            st["offset"] = size
            return []
        start = 0 if start > size else start  # the file was replaced
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(min(size - start, 4 << 20))
        end = data.rfind(b"\n") + 1
        st["offset"] = start + end
        out = []
        for line in data[:end].splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
        return out

    def plan(self, st, events, fleet, at):
        """[(kind, orchestrator pane or None, body)] to push now."""
        out = []
        orchs = {o["pane"]: o for o in (fleet or {}).get("orchestrators") or []}
        lead_of = {a["pane"]: o["pane"] for o in orchs.values() for a in _agents(o)}
        for e in events:
            kind, pane = e.get("kind"), e.get("pane")
            if at - (e.get("time") or 0) > BACKLOG:
                continue
            if kind == "ask" and e.get("id"):
                found = self.gw.question(e["id"])
                if found:
                    out.append(("question", *found))
            elif kind in ENDED and pane in orchs:
                out.append(("ended", pane, orchs[pane]))
            elif kind == "start" and e.get("action"):
                out.append(("start", pane, {k: e.get(k) for k in
                                            ("action", "pane", "step", "status", "error", "time")}))
            elif kind == "spill":
                out.append(("spill", None, {"title": e.get("title"), "text": e.get("body"),
                                            "spill": (fleet or {}).get("spill")}))
            elif kind in SYSTEM:
                out.append(("system", lead_of.get(pane, pane),
                            {k: e.get(k) for k in ("time", "kind", "title", "body", "pane")}))
        if fleet:
            out += self.from_fleet(st, fleet, orchs, at)
        if at - (st.get("heartbeat") or 0) >= HEARTBEAT_EVERY:
            st["heartbeat"] = at
            out.append(("heartbeat", None, {
                "states": {p: s.get("sent") for p, s in (st.get("states") or {}).items()},
                "accounts": (st.get("accounts") or {}).get("sent")}))
        return out

    @staticmethod
    def from_fleet(st, fleet, orchs, at):
        out = []
        done = st.setdefault("done", {})

        def once(key):
            fresh = key not in done
            done[key] = done.get(key, at)
            return fresh

        blocked, open_now = st.get("blocked") or {}, {}
        states = st.setdefault("states", {})
        for pane, o in orchs.items():
            for d in o.get("dialogs") or []:
                key = f"{d['pane']}:{d.get('change')}"
                open_now[key] = blocked.get(key, at)
                if at - open_now[key] >= DIALOG_AFTER and once(f"dialog:{key}"):
                    out.append(("dialog", pane, d))
            stall = o.get("stall") or {}
            if (stall.get("nudged_at") and at - stall["nudged_at"] >= STALL_AFTER
                    and once(f"stall:{pane}:{stall['nudged_at']}")):
                out.append(("stall", pane, stall))
            h, last = digest(o), states.get(pane) or {}
            if h != last.get("hash") and at - (last.get("sent") or 0) >= STATE_EVERY:
                states[pane] = {"hash": h, "sent": at}
                out.append(("state", pane, o))
        st["blocked"] = open_now
        source = (fleet.get("spill") or {}).get("from")
        accounts = copy.deepcopy(fleet.get("accounts") or {})
        for acc, a in accounts.items():
            for name, w in (a.get("windows") or {}).items():
                w["samples"] = [s for s in w.get("samples") or [] if s[0] >= at - SAMPLES]
                if (acc != source and not a.get("stale") and w.get("full_before_reset")
                        and once(f"usage:{acc}:{name}:{w.get('resets_at')}")):
                    out.append(("usage", None, a))
        h = digest({acc: [a.get("stale"), a.get("logged_in"),
                          {n: round(w.get("used_percentage") or 0)
                           for n, w in (a.get("windows") or {}).items()}]
                    for acc, a in accounts.items()})
        last = st.get("accounts") or {}
        if h != last.get("hash") and at - (last.get("sent") or 0) >= ACCOUNTS_EVERY:
            st["accounts"] = {"hash": h, "sent": at}
            out.append(("accounts", None, accounts))
        pushed = st.get("actions") or {}
        changed = [a for a in fleet.get("actions") or [] if pushed.get(a["id"]) != a["status"]]
        st["actions"] = {a["id"]: a["status"] for a in fleet.get("actions") or []}
        if changed:
            out.append(("actions", None, changed))
        st["done"] = {k: t for k, t in done.items() if t >= at - KEEP_DONE}
        return out

    def queue(self, st, phones, kind, lead, body, at):
        if kind not in SAME_SEQ:
            st["seq"] = (st.get("seq") or 0) + 1
        message = fit(self.machine, {"seq": st.get("seq") or 0, "kind": kind,
                                     "orchestrator": lead, "body": body})
        outbox = st.setdefault("outbox", [])
        if kind in COLLAPSIBLE:  # a newer value replaces one not yet sent
            outbox[:] = [m for m in outbox if (m["message"]["kind"], m["message"]["orchestrator"])
                         != (kind, lead)]
        outbox += [{"phone": pid, "queued": at, "message": message} for pid in phones]
        if kind in ACTIONABLE:
            self.fast_until = time.monotonic() + FAST_FOR
            self.next_poll = min(self.next_poll, time.monotonic() + RELAY_FAST)

    def collapse_key(self, message):
        if message["kind"] in COLLAPSIBLE:
            return f"{self.machine}:{message['kind']}:{message.get('orchestrator') or ''}"
        return None

    def flush(self, st, google, key, phones, only=None):
        """Send the outbox in order; what fails for a passing reason waits for the next pass.
        Returns the time the last message for `only` (a phone id) was sent, if it was."""
        keep, stopped, sent_at = [], set(), None
        for item in st.get("outbox") or []:
            pid, message = item["phone"], item["message"]
            phone = phones.get(pid)
            if not phone or orch.now() - item["queued"] > TTL.get(message["kind"], DEFAULT_TTL):
                continue
            if pid in stopped or (only and pid != only):
                keep.append(item)
                continue
            sent = orch.now()
            status, out = google.send(phone["fcm_token"],
                                      seal(key, "push", self.machine, dict(message, sent=sent)),
                                      message["kind"], self.collapse_key(message),
                                      urgent(message))
            if status == 200:
                st["last_sent"] = sent
                if message["kind"] == "heartbeat":
                    st["last_heartbeat"] = sent
                if pid == only:
                    sent_at = sent
                continue
            why, (code, text) = outcome(status, out), error_code(status, out)
            self.error = f"FCM {code}: {text}"[:300]
            if why == "retry":
                keep.append(item)
                stopped.add(pid)
            elif why == "token":
                self.drop_token(pid, code, text)
                stopped.add(pid)
            elif st.get("last_error") != code:
                orch.emit("push-failed", f"Push not sent: {message['kind']}",
                          f"FCM {code}: {text}", message.get("orchestrator"))
            st["last_error"] = code
        st["outbox"] = [m for m in keep if m["phone"] in phones]
        return sent_at

    @staticmethod
    def drop_token(pid, code, text):
        phones = load_phones()
        if pid in phones:
            phones[pid]["token_error"] = code
            save_phones(phones)
        orch.emit("push-token", f"Phone token refused: {phones.get(pid, {}).get('node') or pid}",
                  f"FCM {code}: {text}; no pushes until the app registers a new token", None)

    def test(self, pid):
        """Push `test` to one phone now (after it paired): {"seq", "sent"}, plus "error" when
        it could not be sent."""
        at = orch.now()
        with self.lock:
            st = orch.load(self.path())
            phones = {k: p for k, p in self.targets().items() if k == pid}
            try:
                if not phones:
                    raise Unavailable("this phone has no FCM token")
                google, key = self.setup()
            except Unavailable as e:
                orch.save(self.path(), st)
                return {"seq": None, "sent": None, "error": str(e)}
            self.queue(st, phones, "test", None, None, at)
            seq = st.get("seq") or 0
            sent = self.flush(st, google, key, self.targets(), only=pid)
            orch.save(self.path(), st)
        return {"seq": seq, "sent": sent, **({} if sent else {"error": self.error})}

    def health(self, pid):
        st = orch.load(self.path())
        phone = load_phones().get(pid) or {}
        out = {"registered": bool(phone.get("fcm_token")) and not phone.get("token_error"),
               "seq": st.get("seq") or 0, "last_sent": st.get("last_sent"),
               "last_heartbeat": st.get("last_heartbeat")}
        if phone.get("token_error"):
            out["error"] = f"FCM refused this phone's token: {phone['token_error']}"
        elif self.error:
            out["error"] = self.error
        return out

    # the relay

    def poll(self, at=None):
        """One relay pass: run the sealed actions in the paired phones' mailboxes for this
        machine, then delete them. Returns whether any arrived."""
        phones = {pid: p for pid, p in load_phones().items() if p.get("relay_uid")}
        if not phones:
            return False
        try:
            google, key = self.setup()
        except Unavailable as e:
            self.relay["error"] = str(e)
            return False
        arrived, errors = False, []
        for pid, phone in sorted(phones.items()):
            status, out = google.documents(f"mailboxes/{quote(phone['relay_uid'], safe='')}/"
                                           f"{quote(self.machine, safe='')}")
            if status != 200:
                errors.append("Firestore {}: {}".format(*error_code(status, out)))
                continue
            for doc in sorted(out.get("documents") or [], key=lambda d: d.get("createTime", "")):
                arrived = True
                self.relay_one(google, key, pid, doc)
        self.relay = {"last_poll": orch.now() if len(errors) < len(phones) else
                      self.relay["last_poll"], "error": "; ".join(errors)[:300] or None}
        if arrived:
            self.fast_until = time.monotonic() + FAST_FOR
        return arrived

    def relay_one(self, google, key, pid, doc):
        fields = doc.get("fields") or {}
        envelope = {k: (fields.get(k) or {}).get("stringValue") for k in ("v", "nonce", "ct")}
        try:
            body = unseal(key, "relay", self.machine, envelope)
        except Exception as e:  # noqa: BLE001 whatever doesn't open with our key is dropped
            orch.emit("relay-rejected", "Relay message dropped",
                      f"{doc.get('name', '').rsplit('/', 1)[-1]}: it does not open with this "
                      f"machine's key ({type(e).__name__})", None)
        else:
            self.gw.act(body, source="relay", phone=pid)
        google.delete(doc["name"])  # a failed delete re-reads it: the action id runs once
