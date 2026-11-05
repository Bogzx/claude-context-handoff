"""Offline tests for hooks/orch_new.py (orch new): herdr and the clock are fakes."""
import importlib.util
import json
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
    monkeypatch.setattr(new, "NEW", home / ".claude" / "orch" / "new")
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
                     "--plan-first", "--rules", "No pushes to main",
                     "--rules", "Ask before\na deploy") == 0

    out = capsys.readouterr().out
    assert "tab: pane w1:p7" in out.splitlines()[0]  # the pane comes first
    tab = next(c for c in world.herdr.calls if c[:2] == ("tab", "create"))
    assert tab[tab.index("--cwd") + 1] == str(world.folder) and "--no-focus" in tab
    st = json.loads((world.new.NEW / "r1" / "new.json").read_text())
    assert st["pane"] == PANE and st["step"] == "prompted" and st["error"] is None
    assert [s["step"] for s in st["steps"]] == ["tab", "log", "brief", "start", "ready", "run",
                                                "prompted"]
    # the decision log: the hook's header, then the goal and the rules as the owner's entries
    lineage = Path(st["lineage"])
    assert lineage.parent == world.orch.ROOT / str(world.folder).strip("/").replace("/", "-")
    assert lineage.name.startswith("w1-p7-") and lineage.name.endswith(st["session"][:8])
    log = (lineage / "decisions.md").read_text()
    assert log.startswith("# Decision log") and "## Generation 1\n" in log
    assert (lineage / ".decisions.sealed").read_text() == log
    entries = world.orch.parse_decisions(log)
    assert [(e["id"], e["who"], e["quote"]) for e in entries] == [
        ("D1", "user", "Ship the beta"), ("D2", "user", "No pushes to main"),
        ("D3", "user", "Ask before a deploy")]
    # Claude: claude-acc on the account, with the lineage, session, model and effort
    [(_, _, pane, command)] = [c for c in world.herdr.calls if c[:2] == ("pane", "run")]
    assert pane == PANE and command.startswith(f"HANDOFF_LINEAGE={lineage} ")
    assert command.endswith(f"claude-acc 2 --session-id {st['session']} --model sonnet "
                            "--effort high")
    # the brief, then the prompt that points at it
    brief = Path(st["brief"]).read_text()
    assert "Ship the beta" in brief and "- Ask before\na deploy" in brief
    assert '"Approve", "Edit" and "Cancel"' in brief and "ask_owner" in brief
    assert world.herdr.prompts == [(PANE, f"Read {st['brief']} and do it.")]
    run = world.orch.load_run(PANE)
    assert run["cwd"] == str(world.folder) and run["goal"] == "Ship the beta"
    assert run["until"] == world.t + 8 * 3600 and run["workers"] == {}
    steps = [e["step"] for e in world.events("new")]
    assert steps == ["tab", "log", "brief", "start", "ready", "run", "prompted"]
    assert all(e["id"] == "r1" for e in world.events("new"))
    assert [e["kind"] for e in world.events()].count("run-start") == 1


def test_the_same_id_again_starts_nothing(world, capsys):
    assert world.run("--id", "r1") == 0
    calls = len(world.herdr.calls)
    capsys.readouterr()

    assert world.run("--id", "r1", "--json") == 0

    assert len(world.herdr.calls) == calls
    assert json.loads(capsys.readouterr().out)["pane"] == PANE


def test_a_dialog_is_pushed_and_waited_for(world):
    world.herdr.states = ["starting"] + ["blocked"] * 400 + ["idle"]  # answered after ~7 min

    assert world.run("--id", "r1") == 0

    [dialog] = world.events("respawn-dialog")
    assert dialog["pane"] == PANE and dialog["id"] == "r1" and "folder trust" in dialog["body"]
    steps = [e["step"] for e in world.events("new")]
    assert steps[steps.index("start") + 1:steps.index("start") + 3] == ["dialog", "ready"]
    assert world.herdr.prompts  # the brief went out once the dialog was answered


def test_an_agent_that_never_comes_up_fails_loudly_and_registers_no_run(world, capsys):
    world.herdr.states = ["blocked"]  # nobody answers the dialog

    assert world.run("--id", "r1") == 1

    st = json.loads((world.new.NEW / "r1" / "new.json").read_text())
    assert st["step"] == "failed" and "no ready agent in w1:p7" in st["error"]
    assert world.t >= T0 + 15 * 60  # it waited the dialog's 15 minutes
    assert world.orch.load_run(PANE) == {} and world.herdr.prompts == []
    assert world.events("new")[-1]["step"] == "failed"


def test_the_prompt_is_checked_then_sent_again(world):
    world.herdr.start_on = 2  # the first prompt sits in the input box

    assert world.run("--id", "r1") == 0

    assert ("agent", "send-keys", PANE, "enter") in world.herdr.calls
    assert len(world.herdr.prompts) == 2


def test_dry_run_prints_every_step_and_changes_nothing(world, capsys):
    assert world.run("--dry-run", "--plan-first", "--rules", "No pushes to main") == 0

    out = capsys.readouterr().out
    for step in ("tab", "log", "brief", "start", "ready", "run", "prompted"):
        assert f"would {step}: " in out
    assert "claude-acc 1 --session-id" in out and "No pushes to main" in out
    assert world.herdr.calls == [] and world.events() == []
    assert not world.new.NEW.exists() and not world.orch.ROOT.exists()


@pytest.mark.parametrize("argv, error", [
    (["--cwd", "/no/such/folder"], "no such folder"),
    (["--account", "3"], "not logged in"),
    (["--goal", "  "], "--goal is empty"),
    (["--id", "../x"], "letters, digits"),
])
def test_refused_before_anything_happens(world, capsys, argv, error):
    with pytest.raises(SystemExit):
        world.orch.main(["new", "--cwd", str(world.folder), "--goal", "Ship", "--until", "+1h",
                         *argv])
    assert error in capsys.readouterr().err
    assert world.herdr.calls == [] and not world.new.NEW.exists()
