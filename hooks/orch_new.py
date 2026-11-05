"""orch new: start a new orchestrator in a new herdr tab on this machine.

`orch new --cwd DIR --goal TEXT --until T [--model M] [--effort E] [--account N|auto]
[--plan-first] [--rules TEXT ...] [--name NAME] [--worktree] [--worker-model M]
[--max-workers N] [--id ID] [--dry-run] [--json]`, or the gateway's `start` action. The start is
kept in ~/.claude/orch/new/<id>/new.json; each step is `doing`, `ok` or `failed`, logged as a
`start` event {action, step, status, error} (a failure alerts):

  pane     with --worktree, `git worktree add <folder>-<name> -b orch/<name>` first; a herdr tab
           in the folder (its root pane is the orchestrator's, printed first); a new lineage for
           that pane whose decision log starts with the goal and the owner's rules as user
           entries (D1, D2, ...); the brief, ~/.claude/orch/new/<id>/brief.md
  claude   `HANDOFF_LINEAGE=<lineage> claude-acc N --session-id S [--model M] [--effort E]`
           typed into the pane, until the agent is up
  trust    only when the new agent stops at a dialog (folder trust on that account): it waits
           up to 15 minutes for an answer in the pane, as a takeover does
  run      `orch run` registered for the pane (goal, until, cwd, settings, no workers yet)
  read     with --plan-first: "Read <brief> and do it." sent; ok once the orchestrator asks
           `orch ask --kind plan`, which makes `plan` doing until the owner answers
  brief    without --plan-first: the same prompt, ok once the agent works on it

A second start with the same id starts nothing: it prints the first one's state. --dry-run
prints the steps and the brief and changes nothing. From the app, retry() runs a failed start again
from its failed step (Claude again with another account), cancel() ends one that isn't done, and
trust_keys() answers the new agent's folder-trust question.
"""
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import orch

READY_WAIT = 90  # seconds for the new agent to come up, as a takeover's
EXIT_WAIT = 15  # seconds for a Claude to quit (cancel, or a restart on another account)
BLOCKED_POLLS = 3  # one-second polls at a dialog before it counts as waiting there
WORD = re.compile(r"[A-Za-z0-9._\[\]-]{1,64}")  # model, effort, name: nothing for a shell

PLAN_FIRST = """\
## Plan first

Before any other work, read the folder (README, CLAUDE.md or AGENTS.md, handoff docs) and plan
the run. Then:

1. Call the `report` tool (role orchestrator, state `waiting_on_owner`) with the plan as
   `roadmap` items (status `next` or `later`, with an owner where you know it) and a one-line
   summary.
2. Ask for the owner's OK: `orch ask --kind plan --option "Approve and start workers"
   --option "Cancel the run" "Plan ready: <the plan in one line>"`.
3. End your turn and wait. Start no workers and change no files until the answer arrives as a
   prompt. "Approve and start workers": carry out the plan. "Cancel the run": run
   `orch run stop` and stop. A message with changes to the plan instead is the owner's edit:
   revise the plan, report it again and ask again the same way (that replaces the open
   question).
"""


class Cancelled(Exception):
    """The start was cancelled while it ran (cancel())."""


OPTION = re.compile(r"^\s*(?P<cursor>[❯›>])?\s*(?:\d\.\s+)?(?P<label>(?:Yes|No)\b.*?)\s*$")


def trust_keys(lines, choice):
    """The keys that pick "Yes" (allow) or "No" (deny) in Claude Code's folder-trust question
    on a screen, or None when the screen shows something else: a question about trust above a
    last block of option lines (numbered or not) with the cursor, one Yes and one No. Claude
    Code 2.x puts the cursor on "No, exit"."""
    if not lines:
        return None
    options, cursor, asked, after_text = [], None, False, True
    for line in lines:
        if not line.strip():
            continue
        m = OPTION.match(line)
        if not m:
            asked, after_text = asked or "trust" in line.lower(), True
            continue
        if after_text:  # a new block of options: only the last one counts
            options, cursor, after_text = [], None, False
        if m["cursor"]:
            cursor = len(options)
        options.append(m["label"])
    yes = [i for i, label in enumerate(options) if label.startswith("Yes")]
    no = [i for i, label in enumerate(options) if label.startswith("No")]
    if not asked or cursor is None or len(yes) != 1 or len(no) != 1:
        return None
    moves = (yes if choice == "allow" else no)[0] - cursor
    return ["down" if moves > 0 else "up"] * abs(moves) + ["enter"]


