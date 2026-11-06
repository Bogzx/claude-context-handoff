"""Tests for hooks/orch_mcp.py: a real stdio round trip, then the tools in process."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
PANE = "w1:p0"


def rpc(*messages):
    return "".join(json.dumps(m) + "\n" for m in messages)


def call(mid, name, arguments):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def test_stdio_round_trip_writes_the_report(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), HERDR_ENV="1", HERDR_PANE_ID=PANE,
               ORCH_NOTIFY_CMD="true", PATH="/usr/bin:/bin")
    report = {"summary": "4 of 9 fixes merged", "state": "waiting_on_workers",
              "progress": {"done": 4, "total": 9, "label": "fixes"},
              "waiting_on": [{"who": "w1:p1 builder", "what": "PR #12 tests", "eta": "15m"}],
              "roadmap": [{"id": "R3", "title": "flaky test", "status": "doing", "owner": "w1:p1"}],
              "milestones": [{"kind": "pr_merged", "ref": "#11", "url": "https://x/11"}]}
    out = subprocess.run(
        [sys.executable, str(HOOKS / "orch_mcp.py")], env=env, capture_output=True, text=True,
        timeout=30, check=True,
        input=rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "test", "version": "0"}}},
                  {"jsonrpc": "2.0", "method": "notifications/initialized"},
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                  call(3, "report", report)))
    init, listed, called = [json.loads(x) for x in out.stdout.splitlines()]

    assert init["id"] == 1 and init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"] == {"tools": {}}
    assert [t["name"] for t in listed["result"]["tools"]] == ["report", "ask_owner"]
    assert called["result"] == {"content": [{"type": "text", "text": "Report recorded."}],
                                "isError": False}
    root = tmp_path / ".claude" / "handoffs"
    status = json.loads((root / "agents" / "w1-p0.json").read_text())
    assert status["pane"] == PANE
    saved = status["report"]
    assert saved["role"] == "orchestrator"  # from its fields: no run registered
    assert {k: saved[k] for k in report} == report and abs(saved["time"] - time.time()) < 60
    [event] = [json.loads(x) for x in (root / "events.jsonl").read_text().splitlines()]
    assert event["kind"] == "report" and event["pane"] == PANE
    assert event["body"] == "4 of 9 fixes merged" and event["report"] == saved


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("orch_mcp", HOOKS / "orch_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.orch, "ROOT", tmp_path / "handoffs")
    monkeypatch.setattr(module.orch, "herdr", lambda *a, **k: None)
    monkeypatch.setenv("ORCH_NOTIFY_CMD", "true")

    def run(*messages, pane=PANE):
        monkeypatch.setenv("HERDR_ENV", "1" if pane else "")
        if pane:
            monkeypatch.setenv("HERDR_PANE_ID", pane)
        out = io.StringIO()
        module.main(io.StringIO(rpc(*messages)), out)
        return [json.loads(x) for x in out.getvalue().splitlines()]
    run.module = module
    run.root = tmp_path / "handoffs"
    return run


def test_report_role_comes_from_the_run(mcp):
    run = mcp.root / "runs" / "w1-p0.json"
    run.parent.mkdir(parents=True)
    run.write_text(json.dumps({"orchestrator": PANE, "until": time.time() + 3600,
                               "workers": {"w1:p1": {"label": "builder"}}, "ended": None}))

    mcp(call(1, "report", {"summary": "fixed #12", "status": "done",
                           "tests": [{"cmd": "pytest -q", "passed": 40, "failed": 0}],
                           "changed_files": ["hooks/orch.py"]}), pane="w1:p1")

    saved = json.loads((mcp.root / "agents" / "w1-p1.json").read_text())["report"]
    assert saved["role"] == "worker" and saved["tests"][0]["passed"] == 40


def test_report_takes_the_apps_suggestions_and_why_a_roadmap_item_is_blocked(mcp):
    [resp] = mcp(call(1, "report", {
        "summary": "waiting on the deploy answer",
        "roadmap": [{"title": "Deploy 2.14", "status": "blocked", "why": "waits on your answer"}],
        "suggestions": ["Deploy to staging", "Wrap up by 15:00"]}))

    assert not resp["result"]["isError"]
    saved = json.loads((mcp.root / "agents" / "w1-p0.json").read_text())["report"]
    assert saved["role"] == "orchestrator"  # from its fields: no run registered
    assert saved["roadmap"][0]["why"] == "waits on your answer"
    assert saved["suggestions"] == ["Deploy to staging", "Wrap up by 15:00"]


PLAN = [{"title": "Deploy 2.14", "status": "next"}]


@pytest.fixture
def reports(mcp, monkeypatch):
    """report(at, **fields) through the tool, at a fake time: the stored report back."""
    def report(at, **fields):
        monkeypatch.setattr(mcp.module.orch, "now", lambda: at)
        [resp] = mcp(call(1, "report", dict({"role": "orchestrator", "summary": "s"}, **fields)))
        assert not resp["result"]["isError"]
        return json.loads((mcp.root / "agents" / "w1-p0.json").read_text())["report"]
    return report


def logged(mcp):
    lines = (mcp.root / "events.jsonl").read_text().splitlines()
    return [json.loads(x)["report"] for x in lines]


def test_a_report_without_a_roadmap_keeps_the_plan_and_the_time_it_was_written(reports):
    first = reports(100, roadmap=PLAN, next=["deploy"], risks=["disk"],
                    waiting_on=[{"who": "qa", "what": "a run"}], state="working")
    assert first["roadmap_time"] == 100 == first["time"]

    second = reports(200, summary="still going", state="waiting_on_owner")
    assert second["roadmap"] == PLAN and second["roadmap_time"] == 100
    assert second["time"] == 200 and second["summary"] == "still going"
    assert second["state"] == "waiting_on_owner"
    assert not {"next", "risks", "waiting_on"} & set(second)  # those belong to their turn

    assert reports(300)["roadmap_time"] == 100  # and the one after it


def test_a_newer_roadmap_replaces_the_plan_and_its_time(reports):
    reports(100, roadmap=PLAN)
    reports(200)
    newer = [{"title": "Roll back", "status": "doing"}]
    saved = reports(300, roadmap=newer)
    assert saved["roadmap"] == newer and saved["roadmap_time"] == 300


def test_an_empty_roadmap_clears_the_plan_and_it_stays_cleared(reports):
    reports(100, roadmap=PLAN)
    cleared = reports(200, roadmap=[])
    assert cleared["roadmap"] == [] and cleared["roadmap_time"] == 200
    later = reports(300)
    assert later["roadmap"] == [] and later["roadmap_time"] == 200


def test_a_plan_is_as_old_as_its_roadmap_time_not_its_last_report(mcp):
    keep = mcp.module.orch.keep_plan
    kept = {"time": 300, "roadmap": PLAN, "roadmap_time": 100}
    assert "roadmap" not in keep({"time": 400}, kept, since=200)
    assert keep({"time": 400}, kept, since=100)["roadmap"] == PLAN  # from the run's first moment
    assert keep({"time": 400}, {"time": 300, "roadmap": PLAN}, since=200)["roadmap_time"] == 300


def test_a_report_with_no_plan_before_it_has_no_plan_fields(reports):
    assert not {"roadmap", "roadmap_time"} & set(reports(100, status="done"))


def test_a_plan_stored_before_roadmap_time_existed_is_dated_by_its_report(mcp, reports):
    path = mcp.root / "agents" / "w1-p0.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pane": PANE, "report": {"time": 50, "summary": "old",
                                                         "roadmap": PLAN}}))
    assert reports(200)["roadmap_time"] == 50


def test_the_log_has_what_the_agent_sent_not_the_plan_again(mcp, reports):
    reports(100, roadmap=PLAN)
    reports(200)
    with_plan, without = logged(mcp)
    assert with_plan["roadmap"] == PLAN and with_plan["roadmap_time"] == 100
    assert not {"roadmap", "roadmap_time"} & set(without)


def test_the_report_tool_tells_an_agent_a_roadmap_can_be_left_out(mcp):
    report = next(t for t in mcp.module.TOOLS if t["name"] == "report")
    assert "left out it stays" in report["description"] and "[] clears it" in report["description"]


def test_ask_owner_logs_a_question_with_options(mcp):
    [resp] = mcp(call(1, "ask_owner", {"question": "Deploy to prod?",
                                       "options": [{"label": "Deploy", "danger": "prod"},
                                                   {"label": "Not yet"}]}))

    text = resp["result"]["content"][0]["text"]
    [event] = [json.loads(x) for x in (mcp.root / "events.jsonl").read_text().splitlines()]
    assert event["kind"] == "ask" and event["id"] in text and not resp["result"]["isError"]
    assert event["options"] == [{"label": "Deploy", "danger": "prod"},
                                {"label": "Not yet", "danger": "none"}]


def in_a_run(mcp):
    runs = mcp.root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{PANE.replace(':', '-')}.json").write_text(json.dumps(
        {"orchestrator": PANE, "until": time.time() + 3600, "goal": "x", "workers": {}}))
    return runs / f"{PANE.replace(':', '-')}.json"


def test_ask_owner_takes_evidence_and_a_rule(mcp):
    chip = {"kind": "tests", "label": "212 passed", "pane": "w1:p1", "tab": "report", "ok": True}
    [resp] = mcp(call(1, "ask_owner", {"question": "Merge?", "evidence": [chip], "rule": "D27"}))
    assert resp["result"]["isError"] is False
    [event] = [json.loads(x) for x in (mcp.root / "events.jsonl").read_text().splitlines()]
    assert event["evidence"] == [chip] and event["rule"] == "D27"


@pytest.mark.parametrize(("args", "why"), [
    ({"evidence": [{"kind": "chart", "label": "x"}]}, "must be one of"),
    ({"evidence": [{"kind": "pr"}]}, "evidence[0].label is required"),
    ({"evidence": [{"kind": "pr", "label": "x", "ok": "yes"}]}, "must be true or false"),
    ({"evidence": [{"kind": "pr", "label": "x", "tab": "log"}]}, "must be one of"),
    ({"rule": "be careful"}, "isn't a decision id"),
])
def test_ask_owner_says_what_is_wrong_and_stays_up(mcp, args, why):
    first, second = mcp(call(1, "ask_owner", dict(args, question="Merge?")),
                        call(2, "ask_owner", {"question": "Merge?"}))
    assert first["result"]["isError"] is True and why in first["result"]["content"][0]["text"]
    assert second["result"]["isError"] is False


def test_ask_owner_takes_a_default_and_a_time(mcp):
    run = in_a_run(mcp)

    [resp] = mcp(call(1, "ask_owner", {"question": "Both OK?", "default": "Yes", "by": "+30m",
                                       "options": [{"label": "Yes"}, {"label": "No"}]}))

    assert not resp["result"]["isError"]
    [q] = json.loads(run.read_text())["questions"]
    assert q["default"] == "Yes" and q["default_at"] > time.time() + 1700


def test_a_refused_ask_is_a_tool_error_and_the_server_stays_up(mcp):
    in_a_run(mcp)

    first, second = mcp(
        call(1, "ask_owner", {"question": "Ship?", "default": "Deploy", "by": "+30m",
                              "options": [{"label": "Deploy", "danger": "prod"}]}),
        call(2, "ask_owner", {"question": "Ship?", "options": [{"label": "Later"}]}))

    assert first["result"]["isError"] is True
    assert "the owner has to pick it" in first["result"]["content"][0]["text"]
    assert second["result"]["isError"] is False


@pytest.mark.parametrize("arguments, error", [
    ({"status": "done"}, "arguments.summary is required"),
    ({"summary": "x", "status": "finished"}, "arguments.status must be one of"),
    ({"summary": "x", "progress": {"done": "3", "total": 5}}, "progress.done must be a whole"),
    ({"summary": "x", "tests": [{"cmd": "pytest", "passed": True}]}, "tests[0].passed"),
    ({"summary": "x", "mood": "great"}, "arguments.mood is not a field"),
])
def test_report_rejects_what_does_not_fit_the_schema(mcp, arguments, error):
    [resp] = mcp(call(1, "report", arguments))
    assert resp["result"]["isError"] and error in resp["result"]["content"][0]["text"]
    assert not (mcp.root / "agents").exists()


def test_outside_herdr_there_are_no_tools(mcp):
    listed, called = mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         call(2, "report", {"summary": "x"}), pane=None)
    assert listed["result"] == {"tools": []}
    assert called["error"]["code"] == -32601


def test_unknown_methods_and_bad_json(mcp):
    out = io.StringIO()
    mcp.module.main(io.StringIO('{"jsonrpc":"2.0","id":7,"method":"resources/list"}\nnot json\n'),
                    out)
    unknown, bad = [json.loads(x) for x in out.getvalue().splitlines()]
    assert unknown["id"] == 7 and unknown["error"]["code"] == -32601
    assert bad["id"] is None and bad["error"]["code"] == -32700
