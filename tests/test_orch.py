"""Offline tests for hooks/orch.py: a fake clock and a fake herdr fleet, no real waiting."""
# ruff: noqa: DTZ001  (orch reads and prints the machine's local time, like its users)
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ORCH = Path(__file__).resolve().parent.parent / "hooks" / "orch.py"
BOSS, W1, W2 = "w1:p0", "w1:p1", "w1:p2"
T0 = datetime(2026, 11, 5, 10, 0).timestamp()


class World:
    """The fake herdr fleet, clock and outputs the orch module sees."""

    def __init__(self, orch, root):
        self.orch, self.root = orch, root
        self.t = T0
        self.agents = {}
        self.prompts = []
        self.herdr_calls = []
        self.at = []  # (time, callback): fleet changes while a wait sleeps

    def agent(self, pane, status, session=None, seq=1, change=1):
        self.agents[pane] = {"pane_id": pane, "agent_status": status, "completion_seq": seq,
                             "state_change_seq": change,
                             "agent_session": {"value": session or f"s-{pane}"}}

    def turn_end(self, pane, ago=0, bg=(), last="done", session=None):
        path = self.root / "agents" / f"{pane.replace(':', '-')}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pane": pane, "session": session or f"s-{pane}", "event": "Stop",
            "time": self.t - ago, "last": last,
            "background_tasks": [{"type": "bash", "id": "b", "what": w} for w in bg]}))

    def sleep(self, seconds):
        self.t += seconds
        for when, callback in list(self.at):
            if self.t >= when:
                self.at.remove((when, callback))
                callback()

    def run(self, *argv):
        return self.orch.main(["--orch", BOSS, *argv])

    def events(self):
        path = self.root / "events.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def world(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("orch", ORCH)
    orch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orch)
    w = World(orch, tmp_path / "handoffs")
    monkeypatch.setattr(orch, "ROOT", w.root)
    monkeypatch.setattr(orch, "now", lambda: w.t)
    monkeypatch.setattr(orch, "sleep", w.sleep)
    monkeypatch.setattr(orch, "agents", lambda: dict(w.agents))
    monkeypatch.setattr(orch, "prompt", lambda pane, text: w.prompts.append((pane, text)) or True)
    monkeypatch.setattr(orch, "herdr", lambda *a, **k: w.herdr_calls.append(a))
    monkeypatch.delenv("ORCH_NOTIFY_CMD", raising=False)
    w.agent(BOSS, "idle", seq=1)
    return w


def start_run(world, until="+8h"):
    world.run("run", "start", "--until", until, "--goal", "fix the board",
              "--workers", f"{W1}=builder,{W2}=tester")


def push_log(tmp_path, monkeypatch):
    pushed = tmp_path / "pushed.jsonl"
    script = tmp_path / "notify.py"
    script.write_text("import json, os, sys\n"
                      f"open({str(pushed)!r}, 'a').write(json.dumps("
                      "[os.environ.get('ORCH_EVENT_KIND')] + sys.argv[1:]) + '\\n')\n")
    monkeypatch.setenv("ORCH_NOTIFY_CMD", f"{sys.executable} {script}")
    return lambda: ([json.loads(x) for x in pushed.read_text().splitlines()]
                    if pushed.exists() else [])


# --- run registry ---------------------------------------------------------------------------

def test_run_start_set_stop(world, capsys):
    start_run(world)
    run = json.loads((world.root / "runs" / "w1-p0.json").read_text())
    assert run["until"] == T0 + 8 * 3600 and run["goal"] == "fix the board"
    assert run["workers"] == {W1: {"label": "builder"}, W2: {"label": "tester"}}

    world.run("run", "set", "--add", "w1:p3=docs", "--remove", W2, "--until", "18:30")
    run = json.loads((world.root / "runs" / "w1-p0.json").read_text())
    assert set(run["workers"]) == {W1, "w1:p3"}
    assert run["until"] == datetime(2026, 11, 5, 18, 30).timestamp()

    world.run("run", "stop")
    assert json.loads((world.root / "runs" / "w1-p0.json").read_text())["ended"] == T0
    assert [e["kind"] for e in world.events()] == ["run-start", "run-set", "run-stop"]


@pytest.mark.parametrize("text, expected", [
    ("09:00", datetime(2026, 11, 6, 9, 0)),  # already past today: tomorrow
    ("11:30", datetime(2026, 11, 5, 11, 30)),
    ("2026-11-07 08:00", datetime(2026, 11, 7, 8, 0)),
    ("+90m", datetime(2026, 11, 5, 11, 30)),
])
def test_parse_until(world, text, expected):
    assert world.orch.parse_until(text, T0) == expected.timestamp()


