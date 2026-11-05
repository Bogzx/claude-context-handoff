"""orch new: start a new orchestrator in a new herdr tab on this machine.

`orch new --cwd DIR --goal TEXT --until T [--model M] [--effort E] [--account N|auto]
[--plan-first] [--rules TEXT ...] [--id ID] [--dry-run] [--json]` runs these steps, each one
logged as a `new` event (`id`, `step`) and kept in ~/.claude/orch/new/<id>/new.json:

  tab      a herdr tab in DIR (labelled with the folder's name); its root pane is the new
           orchestrator's pane, printed as soon as it exists
  log      a new lineage for that pane: the decision log starts with the goal and the owner's
           rules as user entries (D1, D2, ...), sealed like any log
  brief    ~/.claude/orch/new/<id>/brief.md: goal, rules, run, and with --plan-first the
           instruction to propose a roadmap and wait for the owner's approval
  start    `HANDOFF_LINEAGE=<lineage> claude-acc N --session-id S [--model M] [--effort E]`
           typed into the pane. A new agent at a dialog (folder trust on that account) pushes
           `respawn-dialog` and waits up to 15 minutes for an answer in the pane, as a takeover
  ready    the agent is up
  run      `orch run` registered for the pane: goal, until, cwd DIR, no workers yet
  prompted "Read <brief> and do it." sent and the agent working on it (checked like a takeover
           prompt: Enter, then the prompt once more)

A failed step is `failed`, with the error, and alerts the owner. A second `orch new` with the
same --id starts nothing: it prints the first one's state. --dry-run prints the steps and
changes nothing.
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

NEW = Path.home() / ".claude" / "orch" / "new"  # <id>/brief.md and <id>/new.json
READY_WAIT = 90  # seconds for the new agent to come up, as a takeover's
BLOCKED_POLLS = 3  # one-second polls at a dialog before it counts as waiting there

PLAN_FIRST = """\
## Plan first

Before any other work, read the folder (README, CLAUDE.md or AGENTS.md, handoff docs) and plan
the run. Then:

1. Call the `report` tool (role orchestrator, state `waiting_on_owner`) with the plan as
   `roadmap` items (status `next` or `later`) and a one-line summary.
2. Ask the owner with `ask_owner`: the question "Plan ready: <the plan in one line>", with the
   options "Approve", "Edit" and "Cancel".
3. End your turn and wait. Start no workers and change no files until the answer arrives as a
   prompt. Approve: carry out the plan. Edit: the answer says what to change; revise the plan
   and ask again. Cancel: run `orch run stop` and stop.
"""


def hook():
    """The context-handoff hook as a module, for its lineage layout and dialog wait."""
    spec = importlib.util.spec_from_file_location("context_handoff", orch.HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def one_line(text):
    return " ".join(text.split())


def log_entries(goal, rules, at):
    """The new decision log's first entries: the goal, then each owner rule."""
    when = orch.clock(at, True)
    out = [f'- **D1** {when}, user (orch new, the goal of this run): "{one_line(goal)}"']
    out += [f'- **D{n}** {when}, user (orch new, owner rule): "{one_line(rule)}"'
            for n, rule in enumerate(rules, 2)]
    return "\n".join(out) + "\n"


def brief_text(st):
    rules = "".join(f"- {r}\n" for r in st["rules"]) or "- none given\n"
    return (
        f"# Orchestrator brief (orch new {st['id']}, {orch.clock(st['time'], True)})\n\n"
        f"You are the orchestrator of a new orch run on {orch.machine_name()}, in herdr pane "
        f"{st['pane']}, folder {st['cwd']}, started by the owner with `orch new`.\n\n"
        f"## Goal (the owner's words)\n\n{st['goal']}\n\n"
        f"## Owner rules\n\n{rules}\n"
        f"They are also D1 (the goal) to D{len(st['rules']) + 1} in your decision log "
        f"{st['lineage']}/decisions.md, which you keep (append-only, rules at its top).\n\n"
        f"## Run\n\nRegistered with `orch run` for this pane until "
        f"{orch.clock(st['until'], True)}, with no workers yet: add them with "
        "`orch run set --add PANE=label` as you start them. `orch status` shows the run.\n\n"
        + (PLAN_FIRST if st["plan_first"] else "## Start\n\nStart on the goal.\n"))


class New:
    def __init__(self, st, dry):
        self.st, self.dry = st, dry
        self.folder = NEW / st["id"]

    def save(self):
        orch.save(self.folder / "new.json", self.st)

    def step(self, name, text, push=False):
        if self.dry:
            print(f"would {name}: {text}")
            return
        self.st.update(step=name, updated=orch.now())
        self.st["steps"].append({"step": name, "time": orch.now(), "text": text})
        self.save()
        print(f"{name}: {text}", flush=True)
        orch.emit("new", f"New orchestrator, {name}: {Path(self.st['cwd']).name}", text,
                  self.st.get("pane"), push=push, id=self.st["id"], step=name)

    def fail(self, text):
        self.st["error"] = text
        self.step("failed", text, push=True)
        return 1


