"""Offline tests for hooks/orch.py: a fake clock and a fake herdr fleet, no real waiting."""
# ruff: noqa: DTZ001  (orch reads and prints the machine's local time, like its users)
import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

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
    monkeypatch.setattr(orch, "CONFIG", tmp_path / "config" / ".env")
    monkeypatch.setattr(orch, "_TAILNET_NAME", ["box1"])  # as if `tailscale status` said so
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


def test_watchdog_leaves_an_orchestrator_alone_while_its_plan_waits_for_the_owner(world):
    world.run("run", "start", "--until", "+8h", "--goal", "new run")  # no workers yet
    world.turn_end(BOSS, ago=2 * 3600)  # idle two hours: past ORCH_SILENT
    world.run("ask", "--kind", "plan", "--option", "Approve and start workers", "Plan ready")

    world.run("watchdog")
    assert world.prompts == []

    [q] = json.loads((world.root / "runs" / "w1-p0.json").read_text())["questions"]
    world.run("answer", q["id"], "Approve and start workers")
    world.prompts.clear()
    world.run("watchdog")  # approved: idle with no workers is worth a nudge again
    assert [p for p, _ in world.prompts] == [BOSS]


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


# --- questions with options, answers ----------------------------------------------------------

def event_log(tmp_path, monkeypatch):
    """A notify command that records ORCH_EVENT (the whole event) for each push."""
    pushed = tmp_path / "events-pushed.jsonl"
    script = tmp_path / "notify-event.py"
    script.write_text("import os\n"
                      f"open({str(pushed)!r}, 'a').write(os.environ['ORCH_EVENT'] + '\\n')\n")
    monkeypatch.setenv("ORCH_NOTIFY_CMD", f"{sys.executable} {script}")
    return lambda: ([json.loads(x) for x in pushed.read_text().splitlines()]
                    if pushed.exists() else [])


def test_ask_with_options_gives_the_push_its_buttons(world, tmp_path, monkeypatch, capsys):
    pushed = event_log(tmp_path, monkeypatch)
    start_run(world)

    world.run("ask", "Deploy 2.14 to staging?", "--option", "Deploy", "--danger", "deploy",
              "--option", "Not yet")

    [q] = json.loads((world.root / "runs" / "w1-p0.json").read_text())["questions"]
    assert q["id"].startswith("q") and q["pane"] == BOSS and q["answered"] is None
    assert q["options"] == [{"label": "Deploy", "danger": "deploy"},
                            {"label": "Not yet", "danger": "none"}]
    [event] = pushed()
    assert event["kind"] == "ask" and event["id"] == q["id"]
    assert event["options"] == q["options"] and event["question"] == "Deploy 2.14 to staging?"
    assert event["body"].endswith("Options: Deploy [deploy] / Not yet")
    assert q["id"] in capsys.readouterr().out


def test_danger_needs_an_option_before_it(world):
    with pytest.raises(SystemExit):
        world.run("ask", "x?", "--danger", "prod", "--option", "Yes")


def test_answer_prompts_the_asker_once(world, capsys):
    start_run(world)
    world.run("ask", "Merge PR #4?", "--option", "Merge", "--option", "Wait")
    [q] = json.loads((world.root / "runs" / "w1-p0.json").read_text())["questions"]
    world.t += 60

    assert world.run("answer", q["id"], "Merge") == 0

    [(pane, text)] = world.prompts
    assert pane == BOSS and q["id"] in text and "Merge PR #4?" in text and text.endswith(": Merge")
    [q] = json.loads((world.root / "runs" / "w1-p0.json").read_text())["questions"]
    assert q["answer"] == "Merge" and q["answered"] == T0 + 60
    assert world.events()[-1]["kind"] == "answer" and world.events()[-1]["id"] == q["id"]
    with pytest.raises(SystemExit):  # closed: a replayed answer is refused
        world.run("answer", q["id"], "Wait")
    with pytest.raises(SystemExit):
        world.run("answer", "q00000000", "Merge")
    assert len(world.prompts) == 1


def ask_with_default(world, by="+30m"):
    start_run(world)
    world.agent(BOSS, "idle")
    world.run("ask", "Both defaults OK?", "--option", "Both OK", "--option", "Only the first",
              "--default", "Both OK", "--by", by)
    return json.loads((world.root / "runs" / "w1-p0.json").read_text())["questions"][0]


def question_now(world):
    return json.loads((world.root / "runs" / "w1-p0.json").read_text())["questions"][0]


def default_prompts(world, q):
    return [text for _, text in world.prompts if q["id"] in text]


def test_a_question_goes_to_its_default_at_its_time_and_the_asker_is_told(world, capsys):
    q = ask_with_default(world)
    assert q["default"] == "Both OK" and q["default_at"] == T0 + 1800
    [asked] = [e for e in world.events() if e["kind"] == "ask"]
    assert asked["default"] == "Both OK" and asked["default_at"] == T0 + 1800
    assert "Goes to 'Both OK' at" in asked["body"]

    world.t = T0 + 1799
    world.run("watchdog")
    assert default_prompts(world, q) == [] and not question_now(world)["answered"]

    world.t = T0 + 1800
    world.run("watchdog")

    [text] = default_prompts(world, q)
    by = world.orch.clock(T0 + 1800)
    assert text.endswith(f"went to its default: Both OK (default, no reply by {by})")
    assert "owner answered" not in text  # nobody did
    closed = question_now(world)
    assert closed["answer"] == "Both OK" and closed["answered"] == T0 + 1800
    assert closed["by_default"] is True
    [event] = [e for e in world.events() if e["kind"] == "answer"]
    assert event["id"] == q["id"] and event["by_default"] is True

    world.t += 600  # once only, and a late answer finds it closed
    world.run("watchdog")
    assert len(default_prompts(world, q)) == 1
    with pytest.raises(SystemExit):
        world.run("answer", q["id"], "Only the first")


