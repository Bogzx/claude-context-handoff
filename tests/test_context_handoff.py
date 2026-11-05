"""Offline tests for hooks/context-handoff.py: run with `uv run --with pytest pytest -q`."""
import importlib.util
import io
import json
import os
import subprocess
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
    st["new"] = cfg.get("new", st["new"])
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


@pytest.mark.parametrize("accounts, env", [
    ({}, {"HANDOFF_LINEAGE": "L", "CLAUDE_CONFIG_DIR": "/home/u/.claude-2"}),  # no rule: kept
    ({"spill": {"from": "1", "at": 98, "to": "2", "active": {"used": 98}}},
     {"HANDOFF_LINEAGE": "L", "CLAUDE_CONFIG_DIR": "ACC2"}),  # spilling: the target
    ({"spill": {"from": "1", "at": 98, "to": "2", "active": None}},
     {"HANDOFF_LINEAGE": "L"}),  # after the reset: the default account, 1
    ({"spill": {"from": "1", "at": 98, "to": "2", "active": None}, "default": "2"},
     {"HANDOFF_LINEAGE": "L", "CLAUDE_CONFIG_DIR": "ACC2"}),
    ({"switch": {PANE: {"account": "1", "when": "handoff"}},
      "spill": {"from": "1", "at": 98, "to": "2", "active": {"used": 98}}},
     {"HANDOFF_LINEAGE": "L"}),  # a queued switch wins
])
def test_the_takeover_account_follows_the_switch_then_the_spill_rule(hook, monkeypatch,
                                                                     accounts, env):
    monkeypatch.setenv("HERDR_PANE_ID", PANE)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/u/.claude-2")
    monkeypatch.setattr(hook, "claude_pid", os.getpid)
    monkeypatch.setattr(hook, "account_dir", lambda n: "ACC" + str(n))
    started = []
    monkeypatch.setattr(hook.subprocess, "Popen", lambda args, **kw: started.append(args))
    hook.ROOT.mkdir(parents=True, exist_ok=True)
    (hook.ROOT / "accounts.json").write_text(json.dumps(accounts))

    assert hook.start_respawn({"session_id": "s1"}, Path("L"), "claude-opus-5-5")

    assert json.loads(started[0][-1])["env"] == env


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


# --- turn-end reports for orch runs ---------------------------------------------------------

def entry(kind, content, **extra):
    return {"type": kind, "uuid": extra.pop("uuid", None) or f"u{abs(hash(str(content))) % 10**8}",
            "message": {"role": kind, "content": content, "model": "claude-opus-5-5",
                        "usage": {"input_tokens": 1000}}, **extra}


def tool(name):
    return entry("assistant", [{"type": "tool_use", "id": "t", "name": name, "input": {}}])


RESULT = entry("user", [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}])


