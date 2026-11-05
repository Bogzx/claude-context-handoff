"""orch usage: Claude account windows and tokens per agent, for orch and the owner's app.

`orch usage capture` runs from the statusline command on every refresh, with the statusline
JSON on stdin. It keeps the account's latest rate-limit windows in ~/.claude/handoffs/usage.json
(per account: claude-acc N runs with CLAUDE_CONFIG_DIR=~/.claude-N, unset is account 1) and
appends ~/.claude/handoffs/usage-history.jsonl at most every 5 minutes per account. A statusline
repeats its session's last limits on every refresh for as long as the session stays open, so a
reading is dated by the session's last API response, and only a newer reading replaces it.

`orch usage [--json]` reports per account and window: used %, reset time, burn rate over the
last 1-2 h of history and the projected time to 100%; and tokens per agent from the transcripts
(~/.claude/projects, shared by every account), parsed incrementally into usage-cache.json and
mapped session -> pane -> run through the status files, lineage folders, herdr and the runs.
Costs are API-equivalent estimates from PRICES, not what a subscription bills.
"""
import calendar
import fcntl
import json
import os
import socket
import sys
import time
from pathlib import Path

HISTORY_EVERY = 5 * 60  # usage-history.jsonl: at most one line per account this often
WINDOWS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
BURN_LOOKBACK = 2 * 3600  # the burn rate uses history this far back
BURN_MIN_SPAN = 15 * 60  # and needs at least this much of it
STALE = 10 * 60  # a reading older than this: no session on the account got an API response
TAIL = 1 << 20  # how much of a transcript's end is searched for its last API response
BUCKET = 10 * 60  # token buckets in the cache
RETENTION = 8 * 86400  # buckets kept (covers the 7-day window)
CHUNK = 8 << 20  # transcripts are read this much at a time

# API list prices in USD per million tokens: (input, output, cache read), from Anthropic's
# published model table (cached 2026-10-06). Cache writes cost 1.25x input (5-minute TTL) or 2x
# (1-hour TTL); fast mode costs 2x. Matched by the longest prefix of the model id. Unlisted
# models are counted but not priced. An estimate of the same tokens on the API.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
LONG_PROMPT = {"claude-haiku-5-5": (100_000, 5.0)}  # past this many prompt tokens: prices x5
FIELDS = ("input", "output", "cache_read", "cache_write", "messages", "cost")


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def save(path, data, indent=1):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=indent))
    os.replace(tmp, path)


def account_of(config_dir):
    """The account a CLAUDE_CONFIG_DIR belongs to (claude-acc N), as a string: unset is "1"."""
    if not config_dir:
        return "1"
    path = os.path.normpath(os.path.expanduser(config_dir))
    if path == str(Path.home() / ".claude"):
        return "1"
    name = os.path.basename(path)
    return name[len(".claude-"):] if name.startswith(".claude-") else path


# --- capture (statusline) ----------------------------------------------------------------------

def api_response(line):
    """The transcript line's entry if it is an API response, else None: Claude Code's own
    notices are assistant entries too, with no real model or usage."""
    try:
        e = json.loads(line)
    except ValueError:
        return None
    if not isinstance(e, dict) or e.get("type") != "assistant":
        return None
    m = e.get("message") or {}
    return e if (m.get("model") or "").startswith("claude") and m.get("usage") else None