def test_the_owner_can_answer_before_the_default(world, capsys):
    q = ask_with_default(world)
    world.t = T0 + 600

    world.run("answer", q["id"], "Only the first")
    world.t = T0 + 3600
    world.run("watchdog")

    closed = question_now(world)
    assert closed["answer"] == "Only the first" and closed["answered"] == T0 + 600
    assert closed["by_default"] is False and len(default_prompts(world, q)) == 1
    assert capsys.readouterr().err == ""  # a closed question isn't tried at every pass


@pytest.mark.parametrize("state", ["blocked", None], ids=["at a dialog", "gone"])
def test_a_default_waits_for_an_asker_it_can_reach(world, state):
    q = ask_with_default(world)
    if state:
        world.agent(BOSS, state)  # typing would answer the dialog
    else:
        world.agents.pop(BOSS)
    world.t = T0 + 3600

    world.run("watchdog")

    assert default_prompts(world, q) == [] and not question_now(world)["answered"]
    world.agent(BOSS, "idle")
    world.run("watchdog")
    assert len(default_prompts(world, q)) == 1 and question_now(world)["by_default"] is True


def test_a_failed_default_prompt_leaves_the_question_open_and_the_watchdog_running(
        world, monkeypatch):
    ask_with_default(world)
    world.t = T0 + 3600
    monkeypatch.setattr(world.orch, "prompt", lambda pane, text: False)

    assert world.run("watchdog") == 0

    assert not question_now(world)["answered"]
    assert [e for e in world.events() if e["kind"] == "answer"] == []


def test_a_default_is_taken_in_the_pass_that_ends_the_run_and_survives_its_save(world):
    ask_with_default(world)
    world.t = T0 + 9 * 3600  # past the default and the run's end: this pass saves the run too

    world.run("watchdog")

    run = json.loads((world.root / "runs" / "w1-p0.json").read_text())
    assert run["ended"] == world.t and run["questions"][0]["answer"] == "Both OK"


@pytest.mark.parametrize("argv, error", [
    (["--default", "Merge"], "go together"),
    (["--by", "+30m"], "go together"),
    (["--default", "Later", "--by", "+30m"], "isn't one of the question's options"),
    (["--default", "Deploy", "--by", "+30m"], "the owner has to pick it"),
    (["--default", "Merge", "--by", "soon"], "can't read the time"),
    (["--default", "Merge", "--by", "2020-01-01 10:00"], "has passed"),
    (["--default", "Merge", "--by", "+30m", "--kind", "plan"], "a plan waits"),
], ids=["no by", "no default", "not an option", "dangerous", "bad time", "past", "plan"])
def test_a_default_silence_could_not_be_trusted_with_is_refused(world, argv, error):
    start_run(world)

    with pytest.raises(world.orch.Refused) as refused:
        world.run("ask", "Ship?", "--option", "Merge", "--option", "Deploy", "--danger", "deploy",
                  *argv)

    assert error in refused.value.text
    assert world.events() == [] or all(e["kind"] != "ask" for e in world.events())
    assert not json.loads((world.root / "runs" / "w1-p0.json").read_text()).get("questions")


def test_a_default_needs_a_run_for_the_watchdog_to_find_it_in(world):
    with pytest.raises(world.orch.Refused) as refused:
        world.orch.ask(W1, "Ship?", [{"label": "Merge"}], default="Merge", by="+30m")
    assert "outside a run" in refused.value.text


def test_a_question_asked_outside_a_run_can_be_answered(world):
    world.run("ask", "Which branch?")
    [ask] = [e for e in world.events() if e["kind"] == "ask"]

    world.run("answer", ask["id"], "main")

    text = f"The owner answered your question {ask['id']} ('Which branch?'): main"
    assert world.prompts == [(BOSS, text)]
    with pytest.raises(SystemExit):
        world.run("answer", ask["id"], "again")


def test_answer_is_not_recorded_when_the_prompt_fails(world, monkeypatch):
    start_run(world)
    world.run("ask", "Go?")
    [q] = json.loads((world.root / "runs" / "w1-p0.json").read_text())["questions"]
    monkeypatch.setattr(world.orch, "prompt", lambda pane, text: False)

    with pytest.raises(SystemExit):
        world.run("answer", q["id"], "yes")

    [q] = json.loads((world.root / "runs" / "w1-p0.json").read_text())["questions"]
    assert q["answered"] is None


def test_status_json_shows_open_questions_reports_and_accounts(world, capsys):
    start_run(world)
    world.agent(W1, "idle")
    world.turn_end(W1, last="all green")
    world.agent(W2, "working")
    world.run("ask", "First?")
    world.run("ask", "Second?")
    first = json.loads((world.root / "runs" / "w1-p0.json").read_text())["questions"][0]
    world.run("answer", first["id"], "ok")
    world.orch.write_report(BOSS, {"role": "orchestrator", "summary": "3 of 5 fixes",
                                   "progress": {"done": 3, "total": 5}})
    world.orch.write_report(W1, {"role": "worker", "summary": "fixed #12", "status": "done"})
    path = world.root / "agents" / "w1-p1.json"
    path.write_text(json.dumps(dict(json.loads(path.read_text()), account="2")))
    capsys.readouterr()

    world.run("status", "--json")

    data = json.loads(capsys.readouterr().out)
    answered, second = data["questions"]  # answered: shown for 10 minutes, with the answer
    assert answered["text"] == "First?" and answered["answer"] == "ok"
    assert answered["answered"] == world.t and second["answered"] is None
    assert data["report"]["progress"] == {"done": 3, "total": 5}
    w1 = data["workers"][0]
    assert w1["report"]["summary"] == "fixed #12" and w1["account"] == "2"
    assert w1["last"] == "all green"  # the report kept the Stop hook's fields
    assert [e["kind"] for e in world.events()].count("report") == 2
    world.t += 10 * 60 + 1
    world.run("status", "--json")
    assert [q["text"] for q in json.loads(capsys.readouterr().out)["questions"]] == ["Second?"]


