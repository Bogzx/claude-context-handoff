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
  orch ask        a question for the owner, with options: logged and pushed, the fleet keeps
                  working; `orch answer <id> <text>` records the answer and prompts the asker.
  orch watchdog   run every 5 minutes by a systemd user timer, outside any Claude session:
                  prompts an idle orchestrator whose workers sit idle, and alerts the owner
                  when it is gone, blocked or not acting.
  orch decisions  the pane's current decision log, parsed (who, what, why, superseded).
  orch account    which Claude account (claude-acc N) an agent runs on, and switching it: now,
                  when it is next idle (the watchdog does it) or at its next handoff.
  orch usage      account windows from the statusline and tokens per agent (orch_usage.py).
  orch new        a new orchestrator in a new herdr tab: brief, decision log, run (orch_new.py).
  orch serve      the HTTP gateway for the owner's phone app, on the tailnet only, and
  orch pair       its pairing token (orch_serve.py).

Agents report at turn end through the orch MCP server (orch_mcp.py), into
~/.claude/handoffs/agents/<pane>.json next to the Stop hook's status.

Events go to ~/.claude/handoffs/events.jsonl. Owner alerts also go to the notify command:
$ORCH_NOTIFY_CMD, else an executable ~/.claude/handoffs/notify, called as
`<command> <title> <body>` with ORCH_EVENT_KIND set and the whole event as JSON in
ORCH_EVENT. Python 3.8 compatible (Ubuntu 20.04).
"""
import argparse
import hashlib
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
DANGER = ("none", "deploy", "prod", "destructive")  # what saying yes to an option does
ANSWERED_SHOWN = 10 * 60  # an answered question stays in status this long (other devices)
HOOK = Path(__file__).resolve().parent / "context-handoff.py"
PROJECTS = Path.home() / ".claude" / "projects"  # the transcripts
STARTS = Path.home() / ".claude" / "orch" / "new"  # orch new: <id>/new.json and <id>/brief.md
START_SHOWN = 86400  # an unfinished start shows in the fleet this long
SMALL_CONTEXT = 60_000  # a switching agent with less context is resumed, not handed off
HANDOFF_WAIT = 30 * 60  # a switch handoff not written by then: resume the agent instead
SWITCH_BUDGET = 200  # seconds of resumes per watchdog pass (its unit times out at 300)


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


class Refused(SystemExit):
    """Why a command can't be carried out: the CLI prints it and exits 2, the gateway (orch
    serve) answers the app with it."""

    def __init__(self, text):
        super().__init__(2)
        self.text = text


def die(text):
    raise Refused(text)


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


def screen_lines(pane, lines, source="recent"):
    """The pane's last `lines` lines of terminal text, or None when herdr can't read it."""
    try:
        r = herdr("agent", "read", pane, "--source", source, "--lines", str(lines),
                  "--format", "text", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if not r or r.returncode != 0:
        return None
    return r.stdout.rstrip("\n").split("\n")[-lines:]


# --- this machine ------------------------------------------------------------------------

CONFIG = Path.home() / ".config" / "orch" / ".env"  # the gateway's: token, key, allow-list


def read_env(path=None):
    """KEY=value lines of ~/.config/orch/.env (or path); {} when it is missing."""
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


_TAILNET_NAME = []


def tailnet_name():
    """This machine's Tailscale device name (Self.HostName), looked up once per process."""
    if not _TAILNET_NAME:
        try:
            out = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                                 text=True, timeout=5, check=False).stdout
            _TAILNET_NAME.append(json.loads(out)["Self"].get("HostName") or None)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            _TAILNET_NAME.append(None)
    return _TAILNET_NAME[0]


def machine_name(env=None):
    """The name the owner's app knows this machine by: ORCH_MACHINE in ~/.config/orch/.env, else
    its Tailscale device name, else the hostname."""
    env = read_env() if env is None else env
    return env.get("ORCH_MACHINE") or tailnet_name() or socket.gethostname()


# --- events -----------------------------------------------------------------------------

def emit(kind, title, body, pane=None, push=False, **extra):
    event = {"time": now(), "host": socket.gethostname(), "kind": kind, "pane": pane,
             "title": title, "body": body, **extra}
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
                           env=dict(os.environ, ORCH_EVENT_KIND=kind, ORCH_EVENT=json.dumps(event)),
                           check=False)
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


def run_of(pane, at=None):
    """(run, role) for the active run this pane leads or works in, else (None, None)."""
    run = load_run(pane)
    if active(run, at):
        return run, "orchestrator"
    for run in all_runs():
        if active(run, at) and pane in (run.get("workers") or {}):
            return run, "worker"
    return None, None


def status_of(pane):
    """The pane's status file: the Stop hook's turn end plus its latest report."""
    return load(ROOT / "agents" / f"{slug(pane)}.json")


# --- accounts (claude-acc N: account 1 is ~/.claude, account N is ~/.claude-N) ----------------

def account_dir(account):
    return Path.home() / (".claude" if str(account) == "1" else f".claude-{account}")


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
    st = status_of(pane)
    session = ((agent or {}).get("agent_session") or {}).get("value")
    fresh = st if st and session and st.get("session") == session else {}
    live = load(ROOT / "agents" / "context" / f"{slug(pane)}.json")  # the hook, mid-turn
    park = ((run or {}).get("parked") or {}).get(pane)
    accounts = load(ROOT / "accounts.json")
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
        "report": st.get("report"),  # the latest, whichever session wrote it (it has its time)
        "account": fresh.get("account"),
        "context_tokens": (live.get("context_tokens") if session and live.get("session") == session
                           else fresh.get("context_tokens")),
        "switch": (accounts.get("switch") or {}).get(pane),  # pending
        "spill": spill_state(pane, agent, fresh, accounts),
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
        run = new_run(orch, parse_until(args.until, at), args.goal or "",
                      parse_workers(args.workers), os.getcwd(), at)
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
        save_run(orch, run, args.action)
    if args.json:
        print(json.dumps(run, indent=1))
        return 0
    state = "ended" if run.get("ended") else "active" if active(run, at) else "expired"
    print(f"run {orch} ({state}) until {clock(run['until'], True)}"
          + (f", goal: {run['goal']}" if run.get("goal") else ""))
    for pane, info in run["workers"].items():
        print(f"  {pane} {info.get('label', '')}")
    return 0


