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

# A stand-in for the herdr CLI: logs each call, reports the pane free until `pane run`, then
# an idle agent in it.
HERDR_STUB = """
import json, sys
from pathlib import Path
here = Path(__file__).parent
args = sys.argv[1:]
with open(here / "calls.jsonl", "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[:2] == ["agent", "get"]:
    print(json.dumps({"result": {"agent": {"name": "w1"}}}))
elif args[:2] == ["agent", "list"]:
    ran = (here / "ran").exists()
    agents = [{"pane_id": "w1:p1", "agent_status": "idle"}] if ran else []
    print(json.dumps({"result": {"agents": agents}}))
elif args[:2] == ["pane", "run"]:
    (here / "ran").touch()
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


def test_respawn_archives_into_the_lineage_and_relaunches_with_it(hook, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "herdr"
    stub.write_text(f"#!{sys.executable}\n{HERDR_STUB}")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(hook.time, "sleep", lambda seconds: None)
    lineage = hook.new_lineage(hook.ROOT / "work-proj", "s1")
    write_handoff(lineage)
    append(lineage / "decisions.md", "- D1 a decision\n")
    env = {"HANDOFF_LINEAGE": str(lineage), "CLAUDE_CONFIG_DIR": "/home/u/.claude-2"}

    # pid: this test process, which is not claude, so nothing is signalled
    hook.respawn({"pane": PANE, "pid": os.getpid(), "dir": str(lineage),
                  "args": ["--model", "claude-opus-5-5"], "env": env})

    assert not (lineage / "handoff.md").exists()
    assert (lineage / "gen-01.md").read_text() == "STATE\n"
    log = (lineage / "decisions.md").read_text()
    assert "- D1 a decision\n" in log
    assert f"## Generation 2: takeover by w1 in herdr pane {PANE}" in log
    assert (lineage / hook.SEALED).read_text() == log
    calls = [json.loads(line) for line in (bin_dir / "calls.jsonl").read_text().splitlines()]
    [command] = [c[3] for c in calls if c[:2] == ["pane", "run"]]
    assert command == (f"HANDOFF_LINEAGE={lineage} CLAUDE_CONFIG_DIR=/home/u/.claude-2 "
                       "claude --model claude-opus-5-5")
    prompt = [c[3] for c in calls if c[:2] == ["agent", "prompt"] and c[3] != "/exit"][-1]
    assert str(lineage / "decisions.md") in prompt and str(lineage / "gen-01.md") in prompt
    assert "respawned w1" in (hook.ROOT / "respawn.log").read_text()