# --- decisions --------------------------------------------------------------------------------

LOG = """# Decision log

Started 2026-11-05 10:00. Append-only.

## Generation 1
- **D1** 2026-11-05 10:40, user: "Use the MCP tool, not a CLI". Agent built orch_mcp.py.
- **D2** 2026-11-05 10:50, agent: question ids are random hex. Why: no counter file to lock.
- **D3** 2026-11-05 11:00, agent: poll herdr every 15 s.
  The gateway samples state for the activity strip.

## Generation 2: takeover by w1 in herdr pane w1:p0, 2026-11-05 12:00, from gen-01.md
- D4 12:05, orchestrator: sample every 30 s instead of D3, because 15 s cost too much CPU.
- **D5** 2026-11-05 12:10, the user (Alex): "drop the widget". Replaces **D2** and D1.
- **D6** 2026-11-05 ~12:20, user proposed: "ship it today". Agent agreed.
- **D7** 2026-11-05 12:30 EET (10:30 UTC), agent: shipped.
- **D8** 2026-11-05 12:40 EET (date), agent: tagged v1.
- **D9** 2026-11-05 12:50 EET, user (checking in at 12:49): "keep going". Agent kept going.
- **D10** 2026-11-05 ~12:55, user picked option "Merge now, tag later (Recommended)": merged.
"""


def write_log(world, name, text, mtime):
    lineage = world.root / "home-me-Repo-x" / name
    lineage.mkdir(parents=True)
    (lineage / "decisions.md").write_text(text)
    os.utime(lineage / "decisions.md", (mtime, mtime))
    return lineage / "decisions.md"


def d_of(data, did):
    return next(e for e in data["decisions"] if e["id"] == did)


def test_decisions_parses_the_newest_log_of_the_pane(world, capsys):
    write_log(world, "w1-p0-20261104-090000-aaaaaaaa", "- **D1** 09:00, agent: old.\n", 100)
    log = write_log(world, "w1-p0-20261105-100000-bbbbbbbb", LOG, 200)
    write_log(world, "w1-p00-20261105-100000-cccccccc", "- **D1** 09:00, agent: other.\n", 300)

    world.run("decisions", "--json")

    data = json.loads(capsys.readouterr().out)
    assert data["log"] == str(log)
    d = {e["id"]: e for e in data["decisions"]}
    assert [e["id"] for e in data["decisions"]] == [
        "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10"]  # log order
    assert d_of(data, "D9")["by"] == "user (checking in at 12:49)"
    assert d_of(data, "D9")["quote"] == "keep going"
    assert d_of(data, "D10")["by"] == "user" and d_of(data, "D10")["who"] == "user"
    assert d["D1"]["who"] == "user" and d["D1"]["quote"] == "Use the MCP tool, not a CLI"
    assert d["D1"]["what"] == "Agent built orch_mcp.py." and d["D1"]["time"] == "2026-11-05 10:40"
    assert d["D2"]["who"] == "agent" and d["D2"]["what"] == "question ids are random hex."
    assert d["D2"]["why"] == "no counter file to lock"
    assert d["D3"]["what"] == "poll herdr every 15 s. The gateway samples state for the activity strip."
    assert d["D3"]["why"] == ""  # no "Why" or "because": nothing is a reason
    assert d["D4"]["by"] == "orchestrator" and d["D4"]["time"] == "12:05"
    assert d["D4"]["what"] == "sample every 30 s instead of D3."
    assert d["D4"]["why"] == "15 s cost too much CPU" and d["D4"]["generation"] == 2
    assert d["D5"]["who"] == "user" and d["D5"]["by"] == "the user (Alex)"
    assert {k: d[k]["superseded_by"] for k in d if d[k]["superseded_by"]} == {
        "D1": "D5", "D2": "D5", "D3": "D4"}
    assert (d["D6"]["who"], d["D6"]["time"], d["D6"]["quote"]) == ("user", "2026-11-05 ~12:20",
                                                                  "ship it today")
    assert (d["D7"]["who"], d["D7"]["time"], d["D7"]["what"]) == ("agent", "2026-11-05 12:30 EET",
                                                                  "shipped.")
    assert (d["D8"]["by"], d["D8"]["what"]) == ("agent", "tagged v1.")


