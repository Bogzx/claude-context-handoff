#!/usr/bin/env python3
"""orch: keep a herdr orchestration run moving.

A run is registered by the orchestrator (`orch run start --until 09:00 --workers ...`) and
lives in ~/.claude/handoffs/runs/<orchestrator pane>.json, keyed by pane, so it survives
context handoffs. The context-handoff Stop hook writes ~/.claude/handoffs/agents/<pane>.json
at every turn end (background tasks still running, last message). From those two:

  orch wait       the orchestrator's one watcher: returns on the first thing to act on
                  (a worker finished or idle, blocked, gone, waiting too long on its own job,
                  a --check command passing) or after the heartbeat, and prints the fleet.
  orch park       a worker on standby, with a reason (and an end); unparked workers don't idle.
  orch ask        a question for the owner: logged and pushed, the fleet keeps working.
  orch watchdog   run every 5 minutes by a systemd user timer, outside any Claude session:
                  prompts an idle orchestrator whose workers sit idle, and alerts the owner
                  when it is gone, blocked or not acting.

Events go to ~/.claude/handoffs/events.jsonl. Owner alerts also go to the notify command:
$ORCH_NOTIFY_CMD, else an executable ~/.claude/handoffs/notify, called as
`<command> <title> <body>` with ORCH_EVENT_KIND set. Python 3.8 compatible (ob1).
"""
import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path.home() / ".claude" / "handoffs"
IDLE_GRACE = 10 * 60  # a worker idle this long with nothing assigned gets the orchestrator nudged
BLOCKED_GRACE = 10 * 60  # a dialog left this long
MISSING_GRACE = 5 * 60  # a pane without its agent (a takeover takes about a minute)
LONG_WAIT = 90 * 60  # idle with background tasks this long: a stuck or forgotten job
ORCH_NO_WAKE = 5 * 60  # orchestrator idle with nothing that can wake it
ORCH_SILENT = 60 * 60  # orchestrator idle this long whatever it waits on
NUDGE_EVERY = 15 * 60
NUDGE_BACKOFF = 60 * 60  # after IGNORED nudges in a row on the same problem
IGNORED = 3


def now():
    return time.time()


def sleep(seconds):
    time.sleep(seconds)


def slug(pane):
    return pane.replace(":", "-")


def clock(ts, date=False):
    return time.strftime("%Y-%m-%d %H:%M" if date else "%H:%M", time.localtime(ts))


def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}"


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def die(text):
    print(f"orch: {text}", file=sys.stderr)
    sys.exit(2)


# --- herdr ------------------------------------------------------------------------------

def herdr(*args, timeout=60):
    binary = shutil.which("herdr") or str(Path.home() / ".local" / "bin" / "herdr")
    return subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout,
                          check=False)


def agents():
    """pane id -> agent from `herdr agent list`, or None when herdr can't be reached."""
    try:
        out = herdr("agent", "list").stdout
        return {a["pane_id"]: a for a in json.loads(out)["result"]["agents"]}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def prompt(pane, text):
    result = herdr("agent", "prompt", pane, text)
    return result.returncode == 0


# --- events -----------------------------------------------------------------------------

def emit(kind, title, body, pane=None, push=False):
    event = {"time": now(), "host": socket.gethostname(), "kind": kind, "pane": pane,
             "title": title, "body": body}
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(ROOT / "events.jsonl", "a") as f:
        f.write(json.dumps(event) + "\n")
    if not push:
        return
    herdr("notification", "show", title, "--body", body)
    command = os.environ.get("ORCH_NOTIFY_CMD") or (
        str(ROOT / "notify") if os.access(ROOT / "notify", os.X_OK) else "")
    if command:
        try:
            subprocess.run(shlex.split(command) + [title, body], capture_output=True, timeout=20,
                           env=dict(os.environ, ORCH_EVENT_KIND=kind), check=False)
        except (OSError, subprocess.SubprocessError):
            pass


# --- runs -------------------------------------------------------------------------------

