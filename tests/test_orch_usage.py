"""Offline tests for hooks/orch_usage.py (orch usage): statusline capture, windows, transcripts."""
import importlib.util
import json
import time
from pathlib import Path

import pytest

USAGE = Path(__file__).resolve().parent.parent / "hooks" / "orch_usage.py"
T0 = 1_793_870_000.0  # 2026-11-05, a fixed clock
HOUR = 3600


@pytest.fixture
def u(tmp_path):
    spec = importlib.util.spec_from_file_location("orch_usage", USAGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.root = tmp_path / "handoffs"
    module.root.mkdir()
    module.projects = tmp_path / "projects"
    module.projects.mkdir()
    return module


def statusline(session="s1", five=20.0, five_reset=T0 + 3 * HOUR, week=60.0,
               week_reset=T0 + 4 * 86400):
    return json.dumps({"session_id": session, "model": {"id": "claude-opus-5-5"},
                       "rate_limits": {
                           "five_hour": {"used_percentage": five, "resets_at": five_reset},
                           "seven_day": {"used_percentage": week, "resets_at": week_reset}}})


def history(u):
    path = u.root / "usage-history.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


# --- capture --------------------------------------------------------------------------------------

def test_capture_keeps_the_latest_per_account_and_history_every_5_minutes(u):
    u.capture(statusline(five=20.0), {}, u.root, T0)
    u.capture(statusline(five=21.0), {}, u.root, T0 + 30)  # changed: latest, no history line
    u.capture(statusline(session="s2", five=70.0),
              {"CLAUDE_CONFIG_DIR": str(Path.home() / ".claude-2")}, u.root, T0 + 40)
    u.capture(statusline(five=22.0), {}, u.root, T0 + 301)

    cur = json.loads((u.root / "usage.json").read_text())
    assert cur["accounts"]["1"]["windows"]["five_hour"]["used_percentage"] == 22.0
    assert cur["accounts"]["2"]["windows"]["five_hour"]["used_percentage"] == 70.0
    assert cur["sessions"] == {"s1": {"account": "1", "time": T0},
                               "s2": {"account": "2", "time": T0 + 40}}
    assert [(h["time"], h["account"]) for h in history(u)] == [
        (T0, "1"), (T0 + 40, "2"), (T0 + 301, "1")]


def test_capture_creates_its_folder(u):
    root = u.root / "not-yet"
    u.capture(statusline(), {}, root, T0)
    assert json.loads((root / "usage.json").read_text())["accounts"]["1"]["time"] == T0
    assert len((root / "usage-history.jsonl").read_text().splitlines()) == 1


def test_capture_starts_a_new_line_after_a_torn_history_record(u):
    # a capture killed mid-write leaves a record without its newline (seen cut at a page edge)
    (u.root / "usage-history.jsonl").write_text(
        json.dumps({"time": T0 - 600, "account": "1", "windows": {}}) + "\n"
        + '{"time": %s, "account": "1", "windows": {"seven_day": {"resets_at": 179' % (T0 - 5))
    u.capture(statusline(), {}, u.root, T0)
    assert [h["time"] for h in u.history(u.root, 0)] == [T0 - 600, T0]


def test_capture_does_not_rewrite_an_unchanged_file(u):
    u.capture(statusline(), {}, u.root, T0)
    mtime = (u.root / "usage.json").stat().st_mtime_ns
    u.capture(statusline(), {}, u.root, T0 + 20)
    assert (u.root / "usage.json").stat().st_mtime_ns == mtime
    u.capture(statusline(), {}, u.root, T0 + 61)  # refreshed once a minute: freshness
    assert json.loads((u.root / "usage.json").read_text())["accounts"]["1"]["time"] == T0 + 61


def test_capture_is_silent_on_bad_input(u, capsys, monkeypatch):
    import io
    import sys
    for text in ("", "not json", "[1, 2]", json.dumps({"session_id": "s1"})):
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))
        assert u.main(type("A", (), {"action": "capture"})(), u.root, T0, dict) == 0
    assert capsys.readouterr() == ("", "")
    assert "accounts" not in json.loads((u.root / "usage.json").read_text())