# Real entries (an orchestrator's log, 2026-11-05) that the first parser got wrong; project, account,
# machine and app names replaced.
REAL = """\
- **D5** 2026-11-05 11:05, agent: decision log onboarding = full log up to 30 KB, else index (first 160 chars of each entry) + latest 1-2 generations up to 60 KB. Ruled out keeping "read in full" (140-190K tokens per takeover) and model-written summaries (lossy, the reason the log exists).
- **D25** 2026-11-05 12:15, user created Firebase project my-app-123 (owner owner@example.com) in the console. Agent registered Android app com.example.app (App ID 1:000000000000:android:0000) and saved google-services.json to ~/.config/orch/google-services.json (600, outside git). The earlier GCP-only project my-app (created by the agent, empty) is left in place; user not asked yet whether to delete it. Service account key + Firestore come with the push/relay task.
- **D29** 2026-11-05 12:33, user: "it's okay" (to: keep the All page before the per-orchestrator pages, app opens on it when something needs him). Forwarded to ui to mark decided in DESIGN.md.
- **D34** 2026-11-05 12:49, user: "1 ok as you reccomend": production deploy 'yes' from a notification opens the app's hold-to-confirm; staging stays one tap after unlock. Live Update on lock screen = progress only (agent default, user did not answer explicitly). Sent to ui and native.
- **D36** 2026-11-05 13:20, user: "make sure to use main fully". So no early move to spare: backend task 3 part A = orch account --spill 1:98:2 (watchdog moves agents at idle once main 7d/5h >= 98%, fresh takeover from handoff preferred over --resume, immediate switch if a limit message is hit), installed + enabled on box1/box2 (not box3, different login); part B = cloud setup (FCM, Firestore eur3, anonymous auth, orch-push SA key in ~/.config/orch on 3 machines) + push sender/relay/pairing + docs/PUSH.md. Committed ui task 3 (82a743c: setup/pairing, settings, widgets + QS tile, light theme, N14 prod deploy opens app). ui task 4: spill-over UI, then APP-QA.
- **D37** 2026-11-05 13:21, agent: at the 400K handoff threshold, stopped my background tasks (orch wait, the demo http.server on 127.0.0.1:8765) and relaunched emulator-5554 detached (setsid nohup, log ~/Android/emulator-OnePlus13R.log) because the takeover waits for background tasks and native depends on the emulator; told native. orch-serve branch is pushed but no PR is open for it yet (deliberately held until the app tests against the gateway). Handoff written to handoff.md in this lineage.
"""


def test_decisions_keep_log_order_and_take_only_explicit_reasons(world):
    d = {e["id"]: e for e in world.orch.parse_decisions(REAL)}
    assert [e["id"] for e in world.orch.parse_decisions(REAL)] == [
        "D5", "D25", "D29", "D34", "D36", "D37"]
    assert [d[k]["line"] for k in d] == [1, 2, 3, 4, 5, 6]
    # D5: "the reason the log exists" is not a reason; "Ruled out ..." stays with the decision
    assert d["D5"]["why"] == "" and d["D5"]["what"].endswith("(lossy, the reason the log exists).")
    assert d["D5"]["what"].startswith("decision log onboarding = full log up to 30 KB")
    # D25: no "user:" prefix, but the entry is the user's
    assert (d["D25"]["who"], d["D25"]["by"], d["D25"]["why"]) == ("user", "user", "")
    assert d["D25"]["what"].startswith("user created Firebase project my-app-123")
    # D29, D34: after the quote, no ": " or "(" tying it to the quote
    assert d["D29"]["quote"] == "it's okay"
    assert d["D29"]["what"] == ("to: keep the All page before the per-orchestrator pages, app opens "
                                "on it when something needs him. Forwarded to ui to mark decided in "
                                "DESIGN.md.")
    assert d["D34"]["quote"] == "1 ok as you reccomend"
    assert d["D34"]["what"].startswith("production deploy 'yes' from a notification")
    assert d["D34"]["what"].endswith("Sent to ui and native.") and d["D34"]["why"] == ""
    # D36: the commit note at the end is not the reason
    assert d["D36"]["quote"] == "make sure to use main fully" and d["D36"]["why"] == ""
    assert d["D36"]["what"].startswith("So no early move to spare: backend task 3 part A")
    assert d["D36"]["what"].endswith("ui task 4: spill-over UI, then APP-QA.")
    # D37: the "because" clause is the reason; the rest of its sentence stays in `what`
    assert d["D37"]["why"] == ("the takeover waits for background tasks and native depends on the "
                               "emulator")
    assert d["D37"]["what"] == (
        "at the 400K handoff threshold, stopped my background tasks (orch wait, the demo http.server "
        "on 127.0.0.1:8765) and relaunched emulator-5554 detached (setsid nohup, log "
        "~/Android/emulator-OnePlus13R.log); told native. orch-serve branch is pushed but no PR is "
        "open for it yet (deliberately held until the app tests against the gateway). Handoff "
        "written to handoff.md in this lineage.")
    for e in d.values():
        assert e["by"] and (e["who"] == "user") == ("user" in e["by"] or "owner" in e["by"])
        assert not e["what"].startswith((":", "("))


def test_decisions_without_a_log(world):
    with pytest.raises(SystemExit):
        world.run("decisions", "--pane", "w9:p9")


# --- accounts -------------------------------------------------------------------------------

@pytest.fixture
def accounts(world, tmp_path, monkeypatch):
    """Accounts 1 and 2 logged in, 3 not; the switch itself is recorded instead of run."""
    def account_dir(n):
        return tmp_path / ("claude" if str(n) == "1" else f"claude-{n}")
    for n in ("1", "2", "3"):
        account_dir(n).mkdir()
    for n in ("1", "2"):
        (account_dir(n) / ".credentials.json").write_text("{}")
    monkeypatch.setattr(world.orch, "account_dir", account_dir)
    monkeypatch.setattr(world.orch, "is_claude", lambda pid: pid == 4242)
    jobs = Jobs()
    monkeypatch.setattr(world.orch, "run_switch", lambda job: jobs.append(job) or
                        type("R", (), {"returncode": 0, "stderr": ""})())

    def claudes():  # pane -> (pid, account) of its claude process, here from its status file
        out = {}
        for path in (world.root / "agents").glob("*.json"):
            st = json.loads(path.read_text())
            out[st["pane"]] = (st.get("pid"), st.get("account", "1"))
        return out
    monkeypatch.setattr(world.orch, "pane_claudes", claudes)
    jobs.handoffs = []

    def request_handoff(pane, session, cwd, sw):
        jobs.handoffs.append((pane, session, cwd, sw["account"], sw.get("why")))
        return f"write your handoff for account {sw['account']}"
    monkeypatch.setattr(world.orch, "request_handoff", request_handoff)
    return jobs