def run_path(orch):
    return ROOT / "runs" / f"{slug(orch)}.json"


def load_run(orch):
    return load(run_path(orch))


def active(run, at=None):
    return bool(run) and not run.get("ended") and (run.get("until") or 0) > (at or now())


def all_runs():
    return [load(p) for p in sorted((ROOT / "runs").glob("*.json"))
            if not p.name.endswith(".seen.json")]


def parse_until(text, at):
    text = text.strip()
    m = re.fullmatch(r"\+(\d+(?:\.\d+)?)([mh])", text)
    if m:
        return at + float(m.group(1)) * (60 if m.group(2) == "m" else 3600)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()  # noqa: DTZ007 local time
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        base = datetime.fromtimestamp(at)  # noqa: DTZ006 local time
        when = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0,
                            microsecond=0)
        if when.timestamp() <= at:
            when += timedelta(days=1)
        return when.timestamp()
    die(f"can't read the time {text!r}: use HH:MM, 'YYYY-MM-DD HH:MM' or +8h")
    return None


def parse_workers(text):
    """'w1:p2=builder,w1:p3' -> {'w1:p2': {'label': 'builder'}, 'w1:p3': {'label': ''}}"""
    workers = {}
    for item in filter(None, (x.strip() for x in (text or "").split(","))):
        pane, _, label = item.partition("=")
        workers[pane.strip()] = {"label": label.strip()}
    return workers


def orchestrator(args):
    pane = getattr(args, "orch", None) or os.environ.get("HERDR_PANE_ID")
    if not pane:
        die("no orchestrator pane: run inside herdr or pass --orch PANE")
    return pane


# --- what a worker is doing ---------------------------------------------------------------

def view(pane, agent, run, at):
    """One worker, as orch sees it."""
    st = load(ROOT / "agents" / f"{slug(pane)}.json")
    session = ((agent or {}).get("agent_session") or {}).get("value")
    fresh = st if st and session and st.get("session") == session else {}
    park = ((run or {}).get("parked") or {}).get(pane)
    seq = (agent or {}).get("completion_seq")
    if park and ((park.get("until") and at >= park["until"])
                 or (seq is not None and park.get("seq") is not None and seq > park["seq"])):
        park = None  # it got its next task (another turn ended) or the park ran out
    return {
        "pane": pane,
        "label": ((run or {}).get("workers") or {}).get(pane, {}).get("label")
                 or (agent or {}).get("name") or "",
        "state": (agent or {}).get("agent_status", "unknown") if agent else "missing",
        "session": session,
        "seq": seq,
        "change": (agent or {}).get("state_change_seq"),
        "since": fresh.get("time"),  # the turn end it is idle since, when known
        "bg": fresh.get("background_tasks") or [],
        "last": fresh.get("last") or "",
        "park": park,
    }


def first_line(text, width=90):
    line = next((x.strip() for x in text.splitlines() if x.strip()), "")
    return line if len(line) <= width else line[:width - 1] + "…"


def table(views, at):
    rows = []
    for v in views:
        state = v["state"]
        if state in ("idle", "done") and v["park"]:
            state = "parked"
        elif state in ("idle", "done") and v["bg"]:
            state = "own-job"
        since = ago(at - v["since"]) if v["since"] and state != "working" else "-"
        note = (f"parked: {v['park'].get('reason', '')}" if state == "parked"
                else f"waits on: {v['bg'][0].get('what', '')}" if state == "own-job"
                else first_line(v["last"]) if state not in ("working", "missing") else "")
        rows.append(f"  {v['pane']:<9} {v['label'][:16]:<16} {state:<8} {since:>6}  "
                    f"{first_line(note, 100)}")
    return "\n".join(rows)


# --- commands ---------------------------------------------------------------------------