# --- windows --------------------------------------------------------------------------------------

def feed(u, points, account_env=None):
    for t, five in points:
        u.capture(statusline(five=five), account_env or {}, u.root, t)


def test_burn_rate_and_projection_before_the_reset(u):
    # 10% an hour over the last 2 h, 40% used, the window resets in 3 h: full in 6 h, after it.
    feed(u, [(T0 - 2 * HOUR, 20.0), (T0 - HOUR, 30.0), (T0, 40.0)])
    w = u.windows_report(u.root, T0)["1"]["windows"]["five_hour"]
    assert w["burn_per_hour"] == 10.0 and w["basis_minutes"] == 120
    assert w["projected_full_at"] == T0 + 6 * HOUR and w["full_before_reset"] is False

    feed(u, [(T0 + 600, 50.0)])  # 30% in 2h10: 8.31%/h, full in ~6 h, still after the reset
    feed(u, [(T0 + 900, 75.0)])  # a burst: 55% in 2h15, 24.4%/h: full in ~1 h, before it
    w = u.windows_report(u.root, T0 + 900)["1"]["windows"]["five_hour"]
    assert w["full_before_reset"] is True
    assert w["projected_full_at"] < T0 + 3 * HOUR


def test_burn_rate_needs_enough_history_in_the_same_window(u):
    feed(u, [(T0 - 2 * HOUR, 90.0)])  # the previous window (another reset time)
    u.capture(statusline(five=5.0, five_reset=T0 + 5 * HOUR), {}, u.root, T0 - 600)
    u.capture(statusline(five=8.0, five_reset=T0 + 5 * HOUR), {}, u.root, T0)
    w = u.windows_report(u.root, T0)["1"]["windows"]["five_hour"]
    assert w["used_percentage"] == 8.0 and w["burn_per_hour"] is None  # 10 min is too little


def test_a_window_past_its_reset_is_marked(u):
    u.capture(statusline(five=97.0, five_reset=T0 + 60), {}, u.root, T0)
    acc = u.windows_report(u.root, T0 + 120)["1"]
    assert acc["windows"]["five_hour"]["reset"] is True and acc["stale"] is False
    assert u.windows_report(u.root, T0 + 3600)["1"]["stale"] is True


# --- transcripts ----------------------------------------------------------------------------------

def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def assistant(mid, t, model="claude-opus-5-5", inp=10, out=100, read=1000, w5=0, w1=200,
              blocks=1, **extra):
    """One API message as Claude Code writes it: a line per content block, same usage."""
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
             "cache_creation_input_tokens": w5 + w1,
             "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1},
             **extra}
    return [{"type": "assistant", "timestamp": stamp(t), "requestId": "r" + mid,
             "message": {"id": mid, "model": model, "usage": usage,
                         "content": [{"type": "text", "text": "x"}]}}] * blocks


def write(path, entries, mode="a"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode) as f:
        f.write("".join(json.dumps(e) + "\n" for e in entries))


def test_price(u):
    opus = u.price("claude-opus-5-5", [1_000_000, 0, 0, 0, 0], False)
    assert opus == pytest.approx(4.0)
    assert u.price("claude-opus-5-5", [0, 1_000_000, 1_000_000, 1_000_000, 1_000_000],
                   False) == pytest.approx(20 + 0.2 + 5 + 8)
    assert u.price("claude-opus-5-5", [1_000_000, 0, 0, 0, 0], True) == pytest.approx(8.0)
    assert u.price("claude-haiku-5-5", [50_000, 0, 0, 0, 0], False) == pytest.approx(0.005)
    assert u.price("claude-haiku-5-5", [150_000, 0, 0, 0, 0], False) == pytest.approx(0.075)
    assert u.price("claude-fable-5-1-20261101", [0, 0, 1_000_000, 0, 0], False) == 0.25
    assert u.price("claude-unknown-9", [1, 1, 1, 1, 1], False) is None