def hook():
    """The context-handoff hook as a module, for its lineage layout and dialog wait."""
    spec = importlib.util.spec_from_file_location("context_handoff", orch.HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def one_line(text):
    return " ".join(text.split())


def tilde(path):
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def under(path, root):
    return path == root or root in path.parents


def paths(text):
    """ORCH_START_ROOTS / ORCH_START_EXCLUDE: comma-separated folders, ~ allowed."""
    return [Path(os.path.expanduser(p.strip())).resolve()
            for p in (text or "").split(",") if p.strip()]


def check(opts, strict=False):
    """A start record (not saved yet) from the request's fields, or orch.Refused saying why.
    strict (a start from the phone): the folder must be under ORCH_START_ROOTS, not under
    ORCH_START_EXCLUDE (~/.config/orch/.env), and not one an orchestrator works in already
    unless it gets a worktree."""
    at = orch.now()
    folder = Path(os.path.expanduser(str(opts.get("folder") or ""))).resolve()
    if not opts.get("folder") or not folder.is_dir():
        orch.die(f"{opts.get('folder') or 'the folder'} is not a folder on {orch.machine_name()}")
    goal = str(opts.get("goal") or "").strip()
    if not goal:
        orch.die("the goal is empty: what the run is for, in the owner's words")
    until = orch.parse_until(str(opts.get("until") or ""), at)
    if until <= at:
        orch.die("the end time has passed")
    for key in ("model", "effort", "worker_model", "name"):
        if opts.get(key) is not None and not WORD.fullmatch(str(opts[key])):
            orch.die(f"{key} {opts[key]!r}: letters, digits and . _ - [ ] only")
    workers = opts.get("max_workers")
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int)
                                or not 0 <= workers <= 16):
        orch.die(f"max_workers {workers!r}: a number from 0 to 16")
    rules = opts.get("rules") or []
    if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
        orch.die("rules is a list of texts")
    account = str(opts.get("account") or "auto")
    if account == "auto":
        account = orch.account_for_new(orch.load(orch.ROOT / "accounts.json"))
    orch.check_account(account)
    sid = str(opts.get("id") or uuid.uuid4().hex[:12])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
        orch.die(f"id {sid!r}: letters, digits, - and _ only")
    name = opts.get("name") or None
    cwd = folder
    if opts.get("worktree"):
        if not (folder / ".git").exists():
            orch.die(f"{tilde(str(folder))} is not a git checkout: no worktree")
        name = name or folder.name
        cwd = folder.parent / f"{folder.name}-{name}"
        if cwd.exists():
            orch.die(f"{tilde(str(cwd))} exists already: pick another name for the worktree")
    if strict:
        env = orch.read_env()
        roots, machine = paths(env.get("ORCH_START_ROOTS")), orch.machine_name(env)
        if any(under(folder, x) for x in paths(env.get("ORCH_START_EXCLUDE"))):
            orch.die(f"{tilde(str(folder))} is excluded from starts on {machine} "
                     "(a live checkout?): pick a working copy")
        if not roots:
            orch.die(f"starts from the app are off on {machine}: ORCH_START_ROOTS is not set "
                     "in ~/.config/orch/.env")
        if not any(under(folder, r) for r in roots):
            orch.die(f"{tilde(str(folder))} is outside the start folders on {machine} ("
                     + ", ".join(tilde(str(r)) for r in roots) + ")")
        busy = next((r for r in orch.all_runs() if orch.active(r, at)
                     and r.get("cwd") and Path(r["cwd"]).resolve() == folder), None)
        if busy and not opts.get("worktree"):
            orch.die(f"{busy['orchestrator']} works in {tilde(str(folder))} already: send it "
                     "there, or start in a new worktree")
    return {"id": sid, "time": at, "by": opts.get("by") or "cli", "folder": str(folder),
            "worktree": bool(opts.get("worktree")), "name": name, "cwd": str(cwd), "goal": goal,
            "until": until, "account": account, "model": opts.get("model"),
            "effort": opts.get("effort"), "worker_model": opts.get("worker_model"),
            "max_workers": workers, "plan_first": bool(opts.get("plan_first")),
            "rules": [r.strip() for r in rules if r.strip()], "session": str(uuid.uuid4()),
            "tab": None, "pane": None, "lineage": None,
            "brief": str(orch.STARTS / sid / "brief.md"), "step": None, "status": "accepted",
            "steps": [], "error": None}


