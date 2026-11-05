"""Offline tests for hooks/context-handoff.py: run with `uv run --with pytest pytest -q`."""
import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "context-handoff.py"
PANE = "w1:p1"

# A stand-in for the herdr CLI: logs each call. The old agent sits in the pane until /exit, the
# pane is free until `pane run`, then a new agent is idle there. stub.json can make the old agent
# keep working, and decide whether a prompt or an Enter starts the new one.
HERDR_STUB = """
import json, sys
from pathlib import Path
here = Path(__file__).parent
args = sys.argv[1:]
with open(here / "calls.jsonl", "a") as f:
    f.write(json.dumps(args) + "\\n")
cfg = json.loads((here / "stub.json").read_text()) if (here / "stub.json").exists() else {}
path = here / "state.json"
st = json.loads(path.read_text()) if path.exists() else {"phase": "old", "new": "idle"}
def agent():
    if st["phase"] == "old":
        return {"pane_id": "w1:p1", "name": "w1", "agent_status": cfg.get("old", "idle"),
                "agent_session": {"value": "s1"}}
    if st["phase"] == "new":
        return {"pane_id": "w1:p1", "agent_status": st["new"], "agent_session": {"value": "s2"}}
    return None
rc = 0
if args[:2] == ["agent", "get"]:
    print(json.dumps({"result": {"agent": agent() or {}}}))
elif args[:2] == ["agent", "list"]:
    print(json.dumps({"result": {"agents": [a for a in [agent()] if a]}}))
elif args[:2] == ["agent", "prompt"]:
    if args[3] == "/exit":
        st["phase"] = "gone"
    elif st["phase"] == "new" and cfg.get("prompt_starts", True):
        st["new"] = "working"
elif args[:2] == ["agent", "send-keys"]:
    if st["phase"] == "new" and cfg.get("enter_starts"):
        st["new"] = "working"
elif args[:2] == ["agent", "wait"] and "--until" in args:
    rc = 0 if (agent() or {}).get("agent_status") == "working" else 1
elif args[:2] == ["pane", "run"]:
    st["phase"] = "new"
path.write_text(json.dumps(st))
sys.exit(rc)
"""


@pytest.fixture
def hook(tmp_path, monkeypatch):
    for name in ("HANDOFF_LINEAGE", "CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR", "CLAUDE_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    spec = importlib.util.spec_from_file_location("context_handoff", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path / "handoffs")
    monkeypatch.setattr(module, "IN_HERDR", False)
    return module


@pytest.fixture
def run(hook, tmp_path, monkeypatch, capsys):
    def run(event, session="s1", tokens=0, **extra):
        transcript = tmp_path / f"{session}.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {
            "model": "claude-opus-5-5", "usage": {"input_tokens": tokens}}}) + "\n")
        data = {"hook_event_name": event, "session_id": session, "cwd": "/work/proj",
                "transcript_path": str(transcript), **extra}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
        hook.main()
        out = capsys.readouterr().out
        return json.loads(out) if out.strip() else None
    return run


def lineages(hook):
    return sorted((hook.ROOT / "work-proj").glob("*-*"))


def append(path, text):
    with open(path, "a") as f:
        f.write(text)


def write_handoff(lineage, text="STATE\n"):
    # File mtimes come from a coarser clock than time.time(), so a handoff written right after
    # the notice could look older than it; date it explicitly.
    handoff = lineage / "handoff.md"
    handoff.write_text(text)
    later = time.time() + 5
    os.utime(handoff, (later, later))


def test_soft_notice_creates_a_lineage_with_a_sealed_log(hook, run):
    out = run("PostToolUse", tokens=450_000)

    [lineage] = lineages(hook)
    assert lineage.name.startswith("local-")
    context = out["hookSpecificOutput"]["additionalContext"]
    assert str(lineage / "handoff.md") in context
    assert str(lineage / "decisions.md") in context
    assert "append-only" in context
    assert "5. Decisions" not in context  # decisions moved from the handoff to the log
    log = (lineage / "decisions.md").read_text()
    assert log.startswith("# Decision log")
    assert (lineage / hook.SEALED).read_text() == log
    assert not list(hook.ROOT.glob("*.md"))  # nothing left in the old flat layout