def cmd_run(args):
    orch = orchestrator(args)
    at = now()
    run = load_run(orch)
    if args.action == "start":
        if not args.until:
            die("--until is required (HH:MM, 'YYYY-MM-DD HH:MM' or +4h)")
        run = {"orchestrator": orch, "host": socket.gethostname(), "cwd": os.getcwd(),
               "created": at, "until": parse_until(args.until, at), "goal": args.goal or "",
               "workers": parse_workers(args.workers), "parked": {}, "ended": None}
    elif args.action in ("set", "stop", "show") and not run:
        die(f"no run registered for {orch}: orch run start --until ... --workers ...")
    if args.action == "set":
        if args.until:
            run["until"] = parse_until(args.until, at)
            run["ended"] = None
        if args.goal is not None:
            run["goal"] = args.goal
        if args.workers:
            run["workers"] = parse_workers(args.workers)
        for pane, info in parse_workers(args.add).items():
            run["workers"][pane] = info
        for pane in filter(None, (args.remove or "").split(",")):
            run["workers"].pop(pane.strip(), None)
            run.get("parked", {}).pop(pane.strip(), None)
    if args.action == "stop":
        run["ended"] = at
    if args.action != "show":
        save(run_path(orch), run)
        emit(f"run-{args.action}", f"Run {args.action}: {orch}",
             f"until {clock(run['until'], True)}; {len(run['workers'])} workers; {run['goal']}",
             orch)
    if args.json:
        print(json.dumps(run, indent=1))
        return 0
    state = "ended" if run.get("ended") else "active" if active(run, at) else "expired"
    print(f"run {orch} ({state}) until {clock(run['until'], True)}"
          + (f", goal: {run['goal']}" if run.get("goal") else ""))
    for pane, info in run["workers"].items():
        print(f"  {pane} {info.get('label', '')}")
    return 0


def cmd_park(args):
    orch = orchestrator(args)
    run = load_run(orch)
    if not run:
        die(f"no run registered for {orch}")
    if args.pane not in run["workers"]:
        die(f"{args.pane} is not a worker of this run (orch run set --add {args.pane}=label)")
    if args.action == "unpark":
        run.setdefault("parked", {}).pop(args.pane, None)
    else:
        agent = (agents() or {}).get(args.pane) or {}
        run.setdefault("parked", {})[args.pane] = {
            "reason": args.reason, "since": now(), "seq": agent.get("completion_seq"),
            "until": parse_until(args.until, now()) if args.until else None}
    save(run_path(orch), run)
    print(f"{args.action}ed {args.pane}" if args.action == "unpark" else
          f"parked {args.pane} until its next task"
          + (f" or {clock(run['parked'][args.pane]['until'])}" if args.until else ""))
    return 0


def cmd_ask(args):
    orch = orchestrator(args)
    run = load_run(orch)
    if run:
        run.setdefault("questions", []).append({"time": now(), "text": args.text})
        save(run_path(orch), run)
    emit("ask", f"Question from {orch} on {socket.gethostname()}", args.text, orch, push=True)
    print("sent to the owner; keep the rest of the fleet working while you wait for the answer")
    return 0


def cmd_last(args):
    st = load(ROOT / "agents" / f"{slug(args.pane)}.json")
    if not st:
        die(f"no turn end recorded for {args.pane} yet")
    print(f"{args.pane} {st.get('event')} at {clock(st.get('time') or 0)}, "
          f"background tasks: {len(st.get('background_tasks') or [])}")
    for t in st.get("background_tasks") or []:
        print(f"  {t.get('type')} {t.get('id')}: {t.get('what')}")
    print(st.get("last") or "(no message)")
    return 0


def fleet(orch, run, workers, at):
    ags = agents()
    if ags is None:
        return None, None
    return ags, [view(p, ags.get(p), run, at) for p in workers]


def cmd_status(args):
    orch = orchestrator(args)
    run = load_run(orch)
    workers = parse_workers(args.workers) or (run or {}).get("workers") or {}
    at = now()
    ags, views = fleet(orch, run, workers, at)
    if ags is None:
        die("herdr is not reachable")
    if args.json:
        print(json.dumps({"run": run, "orchestrator": ags.get(orch), "workers": views},
                         indent=1))
        return 0
    if run:
        state = "ended" if run.get("ended") else "active" if active(run, at) else "expired"
        print(f"run {orch} ({state}) until {clock(run['until'], True)}"
              + (f", goal: {first_line(run.get('goal') or '', 120)}" if run.get("goal") else ""))
    print(table(views, at))
    return 0