def test_transcripts_are_counted_once_per_message_and_incrementally(u):
    main = u.projects / "-work-proj" / "s1.jsonl"
    write(main, assistant("m1", T0, blocks=3)  # thinking, text, tool_use: one message
          + [{"type": "user", "timestamp": stamp(T0), "message": {"content": "hi"}}]
          + assistant("m2", T0 + 60, out=50, w1=0, read=1210))
    sub = u.projects / "-work-proj" / "s1" / "subagents" / "agent-a1.jsonl"
    write(sub, assistant("m3", T0 + 120, model="claude-haiku-5-5", read=0, w1=0))

    cache = u.scan(u.root, u.projects, T0 + 600, {})
    totals = {}
    for slots in cache["buckets"]["s1"].values():
        for model, b in slots.items():
            t = totals.setdefault(model, {k: 0 for k in u.FIELDS})
            for k in u.FIELDS:
                t[k] += b[k]
    opus, haiku = totals["claude-opus-5-5"], totals["claude-haiku-5-5"]
    assert (opus["messages"], opus["input"], opus["output"]) == (2, 20, 150)
    assert (opus["cache_read"], opus["cache_write"]) == (2210, 200)
    assert opus["cost"] == pytest.approx((20 * 4 + 150 * 20 + 2210 * 0.2 + 200 * 8) / 1e6)
    assert (haiku["messages"], haiku["output"]) == (1, 100)

    # A message still being written: its first block now, the rest (with more output) later.
    write(main, assistant("m4", T0 + 700, out=10))
    with open(main, "a") as f:
        f.write('{"type": "assistant", "partial line')  # no newline yet: left for next time
    cache = u.scan(u.root, u.projects, T0 + 800, {})
    m4 = cache["buckets"]["s1"][str(int((T0 + 700) // 600 * 600))]["claude-opus-5-5"]
    assert (m4["messages"], m4["output"]) == (1, 10)
    with open(main, "a") as f:
        f.write('"}\n')
    write(main, assistant("m4", T0 + 700, out=90))  # the same message, final usage
    cache = u.scan(u.root, u.projects, T0 + 900, {})
    m4 = cache["buckets"]["s1"][str(int((T0 + 700) // 600 * 600))]["claude-opus-5-5"]
    assert (m4["messages"], m4["output"]) == (1, 90)
    assert cache["files"][str(main)]["offset"] == main.stat().st_size


def test_old_transcripts_are_skipped(u):
    old = u.projects / "-work-proj" / "s0.jsonl"
    write(old, assistant("m1", T0 - 30 * 86400))
    cache = u.scan(u.root, u.projects, T0, {})
    assert cache["buckets"] == {}


def run_file(u, orch, workers, created, until, goal="ship it"):
    path = u.root / "runs" / f"{orch.replace(':', '-')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"orchestrator": orch, "workers": workers, "created": created,
                                "until": until, "ended": None, "goal": goal}))


def status_file(u, pane, session, account="1"):
    path = u.root / "agents" / f"{pane.replace(':', '-')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pane": pane, "session": session, "time": T0 - HOUR,
                                "account": account}))