def test_clear_consumes_the_handoff_into_the_same_lineage(hook, run):
    run("PostToolUse", tokens=450_000)
    [lineage] = lineages(hook)
    write_handoff(lineage, "STATE OF THE WORK\n")
    append(lineage / "decisions.md", "- D1 2026-10-26 14:00, user: \"keep it\". Kept it.\n")
    assert run("Stop", tokens=450_000) is None  # handoff and log written: nothing to ask

    out = run("SessionStart", session="s2", source="clear")

    context = out["hookSpecificOutput"]["additionalContext"]
    assert "STATE OF THE WORK" in context
    assert str(lineage / "decisions.md") in context
    assert not (lineage / "handoff.md").exists()
    assert (lineage / "gen-01.md").read_text() == "STATE OF THE WORK\n"
    log = (lineage / "decisions.md").read_text()
    assert "D1 2026-10-26 14:00" in log
    assert "## Generation 2: after /clear" in log and "from gen-01.md" in log
    assert (lineage / hook.SEALED).read_text() == log
    out = run("PostToolUse", session="s2", tokens=450_000)
    assert lineages(hook) == [lineage]
    assert str(lineage / "decisions.md") in out["hookSpecificOutput"]["additionalContext"]


def test_clear_without_a_pending_handoff_starts_a_new_lineage(hook, run, monkeypatch):
    run("PostToolUse", tokens=450_000)
    [old] = lineages(hook)
    monkeypatch.setenv("HANDOFF_LINEAGE", str(old))  # the process came from a takeover

    assert run("SessionStart", session="s2", source="clear") is None
    run("PostToolUse", session="s2", tokens=450_000)

    assert len(lineages(hook)) == 2


def test_takeover_startup_continues_the_lineage(hook, run, monkeypatch):
    run("PostToolUse", tokens=450_000)
    [lineage] = lineages(hook)
    monkeypatch.setenv("HANDOFF_LINEAGE", str(lineage))

    assert run("SessionStart", session="s2", source="startup") is None
    out = run("PostToolUse", session="s2", tokens=450_000)

    assert lineages(hook) == [lineage]
    assert str(lineage / "handoff.md") in out["hookSpecificOutput"]["additionalContext"]


def test_compaction_keeps_the_lineage_and_rearms_the_notices(hook, run):
    run("PostToolUse", tokens=450_000)
    [lineage] = lineages(hook)
    write_handoff(lineage)

    out = run("SessionStart", source="compact")
    assert "compaction summary" in out["hookSpecificOutput"]["additionalContext"]
    assert "## Generation 2: after compaction" in (lineage / "decisions.md").read_text()

    out = run("PostToolUse", tokens=450_000)
    assert lineages(hook) == [lineage]
    assert "soft handoff requested" in out["systemMessage"]


def test_stop_blocks_once_when_the_log_lost_entries(hook, run):
    run("PostToolUse", tokens=450_000)
    [lineage] = lineages(hook)
    (lineage / "decisions.md").write_text("# Decisions\n- D1 condensed\n")
    write_handoff(lineage)

    out = run("Stop", tokens=450_000)
    assert out["decision"] == "block"
    assert "edited or removed" in out["reason"]
    assert str(lineage / hook.SEALED) in out["reason"]
    assert run("Stop", tokens=450_000, stop_hook_active=True) is None


def test_stop_blocks_once_when_the_log_has_no_new_entries(hook, run):
    run("PostToolUse", tokens=450_000)
    [lineage] = lineages(hook)
    write_handoff(lineage)

    out = run("Stop", tokens=450_000)
    assert out["decision"] == "block"
    assert "no entries from this session" in out["reason"]
    assert run("Stop", tokens=450_000) is None