def accept(rec):
    """Save a new start; False when its id exists (a retried request), which starts nothing."""
    try:
        (orch.STARTS / rec["id"]).mkdir(parents=True)
    except FileExistsError:
        return False
    orch.save(orch.STARTS / rec["id"] / "new.json", rec)
    return True


def spawn(sid):
    """Run the saved start detached (the gateway: it may wait 15 minutes at a dialog)."""
    with open(orch.STARTS / sid / "start.log", "a") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), sid],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)


def log_entries(goal, rules, at):
    """The new decision log's first entries: the goal, then each owner rule."""
    when = orch.clock(at, True)
    out = [f'- **D1** {when}, user (orch new, the goal of this run): "{one_line(goal)}"']
    out += [f'- **D{n}** {when}, user (orch new, owner rule): "{one_line(rule)}"'
            for n, rule in enumerate(rules, 2)]
    return "\n".join(out) + "\n"


def brief_text(rec):
    rules = "".join(f"- {r}\n" for r in rec["rules"]) or "- none given\n"
    workers = ""
    if rec.get("max_workers") is not None or rec.get("worker_model"):
        workers = ("\n## Workers\n\n"
                   + (f"At most {rec['max_workers']} at a time. " if rec.get("max_workers")
                      is not None else "")
                   + (f"Start them on model {rec['worker_model']}." if rec.get("worker_model")
                      else "") + "\n")
    where = rec["cwd"] + (f" (a new worktree of {rec['folder']}, branch orch/{rec['name']})"
                          if rec.get("worktree") else "")
    return (
        f"# Orchestrator brief (orch new {rec['id']}, {orch.clock(rec['time'], True)})\n\n"
        f"You are the orchestrator of a new orch run on {orch.machine_name()}, in herdr pane "
        f"{rec['pane']}, folder {where}, started by the owner"
        + (" from the phone app" if rec.get("by") == "phone" else " with `orch new`") + ".\n\n"
        f"## Goal (the owner's words)\n\n{rec['goal']}\n\n"
        f"## Owner rules\n\n{rules}\n"
        f"They are also D1 (the goal) to D{len(rec['rules']) + 1} in your decision log "
        f"{rec['lineage']}/decisions.md, which you keep (append-only, rules at its top).\n\n"
        f"## Run\n\nRegistered with `orch run` for this pane until "
        f"{orch.clock(rec['until'], True)}, with no workers yet: add them with "
        "`orch run set --add PANE=label` as you start them. `orch status` shows the run.\n"
        + workers + "\n"
        + (PLAN_FIRST if rec["plan_first"] else "## Start\n\nStart on the goal.\n"))


def main(args):
    rec = check({"folder": args.cwd, "goal": args.goal, "until": args.until,
                 "account": args.account, "model": args.model, "effort": args.effort,
                 "worker_model": args.worker_model, "max_workers": args.max_workers,
                 "rules": args.rules or [], "plan_first": args.plan_first, "name": args.name,
                 "worktree": args.worktree, "id": args.id})
    if args.dry_run:
        return start(rec, dry=True)
    if not accept(rec):
        old = orch.load(orch.STARTS / rec["id"] / "new.json")
        print(json.dumps(old, indent=1) if args.json else
              f"{rec['id']} exists: pane {old.get('pane')}, {old.get('step')} {old.get('status')}"
              + (f" ({old['error']})" if old.get("error") else ""))
        return 1 if old.get("status") == "failed" else 0
    code = start(rec)
    if args.json:
        print(json.dumps(orch.load(orch.STARTS / rec["id"] / "new.json"), indent=1))
    return code