@pytest.fixture
def in_run(hook, tmp_path, monkeypatch, capsys):
    """This pane is a worker of an active run, its session (this process standing in for
    claude) has the orch MCP server loaded as a child process, and stop() ends a turn whose
    transcript is the given entries. stop.unload() ends the server."""
    monkeypatch.setattr(hook, "IN_HERDR", True)
    monkeypatch.setattr(hook, "claude_pid", os.getpid)
    monkeypatch.setenv("HERDR_PANE_ID", PANE)
    server = tmp_path / "orch_mcp.py"
    server.write_text("import time\ntime.sleep(120)\n")
    child = subprocess.Popen([sys.executable, str(server)])  # returns after the exec
    config = tmp_path / "config"
    config.mkdir()
    (config / ".claude.json").write_text(json.dumps({"mcpServers": {"orch": {"command": "x"}}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    run = hook.ROOT / "runs" / "w1-p0.json"
    run.parent.mkdir(parents=True)
    run.write_text(json.dumps({"orchestrator": "w1:p0", "until": time.time() + 3600,
                               "ended": None, "workers": {PANE: {"label": "builder"}}}))
    transcript = tmp_path / "s1.jsonl"

    def stop(*entries, **extra):
        transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))
        data = {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/work/proj",
                "transcript_path": str(transcript), **extra}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
        hook.main()
        out = capsys.readouterr().out
        return json.loads(out) if out.strip() else None
    def unload():
        child.kill()
        child.wait()
    stop.run = run
    stop.config = config
    stop.unload = unload
    yield stop
    unload()


def test_a_working_turn_without_a_report_is_blocked_once(in_run):
    turn = [entry("user", "fix #12", uuid="p1"), tool("Bash"), RESULT,
            entry("assistant", [{"type": "text", "text": "done"}])]

    out = in_run(*turn)
    assert out["decision"] == "block"
    assert "worker turn used tools but sent no report" in out["reason"]
    assert "mcp__orch__report" in out["reason"] and "\n" not in out["reason"]
    # The agent continues; its Stop then comes with stop_hook_active, or (another hook's
    # continuation) without it: neither is blocked again for the same turn.
    feedback = entry("user", "Stop hook feedback: ...", isMeta=True)
    assert in_run(*turn, feedback, entry("assistant", [{"type": "text", "text": "ok"}]),
                  stop_hook_active=True) is None
    assert in_run(*turn, feedback, entry("assistant", [{"type": "text", "text": "ok"}])) is None
    # The next working turn without a report is blocked again.
    assert in_run(*turn, entry("user", "next task", uuid="p2"), tool("Edit"), RESULT)


def test_a_turn_that_reported_is_not_blocked(in_run):
    assert in_run(entry("user", "fix #12"), tool("Bash"), RESULT, tool("mcp__orch__report"),
                  RESULT, entry("assistant", [{"type": "text", "text": "done"}])) is None


def test_a_chat_only_turn_is_not_blocked(in_run):
    assert in_run(entry("user", "fix #12"), tool("Bash"), RESULT,
                  entry("user", "what is the status?", uuid="p2"),
                  entry("assistant", [{"type": "text", "text": "3 of 5 done"}])) is None


def test_no_report_is_asked_outside_an_active_run(in_run):
    turn = [entry("user", "fix #12"), tool("Bash"), RESULT]
    in_run.run.write_text(json.dumps({"orchestrator": "w1:p0", "until": time.time() + 3600,
                                      "ended": time.time() - 5, "workers": {PANE: {}}}))
    assert in_run(*turn) is None
    in_run.run.write_text(json.dumps({"orchestrator": "w1:p0", "until": time.time() + 3600,
                                      "ended": None, "workers": {"w1:p7": {}}}))
    assert in_run(*turn) is None


def test_no_report_is_asked_when_the_session_has_not_loaded_the_orch_server(in_run):
    # Registered in the account's config (the fixture's .claude.json) but not running in this
    # session: it started before the install, so it can't call the tool.
    in_run.unload()
    assert in_run(entry("user", "fix #12"), tool("Bash"), RESULT) is None


def test_orch_loaded_needs_the_server_as_a_child_of_that_claude_process(hook, in_run):
    assert hook.orch_loaded(os.getpid())
    assert not hook.orch_loaded(os.getppid())
    assert not hook.orch_loaded(None)
    # A shell command that only mentions the file is not the server.
    shell = subprocess.Popen(["sh", "-c", "sleep 120; echo orch_mcp.py"])
    try:
        in_run.unload()
        assert not hook.orch_loaded(os.getpid())
    finally:
        shell.kill()
        shell.wait()


def test_no_report_is_asked_while_a_handoff_is_underway(hook, in_run):
    state = hook.ROOT / "state" / "s1.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"dir": "/nowhere", "soft": 1.0, "stop_soft": True,
                                 "respawned": 2.0}))  # the takeover is starting
    assert in_run(entry("user", "fix #12"), tool("Bash"), RESULT) is None