def test_report_per_agent_run_day_and_window_share(u):
    run_file(u, "w1:p0", {"w1:p1": {"label": "builder"}}, T0 - 2 * HOUR, T0 + 6 * HOUR)
    status_file(u, "w1:p0", "boss")
    status_file(u, "w1:p1", "work", account="2")
    lineage = u.root / "home-me-x" / "w1-p2-20261105-080000-oldsessi"  # a gone pane
    lineage.mkdir(parents=True)
    write(u.projects / "p" / "boss.jsonl", assistant("b0", T0 - 5 * HOUR, out=1, read=0, w1=0)
          + assistant("b1", T0 - HOUR, out=1000, read=0, w1=0))  # b0: before the run
    write(u.projects / "p" / "work.jsonl", assistant("w1", T0 - HOUR, out=3000, read=0, w1=0))
    write(u.projects / "p" / "oldsession-1.jsonl", assistant("o1", T0 - 3 * HOUR, read=0, w1=0))
    write(u.projects / "p" / "loose.jsonl", assistant("l1", T0 - HOUR, model="claude-x-1",
                                                      read=0, w1=0))
    u.capture(statusline(session="boss", five=30.0), {}, u.root, T0)
    agents = {"w1:p1": {"name": "builder-agent", "agent_session": {"value": "work"}}}

    rep = u.report(u.root, u.projects, agents, T0, 24)

    by_pane = {a["pane"]: a for a in rep["agents"]}
    assert by_pane["w1:p0"]["role"] == "orchestrator" and by_pane["w1:p0"]["accounts"] == ["1"]
    assert by_pane["w1:p0"]["run"] == f"w1:p0@{time.strftime('%Y-%m-%d %H:%M', time.localtime(T0 - 2 * HOUR))}"
    assert by_pane["w1:p1"]["role"] == "worker" and by_pane["w1:p1"]["label"] == "builder"
    assert by_pane["w1:p1"]["accounts"] == ["2"] and by_pane["w1:p1"]["output"] == 3000
    assert by_pane["w1:p2"]["role"] is None  # mapped from its lineage folder, before the run
    assert by_pane[None]["sessions"] == ["loose"] and rep["unpriced_models"] == ["claude-x-1"]
    [run] = rep["runs"].values()
    assert run["orchestrator_tokens"]["output"] == 1000 and run["worker_tokens"]["output"] == 3000
    assert sum(d["messages"] for d in rep["days"].values()) == 5 == rep["machine"]["messages"]
    assert run["worker_tokens"]["cost"] == round(run["worker_tokens"]["cost"], 2)
    assert rep["per_account"]["2"]["output"] == 3000
    five = rep["accounts"]["1"]["windows"]["five_hour"]
    assert set(five["agents"]) == {"w1:p0"}  # account 1, in the window
    assert rep["per_account"]["unknown"]["messages"] == 2  # no account seen: loose, w1:p2
    assert five["agents"]["w1:p0"]["share"] == 1.0
    assert five["agents"]["w1:p0"]["estimated_points"] == 30.0
    assert "estimate" in rep["note"]


@pytest.fixture
def utc(monkeypatch):
    """Local time is UTC: T0 is 09:13, so "today" starts 9 h 13 min before it."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_report_lists_every_known_account_and_points_of_the_last_hour_and_today(u, utc):
    status_file(u, "w1:p0", "boss")
    write(u.projects / "p" / "boss.jsonl",
          assistant("h", T0 - HOUR / 2, inp=0, out=1000, read=0, w1=0)  # $0.02, the last hour
          + assistant("t", T0 - 3 * HOUR, inp=0, out=3000, read=0, w1=0)  # $0.06, today
          + assistant("y", T0 - 20 * HOUR, inp=0, out=6000, read=0, w1=0))  # $0.12, yesterday
    u.capture(statusline(session="boss", week=60.0), {}, u.root, T0)  # 60 points for $0.20

    rep = u.report(u.root, u.projects, {}, T0, 24, {"1": {"name": "main"}, "2": {"name": "spare"}})

    week = rep["accounts"]["1"]["windows"]["seven_day"]
    assert rep["accounts"]["1"]["id"] == "1" and rep["accounts"]["1"]["name"] == "main"
    assert week["machine_cost_usd"] == 0.2
    assert week["agents"]["w1:p0"] == {"share": 1.0, "estimated_points": 60.0,
                                       "points_last_hour": 6.0, "points_today": 24.0}
    assert rep["accounts"]["2"] == {"id": "2", "name": "spare", "updated": None, "stale": True,
                                    "windows": {}}  # no statusline ran on it yet
    assert list(rep["accounts"]) == ["1", "2"]
    assert "account 2 (spare): no reading yet" in u.human(rep)


def test_a_session_keeps_its_pane_after_the_status_file_moves_on(u):
    status_file(u, "w1:p1", "first")
    write(u.projects / "p" / "first.jsonl", assistant("f1", T0 - HOUR, read=0, w1=0))
    u.report(u.root, u.projects, {}, T0, 24)
    status_file(u, "w1:p1", "second")  # a takeover: the status file now names the new session

    rep = u.report(u.root, u.projects, {}, T0 + 60, 24)

    [agent] = rep["agents"]
    assert agent["pane"] == "w1:p1" and agent["sessions"] == ["first"]
