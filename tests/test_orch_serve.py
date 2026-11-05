"""Offline tests for hooks/orch_serve.py (orch serve): herdr, tailscale and the clock are fakes.
The shapes are checked against orch-app docs/API.md (v1, draft 2026-11-05)."""
import importlib.util
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
BOSS, W1, W2, LOOSE = "w1:p0", "w1:p1", "w1:p2", "w1:p9"
T0 = datetime(2026, 11, 5, 10, 0).timestamp()  # noqa: DTZ001 orch works in local time
TOKEN = "t0ken-for-tests"


class World:
    def __init__(self, serve, home):
        self.serve, self.orch, self.home = serve, serve.orch, home
        self.root = home / ".claude" / "handoffs"
        self.t = T0
        self.agents = {}
        self.prompts = []
        self.prompt_ok = True
        self.herdr_calls = []
        self.screen = "Bash command\n  rm saves/tmp/x.lsv\nDo you want to proceed?\n"
        self.whois = {"127.0.0.1": {"Node": {"StableID": "nPHONE", "ComputedName": "phone",
                                             "Name": "phone.tail.ts.net."}},
                      "100.64.0.1": {"Node": {"StableID": "nDEV1", "ComputedName": "dev1",
                                              "Name": "dev1.tail.ts.net."}}}

    def agent(self, pane, status, seq=1, change=1, name=None):
        self.agents[pane] = {"pane_id": pane, "agent_status": status, "completion_seq": seq,
                             "state_change_seq": change, "cwd": "/work/proj", "name": name,
                             "agent_session": {"value": f"s-{pane}"}}

    def turn_end(self, pane, ago=0, bg=(), last="done"):
        path = self.root / "agents" / f"{pane.replace(':', '-')}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pane": pane, "session": f"s-{pane}", "event": "Stop", "time": self.t - ago,
            "last": last, "account": "1", "pid": 1,
            "background_tasks": [{"type": "bash", "id": "b", "what": w} for w in bg]}))

    def herdr(self, *args, **kw):
        self.herdr_calls.append(args)
        if args[:2] == ("agent", "read"):
            return SimpleNamespace(returncode=0 if args[2] in self.agents else 1,
                                   stdout=self.screen)
        return SimpleNamespace(returncode=0, stdout="")

    def tailscale(self, *args):
        if args == ("ip", "-4"):
            return SimpleNamespace(returncode=0, stdout="100.64.0.1\n")
        if args[:2] == ("whois", "--json") and args[2] in self.whois:
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.whois[args[2]]))
        return SimpleNamespace(returncode=1, stdout="")

    def events(self):
        path = self.root / "events.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def get(self, gw, path):
        url = path.split("?", 1)
        query = {k: [v] for k, v in (x.split("=", 1) for x in url[1].split("&"))} if url[1:] else {}
        return gw.route("GET", url[0], query, None)

    def post(self, gw, **action):
        action.setdefault("id", f"a{len(self.events())}-{action.get('kind')}")
        action.setdefault("time", self.t)
        return gw.route("POST", "/v1/actions", {}, action)