def big(text="done"):  # an assistant entry of a 300K-token context
    e = entry("assistant", [{"type": "text", "text": text}])
    e["message"]["usage"] = {"input_tokens": 1000, "cache_read_input_tokens": 299_000}
    return e


def test_a_pending_idle_switch_asks_for_the_handoff_at_the_turn_end(hook, in_run, monkeypatch):
    accounts = hook.ROOT / "accounts.json"
    accounts.write_text(json.dumps({"switch": {PANE: {
        "account": "2", "time": 1.0, "when": "idle", "reason": "spill",
        "why": "account 1 is at 98% of its seven_day window"}}}))
    started = []
    monkeypatch.setattr(hook.subprocess, "Popen", lambda args, **kw: started.append(args))

    out = in_run(entry("user", "fix #12"), tool("Bash"), RESULT, big())

    assert out["decision"] == "block" and "moving this agent to Claude account 2" in out["reason"]
    assert "account 1 is at 98%" in out["reason"]
    state = json.loads((hook.ROOT / "state" / "s1.json").read_text())
    lineage = Path(state["dir"])
    assert lineage.name.startswith("w1-p1-") and str(lineage / "handoff.md") in out["reason"]
    assert json.loads(accounts.read_text())["switch"][PANE]["handoff"] == pytest.approx(
        time.time(), abs=60)
    # Once asked, the next turn end is the report's, and the handoff starts the takeover with
    # the switch (start_respawn applies the pending account).
    assert "sent no report" in in_run(entry("user", "go on", uuid="p2"), tool("Bash"), RESULT,
                                      big())["reason"]
    write_handoff(lineage)
    append(lineage / "decisions.md", "- D1 moved to account 2\n")
    out = in_run(entry("user", "go on", uuid="p3"), tool("Write"), RESULT, big())
    assert "takes over this pane" in out["systemMessage"]
    [args] = started
    assert json.loads(args[-1])["account"] == "2"