def last_response(path):
    """When the transcript's session last got an API response, or None when the end of the file
    shows none. Not the file's time: an idle session goes on writing its own entries."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - TAIL))
            lines = f.read().splitlines()
    except (OSError, TypeError, ValueError):
        return None
    for line in reversed(lines):
        e = api_response(line) if b'"assistant"' in line else None
        if e:
            return stamp(e.get("timestamp") or "")
    return None


def capture(text, env, root, at):
    """Record the rate-limit windows of the statusline JSON `text` for the session's account."""
    try:
        data = json.loads(text)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    account = account_of(env.get("CLAUDE_CONFIG_DIR"))
    path = root / "usage.json"
    cur = load(path)
    changed = False
    session = data.get("session_id")
    sessions = cur.setdefault("sessions", {})
    if session and (sessions.get(session) or {}).get("account") != account:
        sessions[session] = {"account": account, "time": at}
        for sid in [s for s, v in sessions.items() if v.get("time", 0) < at - RETENTION]:
            sessions.pop(sid)
        changed = True
    limits = data.get("rate_limits") or {}
    windows = {k: v for k, v in limits.items() if isinstance(v, dict)}
    read = last_response(data.get("transcript_path")) if windows else None
    entry = (cur.get("accounts") or {}).get(account) or {}
    newest = entry.get("read_at", 0)
    if read and (read > newest or (read == newest and windows != entry.get("windows"))):
        due = read - entry.get("history_time", 0) >= HISTORY_EVERY
        cur.setdefault("accounts", {})[account] = {
            "read_at": read, "windows": windows, "config_dir": env.get("CLAUDE_CONFIG_DIR") or "",
            "history_time": read if due else entry.get("history_time", 0)}
        if due:
            root.mkdir(parents=True, exist_ok=True)
            line = json.dumps({"time": read, "account": account, "windows": windows}) + "\n"
            with open(root / "usage-history.jsonl", "a+b") as f:
                if f.seek(0, os.SEEK_END):  # a record cut short by a killed capture
                    f.seek(-1, os.SEEK_END)  # stays alone on its line
                    if f.read(1) != b"\n":
                        line = "\n" + line
                f.write(line.encode())
        changed = True
    if changed:
        save(path, cur)


# --- windows -------------------------------------------------------------------------------------

