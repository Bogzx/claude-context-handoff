"""orch serve: the HTTP gateway for the owner's phone app (orch-app docs/API.md, v1).

It binds only to this machine's Tailscale IPv4 (`tailscale ip -4`) on port 7433, never to
0.0.0.0. A request must come from a tailnet node in the allow-list (`tailscale whois`: its
StableID, short name or full name) and carry the pairing token (`Authorization: Bearer ...`);
anything else gets 403 or 401 with an empty body. `orch pair` keeps the token, the allow-list and
the account names in ~/.config/orch/.env (mode 600) and prints the token with a pairing string
for a QR code. `orch serve --allow-self` also admits this machine's own node: for a test only.

Reads: GET /v1/fleet, /v1/usage, /v1/orchestrators/<pane>/decisions,
/v1/orchestrators/<pane>/history, /v1/panes/<pane>/screen, /v1/actions/<id>.
Writes: POST /v1/actions, one action per request (prompt, answer, interrupt, park, unpark, run,
nudge, account, default_account, spill, ack). An action id runs once: a retry gets the first result.
Every action is logged to events.jsonl (kind "action", source "app"). Standard library only,
Python 3.8 compatible (ob1).
"""
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import orch
import orch_usage

PORT = 7433
CONFIG = Path.home() / ".config" / "orch" / ".env"
KEEP = 86400  # action results kept for retries
FRESH = 10 * 60  # older actions are refused; the fleet lists the actions of this period
RECENT_RUN = 86400  # a run that ended this long ago still shows in the fleet
MAX_BODY = 64 * 1024
WHOIS_TTL = 60
EVENTS_TAIL = 8 << 20  # bytes of events.jsonl read for history and the last nudge


def warm_usage(gateway):
    """Keep the transcript scan current, so /v1/usage doesn't pay for a long first scan."""
    orch_usage.scan(orch.ROOT, Path.home() / ".claude" / "projects", orch.now(), orch.agents())


# Jobs for the gateway's background loop: (seconds between runs, function(gateway)), each first
# run 5 s after the start. The next task adds the FCM push sender (encrypted state snapshots,
# alerts and the 15-minute heartbeat) and the relay poller (actions from the mailbox, run through
# Gateway.act(..., source="relay")).
BACKGROUND = [(300, warm_usage)]


# --- config (~/.config/orch/.env) ------------------------------------------------------------

def read_env(path=None):
    out = {}
    try:
        text = Path(path or CONFIG).read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip("'\"")
    return out


def write_env(values, path=None):
    path = Path(path or CONFIG)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in values.items()))
    os.replace(tmp, path)


def items(text):
    return [x.strip() for x in (text or "").split(",") if x.strip()]


def account_names(env):
    """ORCH_ACCOUNT_NAMES="1=pibo1,2=pibo2" -> {"1": "pibo1", "2": "pibo2"}"""
    return dict(x.split("=", 1) for x in items(env.get("ORCH_ACCOUNT_NAMES")) if "=" in x)


# --- tailscale ---------------------------------------------------------------------------------

def tailscale(*args):
    return subprocess.run(["tailscale", *args], capture_output=True, text=True, timeout=10,
                          check=False)


def tailscale_ip():
    try:
        out = tailscale("ip", "-4").stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    return out[0] if out else None