@pytest.mark.parametrize("switch, tokens", [
    ({"account": "2", "time": 1.0, "when": "idle"}, 20_000),  # small: orch resumes it
    ({"account": "2", "time": 1.0, "when": "idle", "reason": "limit"}, 300_000),
    ({"account": "2", "time": 1.0, "when": "handoff"}, 300_000),  # at its own handoff
    ({"account": "1", "time": 1.0, "when": "idle"}, 300_000),  # already there
])
def test_other_switches_dont_ask_for_a_handoff(hook, in_run, switch, tokens, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")  # account 1
    (hook.ROOT / "accounts.json").write_text(json.dumps({"switch": {PANE: switch}}))
    last = big()
    last["message"]["usage"] = {"input_tokens": tokens}
    out = in_run(entry("user", "fix #12"), tool("Bash"), RESULT, last)
    assert "Claude account" not in out["reason"]  # the report rule's block instead


def test_request_handoff_prepares_the_lineage_and_prints_the_request(hook, capsys):
    job = {"pane": "w1:p4", "session": "s9", "cwd": "/work/proj", "account": "2",
           "why": "queued by orch account --when-idle"}
    assert hook.request_handoff(job) == 0
    text = capsys.readouterr().out
    state = json.loads((hook.ROOT / "state" / "s9.json").read_text())
    lineage = Path(state["dir"])
    assert lineage.parent == hook.ROOT / "work-proj" and lineage.name.startswith("w1-p4-")
    assert (lineage / "decisions.md").exists() and "soft" in state
    assert f"It is written now to {lineage / 'handoff.md'}" in text
    assert "takes over this herdr pane" in text  # the herdr wording, even outside herdr
    state["respawned"] = 5.0  # a takeover is starting: nothing to ask
    (hook.ROOT / "state" / "s9.json").write_text(json.dumps(state))
    assert hook.request_handoff(job) == 1


def test_the_orchestrator_is_asked_for_its_fields(in_run):
    in_run.run.write_text(json.dumps({"orchestrator": PANE, "until": time.time() + 3600,
                                      "ended": None, "workers": {}}))
    out = in_run(entry("user", "orch wait returned"), tool("Bash"), RESULT)
    assert "this orchestrator turn" in out["reason"]


def test_the_status_file_keeps_the_report_and_records_the_account(hook, run, monkeypatch,
                                                                   tmp_path):
    monkeypatch.setattr(hook, "IN_HERDR", True)
    monkeypatch.setenv("HERDR_PANE_ID", PANE)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude-2"))
    monkeypatch.setattr(hook, "claude_pid", os.getpid)  # cmdline: this pytest
    path = hook.status_path(PANE)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pane": PANE, "report": {"summary": "fixed #12"}}))

    run("Stop", tokens=1000, effort={"level": "high"})

    status = json.loads(path.read_text())
    assert status["report"] == {"summary": "fixed #12"} and status["account"] == "2"
    assert status["pid"] == os.getpid() and status["relaunch"][-2:] == ["--effort", "high"]
    assert status["relaunch"][-4:-2] == ["--model", "claude-opus-5-5"]


@pytest.mark.parametrize("config_dir, account", [
    (None, "1"), ("~/.claude", "1"), ("~/.claude-2", "2"), ("~/.claude-2/", "2"),
    ("/opt/other", "/opt/other")])
def test_account_of(hook, config_dir, account):
    assert hook.account_of(config_dir) == account


# --- account switches ---------------------------------------------------------------------------

def test_a_queued_account_switch_applies_at_the_takeover(hook, run, monkeypatch, tmp_path,
                                                         herdr_stub):
    monkeypatch.setattr(hook, "IN_HERDR", True)
    monkeypatch.setenv("HERDR_PANE_ID", PANE)
    monkeypatch.setenv("HOME", str(tmp_path))
    started, popen = [], hook.subprocess.Popen
    monkeypatch.setattr(hook, "claude_pid", os.getpid)
    monkeypatch.setattr(hook.subprocess, "Popen", lambda args, **kw: started.append(args))
    (hook.ROOT / "accounts.json").parent.mkdir(parents=True, exist_ok=True)
    (hook.ROOT / "accounts.json").write_text(json.dumps({"switch": {PANE: {"account": "2"}}}))
    run("PostToolUse", tokens=450_000)
    [lineage] = lineages(hook)
    write_handoff(lineage)
    append(lineage / "decisions.md", "- D1 a decision\n")

    run("Stop", tokens=450_000)

    job = json.loads(started[0][-1])
    assert job["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude-2")
    assert job["account"] == "2"
    monkeypatch.setattr(hook.subprocess, "Popen", popen)  # the respawn runs herdr for real
    monkeypatch.setattr(hook.time, "sleep", lambda seconds: None)
    job["pid"] = os.getpid()  # not claude: nothing is signalled
    assert hook.respawn(job) is True
    calls, _ = herdr_stub
    [command] = [c[3] for c in calls() if c[:2] == ["pane", "run"]]
    assert f"CLAUDE_CONFIG_DIR={tmp_path / '.claude-2'} claude " in command
    assert json.loads((hook.ROOT / "accounts.json").read_text())["switch"] == {}
    events = [json.loads(x) for x in (hook.ROOT / "events.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in events] == ["respawn", "account"]
    assert events[-1]["account"] == "2" and events[-1]["when"] == "handoff"


@pytest.fixture
def switch(hook, tmp_path, monkeypatch, herdr_stub):
    """An idle agent in the pane and a resume job for account 2, as orch account --now sends."""
    monkeypatch.setattr(hook.time, "sleep", lambda seconds: None)
    acc = tmp_path / "bin" / "claude-acc"
    acc.write_text("#!/bin/sh\n")
    acc.chmod(0o755)
    job = {"pane": PANE, "pid": os.getpid(), "resume": "s1", "account": "2",
           "args": ["--dangerously-skip-permissions", "--model", "claude-haiku-5-5"],
           "session": "s1", "created": 0}
    return job, acc


def test_account_switch_now_resumes_the_session_on_the_other_account(hook, switch, herdr_stub):
    job, acc = switch
    calls, _ = herdr_stub

    assert hook.respawn(job) is True

    assert ["agent", "prompt", PANE, "/exit"] in calls()
    [command] = [c[3] for c in calls() if c[:2] == ["pane", "run"]]
    assert command == f"{acc} 2 --resume s1 --model claude-haiku-5-5"
    assert ["agent", "rename", PANE, "w1"] in calls()
    assert prompts(calls) == []  # no takeover prompt: the resumed agent waits
    [event] = [json.loads(x) for x in (hook.ROOT / "events.jsonl").read_text().splitlines()]
    assert event["kind"] == "account" and event["when"] == "now" and event["account"] == "2"
    hook.respawn(dict(job, when="idle"))  # carried out by the watchdog for --when-idle
    assert json.loads((hook.ROOT / "events.jsonl").read_text().splitlines()[-1])["when"] == "idle"
    assert "switched w1 to account 2" in (hook.ROOT / "respawn.log").read_text()


def test_account_switch_now_refuses_a_busy_agent(hook, switch, herdr_stub, capsys):
    job, _ = switch
    calls, configure = herdr_stub
    configure(old="working")

    assert hook.respawn(job) is False

    assert ["agent", "prompt", PANE, "/exit"] not in calls()
    assert "not idle" in capsys.readouterr().err
    assert not (hook.ROOT / "events.jsonl").exists()


def test_account_switch_now_refuses_with_background_tasks(hook, switch, herdr_stub):
    job, _ = switch
    calls, _ = herdr_stub
    status = hook.status_path(PANE)
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"session": "s1", "time": time.time() - 600,
                                  "background_tasks": [{"type": "bash"}]}))

    assert hook.respawn(job) is False
    assert ["agent", "prompt", PANE, "/exit"] not in calls()