def main(args):
    at = orch.now()
    cwd = Path(os.path.expanduser(args.cwd)).resolve()
    if not cwd.is_dir():
        orch.die(f"--cwd {args.cwd}: no such folder")
    goal = (args.goal or "").strip()
    if not goal:
        orch.die("--goal is empty: what the run is for, in the owner's words")
    until = orch.parse_until(args.until, at)
    account = (orch.account_for_new(orch.load(orch.ROOT / "accounts.json"))
               if args.account in (None, "auto") else args.account)
    orch.check_account(account)
    nid = args.id or uuid.uuid4().hex[:12]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", nid):
        orch.die(f"--id {nid!r}: letters, digits, - and _ only")
    st = {"id": nid, "time": at, "cwd": str(cwd), "goal": goal, "until": until,
          "account": account, "model": args.model, "effort": args.effort,
          "plan_first": bool(args.plan_first),
          "rules": [r.strip() for r in args.rules or [] if r.strip()],
          "session": str(uuid.uuid4()), "tab": None, "pane": None, "lineage": None,
          "brief": str(NEW / nid / "brief.md"), "step": "accepted", "steps": [], "error": None}
    new = New(st, args.dry_run)
    if not args.dry_run:
        try:
            new.folder.mkdir(parents=True)
        except FileExistsError:  # the same id again (a retried request): report, start nothing
            old = orch.load(new.folder / "new.json")
            print(json.dumps(old, indent=1) if args.json else
                  f"{nid} exists: pane {old.get('pane')}, step {old.get('step')}"
                  + (f" ({old['error']})" if old.get("error") else ""))
            return 1 if old.get("step") == "failed" else 0
        new.save()
    code = start(new)
    if args.json and not args.dry_run:
        print(json.dumps(new.st, indent=1))
    return code


def start(new):
    st, dry = new.st, new.dry
    try:
        if dry:
            pane = st["pane"] = "<pane>"
        else:
            out = orch.herdr("tab", "create", "--cwd", st["cwd"], "--label", Path(st["cwd"]).name,
                             "--no-focus")
            result = json.loads(out.stdout or "{}").get("result") or {}
            pane = (result.get("root_pane") or {}).get("pane_id")
            if out.returncode != 0 or not pane:
                return new.fail(f"herdr tab create failed: {(out.stderr or out.stdout).strip()}")
            st.update(tab=(result.get("tab") or {}).get("tab_id"), pane=pane)
        new.step("tab", f"pane {pane} in a new tab in {st['cwd']}")

        h = hook()
        base = orch.ROOT / st["cwd"].strip("/").replace("/", "-").replace(":", "-")
        if dry:
            st["lineage"] = str(base / f"{pane}-<time>-{st['session'][:8]}")
        else:
            lineage = h.new_lineage(base, st["session"], pane)
            with open(lineage / "decisions.md", "a") as f:
                f.write(log_entries(st["goal"], st["rules"], st["time"]))
            h.seal(lineage)
            st["lineage"] = str(lineage)
        new.step("log", f"decision log {st['lineage']}/decisions.md: the goal and "
                        f"{len(st['rules'])} owner rule(s)")

        if dry:
            print(brief_text(st))
        else:
            Path(st["brief"]).write_text(brief_text(st))
        new.step("brief", st["brief"] + (" (plan first)" if st["plan_first"] else ""))

        acc = shutil.which("claude-acc") or str(Path.home() / ".local" / "bin" / "claude-acc")
        args = ["--session-id", st["session"]]
        args += ["--model", st["model"]] if st["model"] else []
        args += ["--effort", st["effort"]] if st["effort"] else []
        command = (f"HANDOFF_LINEAGE={shlex.quote(st['lineage'])} "
                   + " ".join(shlex.quote(a) for a in [acc, st["account"], *args]))
        if not dry:
            orch.herdr("pane", "run", pane, command)
        new.step("start", command)
        if not dry and not ready(new, pane, h.DIALOG_WAIT):
            return new.fail(f"no ready agent in {pane} after running {command}")
        new.step("ready", f"Claude is up in {pane} on account {st['account']}")

        run = orch.new_run(pane, st["until"], st["goal"], {}, st["cwd"], orch.now())
        if not dry:
            orch.save_run(pane, run, "start")
        new.step("run", f"run {pane} until {orch.clock(st['until'], True)}")

        text = f"Read {st['brief']} and do it."
        if not dry and not deliver(pane, text):
            return new.fail(f"the agent in {pane} did not start on {text!r} (sent twice)")
        new.step("prompted", f"{pane} is working on the brief")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return new.fail(repr(e))


def ready(new, pane, dialog_wait):
    """Wait for the new agent: up to READY_WAIT seconds, or dialog_wait once it sits at a
    dialog (pushed as `respawn-dialog`, answered in the pane)."""
    blocked, deadline = 0, orch.now() + READY_WAIT
    while orch.now() < deadline:
        state = ((orch.agents() or {}).get(pane) or {}).get("agent_status")
        if state in ("idle", "done"):
            return True
        blocked = blocked + 1 if state == "blocked" else 0
        if blocked == BLOCKED_POLLS:
            text = (f"{pane}: the new orchestrator for {new.st['cwd']} is at a dialog (folder "
                    f"trust on account {new.st['account']}?); answer it in the pane, then it "
                    f"gets its brief (waiting up to {dialog_wait // 60} min)")
            new.step("dialog", text)
            orch.emit("respawn-dialog", "New orchestrator waits at a dialog", text, pane,
                      push=True, id=new.st["id"])
            deadline = orch.now() + dialog_wait
        orch.sleep(1)
    return False


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