class Jobs(list):
    pass


def idle_with_status(world, pane, **extra):
    world.agent(pane, "idle", session="s-new")
    world.turn_end(pane, session="s-new")
    path = world.root / "agents" / f"{pane.replace(':', '-')}.json"
    st = dict(json.loads(path.read_text()), pid=4242, account="1", context_tokens=20_000,
              relaunch=["--dangerously-skip-permissions", "--model", "claude-haiku-5-5"])
    path.write_text(json.dumps(dict(st, **extra)))


def test_account_switch_at_handoff_is_queued(world, accounts, capsys):
    world.run("account", W1, "2")

    acc = json.loads((world.root / "accounts.json").read_text())
    assert acc["switch"] == {W1: {"account": "2", "time": T0, "when": "handoff"}}
    [event] = world.events()
    assert event["kind"] == "account" and event["account"] == "2" and event["when"] == "handoff"
    assert accounts == []
    with pytest.raises(SystemExit):  # not logged in
        world.run("account", W1, "3")
    with pytest.raises(SystemExit):
        world.run("account", W1, "work2")


def test_account_switch_now_resumes_an_idle_agent(world, accounts, capsys):
    idle_with_status(world, W1)

    assert world.run("account", W1, "2", "--now") == 0

    [job] = accounts
    assert job == {"pane": W1, "pid": 4242, "resume": "s-new", "account": "2",
                   "args": ["--dangerously-skip-permissions", "--model", "claude-haiku-5-5"],
                   "session": "s-new", "created": 0}
    assert "now runs on account 2" in capsys.readouterr().out


@pytest.mark.parametrize("setup, message", [
    (lambda w: w.agent(W1, "working"), "working"),
    (lambda w: idle_with_status(w, W1, background_tasks=[{"what": "pytest"}]), "background"),
    (lambda w: w.agent(W1, "idle", session="never-ended-a-turn"), "no turn end"),
    (lambda w: idle_with_status(w, W1, account="2"), "already runs on account 2"),
    (lambda w: idle_with_status(w, W1, pid=999), "no turn end"),  # resumed since: stale
])
def test_account_switch_now_refuses(world, accounts, capsys, setup, message):
    setup(world)
    with pytest.raises(SystemExit):
        world.run("account", W1, "2", "--now")
    assert message in capsys.readouterr().err
    assert accounts == []


def test_account_default_and_listing(world, accounts, capsys):
    world.run("account", "--default", "2")
    world.run("account", W2, "1")
    capsys.readouterr()

    world.run("account", "--json")

    data = json.loads(capsys.readouterr().out)
    assert data["default"] == "2" and data["switch"][W2]["account"] == "1"


def test_status_shows_the_context_size_the_hook_keeps_during_the_turn(world, capsys):
    start_run(world)
    world.agent(W1, "working")
    world.turn_end(W1)
    path = world.root / "agents" / "w1-p1.json"
    path.write_text(json.dumps(dict(json.loads(path.read_text()), context_tokens=50_000)))
    live = world.root / "agents" / "context" / "w1-p1.json"
    live.parent.mkdir()
    live.write_text(json.dumps({"session": "s-old", "context_tokens": 300_000}))
    capsys.readouterr()

    world.run("status", "--json")
    assert json.loads(capsys.readouterr().out)["workers"][0]["context_tokens"] == 50_000
    live.write_text(json.dumps({"session": f"s-{W1}", "context_tokens": 80_000}))
    world.run("status", "--json")
    assert json.loads(capsys.readouterr().out)["workers"][0]["context_tokens"] == 80_000


def test_accounts_are_named_in_account_status_and_usage(world, accounts, capsys, tmp_path,
                                                        monkeypatch):
    world.run("account", "--name", "2", "spare")
    world.run("account", "--name", "1", "main")
    world.run("account", "--name", "1", "")  # back to the default
    with pytest.raises(SystemExit):
        world.run("account", "--name", "two", "x")
    capsys.readouterr()

    world.run("account", "--json")
    names = {k: a["name"] for k, a in json.loads(capsys.readouterr().out)["accounts"].items()}
    assert names == {"1": "Acct 1", "2": "spare", "3": "Acct 3"}
    world.run("status", "--json")
    assert json.loads(capsys.readouterr().out)["account_names"] == names
    monkeypatch.setenv("HOME", str(tmp_path))  # no transcripts
    world.run("usage", "--json")  # no reading for any account yet: each one is still listed
    assert json.loads(capsys.readouterr().out)["accounts"]["2"] == {
        "id": "2", "name": "spare", "login": None, "updated": None, "stale": True,
        "windows": {}}