def classify(v, at, started, long_wait):
    """(key, line) for something about this worker that needs the orchestrator, else None.
    The key stays the same while the situation does, so a later wait doesn't repeat it."""
    s, label = v["state"], f"{v['pane']} {v['label']}".strip()
    if s == "blocked":
        return (f"blocked:{v['session']}:{v['change']}",
                (f"blocked {label}: at an approval or question dialog (read it with herdr "
                 "agent read, then answer it or ask the owner)"))
    if s == "missing":
        return f"missing:{v['session']}", f"missing {label}: no agent in the pane"
    if s not in ("idle", "done") or v["park"]:
        return None
    if v["bg"]:
        waited = at - (v["since"] or started)
        if waited < long_wait:
            return None
        return (f"longwait:{v['session']}:{v['seq']}",
                (f"long-wait {label}: idle {ago(waited)} on its own job "
                 f"({v['bg'][0].get('what', '')}); check that it is still alive"))
    since = f"since {clock(v['since'])}" if v["since"] else "(since: not recorded)"
    return (f"idle:{v['session']}:{v['seq']}",
            (f"idle {label}: idle {since}, nothing running, not parked. "
             f"Last: {first_line(v['last'], 160)!r}"))


def cmd_wait(args):
    orch = orchestrator(args)
    run = load_run(orch)
    workers = parse_workers(args.workers) or (run or {}).get("workers") or {}
    if not workers and not args.check:
        die("nothing to wait on: register a run (orch run start) or pass --workers/--check")
    seen_path = ROOT / "runs" / f"{slug(orch)}.seen.json"
    seen = {} if args.reset else load(seen_path)
    started = now()
    missing = {}
    checked = 0.0
    while True:
        at = now()
        run = load_run(orch) or run  # parks and workers can change while waiting
        workers = parse_workers(args.workers) or (run or {}).get("workers") or workers
        _, views = fleet(orch, run, workers, at)
        events = []
        for v in views or []:
            if v["state"] == "missing":
                missing.setdefault(v["pane"], at)
                if at - missing[v["pane"]] < args.missing_grace:
                    continue
            else:
                missing.pop(v["pane"], None)
            hit = classify(v, at, started, args.long_wait)
            if hit and seen.get(v["pane"]) != hit[0]:
                events.append((v["pane"], hit))
        if args.check and at - checked >= args.check_every:
            checked = at
            for command in args.check:
                try:
                    result = subprocess.run(command, shell=True, capture_output=True, text=True,
                                            timeout=600, check=False)
                except subprocess.TimeoutExpired:
                    continue
                if result.returncode == 0:
                    tail = (result.stdout or "").strip().splitlines()[-3:]
                    events.append(("check", (None, f"check passed: {command}"
                                             + "".join(f"\n    {x}" for x in tail))))
        heartbeat = at - started >= args.heartbeat
        if events or heartbeat:
            for pane, (key, _) in events:
                if key:
                    seen[pane] = key
            save(seen_path, seen)
            what = f"{len(events)} event(s)" if events else "heartbeat, nothing new"
            head = f"orch wait {clock(at)}: {what}"
            if run:
                head += (f" (run until {clock(run['until'], True)})" if active(run, at)
                         else " (the run is over: wrap up per its goal and stop)")
            print(head)
            for _, (_, line) in events:
                print("EVENT " + line)
            if views is None:
                print("  herdr is not reachable")
            elif views:
                print(table(views, at))
            print("Each idle worker gets its next task or `orch park <pane> --reason ...`. "
                  "Full last message: orch last <pane>. Then arm one new `orch wait`.")
            return 0
        sleep(args.poll)