def new_run(orch, until, goal, workers, cwd, at):
    return {"orchestrator": orch, "host": socket.gethostname(), "cwd": cwd, "created": at,
            "until": until, "goal": goal, "workers": workers, "parked": {}, "ended": None}


def save_run(orch, run, action):
    save(run_path(orch), run)
    emit(f"run-{action}", f"Run {action}: {orch}",
         f"until {clock(run['until'], True)}; {len(run['workers'])} workers; {run['goal']}", orch)


def park(orch, pane, action, reason=None, until=None):
    """Park (with a reason and an optional end) or unpark a worker of the orchestrator's run;
    returns the run."""
    run = load_run(orch)
    if not run:
        die(f"no run registered for {orch}")
    if pane not in run["workers"]:
        die(f"{pane} is not a worker of this run (orch run set --add {pane}=label)")
    if action == "unpark":
        run.setdefault("parked", {}).pop(pane, None)
    else:
        agent = (agents() or {}).get(pane) or {}
        run.setdefault("parked", {})[pane] = {
            "reason": reason, "since": now(), "seq": agent.get("completion_seq"),
            "until": parse_until(until, now()) if until else None}
    save(run_path(orch), run)
    return run


def cmd_park(args):
    run = park(orchestrator(args), args.pane, args.action, getattr(args, "reason", None),
               getattr(args, "until", None))
    print(f"{args.action}ed {args.pane}" if args.action == "unpark" else
          f"parked {args.pane} until its next task"
          + (f" or {clock(run['parked'][args.pane]['until'])}" if args.until else ""))
    return 0


def default_answer(run, options, kind, default, by):
    """(default, default_at) of a question: what it goes to with no reply by `by`, or (None,
    None) without one. Silence never picks a dangerous option or approves a plan."""
    if default is None and by is None:
        return None, None
    if not default or not by:
        die("--default and --by go together")
    if kind == "plan":
        die("a plan waits for the owner's OK: it has no --default")
    if not run:
        die("a question outside a run has no --default: the watchdog carries it out from the run")
    option = next((o for o in options if o["label"] == default), None)
    if not option:
        die(f"--default {default!r} isn't one of the question's options")
    if option["danger"] != "none":
        die(f"--default {default!r} is a {option['danger']} option: the owner has to pick it")
    at = parse_until(by, now())
    if at <= now():
        die(f"--by {by!r} has passed")
    return default, at


def ask(pane, text, options=(), kind=None, default=None, by=None):
    """Log a question for the owner in the pane's run and events.jsonl and push it; returns its
    id. options: [{"label", "danger"}], the answers the owner's app offers as buttons. kind
    "plan": the plan of a new orchestrator (`orch new --plan-first`), drawn from its report.
    default, by: the option it goes to with no reply by that time (default_answers)."""
    options = [{"label": o["label"], "danger": o.get("danger") or "none"} for o in options]
    run, _ = run_of(pane)
    default, default_at = default_answer(run, options, kind, default, by)
    qid = "q" + os.urandom(4).hex()
    if run:
        for q in run.setdefault("questions", []):  # a revised plan replaces the open one
            if kind == "plan" and q.get("kind") == "plan" and q.get("pane") == pane \
                    and not q.get("answered"):
                q.update(answer=f"(replaced by {qid})", answered=now())
        run["questions"].append(
            {"id": qid, "time": now(), "pane": pane, "text": text, "options": options,
             "kind": kind, "answer": None, "answered": None, "default": default,
             "default_at": default_at})
        save(run_path(run["orchestrator"]), run)
    body = text
    if options:
        body += "\nOptions: " + " / ".join(
            o["label"] + (f" [{o['danger']}]" if o["danger"] != "none" else "") for o in options)
    if default:
        body += f"\nGoes to {default!r} at {clock(default_at)} unless you answer"
    emit("ask", f"Question from {pane} on {machine_name()}", body, pane, push=True, id=qid,
         question=text, options=options, question_kind=kind, default=default,
         default_at=default_at)
    start = start_of(pane) if kind == "plan" else {}
    if start.get("plan_first"):
        start_step(start, "read", "ok")
        start_step(start, "plan", "doing", f"waiting for the owner's OK ({qid})")
    return qid


class OptionAction(argparse.Action):
    """--option LABEL [--danger KIND]: the danger applies to the option just before it."""

    def __call__(self, parser, namespace, value, option_string=None):
        options = list(getattr(namespace, "options", None) or [])
        if option_string == "--danger":
            if not options:
                parser.error("--danger goes after the --option it applies to")
            options[-1] = dict(options[-1], danger=value)
        else:
            options.append({"label": value, "danger": "none"})
        namespace.options = options


def cmd_ask(args):
    qid = ask(orchestrator(args), args.text, args.options or [], args.kind, args.default, args.by)
    print(f"sent to the owner as {qid}; keep the rest of the fleet working while you wait for the "
          "answer")
    return 0


def find_question(qid):
    """(run or None, question) for a question id: from the runs, else (asked outside a run) from
    events.jsonl with its answer, if any."""
    for run in all_runs():
        for q in run.get("questions") or []:
            if q.get("id") == qid:
                return run, q
    found = None
    try:
        with open(ROOT / "events.jsonl") as f:
            for line in f:
                if qid not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("id") != qid:
                    continue
                if e.get("kind") == "ask":
                    found = {"id": qid, "time": e.get("time"), "pane": e.get("pane"),
                             "text": e.get("question") or e.get("body"),
                             "options": e.get("options") or [], "kind": e.get("question_kind"),
                             "answer": None, "answered": None}
                elif e.get("kind") == "answer" and found:
                    found.update(answer=e.get("body"), answered=e.get("time"))
    except OSError:
        pass
    return None, found


def answer(qid, text, by_default=False):
    """Record the owner's answer to question qid and prompt the agent that asked; returns its
    pane. A closed question or a failed prompt is refused and nothing is recorded. by_default:
    nobody answered and the question went to its default."""
    run, q = find_question(qid)
    if not q:
        die(f"no question {qid}")
    if q.get("answered"):
        die(f"{qid} was already answered at {clock(q['answered'], True)}: {q.get('answer')!r}")
    pane = q.get("pane") or (run or {}).get("orchestrator")
    asked = first_line(q.get("text") or "", 160)
    message = (f"Your question {qid} ({asked!r}) went to its default: {text} (default, no reply "
               f"by {clock(q['default_at'])})" if by_default
               else f"The owner answered your question {qid} ({asked!r}): {text}")
    if not prompt(pane, message):
        die(f"could not prompt {pane}; the answer is not recorded")
    q.update(answer=text, answered=now(), by_default=by_default)
    if run:
        save(run_path(run["orchestrator"]), run)
    emit("answer", f"Answer to {qid}", text, pane, id=qid, by_default=by_default)
    start = start_of(pane) if q.get("kind") == "plan" else {}
    if any(s["step"] == "plan" for s in start.get("steps") or []):
        start_step(start, "plan", "ok", f"answered: {first_line(text, 160)}")
    return pane