def test_state_from_the_flat_layout_gets_a_fresh_notice(hook, run):
    state = hook.ROOT / "state" / "s1.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"path": "/old/-work-proj.md", "soft": 1.0}))

    out = run("PostToolUse", tokens=450_000)

    [lineage] = lineages(hook)
    assert str(lineage / "handoff.md") in out["hookSpecificOutput"]["additionalContext"]


def test_herdr_stop_passes_the_sessions_lineage_to_the_respawn(hook, run, monkeypatch):
    monkeypatch.setattr(hook, "IN_HERDR", True)
    monkeypatch.setenv("HERDR_PANE_ID", PANE)
    monkeypatch.setenv("HANDOFF_LINEAGE", "/stale/lineage/from/before/a/clear")
    started = []
    monkeypatch.setattr(hook, "claude_pid", os.getpid)
    monkeypatch.setattr(hook.subprocess, "Popen", lambda args, **kw: started.append(args))
    run("PostToolUse", tokens=450_000)
    [lineage] = lineages(hook)
    assert lineage.name.startswith("w1-p1-")
    write_handoff(lineage)
    append(lineage / "decisions.md", "- D1 a decision\n")

    out = run("Stop", tokens=450_000, effort={"level": "xhigh"})

    assert "takes over this pane" in out["systemMessage"]
    [args] = started
    job = json.loads(args[-1])
    assert job["dir"] == str(lineage)
    assert job["env"] == {"HANDOFF_LINEAGE": str(lineage)}
    assert job["args"][-2:] == ["--effort", "xhigh"]