def nudge_reasons(run, ags, at, ws):
    """Why the run's idle orchestrator should act now (empty when it shouldn't)."""
    orch = run["orchestrator"]
    reasons = {}
    first = ws.setdefault("first_seen", {})
    seen_now = set()
    any_free = False
    for pane in run.get("workers") or {}:
        v = view(pane, ags.get(pane), run, at)
        label = f"{pane} {v['label']}".strip()
        key = f"{pane}:{v['state']}:{v['session']}:{v['seq']}"
        seen_now.add(key)
        since = v["since"] or first.setdefault(key, at)
        if v["state"] == "missing":
            if at - first.setdefault(key, at) >= MISSING_GRACE:
                reasons[f"{pane}:missing"] = f"{label} has no agent in its pane"
            continue
        if v["state"] == "blocked":
            if at - first.setdefault(key, at) >= BLOCKED_GRACE:
                reasons[f"{pane}:blocked"] = f"{label} is at a dialog for {ago(at - first[key])}"
            continue
        if v["state"] not in ("idle", "done") or v["park"]:
            continue
        if v["bg"]:
            if at - since >= LONG_WAIT:
                reasons[f"{pane}:longwait"] = (f"{label} has waited {ago(at - since)} on its "
                                               "own background job")
            continue
        any_free = True
        if at - since >= IDLE_GRACE:
            reasons[f"{pane}:idle"] = f"{label} idle {ago(at - since)}, not parked"
    for key in list(first):
        if key not in seen_now and not key.startswith(orch + ":"):
            first.pop(key)
    o = view(orch, ags.get(orch), None, at)
    okey = f"{orch}:{o['session']}:{o['seq']}"
    for key in [k for k in first if k.startswith(orch + ":") and k != okey]:
        first.pop(key)
    o_since = o["since"] or first.setdefault(okey, at)
    if any_free and not o["bg"] and o["since"] and at - o_since >= ORCH_NO_WAKE:
        reasons["orch:nowake"] = ("your last turn ended with no background task, so nothing "
                                  "would wake you")
    if at - o_since >= ORCH_SILENT:
        reasons["orch:silent"] = f"you have been idle {ago(at - o_since)}"
    return reasons


def cmd_watchdog(args):
    ags = agents()
    if ags is None:
        return 0  # herdr is down; there is nothing to prompt
    state_path = ROOT / "watchdog.json"
    state = load(state_path)
    at = now()
    host = socket.gethostname()
    for run in all_runs():
        orch = run.get("orchestrator")
        if not orch or run.get("ended"):
            continue
        ws = state.setdefault(orch, {})
        if not active(run, at):
            run["ended"] = at
            save(run_path(orch), run)
            emit("run-ended", f"Run ended: {orch} on {host}",
                 f"reached {clock(run['until'], True)}; {run.get('goal', '')}", orch, push=True)
            state.pop(orch, None)
            continue
        agent = ags.get(orch)
        status = agent.get("agent_status") if agent else "missing"
        if status in ("missing", "blocked"):
            ws.pop("idle", None)
            since = ws.setdefault(status, at)
            grace = MISSING_GRACE if status == "missing" else BLOCKED_GRACE
            if at - since >= grace and ws.get("alerted") != status:
                ws["alerted"] = status
                emit(f"orch-{status}", f"Orchestrator {status}: {orch} on {host}",
                     "no agent in the orchestrator pane (failed takeover?)" if status == "missing"
                     else "the orchestrator is at an approval or question dialog", orch,
                     push=True)
            continue
        for key in ("missing", "blocked", "alerted"):
            ws.pop(key, None)
        if status not in ("idle", "done"):
            continue  # working: it will see the fleet at its turn end
        reasons = nudge_reasons(run, ags, at, ws)
        if not reasons:
            ws.pop("ignored", None)
            ws.pop("last_key", None)
            continue
        key = ",".join(sorted(reasons))
        ignored = ws.get("ignored", 0) if ws.get("last_key") == key else 0
        if at - ws.get("last_nudge", 0) < (NUDGE_EVERY if ignored < IGNORED else NUDGE_BACKOFF):
            continue
        if ws.get("last_key") == key and ws.get("last_nudge"):
            ignored += 1
        ws.update(last_key=key, last_nudge=at, ignored=ignored)
        text = (f"Watchdog {clock(at)} (orch run until {clock(run['until'], True)}"
                + (f"; goal: {first_line(run.get('goal') or '', 200)}" if run.get("goal") else "")
                + "): " + "; ".join(reasons[k] for k in sorted(reasons)) + ". Give each idle "
                "worker its next task from your queue or the board, or park it with `orch park "
                "<pane> --reason ...`; send questions for the owner with `orch ask` and keep the "
                "rest working. Then arm one `orch wait`. If the run's work is finished, say so and "
                "run `orch run stop`.")
        if args.dry_run:
            print(f"would prompt {orch}: {text}")
            continue
        prompted = prompt(orch, text)
        emit("nudge", f"Watchdog nudged {orch}", text, orch)
        if ignored == IGNORED or not prompted:
            emit("orch-not-acting", f"Orchestrator not acting: {orch} on {host}",
                 ("the same problem after " + str(IGNORED) + " nudges: " if prompted
                  else "could not prompt it: ") + "; ".join(reasons.values()), orch, push=True)
    save(state_path, state)
    return 0