def claude_acc():
    return shutil.which("claude-acc") or str(Path.home() / ".local" / "bin" / "claude-acc")


def start(rec, dry=False):
    """Carry out a start, or resume a retried one at its first step that isn't ok; 0 when the
    orchestrator got its brief, 1 when a step failed or the start was cancelled."""
    def step(name, status, text=""):
        if dry:
            print(f"would {name} {status}: {text}")
            return
        if orch.start_cancelled(rec):  # cancel() took over: no more steps
            raise Cancelled
        print(f"{name} {status}: {text}", flush=True)
        orch.start_step(rec, name, status, text,
                        **({"folder": rec["folder"], "goal": rec["goal"], "by": rec["by"]}
                           if name == "pane" else {}))

    ok = {s["step"] for s in rec.get("steps") or [] if s["status"] == "ok"}
    restart = rec.pop("restart", False)  # a retry on another account: Claude again
    if not dry:
        rec["pid"] = os.getpid()  # retry() may run a start whose process is gone again
    current = "pane"
    try:
        if "pane" in ok:
            pane, h = rec["pane"], hook()
        else:
            if rec["worktree"] and not Path(rec["cwd"]).exists():
                command = ["git", "-C", rec["folder"], "worktree", "add", rec["cwd"], "-b",
                           f"orch/{rec['name']}"]
                if dry:
                    print("would run: " + " ".join(shlex.quote(a) for a in command))
                else:
                    out = subprocess.run(command, capture_output=True, text=True, timeout=120,
                                         check=False)
                    if out.returncode != 0:
                        step("pane", "failed", "git worktree add failed: "
                             + one_line(out.stderr or out.stdout)[:300])
                        return 1
            if dry:
                pane = rec["pane"] = "<pane>"
            else:
                out = orch.herdr("tab", "create", "--cwd", rec["cwd"], "--label",
                                 rec.get("name") or Path(rec["cwd"]).name, "--no-focus")
                result = json.loads(out.stdout or "{}").get("result") or {}
                pane = (result.get("root_pane") or {}).get("pane_id")
                if out.returncode != 0 or not pane:
                    step("pane", "failed", "herdr couldn't open a tab: "
                         + one_line(out.stderr or out.stdout or "no answer")[:300])
                    return 1
                rec.update(tab=(result.get("tab") or {}).get("tab_id"), pane=pane)
            h = hook()
            base = orch.ROOT / rec["cwd"].strip("/").replace("/", "-").replace(":", "-")
            if dry:
                rec["lineage"] = str(base / f"{pane}-<time>-{rec['session'][:8]}")
                print(brief_text(rec))
            else:
                lineage = h.new_lineage(base, rec["session"], pane)
                with open(lineage / "decisions.md", "a") as f:
                    f.write(log_entries(rec["goal"], rec["rules"], rec["time"]))
                h.seal(lineage)
                rec["lineage"] = str(lineage)
                Path(rec["brief"]).write_text(brief_text(rec))
            step("pane", "ok", f"{pane} in a new tab in {rec['cwd']}; decision log "
                               f"{rec['lineage']}/decisions.md; brief {rec['brief']}")

        current = "claude"
        if "claude" not in ok or restart:
            here = not dry and pane in (orch.agents() or {})
            if here and not restart:  # a retry: Claude is up already, maybe at a dialog
                command = None
                step("claude", "doing", f"waiting for Claude in {pane}")
            else:
                if here and not exit_agent(pane):
                    step("claude", "failed", f"the Claude in {pane} did not quit for the restart")
                    return 1
                if any(s["step"] == "claude" for s in rec.get("steps") or []):
                    rec["session"] = str(uuid.uuid4())  # the first one may be taken
                args = ["--session-id", rec["session"]]
                args += ["--model", rec["model"]] if rec["model"] else []
                args += ["--effort", rec["effort"]] if rec["effort"] else []
                command = (f"HANDOFF_LINEAGE={shlex.quote(rec['lineage'])} "
                           + " ".join(shlex.quote(a) for a in [claude_acc(), rec["account"],
                                                               *args]))
                if not dry:
                    orch.herdr("pane", "run", pane, command)
                step("claude", "doing", command)
            if not dry:
                trust = any(s["step"] == "trust" and s["status"] == "doing"
                            for s in rec.get("steps") or [])  # retried at the trust step
                how = ready(rec, pane, h.DIALOG_WAIT, step, trust)
                if how != "ready":
                    current = how
                    step(how, "failed", f"no ready agent in {pane}"
                         + (f" after running {command}" if command else "")
                         + (" (nobody answered the dialog)" if how == "trust" else ""))
                    return 1
                if rec.get("name"):
                    orch.herdr("agent", "rename", pane, rec["name"])
            step("claude", "ok", f"Claude is up in {pane} on account {rec['account']}")

        current = "run"
        settings = {k: rec[k] for k in ("folder", "worktree", "model", "effort", "worker_model",
                                        "max_workers", "plan_first", "account")}
        if "run" in ok:
            run = orch.load_run(pane)
            if run and restart and not dry:
                run["settings"] = settings
                orch.save(orch.run_path(pane), run)
        else:
            run = orch.new_run(pane, rec["until"], rec["goal"], {}, rec["cwd"], orch.now())
            run["settings"] = settings
            if not dry:
                orch.save_run(pane, run, "start")
            step("run", "ok", f"run {pane} until {orch.clock(rec['until'], True)}")

        current = "read" if rec["plan_first"] else "brief"
        text = f"Read {rec['brief']} and do it."
        if not dry and not deliver(pane, text):
            step(current, "failed", f"the agent in {pane} did not start on {text!r} (sent twice)")
            return 1
        step(current, "doing" if rec["plan_first"] else "ok",
             "reading the folder and writing the plan" if rec["plan_first"]
             else f"{pane} is working on the brief")
        return 0
    except Cancelled:
        print(f"{current}: cancelled", flush=True)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        try:
            step(current, "failed", repr(e))
        except Cancelled:
            pass
        return 1