# --- starts (orch new) -----------------------------------------------------------------------

def start_of(pane):
    """The newest `orch new` start whose orchestrator is in the pane, or {}."""
    found = {}
    for path in STARTS.glob("*/new.json") if STARTS.is_dir() else ():
        rec = load(path)
        if rec.get("pane") == pane and (rec.get("time") or 0) >= (found.get("time") or 0):
            found = rec
    return found


def start_step(rec, step, status, text="", **extra):
    """A step of a start (pane, claude, trust, run, read or brief, plan) is doing, ok or failed:
    kept in its new.json and logged as a `start` event (a failure alerts)."""
    at, error = now(), text if status == "failed" else None
    entry = {"step": step, "status": status, "time": at, **({"error": error} if error else {})}
    steps = rec.setdefault("steps", [])
    for i, s in enumerate(steps):
        if s["step"] == step:
            steps[i] = entry
            break
    else:
        steps.append(entry)
    rec.update(step=step, status=status, updated=at, **({"error": error} if error else {}))
    save(STARTS / rec["id"] / "new.json", rec)
    folder = os.path.basename(rec.get("cwd") or "")
    emit("start", f"Start of {folder}: {step} {status}", text, rec.get("pane"),
         push=status == "failed", action=rec["id"], step=step, status=status, error=error,
         **extra)


def start_cancelled(rec):
    """{time, by} once the start was cancelled, else {}. A file of its own (<id>/cancelled.json),
    so a running start's saves of new.json can't drop it."""
    return load(STARTS / rec["id"] / "cancelled.json")


def start_done(rec):
    """Whether a start got through its last step: the plan answered with --plan-first, else the
    brief sent."""
    last = "plan" if rec.get("plan_first") else "brief"
    return any(s["step"] == last and s["status"] == "ok" for s in rec.get("steps") or [])


def cmd_answer(args):
    pane = answer(args.id, args.text)
    print(f"answered {args.id}; prompted {pane}")
    return 0


def default_answers(ags, dry_run):
    """Watchdog: an open question past its `--by` goes to its default. An asker that is gone or
    at a dialog, where typing would answer it, gets it at a later pass; the question stays open
    for the owner until then."""
    at = now()
    for run in all_runs():
        for q in [] if run.get("ended") else run.get("questions") or []:
            pane = q.get("pane") or run.get("orchestrator")
            if (q.get("answered") or not q.get("default_at") or q["default_at"] > at
                    or (ags.get(pane) or {}).get("agent_status") not in ("idle", "done",
                                                                         "working")):
                continue
            if dry_run:
                print(f"would answer {q['id']} with its default {q['default']!r}")
                continue
            try:
                answer(q["id"], q["default"], by_default=True)
            except Refused as e:  # one bad pane doesn't end the pass: the next one tries again
                print(f"{q['id']}: {e.text}", file=sys.stderr)


def write_report(pane, report):
    """Store an agent's turn-end report in its status file (keeping the Stop hook's fields)
    and log it."""
    path = ROOT / "agents" / f"{slug(pane)}.json"
    st = load(path) or {"pane": pane}
    st["report"] = report
    save(path, st)
    emit("report", f"{report.get('role', 'agent').capitalize()} report: {pane}",
         report.get("summary") or "", pane, report=report)


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