@pytest.fixture
def world(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("orch_serve", HOOKS / "orch_serve.py")
    serve = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(serve)
    home = tmp_path / "home"
    for d in (".claude", ".claude-2"):
        (home / d).mkdir(parents=True)
        (home / d / ".credentials.json").write_text("{}")
    w = World(serve, home)
    orch = serve.orch
    monkeypatch.setitem(sys.modules, "orch_serve", serve)  # what `orch serve` imports
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ORCH_NOTIFY_CMD", raising=False)
    monkeypatch.setattr(orch, "ROOT", w.root)
    monkeypatch.setattr(orch, "now", lambda: w.t)
    monkeypatch.setattr(orch, "agents", lambda: dict(w.agents))
    monkeypatch.setattr(orch, "prompt",
                        lambda pane, text: w.prompts.append((pane, text)) or w.prompt_ok)
    monkeypatch.setattr(orch, "herdr", w.herdr)
    monkeypatch.setattr(serve, "tailscale", w.tailscale)
    monkeypatch.setattr(serve, "CONFIG", tmp_path / "config" / ".env")
    serve.write_env({"ORCH_TOKEN": TOKEN, "ORCH_ALLOW": "phone",
                     "ORCH_ACCOUNT_NAMES": "1=pibo1,2=pibo2"})
    w.agent(BOSS, "idle", name="AlphaProve")
    return w


def start_run(w):
    w.orch.main(["--orch", BOSS, "run", "start", "--until", "+8h", "--goal", "ship the fixes",
                 "--workers", f"{W1}=builder,{W2}=tester"])


# --- the API.md shapes --------------------------------------------------------------------------
# A key ending in "?" may be missing or null. A set is an enum, a list holds items of one shape.

NUM = (int, float)
STATE = {"working", "blocked", "idle", "own-job", "parked", "missing"}
AGENT = {"pane": str, "label": str, "state": STATE,
         "agent_status?": {"working", "idle", "done", "blocked", "unknown"}, "since?": NUM,
         "session?": str, "account?": str, "model?": str, "context_tokens?": int,
         "background_tasks": [{"type?": str, "id?": str, "what": str, "since?": NUM}],
         "last": str, "park?": {"reason": str, "since": NUM, "until?": NUM},
         "switch?": {"account": str, "when": {"handoff", "idle"}, "time": NUM},
         "report?": dict}
ORCH_REPORT = {"time": NUM, "summary": str,
               "state?": {"working", "waiting_on_workers", "waiting_on_owner", "done"},
               "progress?": {"done": int, "total": int, "label?": str},
               "waiting_on?": [{"who": str, "what": str, "eta?": str}], "next?": [str],
               "roadmap?": [{"id?": str, "title": str, "owner?": str, "eta?": str, "why?": str,
                             "status": {"done", "doing", "next", "later", "blocked"}}],
               "risks?": [str], "milestones?": [dict], "suggestions?": [str],
               "source": {"report", "handoff"}}
WORKER_REPORT = {"time": NUM, "summary": str, "status?": {"done", "partial", "blocked"},
                 "tests?": [{"cmd": str, "passed?": int, "failed?": int}],
                 "changed_files?": [{"path": str, "added?": int, "removed?": int}],
                 "needs_live_test?": [str], "issues_for_others?": [str]}
QUESTION = {"id": str, "pane": str, "time": NUM, "text": str,
            "options": [{"label": str, "danger": {"none", "deploy", "prod", "destructive"}}],
            "evidence": [dict], "rule?": str}
DIALOG = {"pane": str, "label": str, "since?": NUM, "change": int, "tool?": str,
          "command?": str, "cwd?": str, "doing?": str, "danger": bool, "text": str}
STALL = {"nudged_at?": NUM, "reasons": [{"key": str, "kind": str, "pane?": str, "text": str,
                                         "fix?": str, "message?": str, "picked": bool}]}
WINDOW = {"used_percentage": NUM, "resets_at": NUM, "burn_per_hour?": NUM, "basis_minutes?": NUM,
          "projected_full_at?": NUM, "full_before_reset?": bool, "samples": [[NUM]]}
ACCOUNT = {"id": str, "name": str, "config_dir": str, "logged_in": bool, "updated?": NUM,
           "stale": bool, "pct_per_100k_uncached?": NUM, "windows": {"seven_day?": WINDOW,
                                                                      "five_hour?": WINDOW}}
ACTION_STATUS = {"id": str, "status": {"delivered", "picked", "rejected", "failed"},
                 "delivered_at?": NUM, "picked_at?": NUM, "error?": str}
ORCHESTRATOR = {"pane": str, "name": str, "machine": str, "cwd?": str,
                "activity": {"working", "waiting", "stalled", "done", "offline"},
                "activity_since?": NUM, "agent": AGENT,
                "run?": {"goal": str, "created": NUM, "until": NUM, "ended?": NUM},
                "report?": ORCH_REPORT, "questions": [QUESTION], "dialogs": [DIALOG],
                "stall?": STALL, "workers": [AGENT], "lanes?": dict}
FLEET = {"version": int, "machine": str, "time": NUM, "orchestrators": [ORCHESTRATOR],
         "accounts": dict, "default_account": str, "actions": [ACTION_STATUS]}
DECISIONS = {"pane": str, "log": str, "decisions": [
    {"id": str, "generation": int, "line": int, "time?": str, "by": str, "who": {"user", "agent"},
     "quote?": str, "what": str, "why": str, "superseded_by?": str}]}
HISTORY = {"events": [{"time": NUM, "host": str, "kind": str, "pane?": str, "title": str,
                       "body": str}],
           "generations": [{"generation": int, "time": NUM, "note": str, "next": [str]}]}
SCREEN = {"pane": str, "time": NUM, "lines": [str]}
USAGE = {"time": NUM, "machine": str, "accounts": dict, "default_account": str,
         "agents": [{"pane": str, "label": str, "role": {"orchestrator", "worker"},
                     "orchestrator": str, "account": str, "model": str,
                     "points_last_hour?": NUM, "points_today?": NUM, "points_7d?": NUM,
                     "tokens": {"input": int, "output": int, "cache_read": int,
                                "cache_write": int},
                     "cost_usd": NUM, "costly?": str}],
         "other": [{"label": str, "account": str, "points_last_hour?": NUM,
                    "points_today?": NUM, "points_7d?": NUM, "note": str}],
         "runs": dict}


def conforms(value, spec, where="$"):
    if isinstance(spec, dict) and spec is not dict:
        assert isinstance(value, dict), f"{where} is not an object: {value!r}"
        for key, sub in spec.items():
            name = key.rstrip("?")
            if name not in value or (key.endswith("?") and value[name] is None):
                assert key.endswith("?"), f"{where}.{name} is missing"
                continue
            conforms(value[name], sub, f"{where}.{name}")
    elif isinstance(spec, list):
        assert isinstance(value, list), f"{where} is not a list: {value!r}"
        for i, item in enumerate(value):
            conforms(item, spec[0], f"{where}[{i}]")
    elif isinstance(spec, set):
        assert value in spec, f"{where} = {value!r}, not one of {sorted(spec)}"
    else:
        assert isinstance(value, spec) and not (spec is not bool and isinstance(value, bool)), (
            f"{where} = {value!r} is not {spec}")


# --- auth and the server ------------------------------------------------------------------------

@pytest.fixture
def http(world):
    servers = []

    def start(**kw):
        gw = world.serve.Gateway(**kw)
        server = world.serve.make_server(gw, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)

        def call(path, token=TOKEN, method="GET", body=None):
            req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}",
                                         method=method,
                                         data=json.dumps(body).encode() if body else None)
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
        return call
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def test_auth_needs_an_allowed_node_and_the_token_and_says_nothing_else(world, http):
    call = http()
    status, body = call("/v1/fleet")
    assert status == 200 and json.loads(body)["machine"]
    assert call("/v1/fleet", token=None) == (401, b"")
    assert call("/v1/fleet", token="wrong") == (401, b"")
    assert call("/v1/nothing-here", token="wrong") == (401, b"")
    world.serve.write_env({"ORCH_TOKEN": TOKEN, "ORCH_ALLOW": "someone-else"})
    assert call("/v1/fleet") == (403, b"")  # the config is read per request
    assert call("/v1/fleet", method="PUT") == (403, b"")
    world.whois.pop("127.0.0.1")  # not a tailnet address at all
    world.serve.write_env({"ORCH_TOKEN": TOKEN, "ORCH_ALLOW": "phone"})
    assert http()("/v1/fleet") == (403, b"")