def ready(rec, pane, dialog_wait, step, trust=False):
    """Wait for the new agent: "ready", or "claude"/"trust" for the step that timed out. Up to
    READY_WAIT seconds, or dialog_wait once it sits at a dialog (the `trust` step; `trust`: it
    was there already, a retry)."""
    blocked = 0
    deadline = orch.now() + (dialog_wait if trust else READY_WAIT)
    while orch.now() < deadline:
        if orch.start_cancelled(rec):
            raise Cancelled
        state = ((orch.agents() or {}).get(pane) or {}).get("agent_status")
        if state in ("idle", "done"):
            if trust:
                step("trust", "ok", "answered")
            return "ready"
        blocked = blocked + 1 if state == "blocked" else 0
        if blocked == BLOCKED_POLLS and not trust:
            trust = True
            text = (f"{pane}: the new orchestrator for {tilde(rec['cwd'])} is at a dialog "
                    f"(folder trust on account {rec['account']}?); answer it in the pane, then it "
                    f"gets its brief (waiting up to {dialog_wait // 60} min)")
            step("trust", "doing", text)
            if rec.get("by") != "phone":  # the app has the start push and the dialog card
                orch.emit("respawn-dialog", "New orchestrator waits at a dialog", text, pane,
                          push=True, action=rec["id"])
            deadline = orch.now() + dialog_wait
        orch.sleep(1)
    return "trust" if trust else "claude"


def deliver(pane, text):
    """Prompt the agent and check that it started: Enter if the text sits in the input box,
    then the prompt once more."""
    def started():
        return orch.herdr("agent", "wait", pane, "--until", "working", "--timeout", "30000",
                          timeout=60).returncode == 0
    orch.prompt(pane, text)
    if started():
        return True
    orch.herdr("agent", "send-keys", pane, "enter")
    if started():
        return True
    orch.prompt(pane, text)
    return started()