def shown_questions(run, at):
    """The run's open questions, and those answered in the last ANSWERED_SHOWN seconds (with
    `answer` and `answered`), so another device sees the answer."""
    return [q for q in (run or {}).get("questions") or []
            if not q.get("answered") or q["answered"] >= at - ANSWERED_SHOWN]


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
        lead = view(orch, ags.get(orch), None, at)
        print(json.dumps({"run": run, "orchestrator": ags.get(orch), "workers": views,
                          "report": lead["report"], "account": lead["account"],
                          "switch": lead["switch"],
                          "spill": spill_summary(load(ROOT / "accounts.json")),
                          "account_names": {k: a["name"] for k, a in account_list().items()},
                          "questions": shown_questions(run, at)},
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
    """Why the run's idle orchestrator should act now (empty when it shouldn't, as while its
    plan waits for the owner's OK: no workers start before it)."""
    orch = run["orchestrator"]
    reasons = {}
    if any(q.get("kind") == "plan" and not q.get("answered") for q in run.get("questions") or []):
        return reasons
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
    claudes = pane_claudes()
    spill_pass(ags, claudes, args.dry_run)
    idle_switches(ags, claudes, args.dry_run)
    default_answers(ags, args.dry_run)  # first: the loop below may save the runs it loads
    state_path = ROOT / "watchdog.json"
    state = load(state_path)
    at = now()
    host = machine_name()
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


# --- decisions ------------------------------------------------------------------------------

ENTRY = re.compile(r"\s{0,3}(?:[-*]\s+)?(?:\*\*)?D(\d+)\b(?:\*\*)?[.:]?\s*(.*)", re.DOTALL)
WHEN = re.compile(r"(~?(?:\d{4}-\d{2}-\d{2}(?:[ T]~?\d{1,2}:\d{2}(?::\d{2})?)?|\d{1,2}:\d{2})"
                  r"(?:\s*(?:[A-Z]{2,5}\b|[+-]\d{2}:?\d{2}))?)"  # "13:55 EET", "~09:05"
                  r"(?:\s*\([^)]*\))?\s*[,;]?\s*")  # "(13:55 UTC)"
WHO = re.compile(r"((?:[^:,.(]|\([^)]*\)){1,60}?):\s+")  # "agent: ", "user (at 22:27): "
QUOTE = re.compile(r"\s*([\"\u201c])(.*?)[\"\u201d]\.?\s*", re.DOTALL)
REPLACES = re.compile(r"\b(?:replaces|replacing|supersedes|superseding|instead of|overrides|"
                      r"overriding)\s+((?:\*\*)?D\d+(?:\*\*)?(?:\s*(?:,|and|&|/)\s*(?:\*\*)?D\d+"
                      r"(?:\*\*)?)*)", re.IGNORECASE)
# A reason is only what an entry marks as one: "Why: ...", a sentence starting "Why", "because ..."
WHY = re.compile(r"(?:(?:^|(?<=[.!?;]\s))why\b|\bwhy(?=:))\s*:?\s*|\bbecause\s+", re.IGNORECASE)
SENTENCE_END = re.compile(r"[.!?](?=\s+[A-Z\"(`]|\s*$)")


def lineage_of(pane):
    """The pane's newest decision log (lineage folders are <pane>-<date>-<time>-<session>)."""
    name = re.compile(re.escape(slug(pane)) + r"-\d{8}-\d{6}-[^-/]+$")
    logs = [p for p in ROOT.glob(f"*/{slug(pane)}-*/decisions.md") if name.match(p.parent.name)]
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def split_why(body):
    """(what, why) of an entry's text. `why` is only an explicit reason: the rest of a "Why:" (or
    "Why ...") sentence, or a "because" clause up to its ";" or full stop. Everything else stays
    in `what`."""
    m = WHY.search(body)
    if not m:
        return body.strip(), ""
    because = m.group(0).lower().startswith("because")
    rest = body[m.end():]
    stop = SENTENCE_END.search(rest)
    end = stop.start() if stop else len(rest)
    if because and 0 <= rest.find(";") < end:
        end = rest.find(";")
    why, before, after = rest[:end].strip(), body[:m.start()].rstrip(), rest[end:]
    if because:  # "X because Y; Z." -> "X; Z."
        what = before.rstrip(" ,") + after
    else:  # the "Why: ..." sentence goes whole, its full stop with it
        what = " ".join(x for x in (before, after[1:].strip() if after[:1] in ".!?" else
                                    after.strip()) if x)
    return what.strip(), why


def after_quote(body):
    """The text after a quoted user line, without the ": " or "( ... )" that tie it to the quote."""
    body = re.sub(r"^\s*[:\u2013\u2014-]\s*", "", body)
    if body.startswith("("):
        depth = 0
        for i, ch in enumerate(body):
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if depth == 0:
                return (body[1:i] + body[i + 1:]).strip()
    return body.strip()


def parse_decisions(text):
    """Entries of a decision log, in log order: id, time, who (user or agent), by (as written,
    else who), quote (the user's words), what (the entry, without its reason), why (only an
    explicit reason), superseded_by, generation, line."""
    entries, generation, current = [], 1, None
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("#"):
            m = re.match(r"## Generation (\d+)", line)
            generation = int(m.group(1)) if m else generation
            current = None  # a heading ends the entry before it
            continue
        m = ENTRY.match(line)
        if m:
            current = {"id": f"D{m.group(1)}", "generation": generation, "line": number,
                       "text": m.group(2).strip()}
            entries.append(current)
        elif current and line.strip():
            current["text"] += " " + line.strip()  # a continuation line
    out, latest = [], {}
    for e in entries:
        rest = e.pop("text")
        when = WHEN.match(rest)
        e["time"] = when.group(1) if when else None
        rest = rest[when.end():] if when else rest
        who = WHO.match(rest)
        e["by"] = who.group(1).strip() if who else ""
        rest = rest[who.end():] if who else rest
        lead = None if e["by"] else re.match(r"(?:the\s+)?(?:user|owner)\b", rest, re.IGNORECASE)
        user = bool(re.search(r"\b(?:user|owner)\b", e["by"], re.IGNORECASE) if e["by"] else lead)
        e["who"] = "user" if user else "agent"
        e["by"] = e["by"] or (lead.group(0) if lead else "agent")
        quote = QUOTE.match(rest) if user else None
        e["quote"] = quote.group(2).strip() if quote else None
        body = after_quote(rest[quote.end():]) if quote else rest
        what, why = split_why(body)
        e["what"], e["why"] = what or e["quote"] or "", why[:1000]
        e["superseded_by"] = None
        for ref in REPLACES.findall(rest):
            for old in re.findall(r"D\d+", ref):
                if old in latest and old != e["id"]:
                    latest[old]["superseded_by"] = e["id"]
        latest[e["id"]] = e
        out.append(e)
    return out


def cmd_decisions(args):
    pane = args.pane or orchestrator(args)
    log = lineage_of(pane)
    if not log:
        die(f"no decision log for {pane} under {ROOT}")
    entries = parse_decisions(log.read_text(errors="replace"))
    if args.json:
        print(json.dumps({"pane": pane, "log": str(log), "decisions": entries}, indent=1))
        return 0
    print(f"{log} ({len(entries)} decisions)")
    for e in entries:
        print(f"  {e['id']:<5} {e['who']:<5} {e['time'] or '':<16} {first_line(e['what'], 100)}"
              + (f" (superseded by {e['superseded_by']})" if e["superseded_by"] else ""))
    return 0


# --- account switching -----------------------------------------------------------------------

def account_config(account):
    """Claude Code's own settings of the account, with its login and trusted folders."""
    return (Path.home() / ".claude.json" if str(account) == "1"
            else account_dir(account) / ".claude.json")


def account_login(account):
    """A hash of the login behind the account, or None without one. Account numbers are local:
    this is what says that two machines share an account. It covers the organization too,
    because the limits are its subscription's."""
    who = load(account_config(account)).get("oauthAccount") or {}
    user, organization = who.get("accountUuid"), who.get("organizationUuid")
    if not user or not organization:
        return None
    return hashlib.sha256(f"{user}:{organization}".encode()).hexdigest()[:16]


def account_list():
    """Account number -> {dir, logged_in, name, login}: ~/.claude, then ~/.claude-2, ... while
    they exist."""
    names = load(ROOT / "accounts.json").get("names") or {}
    n, accounts = 1, {}
    while account_dir(n).exists():
        accounts[str(n)] = {"dir": str(account_dir(n)),
                            "logged_in": (account_dir(n) / ".credentials.json").exists(),
                            "name": account_name(str(n), names), "login": account_login(n)}
        n += 1
    return accounts


def account_name(account, names):
    """The name `orch account --name` gave the account, else "Acct N"."""
    return names.get(account) or f"Acct {account}"


def set_name(account, name):
    """`orch account --name N NAME`; an empty NAME goes back to "Acct N"."""
    if not re.fullmatch(r"\d+", account):
        die(f"accounts are numbers (claude-acc N), not {account!r}")
    path = ROOT / "accounts.json"
    acc = load(path)
    names = acc.setdefault("names", {})
    if name.strip():
        names[account] = name.strip()
    else:
        names.pop(account, None)
    save(path, acc)
    return account_name(account, names)


def pane_claudes():
    """herdr pane -> (pid, account) of the claude processes running in herdr panes, from
    /proc (the account is the process's CLAUDE_CONFIG_DIR, as claude-acc sets it)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import orch_usage
    out = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if (proc / "comm").read_text().strip() != "claude":
                continue
            env = dict(x.split("=", 1) for x in
                       (proc / "environ").read_bytes().decode(errors="replace").split("\0")
                       if "=" in x)
        except OSError:
            continue
        if env.get("HERDR_PANE_ID"):
            out[env["HERDR_PANE_ID"]] = (int(proc.name),
                                         orch_usage.account_of(env.get("CLAUDE_CONFIG_DIR")))
    return out


def limit_hit(session):
    """The usage-limit message ("You've hit your weekly limit · resets ...") the session's last
    turn ended with, else None. Claude Code writes it to the transcript as an API error."""
    paths = list(PROJECTS.glob(f"*/{session}.jsonl")) if session else []
    if not paths:
        return None
    try:
        with open(paths[0], "rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - (256 << 10)))
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") not in ("user", "assistant") or e.get("isSidechain") or e.get("isMeta"):
            continue
        if e["type"] == "user":
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") ==
                                                 "tool_result" for c in content):
                return None  # a tool ran after it
            continue  # a prompt sent after the limit runs into it again
        content = (e.get("message") or {}).get("content")
        text = content if isinstance(content, str) else " ".join(
            c.get("text", "") for c in content or [] if isinstance(c, dict))
        if e.get("isApiErrorMessage") and (e.get("error") == "rate_limit"
                                           or re.match(r"You've hit your \w+ limit", text)):
            return text
        return None
    return None


def check_account(account):
    if not re.fullmatch(r"\d+", account):
        die(f"accounts are numbers (claude-acc N), not {account!r}")
    if not (account_dir(account) / ".credentials.json").exists():
        die(f"account {account} is not logged in ({account_dir(account)}/.credentials.json is "
            f"missing): run `claude-acc {account}` once and log in")


def switch_job(pane, account, agent):
    """(respawn job, None) to restart the pane's agent on `account` now, or (None, why not).
    Only an idle agent with no background tasks is restarted."""
    state = (agent or {}).get("agent_status", "missing")
    if state not in ("idle", "done"):
        return None, f"{pane} is {state}, not idle"
    session = (agent.get("agent_session") or {}).get("value")
    st = status_of(pane)
    # A resumed session keeps its id, so the pid tells whether the status is from this process.
    if not session or st.get("session") != session or not is_claude(st.get("pid")):
        return None, f"no turn end recorded for {pane}'s running agent yet"
    if st.get("background_tasks"):
        return None, (f"{pane} has {len(st['background_tasks'])} background task(s) running "
                      f"({st['background_tasks'][0].get('what', '')})")
    if st.get("account") == account:
        return None, f"{pane} already runs on account {account}"
    return {"pane": pane, "pid": st["pid"], "resume": session, "account": account,
            "args": st.get("relaunch") or [], "session": session, "created": 0}, None


def is_claude(pid):
    try:
        return Path(f"/proc/{int(pid)}/comm").read_text().strip() == "claude"
    except (OSError, TypeError, ValueError):
        return False


def apply_switch(job):
    """Run the switch; on success drop any switch still pending for the pane. Returns the error
    text, or None."""
    result = run_switch(job)
    if result.returncode != 0:
        return (result.stderr or "").strip() or "see ~/.claude/handoffs/respawn.log"
    path = ROOT / "accounts.json"
    acc = load(path)
    sw = (acc.get("switch") or {}).pop(job["pane"], None)
    if (sw or {}).get("reason"):  # moved by the spill rule: the app shows it
        acc.setdefault("moved", {})[job["pane"]] = {"account": job["account"], "time": now(),
                                                    "how": "resume", "reason": sw["reason"]}
        (acc.get("failed") or {}).pop(job["pane"], None)
    if sw:
        save(path, acc)
    return None


def switch_now(pane, account):
    """Restart an idle agent on another account: exit it and resume its session in the same pane
    with `claude-acc N --resume`, through the handoff hook's respawn (resume mode)."""
    ags = agents()
    if ags is None:
        die("herdr is not reachable")
    job, why = switch_job(pane, account, ags.get(pane))
    if not job:
        die(f"{why}: --now needs an idle agent with no background tasks; use --when-idle or "
            "--at-handoff")
    error = apply_switch(job)
    if error:
        die(f"switching {pane} to account {account} failed: {error}")
    print(f"{pane} now runs on account {account} (session {job['session']} resumed)")
    return 0


def idle_switches(ags, claudes, dry_run):
    """Watchdog: carry out the pending --when-idle switches (by hand or the spill rule) of idle
    agents. A large context goes through a handoff: the agent is asked for it, and its takeover
    starts the fresh agent on the new account. An agent that hit its usage limit, a small
    context, or a handoff still unwritten after HANDOFF_WAIT is resumed on the new account
    instead, for up to SWITCH_BUDGET seconds per pass. A resume that fails is dropped (the
    failure was pushed). Every choice is logged with its reason."""
    path = ROOT / "accounts.json"
    started, at = time.monotonic(), now()
    for pane, sw in sorted((load(path).get("switch") or {}).items()):
        agent = ags.get(pane)
        if sw.get("when") != "idle" or (agent or {}).get("agent_status") not in ("idle", "done"):
            continue
        if (claudes.get(pane) or (None, None))[1] == str(sw["account"]):
            already_there(pane, sw)
            continue
        session = (agent.get("agent_session") or {}).get("value")
        st = status_of(pane)
        fresh = st if session and st.get("session") == session else {}
        if fresh.get("background_tasks"):
            continue
        tokens = fresh.get("context_tokens")
        how, why = "handoff", "a fresh agent there reads the handoff, not the whole conversation"
        if sw.get("reason") == "limit":
            how, why = "resume", "it hit its usage limit, so it can't write a handoff first"
        elif tokens is not None and tokens < SMALL_CONTEXT:
            how, why = "resume", (f"its context is {tokens // 1000}K tokens, cheaper to re-read "
                                  "than a handoff")
        elif sw.get("handoff"):
            if at - sw["handoff"] < HANDOFF_WAIT:
                continue  # its takeover follows the handoff it was asked for
            how, why = "resume", f"no handoff {HANDOFF_WAIT // 60} min after asking for one"
        if dry_run:
            print(f"would switch {pane} to account {sw['account']} by {how}: {why}")
            continue
        if how == "handoff":
            message = request_handoff(pane, session, fresh.get("cwd") or agent.get("cwd"), sw)
            if not message:
                continue  # a handoff is already underway; its takeover applies the switch
            if prompt(pane, message):
                mark_switch(pane, handoff=at)
                emit("account", f"Account switch: handoff asked of {pane}",
                     f"to account {sw['account']}: {why}", pane, account=sw["account"],
                     when="idle", how="handoff")
                continue
            how, why = "resume", "the handoff request could not be sent"
        if time.monotonic() - started > SWITCH_BUDGET:
            continue  # the next pass
        job, _ = switch_job(pane, sw["account"], agent)
        if not job:
            continue
        emit("account", f"Account switch: resuming {pane}", f"on account {sw['account']}: {why}",
             pane, account=sw["account"], when="idle", how="resume")
        error = apply_switch(dict(job, when="idle"))
        if error:
            drop_switch(pane)
            if sw.get("reason"):
                path_failed(pane, sw["account"], error, sw["reason"])
            emit("account", f"Account switch dropped: {pane}", error, pane,
                 account=sw["account"], when="idle")


def request_handoff(pane, session, cwd, sw):
    """The handoff hook's request for the agent (its lineage set up to take over on the
    switch), or None while a handoff is already underway."""
    job = {"pane": pane, "session": session, "cwd": cwd or "", "account": sw["account"],
           "why": sw.get("why") or "queued by orch account --when-idle"}
    result = subprocess.run([sys.executable, str(HOOK), "--request-handoff", json.dumps(job)],
                            capture_output=True, text=True, timeout=60, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def mark_switch(pane, **fields):
    path = ROOT / "accounts.json"
    acc = load(path)
    if pane in (acc.get("switch") or {}):
        acc["switch"][pane].update(fields)
        save(path, acc)


def path_failed(pane, account, error, reason):
    path = ROOT / "accounts.json"
    acc = load(path)
    acc.setdefault("failed", {})[pane] = {"account": account, "time": now(), "error": error,
                                          "reason": reason}
    save(path, acc)


def moved_by_hand(acc, pane, account, reason):
    """The agent got to the account without the rule: its pane was restarted there, or the
    dialog its resume stopped at was answered."""
    acc.setdefault("moved", {})[pane] = {"account": account, "time": now(), "how": "by hand",
                                         "reason": reason}
    (acc.get("failed") or {}).pop(pane, None)


def already_there(pane, sw):
    """Drop the pending switch of an agent that runs on its account."""
    path = ROOT / "accounts.json"
    acc = load(path)
    (acc.get("switch") or {}).pop(pane, None)
    if sw.get("reason"):
        moved_by_hand(acc, pane, sw["account"], sw["reason"])
    save(path, acc)


def spill_state(pane, agent, st, acc):
    """Where the spill rule stands with this agent, for the app: moved (at idle or at the
    limit), waiting (and on what), can't move (and why), or None when it isn't involved."""
    sw = (acc.get("switch") or {}).get(pane) or {}
    if sw.get("reason") in ("spill", "limit"):
        status = (agent or {}).get("agent_status")
        reason = ("no agent in the pane" if not agent else "at a dialog" if status == "blocked"
                  else "working" if status == "working"
                  else "own job running" if st.get("background_tasks")
                  else "writing its handoff" if sw.get("handoff") else "the next watchdog pass")
        return {"state": "cant_move" if not agent else "waiting", "reason": reason,
                "time": sw.get("time"), "account": sw["account"]}
    failed = (acc.get("failed") or {}).get(pane)
    if failed:
        return {"state": "cant_move", "reason": failed["error"], "time": failed["time"],
                "account": failed["account"]}
    moved = (acc.get("moved") or {}).get(pane)
    if moved:
        return {"state": "moved_limit" if moved.get("reason") == "limit" else "moved_idle",
                "reason": moved.get("how"), "time": moved["time"], "account": moved["account"]}
    return None


def spill_summary(acc):
    """The spill rule with its progress: since (when it fired), moved of total."""
    rule = acc.get("spill")
    if not rule:
        return None
    involved = ({p for p, sw in (acc.get("switch") or {}).items()
                 if sw.get("reason") in ("spill", "limit")}
                | set(acc.get("moved") or {}) | set(acc.get("failed") or {}))
    return dict(rule, since=(rule.get("active") or {}).get("since"),
                moved=len(acc.get("moved") or {}), total=len(involved))


def account_for_new(acc):
    """The account a new agent should start on: the spill target while the rule is active,
    else the default."""
    rule = acc.get("spill") or {}
    return rule["to"] if rule.get("active") else acc.get("default") or "1"


def drop_switch(pane):
    path = ROOT / "accounts.json"
    acc = load(path)
    if (acc.get("switch") or {}).pop(pane, None):
        save(path, acc)


def spill_end(rule):
    """When the window that fired the rule resets, or None while it isn't active. No reading has
    to confirm it: every agent has left the account by then, so none may come. A rule fired
    before it kept this time finds it in usage.json, where a reset time holds however old."""
    active = rule.get("active") or {}
    entry = (load(ROOT / "usage.json").get("accounts") or {}).get(rule["from"]) or {}
    return active.get("resets_at") or ((entry.get("windows") or {}).get(active.get("window"))
                                       or {}).get("resets_at")


def spill_pass(ags, claudes, dry_run):
    """Watchdog: the spill rule (`orch account --spill FROM:PCT:TO`). Once account FROM's week
    or 5-hour window is at PCT% or more (fresh capture only; stale data counts as unknown),
    every agent on FROM gets a pending when-idle switch to TO. An agent on FROM whose last turn
    ended at the usage limit gets one marked "limit", which is resumed at once. The rule ends
    when a fresh reading is under PCT%, or when the window that fired it resets."""
    path = ROOT / "accounts.json"
    acc = load(path)
    rule = acc.get("spill")
    if not rule:
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import orch_usage
    at = now()
    usage = orch_usage.windows_report(ROOT, at).get(rule["from"])
    resets = spill_end(rule)
    if resets and resets <= at:
        emit("spill", f"Account {rule['from']}'s {rule['active']['window']} window has reset",
             "new agents on it stay there", None)
        rule["active"] = None
    if usage and not usage["stale"]:
        hot = next(({"window": name, "used": w["used_percentage"], "since": at,
                     "resets_at": w["resets_at"]}
                    for name, w in sorted(usage["windows"].items(), reverse=True)
                    if not w["reset"] and w["used_percentage"] is not None
                    and w["used_percentage"] >= rule["at"]), None)
        if hot and not rule.get("active"):
            rule["active"] = hot
            acc["moved"], acc["failed"] = {}, {}
            emit("spill", f"Account {rule['from']} at {hot['used']}%: moving to {rule['to']}",
                 f"its {hot['window']} window reached {rule['at']}%; its agents move to account "
                 f"{rule['to']} at their next turn end or when idle", None, push=True, **hot)
        elif not hot and rule.get("active"):
            rule["active"] = None
            emit("spill", f"Account {rule['from']} is under {rule['at']}% again",
                 "new agents on it stay there", None)
    switches = acc.setdefault("switch", {})
    for pane, agent in sorted(ags.items()):
        on, failed = (claudes.get(pane) or (None, None))[1], (acc.get("failed") or {}).get(pane)
        if failed and on == rule["to"]:
            moved_by_hand(acc, pane, on, failed.get("reason") or "spill")
        if on != rule["from"] or failed:  # can't move: shown, not retried
            continue
        sw = switches.get(pane)
        limit = (agent.get("agent_status") in ("idle", "done")
                 and limit_hit((agent.get("agent_session") or {}).get("value")))
        if limit and (sw or {}).get("reason") != "limit":
            switches[pane] = {"account": rule["to"], "time": at, "when": "idle",
                              "reason": "limit", "why": f"it hit the usage limit ({limit})"}
        elif rule.get("active") and not sw:
            switches[pane] = {"account": rule["to"], "time": at, "when": "idle",
                              "reason": "spill",
                              "why": (f"account {rule['from']} is at {rule['active']['used']}% "
                                      f"of its {rule['active']['window']} window")}
        else:
            continue
        emit("account", f"Account switch queued: {pane}",
             f"moves to account {rule['to']} when idle: {switches[pane]['why']}", pane,
             account=rule["to"], when="idle", reason=switches[pane]["reason"])
    if dry_run:
        print(f"spill {rule['from']}:{rule['at']}:{rule['to']} active={rule.get('active')}; "
              f"pending: {sorted(switches)}")
        return
    save(path, acc)


def set_spill(text):
    """`orch account --spill FROM:PCT:TO` or `--spill off` (which also drops the pending
    switches the rule queued)."""
    path = ROOT / "accounts.json"
    acc = load(path)
    if text == "off":
        acc.pop("spill", None)
        for pane in [p for p, sw in (acc.get("switch") or {}).items()
                     if sw.get("reason") in ("spill", "limit")]:
            acc["switch"].pop(pane)
        save(path, acc)
        emit("spill", "Spill rule off", "pending spill switches dropped", None)
        return "spill rule off"
    m = re.fullmatch(r"(\d+):(\d+(?:\.\d+)?):(\d+)", text)
    if not m or m.group(1) == m.group(3):
        die(f"--spill takes FROM:PCT:TO (e.g. 1:98:2) or off, not {text!r}")
    check_account(m.group(3))
    pct = float(m.group(2))
    acc["spill"] = {"from": m.group(1), "at": int(pct) if pct.is_integer() else pct,
                    "to": m.group(3), "time": now(), "active": None}
    save(path, acc)
    rule = f"{m.group(1)}:{acc['spill']['at']}:{m.group(3)}"
    emit("spill", f"Spill rule on: {rule}", f"account {m.group(1)} at {acc['spill']['at']}% moves "
         f"its agents to account {m.group(3)}", None)
    return (f"spill rule on: when account {m.group(1)}'s week or 5-hour window reaches "
            f"{acc['spill']['at']}%, its agents move to account {m.group(3)} (by the watchdog)")


def run_switch(job):
    return subprocess.run([sys.executable, str(HOOK), "--respawn", json.dumps(job)],
                          capture_output=True, text=True, timeout=600, check=False)


def cmd_account(args):
    acc = load(ROOT / "accounts.json")
    if args.default:
        set_default(args.default)
        print(f"default account for new agents: {args.default}")
        return 0
    if args.spill:
        print(set_spill(args.spill))
        return 0
    if args.for_new:
        print(account_for_new(acc))
        return 0
    if args.name:
        print(f"account {args.name[0]} is named {set_name(*args.name)}")
        return 0
    if not args.pane:
        accounts = account_list()
        out = {"accounts": accounts, "default": acc.get("default") or "1",
               "for_new": account_for_new(acc), "switch": acc.get("switch") or {},
               "spill": spill_summary(acc)}
        if args.json:
            print(json.dumps(out, indent=1))
            return 0
        for k, a in accounts.items():
            print(f"account {k} ({a['name']}): {a['dir']}"
                  + ("" if a["logged_in"] else " (not logged in)")
                  + (" (default)" if k == out["default"] else ""))
        if out["spill"]:
            sp = out["spill"]
            print(f"spill {sp['from']}:{sp['at']}:{sp['to']}" + (
                f" (active since {clock(sp['active']['since'])}: {sp['active']['window']} at "
                f"{sp['active']['used']}%)" if sp.get("active") else " (not reached)"))
        for pane, sw in out["switch"].items():
            print(f"  {pane} moves to account {sw['account']} "
                  + ("when idle" if sw.get("when") == "idle" else "at its next handoff")
                  + (f" ({sw['why']})" if sw.get("why") else ""))
        return 0
    if not args.account:
        die("usage: orch account PANE N [--now | --at-handoff]")
    check_account(args.account)
    if args.now:
        return switch_now(args.pane, args.account)
    text = queue_switch(args.pane, args.account, "idle" if args.when_idle else "handoff")
    print(f"{args.pane} moves to account {args.account} {text}")
    return 0


def set_default(account):
    check_account(account)
    path = ROOT / "accounts.json"
    acc = load(path)
    acc["default"] = account
    save(path, acc)
    emit("account", f"Default account: {account}", "for new agents", None, account=account)


def queue_switch(pane, account, when):
    """Move the pane's agent to account N when it is next idle ("idle", the watchdog does it) or
    at its next handoff ("handoff"); returns how, in words."""
    check_account(account)
    path = ROOT / "accounts.json"
    acc = load(path)
    text = ("at its next turn end or when idle, through a handoff and a fresh agent there (a "
            "small context or an agent at its usage limit is resumed instead)" if when == "idle"
            else "at its next handoff (no extra tokens)")
    acc.setdefault("switch", {})[pane] = {"account": account, "time": now(), "when": when}
    save(path, acc)
    emit("account", f"Account switch queued: {pane}", f"moves to account {account} {text}", pane,
         account=account, when=when)
    return text


def cmd_usage(args):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import orch_usage
    return orch_usage.main(args, ROOT, now(), agents, account_list())


def cmd_new(args):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import orch_new
    return orch_new.main(args)


def cmd_serve(args):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import orch_serve
    return orch_serve.serve(args)


def cmd_pair(args):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import orch_serve
    return orch_serve.pair(args)


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
    a.add_argument("--option", action=OptionAction, dest="options", metavar="LABEL",
                   help="an answer the owner can pick (repeatable)")
    a.add_argument("--danger", action=OptionAction, dest="options", choices=DANGER,
                   help="what the option before it does when picked (default none)")
    a.add_argument("--kind", choices=["plan"],
                   help="plan: a new orchestrator's plan (orch new --plan-first), for approval")
    a.add_argument("--default", help="the option this goes to with no reply by --by; not a "
                                     "dangerous option, not for a plan")
    a.add_argument("--by", help="when: HH:MM, 'YYYY-MM-DD HH:MM' or +30m")
    a.set_defaults(func=cmd_ask)

    ans = sub.add_parser("answer", help="record the owner's answer and prompt the asker")
    ans.add_argument("id")
    ans.add_argument("text")
    ans.set_defaults(func=cmd_answer)

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

    dec = sub.add_parser("decisions", help="the pane's current decision log, parsed")
    dec.add_argument("--pane", help="default: this pane (or --orch)")
    dec.add_argument("--json", action="store_true")
    dec.set_defaults(func=cmd_decisions)

    acct = sub.add_parser("account", help="show accounts, or move an agent to another one")
    acct.add_argument("pane", nargs="?")
    acct.add_argument("account", nargs="?", help="N as in claude-acc N")
    when = acct.add_mutually_exclusive_group()
    when.add_argument("--now", action="store_true",
                      help="restart the idle agent now and resume its session (re-reads its "
                           "context uncached on the new account)")
    when.add_argument("--when-idle", action="store_true",
                      help="at its next turn end or when idle: a handoff and a fresh agent on "
                           "the new account, or a resume for a small context")
    when.add_argument("--at-handoff", action="store_true",
                      help="at its next context handoff (default; costs nothing extra)")
    acct.add_argument("--default", metavar="N", help="the account for new agents")
    acct.add_argument("--spill", metavar="FROM:PCT:TO",
                      help="move every agent on account FROM to account TO once FROM's week or "
                           "5-hour window reaches PCT%% (e.g. 1:98:2), or 'off'")
    acct.add_argument("--for-new", action="store_true",
                      help="print the account a new agent should start on (claude-acc N): the "
                           "spill target while the rule is active, else the default")
    acct.add_argument("--name", nargs=2, metavar=("N", "NAME"),
                      help="a short name for account N in status and the phone app (default "
                           "\"Acct N\"; \"\" resets it)")
    acct.add_argument("--json", action="store_true")
    acct.set_defaults(func=cmd_account)

    nw = sub.add_parser("new", help="start a new orchestrator in a new herdr tab here")
    nw.add_argument("--cwd", required=True, help="the folder it works in (or branches from)")
    nw.add_argument("--goal", required=True, help="what the run is for, in the owner's words")
    nw.add_argument("--until", required=True, help="HH:MM, 'YYYY-MM-DD HH:MM' or +8h")
    nw.add_argument("--model", help="claude --model (default: Claude Code's)")
    nw.add_argument("--effort", help="claude --effort")
    nw.add_argument("--account", default="auto",
                    help="N as in claude-acc N, or auto (default): the account for new agents")
    nw.add_argument("--plan-first", action="store_true",
                    help="propose a roadmap and wait for the owner's Approve before working")
    nw.add_argument("--rules", action="append", metavar="TEXT",
                    help="an owner rule, written at the top of its decision log (repeatable)")
    nw.add_argument("--name", help="the herdr agent's name (and the worktree's)")
    nw.add_argument("--worktree", action="store_true",
                    help="work in a new git worktree <cwd>-<name> on branch orch/<name>; "
                         "with no --name, named after the goal's first three words")
    nw.add_argument("--worker-model", help="the model its workers start on (in its brief)")
    nw.add_argument("--max-workers", type=int, help="how many workers at a time (in its brief)")
    nw.add_argument("--id", help="the request's id: the same id again starts nothing")
    nw.add_argument("--dry-run", action="store_true", help="print the steps, change nothing")
    nw.add_argument("--json", action="store_true")
    nw.set_defaults(func=cmd_new)

    u = sub.add_parser("usage", help="account windows and tokens per agent")
    u.add_argument("action", nargs="?", choices=["capture"],
                   help="capture: read the statusline JSON on stdin (run by the statusline)")
    u.add_argument("--hours", type=float, default=24, help="period for agents (default 24)")
    u.add_argument("--json", action="store_true")
    u.set_defaults(func=cmd_usage)

    sv = sub.add_parser("serve", help="the HTTP gateway for the owner's app, on the tailnet only")
    sv.add_argument("--allow-self", action="store_true",
                    help="also accept requests from this machine's own node (tests only)")
    sv.set_defaults(func=cmd_serve)

    pr = sub.add_parser("pair", help="create the gateway's pairing token and print it")
    pr.add_argument("--allow", action="append", metavar="NODE",
                    help="tailnet node allowed to call the gateway: its name or StableID "
                         "(repeatable; replaces the list)")
    pr.add_argument("--new", action="store_true", help="replace the token")
    pr.set_defaults(func=cmd_pair)

    n = sub.add_parser("notify", help="send a test alert through the notify command")
    n.add_argument("title")
    n.add_argument("body")
    n.set_defaults(func=cmd_notify)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except Refused as e:
        print(f"orch: {e.text}", file=sys.stderr)
        raise


if __name__ == "__main__":
    sys.exit(main())