def test_account_switch_when_idle_resumes_a_small_context(world, accounts, capsys):
    start_run(world)
    world.agent(W1, "working")
    world.run("account", W1, "2", "--when-idle")
    capsys.readouterr()
    world.run("status", "--json")
    [w1, _] = json.loads(capsys.readouterr().out)["workers"]
    assert w1["switch"] == {"account": "2", "time": T0, "when": "idle"}

    world.agent(W2, "working")
    world.run("watchdog")  # still working: nothing yet
    assert accounts == []
    idle_with_status(world, W1, background_tasks=[{"what": "pytest"}])
    world.run("watchdog")  # idle, but waiting on its own job
    assert accounts == []

    idle_with_status(world, W1)  # 20K tokens of context
    world.run("watchdog")

    [job] = accounts
    assert job["pane"] == W1 and job["account"] == "2" and job["resume"] == "s-new"
    assert job["when"] == "idle" and accounts.handoffs == []
    assert json.loads((world.root / "accounts.json").read_text())["switch"] == {}
    why = [e for e in world.events() if e["title"] == f"Account switch: resuming {W1}"]
    assert "20K tokens" in why[0]["body"]
    world.run("watchdog")
    assert len(accounts) == 1


def test_a_large_context_switches_through_a_handoff(world, accounts):
    world.run("account", W1, "2", "--when-idle")
    idle_with_status(world, W1, context_tokens=300_000, cwd="/work/proj")

    world.run("watchdog")

    assert accounts == [] and accounts.handoffs == [
        (W1, "s-new", "/work/proj", "2", None)]
    assert world.prompts == [(W1, "write your handoff for account 2")]
    sw = json.loads((world.root / "accounts.json").read_text())["switch"][W1]
    assert sw["handoff"] == T0
    asked = world.events()[-1]
    assert asked["how"] == "handoff" and "reads the handoff" in asked["body"]
    world.t += 29 * 60
    world.run("watchdog")  # the takeover follows the handoff; nothing more to do
    assert accounts == [] and len(accounts.handoffs) == 1
    world.t += 2 * 60
    world.run("watchdog")  # still idle with no handoff 31 min later: resume instead
    [job] = accounts
    assert job["resume"] == "s-new"
    assert any("no handoff 30 min after" in e["body"] for e in world.events())


def test_an_agent_at_its_usage_limit_is_resumed_whatever_its_context(world, accounts):
    idle_with_status(world, W1, context_tokens=300_000)
    acc = {"switch": {W1: {"account": "2", "time": T0, "when": "idle", "reason": "limit"}}}
    (world.root / "accounts.json").write_text(json.dumps(acc))

    world.run("watchdog")

    assert [j["pane"] for j in accounts] == [W1] and accounts.handoffs == []


def test_the_watchdog_resumes_several_agents_within_its_budget(world, accounts, monkeypatch):
    world.run("account", W1, "2", "--when-idle")
    world.run("account", W2, "2", "--when-idle")
    idle_with_status(world, W1)
    idle_with_status(world, W2)
    monkeypatch.setattr(world.orch, "SWITCH_BUDGET", -1)  # spent: the next pass does them

    world.run("watchdog")
    assert accounts == []
    monkeypatch.setattr(world.orch, "SWITCH_BUDGET", 200)
    world.run("watchdog")
    assert [j["pane"] for j in accounts] == [W1, W2]


def test_status_json_shows_the_orchestrators_pending_switch(world, accounts, capsys):
    start_run(world)
    world.agent(W1, "working")
    world.agent(W2, "working")
    world.run("account", BOSS, "2", "--at-handoff")
    capsys.readouterr()
    world.run("status", "--json")
    assert json.loads(capsys.readouterr().out)["switch"]["when"] == "handoff"


def test_a_failed_idle_switch_is_dropped(world, accounts, monkeypatch):
    world.run("account", W1, "2", "--when-idle")
    idle_with_status(world, W1)
    monkeypatch.setattr(world.orch, "run_switch", lambda job: type(
        "R", (), {"returncode": 1, "stderr": "the new agent is at a dialog"})())

    world.run("watchdog")

    assert json.loads((world.root / "accounts.json").read_text())["switch"] == {}
    dropped = world.events()[-1]
    assert dropped["kind"] == "account" and "at a dialog" in dropped["body"]


def test_an_idle_switch_to_the_current_account_is_dropped(world, accounts):
    world.run("account", W1, "2", "--when-idle")
    idle_with_status(world, W1, account="2")

    world.run("watchdog")

    assert accounts == []
    assert json.loads((world.root / "accounts.json").read_text())["switch"] == {}


def test_a_switch_now_clears_a_pending_one(world, accounts):
    world.run("account", W1, "2")  # at its handoff
    idle_with_status(world, W1)

    world.run("account", W1, "2", "--now")

    assert len(accounts) == 1
    assert json.loads((world.root / "accounts.json").read_text())["switch"] == {}


def usage_at(world, pct, window="seven_day", age=30, resets_in=86400):
    (world.root / "usage.json").write_text(json.dumps({"accounts": {"1": {
        "read_at": world.t - age, "windows": {window: {"used_percentage": pct,
                                                    "resets_at": world.t + resets_in}}}}}))