def test_allow_self_admits_this_machines_own_node_only_when_asked(world, http):
    world.whois["127.0.0.1"] = world.whois["100.64.0.1"]  # the caller is dev1 itself
    assert http()("/v1/fleet") == (403, b"")
    assert http(allow_self=True)("/v1/fleet")[0] == 200


def test_a_bad_request_gets_a_reason_once_authorized(world, http):
    call = http()
    assert call("/v1/actions", method="POST", body=["x"])[0] == 400
    status, body = call("/v1/fleet", method="DELETE")
    assert status == 405 and json.loads(body) == {"error": "method not allowed"}
    assert call("/v2/fleet")[0] == 404


def test_serve_binds_to_the_tailscale_ip_on_the_fixed_port(world, monkeypatch, capsys):
    bound = []

    class Fake:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            bound.append("closed")

    monkeypatch.setattr(world.serve, "make_server", lambda gw, addr, port: bound.append(
        (addr, port)) or Fake())
    assert world.orch.main(["serve"]) == 0
    assert bound == [("100.64.0.1", 7433), "closed"]
    assert "http://100.64.0.1:7433/v1/" in capsys.readouterr().out
    monkeypatch.setattr(world, "tailscale", lambda *a: SimpleNamespace(returncode=1, stdout=""))
    monkeypatch.setattr(world.serve, "tailscale", world.tailscale)
    with pytest.raises(SystemExit):
        world.orch.main(["serve"])
    assert "listens only on the tailnet" in capsys.readouterr().err
    world.serve.write_env({})
    with pytest.raises(SystemExit):
        world.orch.main(["serve"])
    assert "orch pair" in capsys.readouterr().err


