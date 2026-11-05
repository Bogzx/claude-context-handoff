"""Offline tests for hooks/orch_new.py (orch new): herdr and the clock are fakes."""
import importlib.util
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
T0 = datetime(2026, 11, 5, 10, 0).timestamp()  # noqa: DTZ001 orch works in local time
PANE = "w1:p7"


def done(code=0, stdout=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr="")


class Herdr:
    """herdr as orch new sees it: `tab create` gives pane w1:p7; once a command runs there its
    agent goes through `states`, one per poll (the last one stays), and works once prompted
    `start_on` times."""

    def __init__(self):
        self.calls, self.prompts = [], []
        self.states, self.agent, self.start_on = ["idle"], None, 1

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[:2] == ("tab", "create"):
            return done(stdout=json.dumps({"result": {"tab": {"tab_id": "w1:t9"},
                                                      "root_pane": {"pane_id": PANE}}}))
        if args[:2] == ("pane", "run"):
            self.agent = "starting"
        if args[:2] == ("agent", "wait"):
            return done(0 if self.agent == "working" else 1)
        return done()

    def agents(self):
        if self.agent is None:
            return {}
        if self.agent != "working" and self.states:
            self.agent = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {PANE: {"pane_id": PANE, "agent_status": self.agent}}

    def prompt(self, pane, text):
        self.prompts.append((pane, text))
        if len(self.prompts) >= self.start_on:
            self.agent = "working"
        return True


class World:
    def __init__(self, new, home):
        self.new, self.orch, self.home = new, new.orch, home
        self.t = T0
        self.herdr = Herdr()
        self.folder = home / "Repo" / "my-app"
        self.folder.mkdir(parents=True)

    def sleep(self, seconds):
        self.t += seconds

    def run(self, *argv):
        return self.orch.main(["new", "--cwd", str(self.folder), "--goal", "Ship the beta",
                               "--until", "+8h", *argv])

    def record(self, sid="r1"):
        return json.loads((self.orch.STARTS / sid / "new.json").read_text())

    def steps(self):
        return [(e["step"], e["status"]) for e in self.events("start")]

    def events(self, kind=None):
        path = self.orch.ROOT / "events.jsonl"
        lines = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        return [e for e in lines if kind is None or e["kind"] == kind]