def history(root, since):
    """usage-history.jsonl lines from `since` on (reads only the tail of the file)."""
    try:
        with open(root / "usage-history.jsonl", "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - (1 << 20)))
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            h = json.loads(line)
        except ValueError:
            continue
        if isinstance(h, dict) and h.get("time", 0) >= since:
            out.append(h)
    return out


def window_report(name, latest, points, at):
    """One window of one account: used %, reset, burn rate and the projection to 100%."""
    used, resets = latest.get("used_percentage"), latest.get("resets_at")
    out = {"used_percentage": used, "resets_at": resets, "reset": bool(resets and resets <= at),
           "burn_per_hour": None, "projected_full_at": None, "full_before_reset": None,
           "basis_minutes": None}
    if used is None or out["reset"]:
        return out
    same = [(p["time"], p["windows"][name]["used_percentage"]) for p in points
            if (p.get("windows") or {}).get(name, {}).get("used_percentage") is not None
            and abs((p["windows"][name].get("resets_at") or 0) - (resets or 0)) < 120]
    if not same:
        return out
    t0, p0 = min(same)
    t1, p1 = max(same)
    if t1 - t0 < BURN_MIN_SPAN:
        return out
    rate = (p1 - p0) / ((t1 - t0) / 3600)
    out.update(burn_per_hour=round(rate, 2), basis_minutes=round((t1 - t0) / 60))
    if rate > 0:
        full = t1 + (100 - used) / rate * 3600
        out.update(projected_full_at=round(full),
                   full_before_reset=bool(resets) and full < resets)
    return out


def windows_report(root, at):
    cur = load(root / "usage.json")
    points = history(root, at - BURN_LOOKBACK)
    out = {}
    for account, entry in sorted((cur.get("accounts") or {}).items()):
        read = entry.get("read_at")
        if not read:  # from before readings were dated: its age is unknown
            continue
        mine = [p for p in points if p.get("account") == account]
        latest = {"time": read, "windows": entry.get("windows") or {}}
        out[account] = {
            "updated": read,
            "stale": at - read > STALE,
            "windows": {name: window_report(name, w, mine + [latest], at)
                        for name, w in (entry.get("windows") or {}).items()
                        if isinstance(w, dict)},
        }
    return out


# --- transcripts ---------------------------------------------------------------------------------

def price_key(model):
    return max((k for k in PRICES if model.startswith(k)), key=len, default=None)


def price(model, counts, fast):
    """API-equivalent USD for one message's (input, output, cache read, write 5m, write 1h)."""
    key = price_key(model)
    if key is None:
        return None
    inp, out, read = PRICES[key]
    i, o, r, w5, w1 = counts
    limit, factor = LONG_PROMPT.get(key, (None, 1))
    scale = (factor if limit and i + r + w5 + w1 > limit else 1) * (2 if fast else 1)
    return scale * (i * inp + o * out + r * read + w5 * inp * 1.25 + w1 * inp * 2) / 1e6


def stamp(text):
    try:
        return calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def counts_of(usage):
    cache = usage.get("cache_creation") or {}
    w1 = cache.get("ephemeral_1h_input_tokens") or 0
    w5 = cache.get("ephemeral_5m_input_tokens")
    if w5 is None:
        w5 = max(0, (usage.get("cache_creation_input_tokens") or 0) - w1)
    return [usage.get("input_tokens") or 0, usage.get("output_tokens") or 0,
            usage.get("cache_read_input_tokens") or 0, w5, w1]


def add(bucket, delta, messages, cost):
    i, o, r, w5, w1 = delta
    for k, v in zip(FIELDS, (i, o, r, w5 + w1, messages, cost or 0)):
        bucket[k] = bucket.get(k, 0) + v


def scan_file(path, session, state, buckets, at):
    """Parse what was appended to one transcript since the last scan into the session's
    10-minute buckets. A message is written once per content block with the same usage, so a
    repeat of the last message id only adds what grew."""
    try:
        st = path.stat()
    except OSError:
        return
    offset = state.get("offset", 0)
    if st.st_size < offset:
        offset = 0  # rewritten
    if st.st_size == offset:
        return
    if not offset and st.st_mtime < at - RETENTION:
        state["offset"] = st.st_size  # nothing recent in it
        return
    mine = buckets.setdefault(session, {})
    with open(path, "rb") as f:
        f.seek(offset)
        rest = b""
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            data = rest + chunk
            end = data.rfind(b"\n") + 1
            rest = data[end:]
            offset += end
            for line in data[:end].splitlines():
                if b'"usage"' in line and b'"assistant"' in line:
                    take_line(line, state, mine, at)
    state["offset"] = offset


def take_line(line, state, mine, at):
    e = api_response(line)
    if not e:
        return
    m = e["message"]
    model, usage = m["model"], m["usage"]
    counts = counts_of(usage)
    mid = m.get("id") or e.get("requestId")
    if mid and mid == state.get("last_id"):
        delta = [max(0, a - b) for a, b in zip(counts, state["last"])]
        state["last"] = [max(a, b) for a, b in zip(counts, state["last"])]
        messages = 0
    else:
        delta, messages = counts, 1
        state["last_id"], state["last"] = mid, counts
    ts = stamp(e.get("timestamp") or "")
    if ts is None or ts < at - RETENTION or not (any(delta) or messages):
        return
    slot = str(int(ts // BUCKET * BUCKET))
    cost = price(model, delta, usage.get("speed") == "fast")
    add(mine.setdefault(slot, {}).setdefault(model, {}), delta, messages, cost)


def transcripts(projects):
    """(path, session) of every transcript, subagents' included (they count for the session)."""
    for path in projects.glob("*/*.jsonl"):
        yield path, path.stem
    for path in projects.glob("*/*/subagents/*.jsonl"):
        yield path, path.parent.parent.name


def scan(root, projects, at, agents):
    """Bring usage-cache.json up to date with the transcripts and what is known about each
    session; returns the cache."""
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".usage-cache.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cache = load(root / "usage-cache.json")
        if cache.get("version") != 1:
            cache = {"version": 1, "files": {}, "buckets": {}, "sessions": {}}
        files, buckets = cache["files"], cache["buckets"]
        seen = set()
        for path, session in transcripts(projects):
            key = str(path)
            seen.add(key)
            scan_file(path, session, files.setdefault(key, {}), buckets, at)
        for key in [k for k in files if k not in seen]:
            files.pop(key)
        cutoff = at - RETENTION
        for session in list(buckets):
            for slot in [s for s in buckets[session] if int(s) < cutoff]:
                buckets[session].pop(slot)
            if not buckets[session]:
                buckets.pop(session)
        cache["scanned"] = at
        learn_sessions(cache, root, agents, at)
        save(root / "usage-cache.json", cache, indent=None)
    return cache


# --- who is who ----------------------------------------------------------------------------------

def unslug(name):
    return name.replace("-", ":", 1)


def learn_sessions(cache, root, agents, at):
    """Record what is known now about each session (pane, label, account over time) in the cache,
    which keeps it after the status file moves on to the pane's next session."""
    sessions = cache.setdefault("sessions", {})

    def note(sid, pane=None, label=None, account=None, when=None):
        if not sid:
            return
        s = sessions.setdefault(sid, {})
        if pane:
            s["pane"] = pane
        if label:
            s["label"] = label
        if account:
            timeline = s.setdefault("accounts", [])
            if not timeline or timeline[-1][1] != account:
                timeline.append([when or at, account])

    for path in (root / "agents").glob("*.json"):
        st = load(path)
        note(st.get("session"), st.get("pane"), account=st.get("account"), when=st.get("time"))
    for sid, info in (load(root / "usage.json").get("sessions") or {}).items():
        note(sid, account=info.get("account"), when=info.get("time"))
    for pane, agent in (agents or {}).items():
        note(((agent or {}).get("agent_session") or {}).get("value"), pane, agent.get("name"))
    prefixes = {}
    for d in root.glob("*/*-*-*-*"):  # lineage folders: <pane>-<date>-<time>-<session[:8]>
        parts = d.name.rsplit("-", 3)
        if (len(parts) == 4 and parts[0] != "local" and parts[1].isdigit()
                and parts[2].isdigit()):
            prefixes[parts[3]] = unslug(parts[0])
    for sid in list(cache.get("buckets") or {}):
        if not (sessions.get(sid) or {}).get("pane") and sid[:8] in prefixes:
            note(sid, prefixes[sid[:8]])
    current = {((a or {}).get("agent_session") or {}).get("value") for a in (agents or {}).values()}
    for sid in [s for s in sessions if s not in cache.get("buckets", {}) and s not in current]:
        sessions.pop(sid)  # no tokens left in the cache; learned again if it comes back


def account_at(info, when):
    timeline = (info or {}).get("accounts") or []
    current = timeline[0][1] if timeline else "unknown"
    for t, account in timeline:
        if t <= when + BUCKET:
            current = account
    return current


def run_id(run):
    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.get("created") or 0))
    return f"{run['orchestrator']}@{started}"


def runs_of(root):
    out = []
    for path in sorted((root / "runs").glob("*.json")):
        if path.name.endswith(".seen.json"):
            continue
        run = load(path)
        if run.get("orchestrator"):
            run["id"] = run_id(run)
            out.append(run)
    return out


def role_in(run, pane, when):
    end = run.get("ended") or run.get("until") or 0
    if when + BUCKET <= (run.get("created") or 0) or when > end:
        return None
    if pane == run["orchestrator"]:
        return "orchestrator"
    return "worker" if pane in (run.get("workers") or {}) else None


# --- report --------------------------------------------------------------------------------------

def blank():
    return {k: 0 for k in FIELDS}


def report(root, projects, agents, at, hours, known=None):
    """known: orch's accounts (number -> {name, ...}); one with no reading yet is listed with
    no windows. Each window's agents get their share of its points: the week so far
    (estimated_points), the last hour and today (the window's points per USD x their cost)."""
    cache = scan(root, projects, at, agents)
    runs = runs_of(root)
    readings, known = windows_report(root, at), known or {}
    accounts = {acc: {"id": acc, "name": (known.get(acc) or {}).get("name") or f"Acct {acc}",
                      "login": (known.get(acc) or {}).get("login"),
                      **(readings.get(acc) or {"updated": None, "stale": True, "windows": {}})}
                for acc in sorted({*readings, *known}, key=lambda a: (len(a), a))}
    since = at - hours * 3600
    midnight = time.mktime(time.localtime(at)[:3] + (0, 0, 0, 0, 0, -1))
    window_start = {(a, w): (info["resets_at"] or at) - WINDOWS[w]
                    for a, acc in accounts.items() for w, info in acc["windows"].items()
                    if w in WINDOWS}
    per_agent, per_run, per_day, per_account, machine = {}, {}, {}, {}, blank()
    in_window, unpriced = {}, set()
    for sid, slots in (cache.get("buckets") or {}).items():
        info = cache["sessions"].get(sid) or {}
        pane = info.get("pane")
        for slot, models in slots.items():
            when = int(slot)
            account = account_at(info, when)
            run = next((r for r in runs if role_in(r, pane, when)), None)
            role = role_in(run, pane, when) if run else None
            label = ((run or {}).get("workers") or {}).get(pane, {}).get("label") or info.get(
                "label") or ""
            for model, b in models.items():
                if price_key(model) is None:
                    unpriced.add(model)
                day = time.strftime("%Y-%m-%d", time.localtime(when))
                targets = [per_day.setdefault(day, blank()), machine,
                           per_account.setdefault(account, blank())]
                if when >= since:
                    key = pane or f"session:{sid[:8]}"
                    a = per_agent.setdefault(key, {"pane": pane, "label": label, "role": role,
                                                   "run": (run or {}).get("id"),
                                                   "accounts": [], "sessions": [],
                                                   "models": [], **blank()})
                    for k, v in (("accounts", account), ("sessions", sid), ("models", model)):
                        if v not in a[k]:
                            a[k].append(v)
                    a["role"] = a["role"] or role
                    a["run"] = a["run"] or (run or {}).get("id")
                    a["label"] = a["label"] or label
                    targets.append(a)
                if run:
                    r = per_run.setdefault(run["id"], {"orchestrator": run["orchestrator"],
                                                       "goal": run.get("goal") or "",
                                                       "created": run.get("created"),
                                                       "until": run.get("until"),
                                                       "ended": run.get("ended"),
                                                       "orchestrator_tokens": blank(),
                                                       "worker_tokens": blank(), **blank()})
                    targets += [r, r[f"{role}_tokens"]]
                for t in targets:
                    for k in FIELDS:
                        t[k] = t.get(k, 0) + b.get(k, 0)
                cost = b.get("cost", 0)
                for (acc, w), start in window_start.items():
                    if acc == account and when + BUCKET > start:
                        k = pane or f"session:{sid[:8]}"
                        # cost in the window, in its last hour, today
                        per = in_window.setdefault((acc, w), {}).setdefault(k, [0, 0, 0])
                        per[0] += cost
                        per[1] += cost if when >= at - 3600 else 0
                        per[2] += cost if when >= midnight else 0
    for (acc, w), per in in_window.items():
        total = sum(c[0] for c in per.values())
        win = accounts[acc]["windows"][w]
        points = (win["used_percentage"] or 0) / (total or 1)  # per USD of this machine's cost
        win["machine_cost_usd"] = round(total, 2)
        win["agents"] = {k: {"share": round(c[0] / (total or 1), 3),
                             "estimated_points": round(c[0] * points, 1),
                             "points_last_hour": round(c[1] * points, 2),
                             "points_today": round(c[2] * points, 2)}
                         for k, c in sorted(per.items(), key=lambda kv: -kv[1][0])}
    for v in [machine, *per_agent.values(), *per_day.values(), *per_account.values()] + [
            t for r in per_run.values() for t in (r, r["orchestrator_tokens"], r["worker_tokens"])]:
        v["cost"] = round(v["cost"], 2)
    return {
        "time": at, "host": socket.gethostname(), "hours": hours,
        "note": ("cost is an API-equivalent estimate from list prices (orch_usage.PRICES), not a "
                 "bill; window shares count only this machine's transcripts"),
        "accounts": accounts,
        "agents": sorted(per_agent.values(), key=lambda a: -a["cost"]),
        "runs": per_run, "days": dict(sorted(per_day.items())), "per_account": per_account,
        "machine": machine, "unpriced_models": sorted(unpriced),
    }


def human(rep):
    lines = []
    for account, acc in rep["accounts"].items():
        if not acc["windows"]:
            lines.append(f"account {account} ({acc['name']}): no reading yet")
        for name, w in acc["windows"].items():
            line = f"account {account} ({acc['name']}) {name}: {w['used_percentage']}%"
            if w["resets_at"]:
                line += f", resets {time.strftime('%a %H:%M', time.localtime(w['resets_at']))}"
            if w["burn_per_hour"] is not None:
                line += f", {w['burn_per_hour']}%/h"
            if w["projected_full_at"]:
                full = time.strftime("%a %H:%M", time.localtime(w["projected_full_at"]))
                line += f", full {full}" + (" BEFORE the reset" if w["full_before_reset"] else "")
            lines.append(line + (" (stale)" if acc["stale"] else ""))
    lines.append(f"last {rep['hours']:g} h on {rep['host']} (API-equivalent estimate):")
    for a in rep["agents"][:20]:
        name = f"{a['pane'] or ''} {a['label']}".strip() or a["sessions"][0][:8]
        lines.append(f"  {name[:28]:<28} {a['role'] or '-':<12} acct {','.join(a['accounts']):<4}"
                     f" ${a['cost']:>8.2f}  out {a['output']:>9,}  cached {a['cache_read']:>12,}")
    return "\n".join(lines)


def main(args, root, at, agents, known=None):
    """`orch usage`; agents() is orch's pane -> herdr agent lookup (None without herdr), known
    orch's accounts."""
    if args.action == "capture":
        try:
            capture(sys.stdin.read(), os.environ, root, at)
        except Exception:  # noqa: BLE001, S110 the statusline must never show an error
            pass
        return 0
    rep = report(root, Path.home() / ".claude" / "projects", agents(), at, args.hours, known)
    print(json.dumps(rep, indent=1) if args.json else human(rep))
    return 0