def test_pair_keeps_a_private_token_and_prints_the_pairing_string(world, capsys):
    world.serve.CONFIG.unlink()
    world.orch.main(["pair", "--allow", "phone,tablet"])
    out = capsys.readouterr().out
    env = world.serve.read_env()
    token = env["ORCH_TOKEN"]
    assert len(token) >= 40 and env["ORCH_ALLOW"] == "phone,tablet"
    assert f"token: {token}" in out
    assert "orch://pair?machine=" in out and f"addr=100.64.0.1&port=7433&token={token}" in out
    assert stat.S_IMODE(os.stat(world.serve.CONFIG).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(world.serve.CONFIG.parent).st_mode) == 0o700
    world.orch.main(["pair"])
    assert world.serve.read_env() == env  # the same token and list
    world.orch.main(["pair", "--new"])
    assert world.serve.read_env()["ORCH_TOKEN"] != token


# --- reads ----------------------------------------------------------------------------------------

def busy_fleet(w):
    """A run whose orchestrator is stalled: builder idle 20 min, tester at a dialog, the
    orchestrator idle 6 min with nothing to wake it; plus an agent outside any run."""
    start_run(w)
    w.agent(W1, "idle", seq=3, change=7)
    w.agent(W2, "blocked", change=9)
    w.agent(LOOSE, "working")
    w.turn_end(BOSS, ago=6 * 60, last="Assigned the next fixes.")
    w.turn_end(W1, ago=20 * 60, last="PR #12 is open.")
    w.turn_end(W2, ago=3 * 60, last="Cleaning saves.")
    w.orch.write_report(BOSS, {"role": "orchestrator", "time": w.t - 400, "summary": "4 of 9",
                               "state": "waiting_on_workers",
                               "progress": {"done": 4, "total": 9, "label": "fixes"},
                               "roadmap": [{"id": "r1", "title": "flaky test", "status": "next",
                                            "owner": "builder"}]})
    w.orch.write_report(W1, {"role": "worker", "time": w.t - 1200, "summary": "PR #12 open",
                             "status": "partial", "changed_files": ["site/a.py"],
                             "tests": [{"cmd": "pytest -q", "passed": 12, "failed": 0}]})
    w.orch.ask(W1, "Deploy 2.14 to staging?", [{"label": "Deploy", "danger": "deploy"},
                                               {"label": "Not yet"}])
    w.orch.queue_switch(W1, "2", "handoff")
    w.orch.emit("nudge", "Watchdog nudged", "...", BOSS)
    (w.root / "watchdog.json").write_text(json.dumps(
        {BOSS: {"first_seen": {f"{W2}:blocked:s-{W2}:1": w.t - 11 * 60}}}))
    (w.root / "usage.json").write_text(json.dumps({"accounts": {"1": {
        "time": w.t - 30, "windows": {"seven_day": {"used_percentage": 80,
                                                    "resets_at": w.t + 86400}}}}}))
    with open(w.root / "usage-history.jsonl", "w") as f:
        f.writelines(json.dumps({"time": w.t - 3600 + i * 900, "account": "1", "windows": {
                "seven_day": {"used_percentage": 77 + i, "resets_at": w.t + 86400}}}) + "\n" for i in range(4))
        f.write(json.dumps({"time": w.t - 600, "account": "1", "windows": {  # an older window
            "seven_day": {"used_percentage": 5, "resets_at": w.t - 100}}}) + "\n")