@pytest.fixture
def world(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("orch_new", HOOKS / "orch_new.py")
    new = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(new)
    home = tmp_path / "home"
    for d in (".claude", ".claude-2"):
        (home / d).mkdir(parents=True)
        (home / d / ".credentials.json").write_text("{}")
    w = World(new, home)
    orch = new.orch
    monkeypatch.setitem(sys.modules, "orch_new", new)  # what `orch new` imports
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ORCH_NOTIFY_CMD", raising=False)
    monkeypatch.setattr(orch, "STARTS", home / ".claude" / "orch" / "new")
    monkeypatch.setattr(orch, "ROOT", home / ".claude" / "handoffs")
    monkeypatch.setattr(orch, "now", lambda: w.t)
    monkeypatch.setattr(orch, "sleep", w.sleep)
    monkeypatch.setattr(orch, "herdr", w.herdr)
    monkeypatch.setattr(orch, "agents", w.herdr.agents)
    monkeypatch.setattr(orch, "prompt", w.herdr.prompt)
    monkeypatch.setattr(orch, "CONFIG", tmp_path / "config" / ".env")
    monkeypatch.setattr(orch, "_TAILNET_NAME", ["box1"])
    return w


def test_new_starts_an_orchestrator_with_its_brief_log_and_run(world, capsys):
    assert world.run("--id", "r1", "--account", "2", "--model", "sonnet", "--effort", "high",
                     "--name", "beta", "--worker-model", "haiku", "--max-workers", "3",
                     "--plan-first", "--rules", "No pushes to main",
                     "--rules", "Ask before\na deploy") == 0

    out = capsys.readouterr().out
    assert out.startswith("pane ok: w1:p7 in a new tab")  # the pane comes first
    tab = next(c for c in world.herdr.calls if c[:2] == ("tab", "create"))
    assert tab[tab.index("--cwd") + 1] == str(world.folder) and "--no-focus" in tab
    assert tab[tab.index("--label") + 1] == "beta"
    rec = world.record()
    assert rec["pane"] == PANE and rec["by"] == "cli" and rec["error"] is None
    assert [(s["step"], s["status"]) for s in rec["steps"]] == [
        ("pane", "ok"), ("claude", "ok"), ("run", "ok"), ("read", "doing")]
    assert world.steps() == [("pane", "ok"), ("claude", "doing"), ("claude", "ok"),
                             ("run", "ok"), ("read", "doing")]
    first = world.events("start")[0]
    assert first["action"] == "r1" and first["pane"] == PANE and first["by"] == "cli"
    assert first["folder"] == str(world.folder) and first["goal"] == "Ship the beta"
    # the decision log: the hook's header, then the goal and the rules as the owner's entries
    lineage = Path(rec["lineage"])
    assert lineage.parent == world.orch.ROOT / str(world.folder).strip("/").replace("/", "-")
    assert lineage.name.startswith("w1-p7-") and lineage.name.endswith(rec["session"][:8])
    log = (lineage / "decisions.md").read_text()
    assert log.startswith("# Decision log") and "## Generation 1\n" in log
    assert (lineage / ".decisions.sealed").read_text() == log
    entries = world.orch.parse_decisions(log)
    assert [(e["id"], e["who"], e["quote"]) for e in entries] == [
        ("D1", "user", "Ship the beta"), ("D2", "user", "No pushes to main"),
        ("D3", "user", "Ask before a deploy")]
    # Claude: claude-acc on the account, with the lineage, session, model and effort; named
    [(_, _, pane, command)] = [c for c in world.herdr.calls if c[:2] == ("pane", "run")]
    assert pane == PANE and command.startswith(f"HANDOFF_LINEAGE={lineage} ")
    assert command.endswith(f"claude-acc 2 --session-id {rec['session']} --model sonnet "
                            "--effort high")
    assert ("agent", "rename", PANE, "beta") in world.herdr.calls
    # the brief, then the prompt that points at it
    brief = Path(rec["brief"]).read_text()
    assert "Ship the beta" in brief and "- Ask before\na deploy" in brief
    assert "At most 3 at a time. Start them on model haiku." in brief
    assert 'orch ask --kind plan --option "Approve and start workers"' in brief
    assert world.herdr.prompts == [(PANE, f"Read {rec['brief']} and do it.")]
    run = world.orch.load_run(PANE)
    assert run["cwd"] == str(world.folder) and run["goal"] == "Ship the beta"
    assert run["until"] == world.t + 8 * 3600 and run["workers"] == {}
    assert run["settings"] == {"folder": str(world.folder), "worktree": False,
                               "model": "sonnet", "effort": "high", "worker_model": "haiku",
                               "max_workers": 3, "plan_first": True, "account": "2"}
    assert [e["kind"] for e in world.events()].count("run-start") == 1


def test_the_plan_question_moves_the_start_on_and_a_revision_replaces_it(world, capsys):
    world.run("--id", "r1", "--plan-first")
    approve = ["--option", "Approve and start workers", "--option", "Cancel the run"]
    world.orch.main(["--orch", PANE, "ask", "--kind", "plan", *approve, "Plan ready: 3 items"])
    first = world.orch.load_run(PANE)["questions"][0]
    assert first["kind"] == "plan"
    assert world.steps()[-2:] == [("read", "ok"), ("plan", "doing")]
    assert world.events("ask")[-1]["question_kind"] == "plan"
    world.orch.main(["--orch", PANE, "ask", "--kind", "plan", *approve, "Plan ready: 4 items"])
    first, second = world.orch.load_run(PANE)["questions"]
    assert first["answer"] == f"(replaced by {second['id']})" and second["answered"] is None

    world.orch.answer(second["id"], "Approve and start workers")

    assert world.steps()[-1] == ("plan", "ok")
    assert world.orch.start_done(world.record()) is True


def test_without_plan_first_the_start_ends_with_the_brief(world):
    assert world.run("--id", "r1") == 0
    assert world.steps()[-1] == ("brief", "ok")
    assert world.orch.start_done(world.record()) is True
    assert "## Start\n\nStart on the goal." in Path(world.record()["brief"]).read_text()


def test_the_same_id_again_starts_nothing(world, capsys):
    assert world.run("--id", "r1") == 0
    calls = len(world.herdr.calls)
    capsys.readouterr()

    assert world.run("--id", "r1", "--json") == 0

    assert len(world.herdr.calls) == calls
    assert json.loads(capsys.readouterr().out)["pane"] == PANE


def test_a_dialog_is_the_trust_step_and_is_waited_for(world):
    world.herdr.states = ["starting"] + ["blocked"] * 400 + ["idle"]  # answered after ~7 min

    assert world.run("--id", "r1") == 0

    assert world.steps()[1:4] == [("claude", "doing"), ("trust", "doing"), ("trust", "ok")]
    [dialog] = world.events("respawn-dialog")  # started from the CLI: alert the owner
    assert dialog["pane"] == PANE and dialog["action"] == "r1"
    assert "folder trust" in dialog["body"]
    assert world.herdr.prompts  # the brief went out once the dialog was answered


def test_a_start_from_the_phone_runs_from_its_record_without_the_dialog_alert(world):
    rec = world.new.check({"folder": str(world.folder), "goal": "Ship", "until": "+2h",
                           "id": "a1", "by": "phone"})
    assert world.new.accept(rec) and not world.new.accept(dict(rec))  # the id is taken
    world.herdr.states = ["blocked"] * 5 + ["idle"]

    assert world.new.start(world.orch.load(world.orch.STARTS / "a1" / "new.json")) == 0

    assert ("trust", "ok") in world.steps() and world.events("respawn-dialog") == []
    assert world.events("start")[0]["by"] == "phone"


def test_nobody_answering_the_dialog_fails_the_trust_step_and_registers_no_run(world):
    world.herdr.states = ["blocked"]

    assert world.run("--id", "r1") == 1

    rec = world.record()
    assert rec["status"] == "failed" and rec["step"] == "trust"
    assert "nobody answered the dialog" in rec["error"]
    assert world.t >= T0 + 15 * 60  # it waited the dialog's 15 minutes
    assert world.orch.load_run(PANE) == {} and world.herdr.prompts == []
    failed = world.events("start")[-1]
    assert (failed["step"], failed["status"]) == ("trust", "failed") and failed["error"]


def test_an_agent_that_never_comes_up_fails_the_claude_step(world):
    world.herdr.states = ["starting"]

    assert world.run("--id", "r1") == 1

    assert world.steps()[-1] == ("claude", "failed")
    assert world.t >= T0 + 90 and world.orch.load_run(PANE) == {}


def test_the_prompt_is_checked_then_sent_again(world):
    world.herdr.start_on = 2  # the first prompt sits in the input box

    assert world.run("--id", "r1") == 0

    assert ("agent", "send-keys", PANE, "enter") in world.herdr.calls
    assert len(world.herdr.prompts) == 2


def test_a_worktree_start_works_in_a_new_branch_next_to_the_folder(world, tmp_path):
    repo = world.folder
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)

    assert world.run("--id", "r1", "--worktree", "--name", "beta") == 0

    tree = repo.parent / "my-app-beta"
    assert world.record()["cwd"] == str(tree) and (tree / ".git").is_file()
    branch = subprocess.run(["git", "-C", str(tree), "branch", "--show-current"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert branch == "orch/beta"
    tab = next(c for c in world.herdr.calls if c[:2] == ("tab", "create"))
    assert tab[tab.index("--cwd") + 1] == str(tree)
    with pytest.raises(SystemExit):  # the same worktree again
        world.run("--id", "r2", "--worktree", "--name", "beta")


def test_dry_run_prints_every_step_and_changes_nothing(world, capsys):
    assert world.run("--dry-run", "--plan-first", "--rules", "No pushes to main") == 0

    out = capsys.readouterr().out
    for step in ("pane ok", "claude doing", "claude ok", "run ok", "read doing"):
        assert f"would {step}: " in out
    assert "claude-acc 1 --session-id" in out and "No pushes to main" in out
    assert world.herdr.calls == [] and world.events() == []
    assert not world.orch.STARTS.exists() and not world.orch.ROOT.exists()


@pytest.mark.parametrize("argv, error", [
    (["--cwd", "/no/such/folder"], "is not a folder"),
    (["--account", "3"], "not logged in"),
    (["--goal", "  "], "the goal is empty"),
    (["--id", "../x"], "letters, digits"),
    (["--model", "x; rm"], "letters, digits and"),
    (["--worktree"], "not a git checkout"),
])
def test_refused_before_anything_happens(world, capsys, argv, error):
    with pytest.raises(SystemExit):
        world.orch.main(["new", "--cwd", str(world.folder), "--goal", "Ship", "--until", "+1h",
                         *argv])
    assert error in capsys.readouterr().err
    assert world.herdr.calls == [] and not world.orch.STARTS.exists()


def env(world, **values):
    world.orch.CONFIG.parent.mkdir(parents=True, exist_ok=True)
    world.orch.CONFIG.write_text("".join(f"{k}={v}\n" for k, v in values.items()))


def test_a_phone_start_stays_inside_the_start_roots(world):
    def check(folder, **extra):
        return world.new.check({"folder": str(folder), "goal": "x", "until": "+1h", **extra},
                               strict=True)
    live = world.home / "live"
    live.mkdir()
    with pytest.raises(SystemExit, match="2"):
        check(world.folder)  # no roots set: starts from the app are off
    env(world, ORCH_START_ROOTS="~/Repo,~/live", ORCH_START_EXCLUDE="~/live")
    assert check(world.folder)["cwd"] == str(world.folder)
    for folder, text in ((live, "excluded from starts"), (world.home, "outside the start")):
        with pytest.raises(world.orch.Refused) as e:
            check(folder)
        assert text in e.value.text
    world.orch.save_run(PANE, world.orch.new_run(PANE, world.t + 3600, "g", {},
                                                 str(world.folder), world.t), "start")
    with pytest.raises(world.orch.Refused) as e:
        check(world.folder)
    assert f"{PANE} works in ~/Repo/my-app already" in e.value.text


TRUST_SCREEN = """\
❯ Yes, a shell prompt that happens to start like an answer
 Accessing workspace:

 /home/me/Repo/my-app

 Quick safety check: Is this a project you created or one you trust? (Like your own code, a
 well-known open source project, or work from your team).

 {a} No, exit
 {b} Yes, I trust this folder

 Enter to confirm · Esc to cancel"""  # Claude Code 2.x, cursor on "No, exit" at first
NUMBERED = """\
 Do you trust the files in this folder?

 ❯ 1. Yes, proceed
   2. No, exit

 Enter to confirm · Esc to exit"""  # an older Claude Code


@pytest.mark.parametrize(("cursor", "choice", "keys"), [
    (0, "allow", ["down", "enter"]), (0, "deny", ["enter"]),
    (1, "allow", ["enter"]), (1, "deny", ["up", "enter"])])
def test_trust_keys_move_from_the_cursor_to_yes_or_no(world, cursor, choice, keys):
    screen = TRUST_SCREEN.format(a="❯" if cursor == 0 else " ", b="❯" if cursor == 1 else " ")
    assert world.new.trust_keys(screen.split("\n"), choice) == keys
    assert world.new.trust_keys(NUMBERED.split("\n"), choice) == (
        ["enter"] if choice == "allow" else ["down", "enter"])


@pytest.mark.parametrize("screen", [
    "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. No",  # not trust
    TRUST_SCREEN.format(a=" ", b=" "),  # no cursor
    "Do you trust this folder?\n❯ 1. Yes, proceed\n  2. Yes, and remember",  # no No
    "", None])
def test_trust_keys_refuse_any_other_screen(world, screen):
    assert world.new.trust_keys(screen.split("\n") if screen else screen, "allow") is None


def test_a_cancel_stops_a_start_waiting_at_the_trust_question(world, monkeypatch):
    world.herdr.states = ["blocked"]
    polls = []

    def sleep(seconds):  # the owner cancels a minute into the wait
        polls.append(seconds)
        world.t += seconds
        if len(polls) == 60:
            world.orch.save(world.orch.STARTS / "r1" / "cancelled.json",
                            {"time": world.t, "by": "phone"})

    monkeypatch.setattr(world.orch, "sleep", sleep)
    assert world.run("--id", "r1") == 1
    assert len(polls) == 60  # it stopped at the next poll, not after 15 minutes
    assert world.steps()[-1] == ("trust", "doing")  # no failed step: the cancel says it all
    assert world.orch.load_run(PANE) == {} and world.herdr.prompts == []