# --- wait -------------------------------------------------------------------------------------

def test_wait_returns_when_a_worker_finishes(world, capsys):
    start_run(world)
    world.agent(W1, "working")
    world.agent(W2, "working")

    def finish():
        world.agent(W1, "idle", seq=2)
        world.turn_end(W1, last="Task 4 is done: 3 commits, not pushed.")
    world.at.append((T0 + 300, finish))

    assert world.run("wait") == 0
    out = capsys.readouterr().out
    assert world.t < T0 + 400  # it returned on the event, not the heartbeat
    assert "EVENT idle w1:p1 builder: idle since 10:05" in out
    assert "Task 4 is done" in out
    assert "w1:p2" in out and "working" in out


def test_a_worker_waiting_on_its_own_job_is_not_finished(world, capsys):
    start_run(world)
    world.agent(W1, "idle")
    world.turn_end(W1, bg=["pytest under heavy.lock"])
    world.agent(W2, "working")

    world.run("wait", "--heartbeat", "3600", "--long-wait", "1800")

    out = capsys.readouterr().out
    assert world.t >= T0 + 1800
    assert "EVENT long-wait w1:p1 builder: idle 30m on its own job (pytest under heavy.lock)" in out


def test_an_idle_worker_is_reported_once_and_parked_ones_not_at_all(world, capsys):
    start_run(world)
    world.agent(W1, "idle", seq=5)
    world.turn_end(W1, ago=120)
    world.agent(W2, "idle", seq=3)
    world.turn_end(W2, ago=60)
    world.run("park", W2, "--reason", "waits for the deploy approval")
    capsys.readouterr()

    world.run("wait")
    out = capsys.readouterr().out
    assert world.t == T0 and "EVENT idle w1:p1" in out and "EVENT idle w1:p2" not in out
    assert "parked" in out and "waits for the deploy approval" in out

    world.run("wait", "--heartbeat", "600")  # nothing new: runs to the heartbeat
    out = capsys.readouterr().out
    assert world.t >= T0 + 600 and "heartbeat, nothing new" in out and "EVENT" not in out


def test_a_park_ends_with_the_workers_next_task(world, capsys):
    start_run(world)
    world.agent(W1, "working")
    world.agent(W2, "idle", seq=3)
    world.turn_end(W2)
    world.run("park", W2, "--reason", "nothing ready")
    world.agent(W2, "idle", seq=4)  # it was given a task and finished it
    world.turn_end(W2, last="second task done")
    capsys.readouterr()

    world.run("wait")
    assert "EVENT idle w1:p2 tester" in capsys.readouterr().out


def test_blocked_and_missing_workers(world, capsys):
    start_run(world)
    world.agent(W1, "blocked")
    world.run("wait")
    assert "EVENT blocked w1:p1" in capsys.readouterr().out

    world.agents.pop(W1)
    world.agent(W2, "working")
    world.run("wait", "--missing-grace", "180")
    out = capsys.readouterr().out
    assert world.t >= T0 + 180 and "EVENT missing w1:p1" in out


def test_a_status_from_an_earlier_session_is_ignored(world, capsys):
    start_run(world)
    world.agent(W1, "idle", session="new")
    world.turn_end(W1, bg=["old job"], session="old")  # before a /clear
    world.agent(W2, "working")

    world.run("wait")
    out = capsys.readouterr().out
    assert world.t == T0 and "EVENT idle w1:p1 builder: idle (since: not recorded)" in out


def test_wait_check_command(world, capsys, tmp_path):
    flag = tmp_path / "ci-done"
    world.at.append((T0 + 200, flag.touch))

    world.orch.main(["--orch", BOSS, "wait", "--check", f"test -e {flag} && echo green",
                     "--check-every", "60"])
    out = capsys.readouterr().out
    assert "EVENT check passed" in out and "green" in out and world.t <= T0 + 300


# --- watchdog -----------------------------------------------------------------------------------

def test_watchdog_nudges_an_idle_orchestrator_about_an_idle_worker(world):
    start_run(world)
    world.turn_end(BOSS, bg=["orch wait"])
    world.agent(W1, "idle")
    world.turn_end(W1, ago=11 * 60)
    world.agent(W2, "working")

    world.run("watchdog")

    [(pane, text)] = world.prompts
    assert pane == BOSS
    assert "w1:p1 builder idle 11m, not parked" in text and "orch park" in text
    assert "fix the board" in text

    world.sleep(5 * 60)
    world.run("watchdog")  # rate limited
    assert len(world.prompts) == 1