def test_fleet_has_the_api_shape_and_the_stalled_run(world):
    busy_fleet(world)
    status, fleet = world.get(world.serve.Gateway(), "/v1/fleet")
    assert status == 200
    conforms(fleet, FLEET)
    for acc in fleet["accounts"].values():
        conforms(acc, ACCOUNT)
    run, loose = fleet["orchestrators"]
    assert run["pane"] == BOSS and run["name"] == "AlphaProve" and loose["pane"] == LOOSE
    assert run["activity"] == "stalled" and run["activity_since"] == world.t - 6 * 60
    assert run["run"]["goal"] == "ship the fixes" and loose["run"] is None
    assert loose["activity"] == "working" and loose["workers"] == []
    builder, tester = run["workers"]
    assert builder["state"] == "idle" and builder["since"] == world.t - 20 * 60
    assert builder["switch"]["account"] == "2" and builder["account"] == "1"
    assert builder["report"]["changed_files"] == [{"path": "site/a.py"}]
    conforms(builder["report"], WORKER_REPORT)
    assert tester["state"] == "blocked" and tester["since"] == world.t - 11 * 60
    [question] = run["questions"]
    assert question["pane"] == W1 and question["options"][0] == {"label": "Deploy",
                                                                  "danger": "deploy"}
    [dialog] = run["dialogs"]
    assert dialog["pane"] == W2 and dialog["change"] == 9 and "rm saves" in dialog["text"]
    assert dialog["doing"] == "Cleaning saves." and dialog["danger"] is True
    reasons = {r["key"]: r for r in run["stall"]["reasons"]}
    assert set(reasons) == {f"{W1}:idle", f"{W2}:blocked", "orch:nowake"}
    assert reasons[f"{W1}:idle"]["fix"] == "Assign: flaky test" and reasons[f"{W1}:idle"]["picked"]
    assert reasons[f"{W2}:blocked"]["picked"] is False and reasons[f"{W2}:blocked"]["pane"] == W2
    assert reasons["orch:nowake"]["message"] == "then arm one orch wait"
    assert run["stall"]["nudged_at"] == world.t
    assert run["report"]["source"] == "report" and run["report"]["progress"]["done"] == 4
    one, two = fleet["accounts"]["1"], fleet["accounts"]["2"]
    assert one["name"] == "pibo1" and one["config_dir"] == "~/.claude" and not one["stale"]
    assert [p[1] for p in one["windows"]["seven_day"]["samples"]] == [77, 78, 79, 80]
    assert two["name"] == "pibo2" and two["windows"] == {} and two["stale"]
    assert fleet["default_account"] == "1"


def test_activity_follows_the_orchestrator(world):
    gw = world.serve.Gateway()
    start_run(world)
    world.agent(BOSS, "working")
    assert world.get(gw, "/v1/fleet")[1]["orchestrators"][0]["activity"] == "working"
    world.agent(BOSS, "idle")
    world.turn_end(BOSS, ago=60)
    assert world.get(gw, "/v1/fleet")[1]["orchestrators"][0]["activity"] == "waiting"
    world.agents.pop(BOSS)
    assert world.get(gw, "/v1/fleet")[1]["orchestrators"][0]["activity"] == "offline"
    world.orch.main(["--orch", BOSS, "run", "stop"])
    lead = world.get(gw, "/v1/fleet")[1]["orchestrators"][0]
    assert lead["activity"] == "done" and lead["activity_since"] == world.t
    world.t += 2 * 86400  # long over and the pane is gone: no longer shown
    assert world.get(gw, "/v1/fleet")[1]["orchestrators"] == []


def test_fleet_says_when_herdr_is_down(world, monkeypatch):
    monkeypatch.setattr(world.orch, "agents", lambda: None)
    assert world.get(world.serve.Gateway(), "/v1/fleet") == (503, {"error": "herdr is not reachable"})


def lineage(w):
    d = w.root / "home-bogdan-proj" / f"{BOSS.replace(':', '-')}-20261105-093000-abcd1234"
    d.mkdir(parents=True)
    (d / "decisions.md").write_text(
        "# Decision log\n\n## Generation 1\n"
        "- **D1** 2026-11-05 09:30, user: \"staging only today\". Why: prod freeze.\n"
        "- **D2** 09:40, agent: retry budget of 2. Because: flaky upstream.\n"
        "- **D3** 09:50, agent: retry budget of 3, replaces D2. Why: still flaky.\n")
    (d / "gen-01.md").write_text(
        "# Handoff: orchestrator of the fixes\n\n## 3. Progress\n- 4 of 9 done\n\n"
        "## 7. Next steps\n1. Deploy 2.14 to staging\n   - after the k6 run\n2. Review #13\n\n"
        "## 8. Verify first\n- tests\n")
    return d