def whois(addr):
    """The names a tailnet address goes by (node StableID, short name, full name), or an empty
    set when tailscale doesn't know it."""
    try:
        node = json.loads(tailscale("whois", "--json", addr).stdout)["Node"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return set()
    names = {node.get("StableID"), node.get("ComputedName"), (node.get("Name") or "").rstrip(".")}
    return {n for n in names if isinstance(n, str) and n}


# --- the pieces of the fleet --------------------------------------------------------------------

def read_events(limit=EVENTS_TAIL):
    """events.jsonl lines from its last `limit` bytes, oldest first."""
    try:
        with open(orch.ROOT / "events.jsonl", "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - limit))
            lines = f.read().splitlines()[1 if size > limit else 0:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def state_of(v):
    """The six agent states of the app (as in `orch status`'s table)."""
    s = v["state"]
    if s in ("idle", "done"):
        return "parked" if v["park"] else "own-job" if v["bg"] else "idle"
    return s if s in ("working", "blocked", "missing") else "working"  # herdr "unknown"


def report_json(report, role):
    if not report:
        return None
    out = dict(report)
    if role == "orchestrator":
        out.setdefault("source", "report")
    else:
        out["changed_files"] = [f if isinstance(f, dict) else {"path": f}
                                for f in report.get("changed_files") or []]
    return out


def agent_json(v, agent, role, first_seen):
    state = state_of(v)
    since = v["since"] if v["state"] in ("idle", "done") else None
    if since is None:  # the watchdog's first sight of this state (it looks every 5 min)
        since = first_seen.get(f"{v['pane']}:{v['state']}:{v['session']}:{v['seq']}")
    park = v["park"]
    return {
        "pane": v["pane"], "label": v["label"], "state": state,
        "agent_status": (agent or {}).get("agent_status") if agent else None,
        "since": since, "session": v["session"], "account": v["account"],
        "context_tokens": v.get("context_tokens"),
        "background_tasks": v["bg"], "last": v["last"],
        "park": {"reason": park.get("reason"), "since": park.get("since"),
                 "until": park.get("until")} if park else None,
        "switch": v["switch"], "report": report_json(v["report"], role),
    }


def question_json(q):
    return {"id": q.get("id"), "pane": q.get("pane"), "time": q.get("time"),
            "text": q.get("text") or "", "options": q.get("options") or [],
            "evidence": q.get("evidence") or [], "rule": q.get("rule")}


def screen_lines(pane, lines, source="recent"):
    """The pane's last `lines` lines of terminal text, or None when herdr can't read it."""
    try:
        r = orch.herdr("agent", "read", pane, "--source", source, "--lines", str(lines),
                       "--format", "text", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if not r or r.returncode != 0:
        return None
    return r.stdout.rstrip("\n").split("\n")[-lines:]


def dialog_json(a, agent):
    report = a["report"] or {}
    return {"pane": a["pane"], "label": a["label"], "since": a["since"],
            "change": (agent or {}).get("state_change_seq"),
            "text": "\n".join(screen_lines(a["pane"], 40, "visible") or []),
            "doing": report.get("summary") or orch.first_line(a["last"], 160),
            "danger": True}  # no rule yet: every dialog answer is treated as dangerous


def stall_json(reasons, workers, report, nudged_at):
    roadmap = (report or {}).get("roadmap") or []
    labels = {w["pane"]: w["label"] for w in workers}
    out = []
    for key in sorted(reasons):
        target, kind = key.rsplit(":", 1)
        r = {"key": key, "kind": kind, "text": reasons[key]}
        fix = message = None
        if target != "orch":
            r["pane"] = target
        if kind == "idle":
            label = labels.get(target) or target
            item = next((i for i in roadmap if i.get("owner") in (label, target)
                         and i.get("status") in ("next", "doing")), None)
            if item:
                fix = f"Assign: {item.get('title')}"
                message = f"give {label} its next task: {item.get('title')}"
        elif kind == "nowake":
            fix, message = "Ask it to arm one orch wait", "then arm one orch wait"
        r.update(fix=fix, message=message, picked=fix is not None)
        out.append(r)
    return {"nudged_at": nudged_at, "reasons": out}


def generations(pane):
    """The handoff generations (gen-NN.md) of the pane's newest lineage."""
    log = orch.lineage_of(pane)
    out = []
    for path in sorted(log.parent.glob("gen-*.md")) if log else []:
        m = re.fullmatch(r"gen-(\d+)\.md", path.name)
        if not m:
            continue
        text = path.read_text(errors="replace")
        title = next((x[2:].strip() for x in text.splitlines() if x.startswith("# ")), "")
        steps, inside = [], False
        for line in text.splitlines():
            if line.startswith("#"):
                inside = "next steps" in line.lower()
                continue
            item = re.match(r"(?:\d+\.|[-*])\s+(.*)", line) if inside else None
            if item:
                steps.append(item.group(1).strip())
        out.append({"generation": int(m.group(1)), "time": path.stat().st_mtime,
                    "note": re.sub(r"^Handoff:\s*", "", title), "next": steps})
    return out


# --- the gateway ---------------------------------------------------------------------------------

class Reject(Exception):
    def __init__(self, text, code=409, status="rejected"):
        super().__init__(text)
        self.text, self.code, self.status = text, code, status


def need(body, field, kind=str):
    value = body.get(field)
    if not isinstance(value, kind) or (kind is str and not value.strip()):
        raise Reject(f"{field} is missing", 400)
    return value


class Gateway:
    def __init__(self, allow_self=False, config=None):
        self.config = config
        self.lock = threading.Lock()  # the action log, and one action at a time
        self.seen = {}  # whois cache: address -> (time, names)
        self.self_names = whois(tailscale_ip() or "") if allow_self else set()

    # auth

    def names(self, addr):
        at = time.monotonic()
        hit = self.seen.get(addr)
        if not hit or at - hit[0] > WHOIS_TTL:
            hit = self.seen[addr] = (at, whois(addr))
        return hit[1]

    def authorize(self, addr, header):
        """None when the caller may go on, else the HTTP status to refuse it with."""
        env = read_env(self.config)
        allowed = set(items(env.get("ORCH_ALLOW"))) | self.self_names
        if not self.names(addr) & allowed:
            return 403
        token = env.get("ORCH_TOKEN") or ""
        given = header[7:].strip() if header.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(given.encode(), token.encode()):
            return 401
        return None

    # routes

    def route(self, method, path, query, body):
        """(status, JSON body) for an authorized request."""
        parts = [unquote(p) for p in path.split("/") if p]
        arg = {k: v[-1] for k, v in query.items()}
        if parts[:1] != ["v1"]:
            return 404, {"error": "not found"}
        parts = parts[1:]
        if method == "POST" and parts == ["actions"]:
            return self.act(body)
        if method != "GET":
            return 405, {"error": "method not allowed"}
        try:
            if parts == ["fleet"]:
                return self.fleet()
            if parts == ["usage"]:
                return 200, self.usage(float(arg.get("hours") or 168))
            if len(parts) == 3 and parts[0] == "orchestrators" and parts[2] == "decisions":
                return self.decisions(parts[1])
            if len(parts) == 3 and parts[0] == "orchestrators" and parts[2] == "history":
                return 200, self.history(parts[1], float(arg.get("since") or 0),
                                         int(arg.get("limit") or 200))
            if len(parts) == 3 and parts[0] == "panes" and parts[2] == "screen":
                lines = max(1, min(500, int(arg.get("lines") or 15)))
                text = screen_lines(parts[1], lines)
                if text is None:
                    return 404, {"error": f"can't read the screen of {parts[1]}"}
                return 200, {"pane": parts[1], "time": orch.now(), "lines": text}
            if len(parts) == 2 and parts[0] == "actions":
                return self.action_status(parts[1])
        except ValueError:
            return 400, {"error": "bad query parameter"}
        return 404, {"error": "not found"}

    # reads

    def fleet(self):
        at = orch.now()
        ags = orch.agents()
        if ags is None:
            return 503, {"error": "herdr is not reachable"}
        runs = [r for r in orch.all_runs() if r.get("orchestrator")]
        runs = [r for r in runs if r["orchestrator"] in ags or orch.active(r, at)
                or (r.get("ended") or r.get("until") or 0) > at - RECENT_RUN]
        busy = {p for r in runs for p in r.get("workers") or {}} | {r["orchestrator"] for r in runs}
        watchdog = orch.load(orch.ROOT / "watchdog.json")
        nudges = {e.get("pane"): e.get("time") for e in read_events() if e.get("kind") == "nudge"}
        leads = [(r["orchestrator"], r) for r in runs] + [(p, None) for p in sorted(ags)
                                                          if p not in busy]
        return 200, {
            "version": 1, "machine": socket.gethostname(), "time": at,
            "orchestrators": [self.orchestrator(p, r, ags, at, watchdog.get(p) or {}, nudges)
                              for p, r in leads],
            "accounts": self.accounts(at), "default_account": self.default_account(),
            "spill": orch.load(orch.ROOT / "accounts.json").get("spill"),
            "actions": self.recent_actions(at, ags),
        }

    def orchestrator(self, pane, run, ags, at, ws, nudges):
        first = ws.get("first_seen") or {}
        agent = ags.get(pane)
        v = orch.view(pane, agent, None, at)
        lead = agent_json(v, agent, "orchestrator", first)
        workers = [agent_json(orch.view(p, ags.get(p), run, at), ags.get(p), "worker", first)
                   for p in (run or {}).get("workers") or {}]
        stall = None
        if run and not orch.active(run, at):
            activity, since = "done", run.get("ended") or run.get("until")
        elif lead["state"] == "missing":
            activity, since = "offline", None
        elif lead["state"] in ("working", "blocked"):
            activity, since = ("working" if lead["state"] == "working" else "waiting"), None
        else:
            reasons = orch.nudge_reasons(run, ags, at, copy.deepcopy(ws)) if run else {}
            activity, since = ("stalled" if reasons else "waiting"), lead["since"]
            if reasons:
                stall = stall_json(reasons, workers, v["report"], nudges.get(pane))
        name = (agent or {}).get("name") or orch.first_line((run or {}).get("goal") or "", 60)
        return {
            "pane": pane, "name": name or pane, "machine": socket.gethostname(),
            "cwd": (agent or {}).get("cwd") or (run or {}).get("cwd"),
            "activity": activity, "activity_since": since, "agent": lead,
            "run": {k: run.get(k) for k in ("goal", "created", "until", "ended")} if run else None,
            "report": lead["report"],
            "questions": [question_json(q) for q in (run or {}).get("questions") or []
                          if not q.get("answered")],
            "dialogs": [dialog_json(a, ags.get(a["pane"])) for a in [lead, *workers]
                        if a["state"] == "blocked"],
            "stall": stall, "workers": workers,
        }

    def default_account(self):
        return orch.load(orch.ROOT / "accounts.json").get("default") or "1"

    def accounts(self, at):
        """Account id -> the API's Account: windows from usage.json with today's samples."""
        names = account_names(read_env(self.config))
        known = orch.account_list()
        windows = orch_usage.windows_report(orch.ROOT, at)
        history = orch_usage.history(orch.ROOT, at - 86400)
        out = {}
        for acc in sorted(set(known) | set(windows), key=lambda a: (len(a), a)):
            w = windows.get(acc) or {"updated": None, "stale": True, "windows": {}}
            for name, win in w["windows"].items():
                win["samples"] = [
                    [h["time"], h["windows"][name].get("used_percentage")]
                    for h in history if h.get("account") == acc
                    and isinstance((h.get("windows") or {}).get(name), dict)
                    and abs((h["windows"][name].get("resets_at") or 0)
                            - (win.get("resets_at") or 0)) < 120]
            out[acc] = {"id": acc, "name": names.get(acc) or f"account {acc}",
                        "config_dir": "~/.claude" if acc == "1" else f"~/.claude-{acc}",
                        "logged_in": (known.get(acc) or {}).get("logged_in", False),
                        "updated": w["updated"], "stale": w["stale"], "windows": w["windows"]}
        return out

    def usage(self, hours):
        """`orch usage --json` for the period, with the API's per-agent numbers: tokens, cost and
        the points of the agent's account's week: its share of the current week window
        (points_7d), and its cost in the last hour and today at the window's points per USD."""
        at = orch.now()
        ags = orch.agents()
        projects = Path.home() / ".claude" / "projects"
        rep = orch_usage.report(orch.ROOT, projects, ags, at, hours)
        midnight = time.mktime(time.localtime(at)[:3] + (0, 0, 0, 0, 0, -1))
        periods = {"points_last_hour": 1.0, "points_today": (at - midnight) / 3600}
        costs = {k: {self.key(a): a["cost"] for a in
                     orch_usage.report(orch.ROOT, projects, ags, at, h)["agents"]}
                 for k, h in periods.items()}
        shares = {acc: ((info.get("windows") or {}).get("seven_day") or {}).get("agents") or {}
                  for acc, info in rep["accounts"].items()}
        accounts = self.accounts(at)
        ratio = {}  # account -> week points per USD of this machine's transcripts
        for acc, info in rep["accounts"].items():
            week = (info.get("windows") or {}).get("seven_day") or {}
            if acc in accounts and "seven_day" in accounts[acc]["windows"]:
                accounts[acc]["windows"]["seven_day"].update(
                    agents=week.get("agents") or {}, machine_cost_usd=week.get("machine_cost_usd"))
            if week.get("machine_cost_usd") and week.get("used_percentage") is not None:
                ratio[acc] = week["used_percentage"] / week["machine_cost_usd"]
        agents, other = [], {}
        for a in rep["agents"]:
            account = a["accounts"][-1] if a["accounts"] else None
            a.update(account=account, model=a["models"][-1] if a["models"] else None,
                     orchestrator=(a["run"] or "").split("@")[0] or None,
                     tokens={k: a[k] for k in ("input", "output", "cache_read", "cache_write")},
                     cost_usd=a["cost"])
            for k in periods:
                cost = costs[k].get(self.key(a), 0)
                a[k] = round(cost * ratio[account], 2) if account in ratio else None
            a["points_7d"] = ((shares[account].get(self.key(a)) or {}).get("estimated_points", 0)
                              if account in ratio else None)
            if a["role"]:
                agents.append(a)
                continue
            o = other.setdefault(account, {"label": "Other sessions", "account": account,
                                           "sessions": 0, "points_7d": 0, **{k: 0 for k in periods}})
            o["sessions"] += len(a["sessions"])
            for k in (*periods, "points_7d"):
                o[k] = None if a[k] is None or o[k] is None else round(o[k] + a[k], 2)
        for o in other.values():
            o["note"] = f"{o.pop('sessions')} sessions outside a run on {socket.gethostname()}"
        for run in rep["runs"].values():
            lead = next((a for a in agents if a["pane"] == run["orchestrator"]), None)
            account = (lead or {}).get("account")
            run["week_points"] = (round(run["cost"] * ratio[account], 2) if account in ratio
                                  else None)
        rep.update(machine=socket.gethostname(), accounts=accounts, agents=agents,
                   other=list(other.values()), default_account=self.default_account())
        return rep

    @staticmethod
    def key(a):
        return a["pane"] or f"session:{a['sessions'][0][:8]}"

    def decisions(self, pane):
        log = orch.lineage_of(pane)
        if not log:
            return 404, {"error": f"no decision log for {pane}"}
        seen = orch.load(orch.ROOT / "decisions-seen.json").get(pane) or {}
        return 200, {"pane": pane, "log": str(log), "seen": sorted(seen),
                     "decisions": orch.parse_decisions(log.read_text(errors="replace"))}

    def history(self, pane, since, limit):
        panes = {pane, *(orch.load_run(pane).get("workers") or {})}
        events = [e for e in read_events() if e.get("pane") in panes
                  and (e.get("time") or 0) >= since]
        return {"events": events[::-1][:max(1, min(limit, 2000))],
                "generations": generations(pane)}

    # actions

    def load_actions(self):
        return orch.load(orch.ROOT / "gateway-actions.json")

    def save_actions(self, log, at):
        for aid in [a for a, r in log.items() if (r.get("received") or 0) < at - KEEP]:
            log.pop(aid)
        orch.save(orch.ROOT / "gateway-actions.json", log)

    @staticmethod
    def status_json(rec):
        return {k: rec.get(k) for k in ("id", "status", "delivered_at", "picked_at", "error")}

    def picked(self, log, ags, at):
        """Mark delivered actions whose target agent has started a turn since (its herdr
        state_change_seq moved on) as picked; returns whether any changed."""
        changed = False
        for rec in log.values():
            agent = (ags or {}).get(rec.get("pane") or "")
            if (rec.get("status") == "delivered" and rec.get("change") is not None and agent
                    and (agent.get("state_change_seq") or 0) > rec["change"]):
                rec.update(status="picked", picked_at=at)
                changed = True
        return changed

    def recent_actions(self, at, ags):
        with self.lock:
            log = self.load_actions()
            if self.picked(log, ags, at):
                self.save_actions(log, at)
        return [self.status_json(r) for r in log.values() if (r.get("received") or 0) >= at - FRESH]

    def action_status(self, aid):
        with self.lock:
            log = self.load_actions()
            if aid in log and self.picked(log, orch.agents(), orch.now()):
                self.save_actions(log, orch.now())
        if aid not in log:
            return 404, {"error": f"no action {aid}"}
        return 200, self.status_json(log[aid])

    def act(self, body, source="app"):
        """Run one action from the app (or, later, the relay) at most once per id: (HTTP
        status, ActionStatus). A repeated id gets the first result without running again."""
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not body["id"]:
            return 400, {"id": None, "status": "rejected", "error": "an action needs an id"}
        aid = body["id"]
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with self.lock:
            log = self.load_actions()
            old = log.get(aid)
            if old:
                if old.get("fingerprint") != fingerprint:
                    return 409, {"id": aid, "status": "rejected",
                                 "error": "this id was already used for another action"}
                return old["code"], self.status_json(old)
            at = orch.now()
            rec = {"id": aid, "kind": body.get("kind"), "time": body.get("time"), "received": at,
                   "source": source, "fingerprint": fingerprint, "delivered_at": None,
                   "picked_at": None, "error": None}
            try:
                sent = body.get("time")
                if not isinstance(sent, (int, float)):
                    raise Reject("time is missing", 400)
                if at - sent > FRESH:
                    raise Reject(f"sent {orch.ago(at - sent)} ago; actions expire after "
                                 f"{FRESH // 60} minutes")
                handler = ACTIONS.get(body.get("kind"))
                if not handler:
                    raise Reject(f"unknown action kind {body.get('kind')!r}", 400)
                tracked = handler(self, body, rec) or {}
                rec.update(status="delivered", delivered_at=orch.now(), code=202, **tracked)
            except Reject as e:
                rec.update(status=e.status, error=e.text, code=e.code)
            except orch.Refused as e:
                rec.update(status="rejected", error=e.text, code=409)
            log[aid] = rec
            self.save_actions(log, at)
            self.log_action(rec, body, source)
        return rec["code"], self.status_json(rec)

    def log_action(self, rec, body, source):
        fields = {k: v for k, v in body.items() if k not in ("id", "kind", "time")}
        text = fields.get("text") or fields.get("reason") or ""
        orch.emit("action", f"App action {rec['kind']}: {rec['status']}"
                  + (f" ({rec['error']})" if rec.get("error") else ""), text,
                  rec.get("pane") or body.get("pane") or body.get("orchestrator"),
                  source=source, id=rec["id"], action=rec["kind"], status=rec["status"],
                  error=rec.get("error"), fields=fields)

    def finish(self, aid, error):
        """The end of an action that runs in the background (an account switch now)."""
        with self.lock:
            log = self.load_actions()
            rec = log.get(aid)
            if not rec:
                return
            at = orch.now()
            if error:
                rec.update(status="failed", error=error)
            else:
                rec.update(status="picked", picked_at=at)
            self.save_actions(log, at)


def target(pane):
    """The fields that let the action log tell when the pane's agent picks the action up."""
    agent = (orch.agents() or {}).get(pane) or {}
    return {"pane": pane, "change": agent.get("state_change_seq")}


def act_prompt(gw, body, rec):
    pane, text = need(body, "pane"), need(body, "text")
    out = target(pane)
    if not orch.prompt(pane, text):
        raise Reject(f"could not prompt {pane}", 502, "failed")
    return out


def act_answer(gw, body, rec):
    qid, text = need(body, "question"), need(body, "text")
    run, q = orch.find_question(qid)
    out = target((q or {}).get("pane") or (run or {}).get("orchestrator") or "")
    orch.answer(qid, text)
    return out


def act_interrupt(gw, body, rec):
    pane = need(body, "pane")
    out = target(pane)
    result = orch.herdr("pane", "send-keys", pane, "esc")
    if not result or result.returncode != 0:
        raise Reject(f"could not send Esc to {pane}", 502, "failed")
    return out


def act_park(gw, body, rec):
    reason = body.get("reason") if isinstance(body.get("reason"), str) else ""
    until = body.get("until") if isinstance(body.get("until"), str) else None
    orch.park(need(body, "orchestrator"), need(body, "pane"), body["kind"],
              reason.strip() or "parked from the app", until)
    return {"pane": body["pane"]}


def act_run(gw, body, rec):
    lead, op = need(body, "orchestrator"), need(body, "op")
    run = orch.load_run(lead)
    if not run:
        raise Reject(f"no run registered for {lead}")
    at = orch.now()
    if op == "stop":
        run["ended"] = at
    elif op == "extend":
        until = body.get("until") if isinstance(body.get("until"), str) else "+1h"
        m = re.fullmatch(r"\+(\d+(?:\.\d+)?)([mh])", until.strip())
        if m:  # +1h extends the end, not now, so it never shortens the run
            run["until"] = (max(run.get("until") or 0, at)
                            + float(m.group(1)) * (60 if m.group(2) == "m" else 3600))
        else:
            run["until"] = orch.parse_until(until, at)
        run["ended"] = None
    else:
        raise Reject("op is extend or stop", 400)
    orch.save_run(lead, run, "stop" if op == "stop" else "set")
    return {"pane": lead}


def act_nudge(gw, body, rec):
    lead, text = need(body, "orchestrator"), need(body, "text")
    out = target(lead)
    if not orch.prompt(lead, text):
        raise Reject(f"could not prompt {lead}", 502, "failed")
    return out


def act_account(gw, body, rec):
    pane, account, when = need(body, "pane"), need(body, "account"), need(body, "when")
    orch.check_account(account)
    if when in ("idle", "handoff"):
        orch.queue_switch(pane, account, when)
        return {"pane": pane}
    if when != "now":
        raise Reject("when is now, idle or handoff", 400)
    job, why = orch.switch_job(pane, account, (orch.agents() or {}).get(pane))
    if not job:
        raise Reject(why)
    aid = rec["id"]

    def switch():
        try:
            error = orch.apply_switch(job)
        except Exception as e:  # noqa: BLE001 the action must not stay "delivered" forever
            error = f"the switch crashed: {e!r}"
        gw.finish(aid, error)
    threading.Thread(target=switch, daemon=True).start()
    return {"pane": pane}


def act_default_account(gw, body, rec):
    orch.set_default(need(body, "account"))


def act_spill(gw, body, rec):
    orch.set_spill(need(body, "rule"))


def act_ack(gw, body, rec):
    lead, ids = need(body, "orchestrator"), need(body, "decisions", list)
    path = orch.ROOT / "decisions-seen.json"
    seen = orch.load(path)
    mine = seen.setdefault(lead, {})
    for d in ids:
        if isinstance(d, str):
            mine.setdefault(d, orch.now())
    orch.save(path, seen)
    return {"pane": lead}


def act_dialog(gw, body, rec):
    raise Reject("answering a dialog from the app is not supported by this gateway yet", 400)


ACTIONS = {"prompt": act_prompt, "answer": act_answer, "interrupt": act_interrupt,
           "park": act_park, "unpark": act_park, "run": act_run, "nudge": act_nudge,
           "account": act_account, "default_account": act_default_account, "ack": act_ack,
           "spill": act_spill,
           "dialog": act_dialog}


# --- HTTP ----------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "orch"
    sys_version = ""

    def do_GET(self):  # http.server calls do_<METHOD>
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_OPTIONS(self):
        self.dispatch("OPTIONS")

    def dispatch(self, method):
        gw = self.server.gateway
        refused = gw.authorize(self.client_address[0], self.headers.get("Authorization") or "")
        if refused:
            self.reply(refused, None)
            return
        url = urlsplit(self.path)
        body = None
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self.reply(413, {"error": "too large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                self.reply(400, {"id": None, "status": "rejected", "error": "the body is not JSON"})
                return
        try:
            status, out = gw.route(method, url.path, parse_qs(url.query), body)
        except Exception:  # noqa: BLE001 one bad request must not stop the gateway
            traceback.print_exc()
            status, out = 500, {"error": "internal error"}
        self.reply(status, out)

    def reply(self, status, out):
        data = json.dumps(out).encode() if out is not None else b""
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)


def make_server(gateway, addr, port):
    server = ThreadingHTTPServer((addr, port), Handler)
    server.gateway = gateway
    return server


def background(gateway, stop):
    last = {}
    while not stop.wait(5):
        for every, job in BACKGROUND:
            if time.monotonic() - last.get(job, 0) >= every:
                last[job] = time.monotonic()
                try:
                    job(gateway)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()


# --- commands ------------------------------------------------------------------------------------

def serve(args):
    if not read_env().get("ORCH_TOKEN"):
        orch.die(f"not paired: run `orch pair` first (it creates {CONFIG})")
    addr = tailscale_ip()
    if not addr:
        orch.die("no Tailscale IPv4 (`tailscale ip -4`): the gateway listens only on the tailnet")
    gateway = Gateway(allow_self=args.allow_self)
    server = make_server(gateway, addr, PORT)
    print(f"orch serve: http://{addr}:{PORT}/v1/"
          + (" (also allowing this machine's own node: for a test only)" if args.allow_self
             else ""), flush=True)
    stop = threading.Event()
    threading.Thread(target=background, args=(gateway, stop), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


def pair(args):
    env = read_env()
    if args.new or not env.get("ORCH_TOKEN"):
        env["ORCH_TOKEN"] = secrets.token_urlsafe(32)
    if args.allow is not None:
        env["ORCH_ALLOW"] = ",".join(x for a in args.allow for x in items(a))
    write_env(env)
    machine, addr = socket.gethostname(), tailscale_ip() or ""
    print(f"token: {env['ORCH_TOKEN']}")
    print("pairing string (for a QR code): orch://pair?"
          + urlencode({"machine": machine, "addr": addr, "port": PORT,
                       "token": env["ORCH_TOKEN"]}))
    print(f"allowed tailnet nodes: {env.get('ORCH_ALLOW') or 'none yet (orch pair --allow NODE)'}")
    print(f"config: {CONFIG} (ORCH_ACCOUNT_NAMES=1=pibo1,2=pibo2 names the accounts)")
    print("start the gateway: systemctl --user start orch-serve")
    return 0