def cmd_notify(args):
    emit("test", args.title, args.body, push=True)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="orch", description=__doc__.split("\n\n")[0])
    p.add_argument("--orch", help="orchestrator pane (default: $HERDR_PANE_ID)")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="register, change, stop or show this pane's run")
    r.add_argument("action", choices=["start", "set", "stop", "show"])
    r.add_argument("--until", help="HH:MM, 'YYYY-MM-DD HH:MM' or +8h (start: required)")
    r.add_argument("--goal", help="what the run is for, in the owner's words")
    r.add_argument("--workers", help="pane[=label],... (replaces the list)")
    r.add_argument("--add", help="pane[=label],... to add")
    r.add_argument("--remove", help="pane,... to remove")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_run)

    for name, text in (("park", "put a worker on standby, with a reason"),
                       ("unpark", "take a worker off standby")):
        k = sub.add_parser(name, help=text)
        k.add_argument("pane")
        if name == "park":
            k.add_argument("--reason", required=True)
            k.add_argument("--until", help="when the standby ends (default: its next task)")
        k.set_defaults(func=cmd_park, action=name)

    a = sub.add_parser("ask", help="send the owner a question (logged and pushed)")
    a.add_argument("text")
    a.set_defaults(func=cmd_ask)

    s = sub.add_parser("status", help="the fleet of this run")
    s.add_argument("--workers")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    w = sub.add_parser("wait", help="block until something needs the orchestrator")
    w.add_argument("--workers", help="pane[=label],... (default: the run's workers)")
    w.add_argument("--heartbeat", type=float, default=30 * 60, help="seconds (default 1800)")
    w.add_argument("--poll", type=float, default=15)
    w.add_argument("--long-wait", type=float, default=LONG_WAIT,
                   help="seconds a worker may wait on its own job (default 5400)")
    w.add_argument("--missing-grace", type=float, default=180)
    w.add_argument("--check", action="append",
                   help="shell command; the wait returns when it exits 0 (repeatable)")
    w.add_argument("--check-every", type=float, default=120)
    w.add_argument("--reset", action="store_true", help="report everything again")
    w.set_defaults(func=cmd_wait)

    last = sub.add_parser("last", help="a worker's last message and background tasks")
    last.add_argument("pane")
    last.set_defaults(func=cmd_last)

    d = sub.add_parser("watchdog", help="one watchdog pass (the systemd timer runs this)")
    d.add_argument("--dry-run", action="store_true")
    d.set_defaults(func=cmd_watchdog)

    n = sub.add_parser("notify", help="send a test alert through the notify command")
    n.add_argument("title")
    n.add_argument("body")
    n.set_defaults(func=cmd_notify)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