def test_a_login_hash_is_the_same_for_one_login_whatever_its_local_number(world, accounts,
                                                                           tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    first, second = tmp_path / ".claude.json", tmp_path / "claude-2" / ".claude.json"

    def login(path, **who):
        path.write_text(json.dumps({"oauthAccount": who}))

    me = {"accountUuid": "u1", "organizationUuid": "o1", "emailAddress": "me@example.com"}
    login(first, **me)
    login(second, **me, displayName="the same login, another copy of its profile")
    listed = world.orch.account_list()
    assert listed["1"]["login"] == listed["2"]["login"]
    assert len(listed["1"]["login"]) == 16 and "example" not in json.dumps(listed)

    login(second, **dict(me, accountUuid="u2"))
    assert world.orch.account_login(2) != listed["1"]["login"]
    login(second, **dict(me, organizationUuid="o2"))  # its limits are the organization's
    assert world.orch.account_login(2) != listed["1"]["login"]

    login(second, emailAddress="me@example.com")  # no ids to tell it by: never the same
    assert world.orch.account_login(2) is None
    second.unlink()
    assert world.orch.account_login(2) is None


def test_spill_moves_account_one_agents_once_it_reaches_the_threshold(world, accounts, capsys):
    world.agent(BOSS, "working")
    world.agent(W1, "working")
    idle_with_status(world, W1)
    idle_with_status(world, W2, account="2", context_tokens=300_000)
    world.agent(W1, "working")
    world.run("account", "--spill", "1:98:2")
    capsys.readouterr()
    world.run("account", "--json")
    listed = json.loads(capsys.readouterr().out)
    assert listed["spill"] == {"from": "1", "at": 98, "to": "2", "time": T0, "active": None,
                               "since": None, "moved": 0, "total": 0}
    assert listed["for_new"] == "1"

    usage_at(world, 97)
    world.run("watchdog")
    assert "switch" not in json.loads((world.root / "accounts.json").read_text()) or not json.loads(
        (world.root / "accounts.json").read_text())["switch"]
    usage_at(world, 99, age=20 * 60)  # stale: unknown, not reached
    world.run("watchdog")
    assert not json.loads((world.root / "accounts.json").read_text()).get("spill")["active"]
    usage_at(world, 98, resets_in=-60)  # that window already reset
    world.run("watchdog")
    assert not json.loads((world.root / "accounts.json").read_text()).get("spill")["active"]

    usage_at(world, 98)
    world.run("watchdog")

    acc = json.loads((world.root / "accounts.json").read_text())
    assert acc["spill"]["active"] == {"window": "seven_day", "used": 98, "since": world.t}
    assert set(acc["switch"]) == {W1}  # on account 1; W2 is on 2, BOSS has no claude here
    assert acc["switch"][W1]["reason"] == "spill" and acc["switch"][W1]["when"] == "idle"
    assert "98% of its seven_day window" in acc["switch"][W1]["why"]
    [spill] = [e for e in world.events() if e["kind"] == "spill" and "moving" in e["title"]]
    assert spill["title"] == "Account 1 at 98%: moving to 2" and world.herdr_calls  # pushed
    world.run("watchdog")  # once
    assert len([e for e in world.events() if e["kind"] == "spill" and "moving" in e["title"]]) == 1
    capsys.readouterr()
    world.run("status", "--json")
    assert json.loads(capsys.readouterr().out)["spill"]["active"]["used"] == 98

    usage_at(world, 3, resets_in=7 * 86400)  # the week reset
    world.run("watchdog")
    assert json.loads((world.root / "accounts.json").read_text())["spill"]["active"] is None


def test_spill_off_drops_its_pending_switches_and_bad_rules_are_refused(world, accounts, capsys):
    world.run("account", "--spill", "1:98.5:2")
    world.run("account", W2, "2", "--when-idle")  # by hand: kept
    acc = json.loads((world.root / "accounts.json").read_text())
    assert acc["spill"]["at"] == 98.5
    acc["switch"][W1] = {"account": "2", "time": T0, "when": "idle", "reason": "spill"}
    (world.root / "accounts.json").write_text(json.dumps(acc))
    world.run("account", "--spill", "off")
    acc = json.loads((world.root / "accounts.json").read_text())
    assert "spill" not in acc and set(acc["switch"]) == {W2}
    for bad in ("1:98", "1:98:1", "2:98:3", "main:98:spare"):
        with pytest.raises(SystemExit):
            world.run("account", "--spill", bad)


def test_spill_states_per_agent_and_the_runs_progress(world, accounts, capsys, tmp_path,
                                                       monkeypatch):
    monkeypatch.setattr(world.orch, "PROJECTS", tmp_path / "projects")
    start_run(world)
    world.agent(BOSS, "working")
    idle_with_status(world, W1)  # 20K: resumed at the pass
    world.agent(W2, "working")
    world.turn_end(W2)
    path = world.root / "agents" / f"{W2.replace(':', '-')}.json"
    path.write_text(json.dumps(dict(json.loads(path.read_text()), pid=4242, account="1",
                                    session="s-w1:p2", context_tokens=20_000)))
    world.run("account", "--spill", "1:98:2")
    usage_at(world, 98)

    world.run("watchdog")
    capsys.readouterr()
    world.run("account", "--for-new")
    assert capsys.readouterr().out.strip() == "2"
    world.run("status", "--json")
    status = json.loads(capsys.readouterr().out)
    w1, w2 = status["workers"]
    assert w1["spill"] == {"state": "moved_idle", "reason": "resume", "time": world.t,
                           "account": "2"}
    assert w2["spill"] == {"state": "waiting", "reason": "working", "time": world.t,
                           "account": "2"}
    assert status["spill"]["since"] == world.t
    assert (status["spill"]["moved"], status["spill"]["total"]) == (1, 2)

    world.agent(W2, "idle")  # done, but its resume fails
    monkeypatch.setattr(world.orch, "run_switch", lambda job: type(
        "R", (), {"returncode": 1, "stderr": "the new agent is at a dialog"})())
    world.run("watchdog")
    world.run("status", "--json")
    w2 = json.loads(capsys.readouterr().out)["workers"][1]
    assert w2["spill"]["state"] == "cant_move" and "at a dialog" in w2["spill"]["reason"]
    world.run("watchdog")  # not queued again
    assert W2 not in json.loads((world.root / "accounts.json").read_text())["switch"]


def test_an_agent_moved_by_hand_counts_as_moved_and_is_no_longer_cant_move(
        world, accounts, capsys, tmp_path, monkeypatch):
    # 2026-11-05: one agent's resume stopped at the trust question, which was answered in the
    # pane; another was restarted by hand. Both ran on the target and the rule didn't know.
    monkeypatch.setattr(world.orch, "PROJECTS", tmp_path / "projects")
    start_run(world)
    world.agent(BOSS, "working")
    idle_with_status(world, W1)
    world.agent(W2, "working")
    world.turn_end(W2)

    def runs_on(pane, account):
        path = world.root / "agents" / f"{pane.replace(':', '-')}.json"
        path.write_text(json.dumps(dict(json.loads(path.read_text()), pid=4242, account=account,
                                        session=f"s-{pane}", context_tokens=20_000)))

    runs_on(W2, "1")
    world.run("account", "--spill", "1:98:2")
    usage_at(world, 98)
    monkeypatch.setattr(world.orch, "run_switch", lambda job: type(
        "R", (), {"returncode": 1, "stderr": "the new agent is at a dialog"})())
    world.run("watchdog")
    capsys.readouterr()
    world.run("status", "--json")
    status = json.loads(capsys.readouterr().out)
    assert [w["spill"]["state"] for w in status["workers"]] == ["cant_move", "waiting"]
    assert (status["spill"]["moved"], status["spill"]["total"]) == (0, 2)

    runs_on(W1, "2")  # someone answered it, and restarted the other one
    runs_on(W2, "2")
    world.agent(W2, "idle")
    world.t += 300
    usage_at(world, 98)
    world.run("watchdog")
    capsys.readouterr()
    world.run("status", "--json")
    status = json.loads(capsys.readouterr().out)

    assert [w["spill"] for w in status["workers"]] == [
        {"state": "moved_idle", "reason": "by hand", "time": world.t, "account": "2"}] * 2
    assert (status["spill"]["moved"], status["spill"]["total"]) == (2, 2)
    acc = json.loads((world.root / "accounts.json").read_text())
    assert acc["failed"] == {} and acc["switch"] == {}


def transcript(world, session, *entries):
    path = world.orch.PROJECTS / "-work-proj" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


LIMIT = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
         "message": {"model": "<synthetic>", "content": [
             {"type": "text", "text": "You've hit your weekly limit · resets 7am (Europe/Bucharest)"}]}}