def exit_agent(pane):
    """Quit the Claude in the pane: "No" at the folder-trust question (that exits it), else Esc
    at another dialog or while it works, then /exit. True once herdr lists no agent there."""
    def state():
        return ((orch.agents() or {}).get(pane) or {}).get("agent_status")

    now = state()
    keys = trust_keys(orch.screen_lines(pane, 40, "visible"), "deny") if now == "blocked" else None
    if keys:
        orch.herdr("agent", "send-keys", pane, *keys)
    elif now:
        if now in ("blocked", "working"):
            orch.herdr("agent", "send-keys", pane, "esc")
            orch.sleep(1)
        orch.prompt(pane, "/exit")
    deadline = orch.now() + EXIT_WAIT
    while state() and orch.now() < deadline:
        orch.sleep(1)
    return not state()


def cancel(rec, by):
    """Cancel a start that isn't done: mark it (a start still running stops at its next step),
    end its run, quit its Claude, then close the pane it opened; herdr drops the tab with its
    last pane, and a tab someone else split stays. The worktree stays. Returns a sentence."""
    orch.save(orch.STARTS / rec["id"] / "cancelled.json", {"time": orch.now(), "by": by})
    pane, done = rec.get("pane"), []
    run = orch.load_run(pane) if pane else {}
    if orch.active(run):
        run["ended"] = orch.now()
        orch.save_run(pane, run, "stop")
        done.append("run stopped")
    if pane:
        if not exit_agent(pane):
            done.append(f"Claude didn't quit within {EXIT_WAIT} s")
        orch.herdr("pane", "close", pane)
        done.append(f"pane {pane} closed")
        if rec.get("tab") and orch.herdr("tab", "get", rec["tab"]).returncode == 0:
            done.append(f"tab {rec['tab']} kept: it has other panes")
    text = "; ".join(done) or "nothing was open"
    orch.emit("start-cancel", f"Start of {os.path.basename(rec.get('cwd') or '')} cancelled",
              f"by {by}: {text}", pane, action=rec["id"], by=by)
    return text


def retry(rec, account, by):
    """Run a failed start again from its failed step, detached; with `account`, Claude starts
    again on that account. orch.Refused says why not."""
    if orch.start_cancelled(rec):
        orch.die(f"the start {rec['id']} was cancelled")
    if orch.start_done(rec) or (rec.get("status") != "failed" and running(rec.get("pid"))):
        orch.die(f"the start {rec['id']} has not failed ({rec.get('step')} "
                 f"{rec.get('status')}): nothing to retry")
    failed = rec["step"]
    if account is not None and str(account) != str(rec["account"]):
        orch.check_account(str(account))
        rec.update(account=str(account), restart="claude" in {s["step"] for s in rec["steps"]})
        rec["steps"] = [s for s in rec["steps"] if s["step"] != "trust"]  # that account's own
        if failed == "trust":
            failed = "claude"
    rec.setdefault("retries", []).append({"time": orch.now(), "by": by, "step": failed,
                                          "account": rec["account"]})
    orch.start_step(rec, failed, "doing", f"retry by {by}"
                    + (f" on account {rec['account']}" if rec.get("restart") else ""))
    spawn(rec["id"])


def running(pid):
    """Whether the start's process is alive (a start left `doing` by a killed process is not)."""
    if not pid:
        return True  # not recorded: assume it runs
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def cannot_start(env):
    """Why a start from the app can't work on this machine now, or None."""
    if not paths(env.get("ORCH_START_ROOTS")):
        return "starts from the app are off: ORCH_START_ROOTS is not set in ~/.config/orch/.env"
    if orch.agents() is None:
        return "herdr isn't running"
    if not os.access(claude_acc(), os.X_OK):
        return "claude-acc is not installed"
    return None


if __name__ == "__main__":  # spawn(): the gateway's start or retry, saved in new.json
    sys.exit(start(orch.load(orch.STARTS / sys.argv[1] / "new.json")))