def test_decisions_and_acks(world):
    gw = world.serve.Gateway()
    assert world.get(gw, f"/v1/orchestrators/{BOSS}/decisions")[0] == 404
    lineage(world)
    status, out = world.get(gw, f"/v1/orchestrators/{BOSS.replace(':', '%3A')}/decisions")
    assert status == 200
    conforms(out, DECISIONS)
    assert [d["id"] for d in out["decisions"]] == ["D1", "D2", "D3"]
    assert out["decisions"][1]["superseded_by"] == "D3" and out["seen"] == []
    assert world.post(gw, kind="ack", orchestrator=BOSS, decisions=["D2", "D3"])[0] == 202
    assert world.get(gw, f"/v1/orchestrators/{BOSS}/decisions")[1]["seen"] == ["D2", "D3"]


def test_history_has_the_runs_events_newest_first_and_the_generations(world):
    gw = world.serve.Gateway()
    start_run(world)
    lineage(world)
    world.t += 60
    world.orch.emit("report", "Worker report", "PR #12", W1)
    world.orch.emit("report", "Elsewhere", "x", LOOSE)
    world.t += 60
    world.orch.emit("nudge", "Watchdog nudged", "go", BOSS)
    status, out = world.get(gw, f"/v1/orchestrators/{BOSS}/history")
    assert status == 200
    conforms(out, HISTORY)
    assert [e["kind"] for e in out["events"]] == ["nudge", "report", "run-start"]
    since = world.get(gw, f"/v1/orchestrators/{BOSS}/history?since={world.t - 90}&limit=1")[1]
    assert [e["kind"] for e in since["events"]] == ["nudge"]
    [gen] = out["generations"]
    assert gen["generation"] == 1 and gen["note"] == "orchestrator of the fixes"
    assert gen["next"] == ["Deploy 2.14 to staging", "Review #13"]


def test_screen(world):
    gw = world.serve.Gateway()
    status, out = world.get(gw, f"/v1/panes/{BOSS}/screen?lines=2")
    assert status == 200
    conforms(out, SCREEN)
    assert out["lines"] == ["  rm saves/tmp/x.lsv", "Do you want to proceed?"]
    assert ("agent", "read", BOSS, "--source", "recent", "--lines", "2", "--format",
            "text") in world.herdr_calls
    assert world.get(gw, "/v1/panes/w9:p9/screen")[0] == 404
    assert world.get(gw, f"/v1/panes/{BOSS}/screen?lines=x")[0] == 400


def test_the_background_loop_keeps_the_usage_scan_warm(world):
    assert (300, world.serve.warm_usage) in world.serve.BACKGROUND
    world.serve.warm_usage(None)
    assert (world.root / "usage-cache.json").exists()


def test_usage_has_the_api_shape_and_points_per_period(world):
    busy_fleet(world)
    usage = json.loads((world.root / "usage.json").read_text())
    usage["sessions"] = {"s-other": {"account": "1", "time": world.t}}
    (world.root / "usage.json").write_text(json.dumps(usage))
    project = world.home / ".claude" / "projects" / "-work-proj"
    project.mkdir(parents=True)

    def assistant(at, mid, out_tokens):  # opus 5.5: $4 / $20 per million in / out
        stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return json.dumps({"type": "assistant", "timestamp": stamp, "message": {
            "id": mid, "model": "claude-opus-5-5",
            "usage": {"input_tokens": 1000, "output_tokens": out_tokens}}}) + "\n"
    (project / f"s-{W1}.jsonl").write_text(assistant(world.t - 1800, "m1", 1_000_000)
                                           + assistant(world.t - 3 * 86400, "m2", 3_000_000))
    (project / "s-other.jsonl").write_text(assistant(world.t - 600, "m3", 500_000))
    run = json.loads((world.root / "runs" / "w1-p0.json").read_text())
    run["created"] = world.t - 4 * 86400
    (world.root / "runs" / "w1-p0.json").write_text(json.dumps(run))
    status, out = world.get(world.serve.Gateway(), "/v1/usage?hours=168")
    assert status == 200
    conforms(out, USAGE)
    [builder] = out["agents"]
    assert builder["pane"] == W1 and builder["orchestrator"] == BOSS and builder["account"] == "1"
    assert builder["tokens"]["output"] == 4_000_000 and builder["model"] == "claude-opus-5-5"
    # The week is at 80% and this machine spent $90.012 of it: $80.008 builder, $10.004 other.
    week = 80 / 90.012
    assert builder["points_7d"] == pytest.approx(80.008 * week, abs=0.05)  # its window share
    assert builder["points_last_hour"] == pytest.approx(20.004 * week, abs=0.01)
    [other] = out["other"]
    assert other["account"] == "1" and other["points_7d"] == pytest.approx(10.004 * week,
                                                                           abs=0.05)
    assert builder["points_7d"] + other["points_7d"] == pytest.approx(80, abs=0.1)  # never more
    assert other["note"].startswith("1 sessions outside a run")
    [run] = out["runs"].values()
    assert run["week_points"] is None  # the orchestrator itself spent nothing here
    assert out["accounts"]["1"]["windows"]["seven_day"]["machine_cost_usd"] == 90.01