PROMPT = {"type": "user", "message": {"content": "fix #12"}}
REPLY = {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}


def test_limit_hit_reads_the_last_turn_of_the_transcript(world, tmp_path, monkeypatch):
    monkeypatch.setattr(world.orch, "PROJECTS", tmp_path / "projects")
    transcript(world, "s1", PROMPT, LIMIT)
    assert world.orch.limit_hit("s1").startswith("You've hit your weekly limit")
    transcript(world, "s1", PROMPT, LIMIT, PROMPT)  # a prompt sent since runs into it again
    assert world.orch.limit_hit("s1")
    transcript(world, "s1", PROMPT, LIMIT, PROMPT, REPLY)  # the limit is over
    assert world.orch.limit_hit("s1") is None
    other = dict(LIMIT, error="server_error", message={"content": "API Error: Connection lost"})
    transcript(world, "s1", PROMPT, other)
    assert world.orch.limit_hit("s1") is None
    assert world.orch.limit_hit("missing") is None and world.orch.limit_hit(None) is None


def test_an_agent_at_its_limit_is_switched_at_the_next_pass(world, accounts, tmp_path,
                                                             monkeypatch):
    monkeypatch.setattr(world.orch, "PROJECTS", tmp_path / "projects")
    world.run("account", "--spill", "1:98:2")
    usage_at(world, 90)  # not reached: the limit alone moves it
    idle_with_status(world, W1, context_tokens=300_000)
    transcript(world, "s-new", PROMPT, LIMIT)

    world.run("watchdog")

    acc = json.loads((world.root / "accounts.json").read_text())
    assert not acc["spill"]["active"]
    assert [j["pane"] for j in accounts] == [W1] and accounts.handoffs == []
    assert any("usage limit" in e["body"] for e in world.events() if e["kind"] == "account")


def test_is_claude(world):
    assert world.orch.is_claude(os.getpid()) is False  # pytest
    assert world.orch.is_claude(None) is False and world.orch.is_claude(2 ** 30) is False


def test_the_tailnet_name_is_tailscales_self_hostname_asked_once(world, monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(stdout=json.dumps({"Self": {"HostName": "box3",
                                                           "DNSName": "box3.tail.ts.net."}}))
    monkeypatch.setattr(world.orch, "_TAILNET_NAME", [])
    monkeypatch.setattr(world.orch.subprocess, "run", run)
    assert world.orch.machine_name() == "box3" and world.orch.machine_name() == "box3"
    assert calls == [["tailscale", "status", "--json"]]
    world.orch.CONFIG.parent.mkdir(parents=True)
    world.orch.CONFIG.write_text("ORCH_TOKEN=x\nORCH_MACHINE=build-box\n")
    assert world.orch.machine_name() == "build-box"
    monkeypatch.setattr(world.orch, "_TAILNET_NAME", [])
    monkeypatch.setattr(world.orch.subprocess, "run", lambda cmd, **kw: SimpleNamespace(stdout=""))
    assert world.orch.tailnet_name() is None