@pytest.fixture
def herdr_stub(tmp_path, monkeypatch):
    """Put the stub herdr on PATH; returns (calls(), configure(**stub.json))."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "herdr"
    stub.write_text(f"#!{sys.executable}\n{HERDR_STUB}")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def calls():
        path = bin_dir / "calls.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def configure(**cfg):
        (bin_dir / "stub.json").write_text(json.dumps(cfg))
    return calls, configure


@pytest.fixture
def takeover(hook, tmp_path, monkeypatch, herdr_stub):
    """A lineage with a written handoff and a respawn job for it; call it to run the respawn."""
    monkeypatch.setattr(hook.time, "sleep", lambda seconds: None)
    lineage = hook.new_lineage(hook.ROOT / "work-proj", "s1")
    write_handoff(lineage)
    append(lineage / "decisions.md", "- D1 a decision\n")
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text("")
    job = {"pane": PANE, "pid": os.getpid(), "dir": str(lineage),  # pid: not claude, not signalled
           "args": ["--model", "claude-opus-5-5"],
           "env": {"HANDOFF_LINEAGE": str(lineage), "CLAUDE_CONFIG_DIR": "/home/u/.claude-2"},
           "session": "s1", "transcript": str(transcript), "created": time.time()}
    state = hook.ROOT / "state" / "s1.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"dir": str(lineage), "soft": 1.0, "respawned": 2.0,
                                 "stop_tasks": True}))

    def go():
        hook.respawn(job)
        return lineage
    go.job, go.state, go.transcript = job, state, transcript
    return go


def prompts(calls):
    return [c[3] for c in calls() if c[:2] == ["agent", "prompt"] and c[3] != "/exit"]


def test_respawn_archives_into_the_lineage_and_relaunches_with_it(hook, takeover, herdr_stub):
    calls, _ = herdr_stub
    lineage = takeover()

    assert not (lineage / "handoff.md").exists()
    assert (lineage / "gen-01.md").read_text() == "STATE\n"
    log = (lineage / "decisions.md").read_text()
    assert "- D1 a decision\n" in log
    assert f"## Generation 2: takeover by w1 in herdr pane {PANE}" in log
    assert (lineage / hook.SEALED).read_text() == log
    [command] = [c[3] for c in calls() if c[:2] == ["pane", "run"]]
    assert command == (f"HANDOFF_LINEAGE={lineage} CLAUDE_CONFIG_DIR=/home/u/.claude-2 "
                       "claude --model claude-opus-5-5")
    [prompt] = prompts(calls)
    assert f"the decision log {lineage / 'decisions.md'} in full" in prompt  # a small log
    assert str(lineage / "gen-01.md") in prompt and "Read tool" in prompt
    assert ["agent", "wait", PANE, "--until", "working", "--timeout", "30000"] in calls()
    assert not [c for c in calls() if c[:2] == ["agent", "send-keys"]]
    assert "respawned w1" in (hook.ROOT / "respawn.log").read_text()
    [event] = [json.loads(x) for x in (hook.ROOT / "events.jsonl").read_text().splitlines()]
    assert event["kind"] == "respawn" and event["pane"] == PANE


def test_respawn_presses_enter_when_the_prompt_did_not_start_the_agent(hook, takeover,
                                                                       herdr_stub):
    calls, configure = herdr_stub
    configure(prompt_starts=False, enter_starts=True)

    takeover()

    assert [c for c in calls() if c[:2] == ["agent", "send-keys"]] == [
        ["agent", "send-keys", PANE, "enter"]]
    assert len(prompts(calls)) == 1
    assert "respawned w1" in (hook.ROOT / "respawn.log").read_text()


def test_respawn_fails_loudly_when_the_agent_never_starts(hook, takeover, herdr_stub, tmp_path,
                                                          monkeypatch):
    calls, configure = herdr_stub
    configure(prompt_starts=False, enter_starts=False)
    pushed = tmp_path / "pushed.txt"
    notify = tmp_path / "notify.py"
    notify.write_text(f"import sys; open({str(pushed)!r}, 'a').write('|'.join(sys.argv[1:]))")
    monkeypatch.setenv("ORCH_NOTIFY_CMD", f"{sys.executable} {notify}")

    takeover()

    assert len(prompts(calls)) == 2  # sent, Enter, sent again
    log = (hook.ROOT / "respawn.log").read_text()
    assert "FAILED: the new agent did not start on the takeover prompt" in log
    assert "respawned" not in log
    assert any(c[:3] == ["notification", "show", "Handoff respawn failed"] for c in calls())
    assert pushed.read_text().startswith("Handoff respawn failed|w1:p1: the new agent")


def test_respawn_postpones_when_background_tasks_started_after_the_handoff(hook, takeover,
                                                                          herdr_stub):
    calls, _ = herdr_stub
    status = hook.status_path(PANE)
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"session": "s1", "time": time.time() + 5, "background_tasks": [
        {"type": "bash", "id": "b1", "what": "wait for worker"}]}))

    lineage = takeover()

    assert ["agent", "prompt", PANE, "/exit"] not in calls()
    assert (lineage / "handoff.md").exists()  # not consumed
    state = json.loads(takeover.state.read_text())
    assert "respawned" not in state and "stop_tasks" not in state  # the next Stop retries
    assert "postponed: 1 background task(s)" in (hook.ROOT / "respawn.log").read_text()


def test_respawn_ignores_background_tasks_from_before_the_handoff(hook, takeover, herdr_stub):
    calls, _ = herdr_stub
    status = hook.status_path(PANE)
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"session": "s1", "time": time.time() - 60,
                                  "background_tasks": [{"type": "bash"}]}))

    takeover()

    assert ["agent", "prompt", PANE, "/exit"] in calls()


def test_respawn_postpones_while_the_old_agent_keeps_working(hook, takeover, herdr_stub):
    calls, configure = herdr_stub
    configure(old="working")  # e.g. a /goal Stop hook keeps the turn going

    lineage = takeover()

    assert ["agent", "prompt", PANE, "/exit"] not in calls()
    assert (lineage / "handoff.md").exists()
    assert "postponed: the agent was still working" in (hook.ROOT / "respawn.log").read_text()


def test_respawn_sets_the_users_goal_again(hook, takeover, herdr_stub):
    calls, _ = herdr_stub
    takeover.transcript.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"content": "<command-name>/goal</command-name>\n"
                                                 "<command-args>old goal</command-args>"}},
        {"type": "attachment", "attachment": {"type": "goal_status", "met": False,
                                               "condition": "work until 08:00"}},
    ]) + "\n")

    takeover()

    first, second = prompts(calls)
    assert 'the user\'s /goal "work until 08:00"' in first
    assert second == "/goal work until 08:00"
    assert "/goal set again" in (hook.ROOT / "respawn.log").read_text()


@pytest.mark.parametrize("lines, goal", [
    ([{"type": "attachment", "attachment": {"type": "goal_status", "met": True,
                                             "condition": "x"}}], None),
    ([{"type": "attachment", "attachment": {"type": "goal_status", "met": False,
                                             "condition": "x"}},
      {"type": "user", "message": {"content": "<command-name>/goal</command-name>"
                                               "<command-args>clear</command-args>"}}], None),
    ([{"type": "user", "message": {"content": "<command-name>/goal</command-name>"
                                               "<command-args>ship it</command-args>"}}],
     "ship it"),
    ([], None),
])
def test_active_goal(hook, tmp_path, lines, goal):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(json.dumps(x) + "\n" for x in lines))
    assert hook.active_goal(str(transcript)) == goal


def test_takeover_of_a_large_log_reads_its_index_and_the_latest_generations(hook, takeover,
                                                                          herdr_stub):
    calls, _ = herdr_stub
    lineage = Path(takeover.job["dir"])
    old = "".join(f"- **D{n}** 2026-11-04, agent: decision {n} " + "x" * 600 + "\n"
                  for n in range(2, 130))  # past RECENT_LIMIT with generation 1
    append(lineage / "decisions.md", old + "\n## Generation 2: takeover\n- **D80** latest\n")
    hook.seal(lineage)

    takeover()

    [prompt] = prompts(calls)
    index = lineage / "decisions-index.md"
    assert str(index) in prompt
    lines = (lineage / "decisions.md").read_text().splitlines()
    start = lines.index("## Generation 2: takeover") + 1  # the generation that just finished
    assert f"from line {start} to the end" in prompt
    text = index.read_text()
    assert "- **D1** a decision" not in text  # "- D1" without bold still counts as an entry
    assert "- D1 a decision" in text and "- **D80** latest" in text
    assert max(len(x) for x in text.splitlines()) <= 160 + 60  # the header line is longer
    assert "\n## Generation 2: takeover\n" in text and "takeover by w1 in herdr pane" in text


def test_takeover_prompt_carries_the_panes_orch_run(hook, takeover, herdr_stub):
    calls, _ = herdr_stub
    run = hook.ROOT / "runs" / "w1-p1.json"
    run.parent.mkdir(parents=True)
    run.write_text(json.dumps({"orchestrator": PANE, "until": time.time() + 3600,
                               "goal": "fix the board", "ended": None}))

    takeover()

    [prompt] = prompts(calls)
    assert "orch status" in prompt and '"fix the board"' in prompt


def test_stop_in_herdr_records_the_agents_turn_end(hook, run, tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "IN_HERDR", True)
    monkeypatch.setenv("HERDR_PANE_ID", PANE)
    transcript_line = {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [
        {"type": "text", "text": "Task 3 is done: 2 commits, tests pass."}],
        "usage": {"input_tokens": 1000}}}

    run("Stop", tokens=1000, background_tasks=[{"type": "bash", "id": "b7",
                                                "description": "pytest under the lock"}])
    status = json.loads(hook.status_path(PANE).read_text())
    assert status["session"] == "s1" and status["event"] == "Stop"
    assert status["background_tasks"] == [{"type": "bash", "id": "b7",
                                           "what": "pytest under the lock"}]

    (tmp_path / "s2.jsonl").write_text(json.dumps(transcript_line) + "\n")
    monkeypatch.setattr(hook, "last_call", lambda path: (1000, "claude-opus-5-5"))
    data = {"hook_event_name": "Stop", "session_id": "s2", "cwd": "/work/proj",
            "transcript_path": str(tmp_path / "s2.jsonl")}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    hook.main()
    status = json.loads(hook.status_path(PANE).read_text())
    assert status["background_tasks"] == []
    assert status["last"] == "Task 3 is done: 2 commits, tests pass."