# --- actions ----------------------------------------------------------------------------------------

def test_an_action_runs_once_per_id_is_logged_and_gets_picked_up(world):
    gw = world.serve.Gateway()
    world.agent(W1, "idle", change=5)
    action = {"id": "a1", "time": world.t, "kind": "prompt", "pane": W1, "text": "Take #13"}
    status, out = gw.route("POST", "/v1/actions", {}, dict(action))
    assert status == 202 and out == {"id": "a1", "status": "delivered", "delivered_at": world.t,
                                     "picked_at": None, "error": None}
    conforms(out, ACTION_STATUS)
    assert gw.route("POST", "/v1/actions", {}, dict(action)) == (202, out)  # a retry
    assert world.prompts == [(W1, "Take #13")]
    reuse = gw.route("POST", "/v1/actions", {}, dict(action, text="other"))
    assert reuse[0] == 409 and "already used" in reuse[1]["error"]
    [event] = [e for e in world.events() if e["kind"] == "action"]
    assert event["source"] == "app" and event["action"] == "prompt" and event["pane"] == W1
    assert event["body"] == "Take #13" and event["status"] == "delivered"
    world.t += 5
    world.agent(W1, "working", change=6)
    assert world.get(gw, "/v1/actions/a1")[1]["status"] == "picked"
    [listed] = world.get(gw, "/v1/fleet")[1]["actions"]
    assert listed == {"id": "a1", "status": "picked", "delivered_at": world.t - 5,
                      "picked_at": world.t, "error": None}
    world.t += 11 * 60
    assert world.get(gw, "/v1/fleet")[1]["actions"] == []  # older than 10 minutes
    assert world.get(gw, "/v1/actions/nope")[0] == 404


def test_actions_are_refused_with_a_reason(world):
    gw = world.serve.Gateway()
    start_run(world)
    world.agent(W1, "working")
    post = gw.route
    assert post("POST", "/v1/actions", {}, {"kind": "prompt"})[0] == 400
    assert post("POST", "/v1/actions", {}, {"id": "n1", "kind": "prompt", "pane": W1,
                                            "text": "x"})[1]["error"] == "time is missing"
    old = world.post(gw, id="o1", time=world.t - 11 * 60, kind="prompt", pane=W1, text="x")
    assert old[0] == 409 and "expire after 10 minutes" in old[1]["error"]
    assert world.post(gw, id="k1", kind="reboot")[0] == 400
    assert world.post(gw, id="p0", kind="prompt", pane=W1)[1]["error"] == "text is missing"
    world.prompt_ok = False
    failed = world.post(gw, id="p1", kind="prompt", pane=W1, text="x")
    assert failed == (502, {"id": "p1", "status": "failed", "delivered_at": None,
                            "picked_at": None, "error": f"could not prompt {W1}"})
    world.prompt_ok = True
    busy = world.post(gw, id="s1", kind="account", pane=W1, account="2", when="now")
    assert busy[0] == 409 and busy[1]["error"] == f"{W1} is working, not idle"
    assert "not logged in" in world.post(gw, id="s2", kind="account", pane=W1, account="3",
                                         when="idle")[1]["error"]
    assert world.post(gw, id="d1", kind="dialog", pane=W2, change=1, choice="allow")[0] == 400
    assert world.post(gw, id="r1", kind="run", orchestrator=W2, op="extend")[0] == 409
    assert world.post(gw, id="pk", kind="park", orchestrator=BOSS, pane=LOOSE)[0] == 409
    qid = world.orch.ask(W1, "Deploy?")
    assert world.post(gw, id="q1", kind="answer", question=qid, text="yes")[0] == 202
    again = world.post(gw, id="q2", kind="answer", question=qid, text="no")
    assert again[0] == 409 and "already answered" in again[1]["error"]
    rejected = [e for e in world.events() if e["kind"] == "action" and e["status"] != "delivered"]
    assert len(rejected) == 11 and all(e["source"] == "app" for e in rejected)