def test_a_takeover_records_a_spill_move(hook, takeover, herdr_stub):
    takeover.job.update(account="2", reason="spill")
    (hook.ROOT / "accounts.json").write_text(json.dumps({"switch": {PANE: {
        "account": "2", "when": "idle", "reason": "spill"}}}))

    takeover()

    acc = json.loads((hook.ROOT / "accounts.json").read_text())
    assert acc["switch"] == {} and acc["moved"][PANE]["how"] == "handoff"
    assert acc["moved"][PANE]["reason"] == "spill"


def test_a_takeover_waits_for_a_dialog_on_the_new_account(hook, takeover, herdr_stub,
                                                           monkeypatch):
    calls, configure = herdr_stub
    configure(new="blocked")  # account 2 asks to trust the folder; nobody answers
    clock = iter(range(0, 10 ** 6, 5))
    monkeypatch.setattr(hook.time, "time", lambda: float(next(clock)))

    assert hook.respawn(takeover.job) is False

    events = [json.loads(x) for x in (hook.ROOT / "events.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in events] == ["respawn-dialog", "respawn-failed"]
    assert "folder trust" in events[0]["body"]
    polls = len([c for c in calls() if c[:2] == ["agent", "list"]])
    assert polls > hook.DIALOG_WAIT / 5 / 2  # it kept waiting well past the 90 s start
    assert prompts(calls) == []  # no takeover prompt into a dialog


def test_account_switch_now_reports_a_dialog_in_the_new_agent(hook, switch, herdr_stub, capsys):
    job, _ = switch
    calls, configure = herdr_stub
    configure(new="blocked")  # e.g. account 2 has not trusted this folder yet

    assert hook.respawn(job) is False

    err = capsys.readouterr().err
    assert "at a dialog in the pane" in err and "folder trust" in err
    assert len([c for c in calls() if c[:2] == ["agent", "list"]]) < 10  # did not wait 90 s
    [event] = [json.loads(x) for x in (hook.ROOT / "events.jsonl").read_text().splitlines()]
    assert event["kind"] == "account-failed"