def test_watchdog_leaves_a_working_orchestrator_and_parked_workers_alone(world):
    start_run(world)
    world.agent(W1, "idle")
    world.turn_end(W1, ago=3600)
    world.agent(BOSS, "working")
    world.run("watchdog")
    assert world.prompts == []

    world.agent(BOSS, "idle")
    world.turn_end(BOSS, bg=["orch wait"])
    world.run("park", W1, "--reason", "nothing ready until the release")
    world.agent(W2, "working")
    world.run("watchdog")
    assert world.prompts == []


def test_watchdog_wakes_an_orchestrator_that_nothing_would_wake(world):
    start_run(world)
    world.agent(W1, "idle")
    world.turn_end(W1, ago=60)  # idle only a minute
    world.agent(W2, "working")
    world.turn_end(BOSS, ago=6 * 60)  # its last turn ended with no background task

    world.run("watchdog")

    [(_, text)] = world.prompts
    assert "nothing would wake you" in text


def test_watchdog_tells_the_owner_when_nudges_are_ignored(world, tmp_path, monkeypatch):
    pushed = push_log(tmp_path, monkeypatch)
    start_run(world)
    world.turn_end(BOSS, bg=["orch wait"])
    world.agent(W1, "idle")
    world.turn_end(W1, ago=20 * 60)
    world.agent(W2, "working")

    for _ in range(4):
        world.run("watchdog")
        world.sleep(16 * 60)
        world.turn_end(BOSS, bg=["orch wait"])  # it answers each nudge but assigns nothing

    assert len(world.prompts) == 4
    [alert] = pushed()
    assert alert[0] == "orch-not-acting" and "after 3 nudges" in alert[2]
    world.run("watchdog")  # backed off to hourly now
    assert len(world.prompts) == 4


def test_watchdog_alerts_when_the_orchestrator_is_gone(world, tmp_path, monkeypatch):
    pushed = push_log(tmp_path, monkeypatch)
    start_run(world)
    world.agents.pop(BOSS)
    world.agent(W1, "working")

    world.run("watchdog")
    world.sleep(6 * 60)
    world.run("watchdog")
    world.sleep(6 * 60)
    world.run("watchdog")

    assert [p[0] for p in pushed()] == ["orch-missing"]  # once
    assert world.prompts == []


def test_watchdog_ends_an_expired_run(world, tmp_path, monkeypatch):
    pushed = push_log(tmp_path, monkeypatch)
    start_run(world, until="+1h")
    world.agent(W1, "idle")
    world.turn_end(W1, ago=3600)
    world.sleep(3601)

    world.run("watchdog")
    world.run("watchdog")

    assert world.prompts == []
    assert [p[0] for p in pushed()] == ["run-ended"]
    run = json.loads((world.root / "runs" / "w1-p0.json").read_text())
    assert run["ended"] == world.t


def test_watchdog_without_runs_does_nothing(world):
    world.agent(W1, "idle")
    assert world.run("watchdog") == 0
    assert world.prompts == [] and world.events() == []


# --- owner ------------------------------------------------------------------------------------

def test_ask_pushes_the_question_and_logs_it(world, tmp_path, monkeypatch, capsys):
    pushed = push_log(tmp_path, monkeypatch)
    start_run(world)

    world.run("ask", "OK to deploy main 0892241c to prod?")

    [alert] = pushed()
    assert alert[0] == "ask" and alert[2] == "OK to deploy main 0892241c to prod?"
    run = json.loads((world.root / "runs" / "w1-p0.json").read_text())
    assert run["questions"][0]["text"] == "OK to deploy main 0892241c to prod?"
    assert "keep the rest of the fleet working" in capsys.readouterr().out


def test_status_json(world, capsys):
    start_run(world)
    world.agent(W1, "idle")
    world.turn_end(W1, last="all green")
    world.agent(W2, "working")
    capsys.readouterr()

    world.run("status", "--json")

    data = json.loads(capsys.readouterr().out)
    assert data["run"]["goal"] == "fix the board"
    assert [(v["pane"], v["state"], v["last"]) for v in data["workers"]] == [
        (W1, "idle", "all green"), (W2, "working", "")]