def test_each_action_kind_does_its_job(world):
    gw = world.serve.Gateway()
    start_run(world)
    world.agent(W1, "idle")
    world.agent(W2, "idle")
    run = lambda: json.loads((world.root / "runs" / "w1-p0.json").read_text())
    until = run()["until"]
    qid = world.orch.ask(W2, "Merge #12?", [{"label": "Merge"}])
    assert world.post(gw, kind="answer", question=qid, text="Merge")[0] == 202
    assert world.prompts[-1][0] == W2 and "Merge" in world.prompts[-1][1]
    assert run()["questions"][0]["answer"] == "Merge"
    assert world.post(gw, kind="park", orchestrator=BOSS, pane=W1, reason="waits on CI",
                      until="+30m")[0] == 202
    assert run()["parked"][W1]["reason"] == "waits on CI"
    assert run()["parked"][W1]["until"] == world.t + 1800
    assert world.post(gw, kind="unpark", orchestrator=BOSS, pane=W1)[0] == 202
    assert W1 not in run()["parked"]
    assert world.post(gw, kind="run", orchestrator=BOSS, op="extend")[0] == 202
    assert run()["until"] == until + 3600  # from the end, not from now
    assert world.post(gw, kind="run", orchestrator=BOSS, op="extend", until="+30m")[0] == 202
    assert run()["until"] == until + 5400
    assert world.post(gw, kind="run", orchestrator=BOSS, op="stop")[0] == 202
    assert run()["ended"] == world.t
    assert world.post(gw, kind="run", orchestrator=BOSS, op="extend")[0] == 202
    assert run()["ended"] is None and run()["until"] == until + 9000
    assert world.post(gw, kind="nudge", orchestrator=BOSS, reasons=["orch:nowake"],
                      text="Give builder #13, then arm one orch wait")[0] == 202
    assert world.prompts[-1] == (BOSS, "Give builder #13, then arm one orch wait")
    assert world.post(gw, kind="interrupt", pane=W1)[0] == 202
    assert world.herdr_calls[-1] == ("pane", "send-keys", W1, "esc")
    assert world.post(gw, kind="account", pane=W1, account="2", when="idle")[0] == 202
    assert world.post(gw, kind="account", pane=W2, account="2", when="handoff")[0] == 202
    switches = json.loads((world.root / "accounts.json").read_text())["switch"]
    assert {p: s["when"] for p, s in switches.items()} == {W1: "idle", W2: "handoff"}
    assert world.post(gw, kind="default_account", account="2")[0] == 202
    assert json.loads((world.root / "accounts.json").read_text())["default"] == "2"
    assert world.get(gw, "/v1/fleet")[1]["default_account"] == "2"
    kinds = [e["action"] for e in world.events() if e["kind"] == "action"]
    assert kinds == ["answer", "park", "unpark", "run", "run", "run", "run", "nudge",
                     "interrupt", "account", "account", "default_account"]


def test_an_account_switch_now_runs_in_the_background(world, monkeypatch):
    gw = world.serve.Gateway()
    world.agent(W1, "idle")
    started = []

    class Now:  # runs the thread's work when the test says so
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            started.append(self.target)

    monkeypatch.setattr(world.serve, "threading", SimpleNamespace(Thread=Now))
    monkeypatch.setattr(world.orch, "switch_job", lambda pane, account, agent: (
        {"pane": pane, "account": account}, None))
    result = iter(["respawn failed: at a dialog", None])
    monkeypatch.setattr(world.orch, "apply_switch", lambda job: next(result))
    assert world.post(gw, id="n1", kind="account", pane=W1, account="2", when="now") == (
        202, {"id": "n1", "status": "delivered", "delivered_at": world.t, "picked_at": None,
              "error": None})
    started.pop()()
    assert world.get(gw, "/v1/actions/n1")[1]["status"] == "failed"
    assert world.get(gw, "/v1/actions/n1")[1]["error"] == "respawn failed: at a dialog"
    assert world.post(gw, id="n2", kind="account", pane=W1, account="2", when="now")[0] == 202
    started.pop()()
    assert world.get(gw, "/v1/actions/n2")[1]["status"] == "picked"
