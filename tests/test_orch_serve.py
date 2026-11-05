"""Offline tests for hooks/orch_serve.py (orch serve): herdr, tailscale and the clock are fakes.
The shapes are checked against orch-app docs/API.md (v1, draft 2026-11-05)."""
import importlib.util
import json
import os
import socket
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
                      "100.64.0.1": {"Node": {"StableID": "nBOX1", "ComputedName": "box1",
                                              "Name": "box1.tail.ts.net."}}}

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
    monkeypatch.setattr(orch, "CONFIG", tmp_path / "config" / ".env")
    monkeypatch.setattr(orch, "_TAILNET_NAME", ["box1"])  # `tailscale status`'s Self.HostName
    monkeypatch.setattr(serve.orch_push, "PHONES", tmp_path / "config" / "phones.json")
    monkeypatch.setattr(serve.orch_push, "SA_FILE", tmp_path / "config" / "firebase-sa.json")
    serve.write_env({"ORCH_TOKEN": TOKEN, "ORCH_ALLOW": "phone"})
    orch.set_name("1", "main")  # `orch account --name`
    orch.set_name("2", "spare")
    w.agent(BOSS, "idle", name="Website")
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
         "switch?": {"account": str, "when": {"handoff", "idle"}, "time": NUM,
                     "reason?": {"spill", "limit"}, "why?": str, "handoff?": NUM},
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
            "evidence": [dict], "rule?": str, "answer?": str, "answered?": NUM}
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
SPILL = {"from": str, "at": NUM, "to": str, "time": NUM,
         "active?": {"window": {"seven_day", "five_hour"}, "used": NUM, "since": NUM}}
FLEET = {"version": int, "machine": str, "time": NUM, "orchestrators": [ORCHESTRATOR],
         "accounts": dict, "default_account": str, "actions": [ACTION_STATUS], "spill?": SPILL}
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
    world.whois["127.0.0.1"] = world.whois["100.64.0.1"]  # the caller is box1 itself
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
    key = env["ORCH_KEY"]
    assert len(world.serve.orch_push.b64d(key)) == 32 and f"&key={key}" in out
    assert "project=" not in out and "pushes: off until the service account key" in out
    world.orch.main(["pair", "--new"])
    assert world.serve.read_env()["ORCH_TOKEN"] != token
    assert world.serve.read_env()["ORCH_KEY"] != key  # a new pairing gets a new push key
    world.serve.orch_push.SA_FILE.write_text(json.dumps({"project_id": "orch-test"}))
    world.orch.main(["pair"])
    out = capsys.readouterr().out
    assert "&project=orch-test" in out and "pushes: Firebase project orch-test" in out


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
    assert run["pane"] == BOSS and run["name"] == "Website" and loose["pane"] == LOOSE
    assert run["activity"] == "stalled" and run["activity_since"] == world.t - 6 * 60
    assert run["run"]["goal"] == "ship the fixes" and loose["run"] is None
    assert loose["activity"] == "working" and loose["workers"] == []
    builder, tester = run["workers"]
    assert builder["state"] == "idle" and builder["since"] == world.t - 20 * 60
    assert builder["context_tokens"] is None  # not in this status file
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
    assert one["name"] == "main" and one["config_dir"] == "~/.claude" and not one["stale"]
    assert [p[1] for p in one["windows"]["seven_day"]["samples"]] == [77, 78, 79, 80]
    assert two["name"] == "spare" and two["windows"] == {} and two["stale"]
    assert fleet["default_account"] == "1"


def test_an_unnamed_orchestrator_is_named_by_its_runs_folder_not_its_goal(world, tmp_path,
                                                                          monkeypatch):
    gw = world.serve.Gateway()
    (tmp_path / "orch-app").mkdir()
    monkeypatch.chdir(tmp_path / "orch-app")
    start_run(world)  # the run's cwd
    world.agent(BOSS, "idle")  # no herdr name
    world.agent(LOOSE, "working")  # no run: its own cwd /work/proj
    run, loose = world.get(gw, "/v1/fleet")[1]["orchestrators"]
    assert run["name"] == "orch-app" and loose["name"] == "proj"
    world.agents[LOOSE]["cwd"] = None
    assert world.get(gw, "/v1/fleet")[1]["orchestrators"][1]["name"] == LOOSE


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
    d = w.root / "home-me-proj" / f"{BOSS.replace(':', '-')}-20261105-093000-abcd1234"
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
    assert (5, world.serve.push_and_relay) in world.serve.BACKGROUND
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


def test_an_answered_question_stays_in_the_fleet_for_10_minutes(world):
    gw = world.serve.Gateway()
    start_run(world)
    world.agent(W2, "idle")
    qid = world.orch.ask(W2, "Merge #12?", [{"label": "Merge"}])
    assert world.post(gw, kind="answer", question=qid, text="Merge")[0] == 202
    world.t += 10 * 60
    [q] = world.get(gw, "/v1/fleet")[1]["orchestrators"][0]["questions"]
    conforms(q, QUESTION)
    assert q["id"] == qid and q["answer"] == "Merge" and q["answered"] == world.t - 600
    world.t += 1
    assert world.get(gw, "/v1/fleet")[1]["orchestrators"][0]["questions"] == []


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
    assert world.post(gw, kind="spill", rule="1:98:2")[0] == 202
    status, fleet = world.get(gw, "/v1/fleet")
    assert status == 200
    conforms(fleet, FLEET)
    assert fleet["spill"]["at"] == 98 and fleet["spill"]["active"] is None
    assert world.post(gw, kind="spill", rule="1:2")[0] == 409
    assert world.post(gw, kind="spill", rule="off")[0] == 202
    assert world.get(gw, "/v1/fleet")[1]["spill"] is None
    assert world.post(gw, kind="default_account", account="2")[0] == 202
    assert json.loads((world.root / "accounts.json").read_text())["default"] == "2"
    assert world.get(gw, "/v1/fleet")[1]["default_account"] == "2"
    kinds = [e["action"] for e in world.events() if e["kind"] == "action"]
    assert kinds == ["answer", "park", "unpark", "run", "run", "run", "run", "nudge",
                     "interrupt", "account", "account", "spill", "spill", "spill",
                     "default_account"]


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


# --- pairing, health, pushes and the relay (orch_push.py; needs `cryptography`) ------------------

KEY = bytes(range(32))
MACHINE = "box1"  # the tailnet name the world fixture gives this machine
HEALTH = {"machine": str, "version": str, "time": NUM, "addr?": str, "orchestrators": [str],
          "accounts": dict, "push": {"registered": bool, "seq": int, "last_sent?": NUM,
                                     "last_heartbeat?": NUM, "error?": str},
          "relay?": {"last_poll?": NUM, "error?": str}}
PAIRED = {"machine": str, "version": str, "orchestrators": [str], "accounts": dict,
          "test_push": {"seq": int, "sent": NUM, "error?": str}}
_RSA = []


class Cloud:
    """orch_push.http's fake: the OAuth token, FCM (it opens what it is sent) and Firestore."""

    def __init__(self, push):
        self.push = push
        self.pushes = []  # {"token", "android", "message"} of every accepted send
        self.fcm = []  # answers to the next sends, (status, body); then 200
        self.docs = {}  # Firestore document name -> fields
        self.sends = 0

    def __call__(self, method, url, body=None, headers=None, form=False, timeout=20):
        if url == self.push.TOKEN_URI:
            return 200, {"access_token": "at", "expires_in": 3600}
        if url.endswith("/messages:send"):
            self.sends += 1
            m = body["message"]
            answer = self.fcm.pop(0) if self.fcm else (200, {"name": f"m{self.sends}"})
            if answer[0] == 200:
                self.pushes.append({"token": m["token"], "android": m["android"],
                                    "message": self.push.unseal(KEY, "push", MACHINE, m["data"])})
            return answer
        name = url.split("/v1/", 1)[1].split("?")[0]
        if method == "DELETE":
            return (200, {}) if self.docs.pop(name, None) is not None else (404, {})
        docs = [{"name": n, "fields": f, "createTime": f"2026-11-05T10:00:0{i}Z"}
                for i, (n, f) in enumerate(self.docs.items()) if n.rsplit("/", 1)[0] == name]
        return 200, ({"documents": docs} if docs else {})

    def write(self, uid, action, key=KEY, doc=None):
        """What the app does off the tailnet: a sealed action in its mailbox for this machine."""
        env = self.push.seal(key, "relay", MACHINE, action)
        doc = doc or f"d{len(self.docs) + 1}"
        name = f"projects/orch-test/databases/(default)/documents/mailboxes/{uid}/{MACHINE}/{doc}"
        self.docs[name] = {k: {"stringValue": env[k]} for k in ("v", "nonce", "ct")}
        return name

    def kinds(self):
        return [p["message"]["kind"] for p in self.pushes]


@pytest.fixture
def cloud(world, monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    if not _RSA:
        _RSA.append(rsa.generate_private_key(65537, 2048, default_backend()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode())
    push = world.serve.orch_push
    push.SA_FILE.write_text(json.dumps({
        "project_id": "orch-test", "private_key_id": "k1", "private_key": _RSA[0],
        "client_email": "orch-push@orch-test.iam.gserviceaccount.com"}))
    world.serve.write_env(dict(world.serve.read_env(), ORCH_KEY=push.b64e(KEY)))
    c = Cloud(push)
    monkeypatch.setattr(push, "http", c)
    return c


def pair(world, gw, token="fcm-1", uid="uid1", addr="127.0.0.1"):
    return gw.route("POST", "/v1/pair", {}, {"fcm_token": token, "device": "OnePlus 12",
                                             "app_version": "0.3.0", "relay_uid": uid}, addr)


def test_health_and_the_server_header(world, cloud):
    gw = world.serve.Gateway()
    server = world.serve.make_server(gw, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v1/health"
    try:
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(url, timeout=10)
        assert refused.value.code == 401 and refused.value.headers["Server"] == "orch/1.0.0"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.headers["Server"] == "orch/1.0.0"
            health = json.loads(r.read())
    finally:
        server.shutdown()
        server.server_close()
    conforms(health, HEALTH)
    assert health["version"] == "1.0.0" and health["addr"] == "100.64.0.1"
    assert health["accounts"] == {"1": "main", "2": "spare"} and "relay" not in health
    assert health["push"]["registered"] is False and health["push"]["seq"] == 0


def test_pairing_stores_the_token_and_sends_a_test_push(world, cloud):
    gw = world.serve.Gateway()
    status, paired = pair(world, gw)
    assert status == 200
    conforms(paired, PAIRED)
    assert paired["test_push"] == {"seq": 1, "sent": world.t}
    [push] = cloud.pushes
    assert push["token"] == "fcm-1" and push["android"] == {"priority": "NORMAL", "ttl": "300s"}
    assert push["message"] == {"seq": 1, "sent": world.t, "kind": "test", "orchestrator": None,
                               "body": None}
    phones = world.serve.orch_push.PHONES
    assert stat.S_IMODE(os.stat(phones).st_mode) == 0o600
    assert json.loads(phones.read_text())["nPHONE"] == {
        "paired": world.t, "updated": world.t, "node": "phone", "fcm_token": "fcm-1",
        "token_error": None, "device": "OnePlus 12", "app_version": "0.3.0", "relay_uid": "uid1"}
    health = gw.route("GET", "/v1/health", {}, None, "127.0.0.1")[1]
    conforms(health, HEALTH)
    assert health["push"] == {"registered": True, "seq": 1, "last_sent": world.t,
                              "last_heartbeat": None}
    assert health["relay"] == {"last_poll": None, "error": None}
    world.t += 60
    assert pair(world, gw, token="fcm-2")[1]["test_push"]["seq"] == 2  # a new token replaces it
    assert json.loads(phones.read_text())["nPHONE"]["fcm_token"] == "fcm-2"
    assert cloud.pushes[-1]["token"] == "fcm-2"


def test_pairing_admits_a_node_of_the_machines_own_user_only(world, cloud, http):
    world.whois["100.64.0.1"]["UserProfile"] = {"LoginName": "owner@example.com"}
    world.whois["127.0.0.1"] = {"Node": {"StableID": "nTAB", "ComputedName": "tablet",
                                         "Name": "tablet.tail.ts.net."},
                                "UserProfile": {"LoginName": "owner@example.com"}}
    call = http()
    assert call("/v1/fleet") == (403, b"")
    body = {"fcm_token": "fcm-t"}
    assert call("/v1/pair", method="POST", body=body, token="wrong") == (401, b"")
    status, out = call("/v1/pair", method="POST", body=body)
    assert status == 200 and json.loads(out)["test_push"]["sent"] == world.t
    assert world.serve.read_env()["ORCH_ALLOW"] == "phone,nTAB"
    assert call("/v1/fleet")[0] == 200
    assert call("/v1/pair", method="DELETE") == (204, b"")
    assert world.serve.read_env()["ORCH_ALLOW"] == "phone"
    assert "nTAB" not in json.loads(world.serve.orch_push.PHONES.read_text())
    assert call("/v1/fleet") == (403, b"")
    world.whois["127.0.0.1"]["UserProfile"]["LoginName"] = "someone@else.com"
    assert call("/v1/pair", method="POST", body=body) == (403, b"")
    world.whois["127.0.0.1"]["UserProfile"]["LoginName"] = "owner@example.com"
    world.whois["100.64.0.1"]["UserProfile"]["LoginName"] = "tagged-devices"
    assert call("/v1/pair", method="POST", body=body) == (403, b"")
    assert [e["title"] for e in world.events() if e["kind"] == "pair"] == [
        "Phone paired: tablet", "Phone unpaired: tablet"]


def test_push_token_replaces_the_phones_token(world, cloud):
    gw = world.serve.Gateway()
    pair(world, gw)
    phones = world.serve.orch_push.PHONES
    action = {"id": "t1", "time": world.t, "kind": "push_token", "fcm_token": "fcm-new"}
    assert gw.route("POST", "/v1/actions", {}, action, "127.0.0.1")[1]["status"] == "delivered"
    assert json.loads(phones.read_text())["nPHONE"]["fcm_token"] == "fcm-new"
    status, out = world.post(gw, id="t2", kind="push_token", fcm_token="x")  # no known phone
    assert status == 409 and "pair it again" in out["error"]


def test_the_push_loop_sends_what_the_phone_needs_once(world, cloud):
    busy_fleet(world)
    gw = world.serve.Gateway()
    pair(world, gw)
    cloud.pushes.clear()
    gw.pusher.cycle()
    # in the order queued; collapsible kinds carry the current seq without raising it, and
    # account 1 burns 4 %/h, so it runs out before its reset: a `usage` alert
    assert [(p["message"]["kind"], p["message"]["orchestrator"], p["message"]["seq"])
            for p in cloud.pushes] == [("state", BOSS, 1), ("state", LOOSE, 1),
                                       ("usage", None, 2), ("accounts", None, 2),
                                       ("heartbeat", None, 2)]
    states = {p["message"]["orchestrator"]: p for p in cloud.pushes if p["message"]["kind"] == "state"}
    assert states[BOSS]["android"] == {"priority": "NORMAL", "ttl": "86400s",
                                       "collapse_key": f"{MACHINE}:state:{BOSS}"}
    _, fleet = gw.fleet()
    assert states[BOSS]["message"]["body"] == fleet["orchestrators"][0]
    [accounts] = [p for p in cloud.pushes if p["message"]["kind"] == "accounts"]
    samples = accounts["message"]["body"]["1"]["windows"]["seven_day"]["samples"]
    assert [s[1] for s in samples] == [77, 78, 79, 80]  # within the last 2 h
    cloud.pushes.clear()
    world.t += 31  # the tester's dialog has been open for 30 s now
    gw.pusher.cycle()
    [dialog] = cloud.pushes
    assert dialog["message"]["kind"] == "dialog" and dialog["message"]["seq"] == 3
    assert dialog["android"] == {"priority": "HIGH", "ttl": "86400s"}
    assert dialog["message"]["orchestrator"] == BOSS and dialog["message"]["body"]["pane"] == W2
    cloud.pushes.clear()
    qid = world.orch.ask(W1, "Merge #12?", [{"label": "Merge"}, {"label": "Not yet"}])
    gw.pusher.cycle()
    [question] = cloud.pushes
    assert question["message"]["kind"] == "question" and question["message"]["seq"] == 4
    assert question["android"]["priority"] == "HIGH"
    assert question["message"]["orchestrator"] == BOSS
    conforms(question["message"]["body"], QUESTION)
    assert question["message"]["body"]["id"] == qid
    cloud.pushes.clear()
    world.t += 300  # the watchdog's nudge was 5 min ago and the run is still stalled
    gw.pusher.cycle()
    assert sorted(cloud.kinds()) == ["stall", "state"]
    [stall] = [p for p in cloud.pushes if p["message"]["kind"] == "stall"]
    assert stall["message"]["seq"] == 5 and stall["message"]["orchestrator"] == BOSS
    conforms(stall["message"]["body"], STALL)
    cloud.pushes.clear()
    world.t += 600
    gw.pusher.cycle()
    [beat] = [p for p in cloud.pushes if p["message"]["kind"] == "heartbeat"]
    assert beat["android"]["collapse_key"] == f"{MACHINE}:heartbeat:"
    assert beat["message"]["seq"] == 5 and set(beat["message"]["body"]["states"]) == {BOSS, LOOSE}
    assert "dialog" not in cloud.kinds() and "stall" not in cloud.kinds()  # each went once
    health = gw.route("GET", "/v1/health", {}, None, "127.0.0.1")[1]["push"]
    assert health["seq"] == 5 and health["last_heartbeat"] == world.t


def test_events_become_pushes_and_old_ones_are_skipped(world, cloud):
    start_run(world)
    gw = world.serve.Gateway()
    pair(world, gw)
    gw.pusher.cycle()
    cloud.pushes.clear()
    world.orch.emit("respawn-failed", "Handoff takeover failed", "w1:p1: at a dialog", W1)
    world.orch.emit("spill", "Account 1 at 98%: moving to 2", "its week reached 98%", None)
    world.orch.emit("report", "Worker report", "...", W1)  # not a push
    gw.pusher.cycle()
    assert [(p["message"]["kind"], p["message"]["orchestrator"], p["message"]["seq"])
            for p in cloud.pushes] == [("system", BOSS, 2), ("spill", None, 3)]
    assert cloud.pushes[0]["message"]["body"] == {
        "time": world.t, "kind": "respawn-failed", "title": "Handoff takeover failed",
        "body": "w1:p1: at a dialog", "pane": W1}
    assert cloud.pushes[1]["message"]["body"]["title"] == "Account 1 at 98%: moving to 2"
    cloud.pushes.clear()
    old = world.t
    world.t += 3700
    world.orch.emit("run-stop", "Run stop", "...", BOSS)
    with open(world.root / "events.jsonl", "a") as f:  # caught up late: over an hour old
        f.write(json.dumps({"time": old, "kind": "orch-missing", "pane": BOSS, "title": "x",
                            "body": "y"}) + "\n")
    gw.pusher.cycle()
    assert sorted(k for k in cloud.kinds() if k != "state") == ["ended", "heartbeat"]
    [ended] = [p for p in cloud.pushes if p["message"]["kind"] == "ended"]
    assert ended["message"]["orchestrator"] == BOSS and ended["message"]["body"]["pane"] == BOSS


def test_the_relay_runs_each_action_once_and_pushes_its_status(world, cloud):
    busy_fleet(world)
    gw = world.serve.Gateway()
    pair(world, gw)
    gw.pusher.cycle()
    cloud.pushes.clear()
    [q] = world.orch.load_run(BOSS)["questions"]
    answer = {"id": "r1", "time": world.t - 20, "kind": "answer", "question": q["id"],
              "text": "Not yet"}
    cloud.write("uid1", answer)
    cloud.write("uid2", dict(answer, id="r9"))  # another phone's mailbox: not ours to read
    assert gw.pusher.poll() is True
    assert world.prompts[-1][0] == W1 and "Not yet" in world.prompts[-1][1]
    assert [n.split("/")[-3] for n in cloud.docs] == ["uid2"]  # ours was deleted
    [event] = [e for e in world.events() if e["kind"] == "action"]
    assert event["source"] == "relay" and event["status"] == "delivered"
    gw.pusher.cycle()
    [acts] = [p for p in cloud.pushes if p["message"]["kind"] == "actions"]
    assert acts["message"]["body"] == [{"id": "r1", "status": "delivered",
                                        "delivered_at": world.t, "picked_at": None,
                                        "error": None}]
    assert acts["message"]["seq"] == 3 and acts["android"]["priority"] == "NORMAL"
    prompts = len(world.prompts)
    cloud.write("uid1", answer)  # the same message again (a replay, or a failed delete)
    cloud.write("uid1", dict(answer, id="r2", text="Merge"))  # the question is closed now
    cloud.write("uid1", dict(answer, id="r3", time=world.t - 11 * 60))  # too old
    cloud.write("uid1", dict(answer, id="r4"), key=bytes(32))  # not sealed with our key
    cloud.pushes.clear()
    assert gw.pusher.poll() is True
    assert len(world.prompts) == prompts and not [n for n in cloud.docs if "/uid1/" in n]
    log = json.loads((world.root / "gateway-actions.json").read_text())
    assert log["r2"]["status"] == "rejected" and "already answered" in log["r2"]["error"]
    assert log["r3"]["status"] == "rejected" and "expire" in log["r3"]["error"]
    assert "r4" not in log
    [dropped] = [e for e in world.events() if e["kind"] == "relay-rejected"]
    assert "does not open with this machine's key" in dropped["body"]
    gw.pusher.cycle()
    [acts] = [p for p in cloud.pushes if p["message"]["kind"] == "actions"]
    assert sorted(a["id"] for a in acts["message"]["body"]) == ["r2", "r3"]
    relay = gw.route("GET", "/v1/health", {}, None, "127.0.0.1")[1]["relay"]
    assert relay == {"last_poll": world.t, "error": None}


def test_a_dead_token_stops_pushes_until_the_app_sends_a_new_one(world, cloud):
    gw = world.serve.Gateway()
    pair(world, gw)
    cloud.fcm = [(400, {"error": {"status": "INVALID_ARGUMENT", "message": "The registration "
                                  "token is not a valid FCM registration token"}})]
    world.orch.ask(BOSS, "Ship?", [])
    gw.pusher.cycle()
    sends = cloud.sends
    assert json.loads(world.serve.orch_push.PHONES.read_text())["nPHONE"]["token_error"] == (
        "INVALID_ARGUMENT")
    assert [e["kind"] for e in world.events()].count("push-token") == 1
    world.t += 700
    gw.pusher.cycle()
    assert cloud.sends == sends  # nothing is sent to a dead token
    push = gw.route("GET", "/v1/health", {}, None, "127.0.0.1")[1]["push"]
    assert push["registered"] is False and "INVALID_ARGUMENT" in push["error"]
    gw.route("POST", "/v1/actions", {}, {"id": "t", "time": world.t, "kind": "push_token",
                                         "fcm_token": "fcm-2"}, "127.0.0.1")
    gw.pusher.cycle()
    assert cloud.pushes[-1]["token"] == "fcm-2"


def test_a_passing_failure_keeps_the_push_for_the_next_pass(world, cloud):
    gw = world.serve.Gateway()
    pair(world, gw)
    gw.pusher.cycle()
    cloud.pushes.clear()
    cloud.fcm = [(503, {"error": {"status": "UNAVAILABLE", "message": "try later"}})]
    world.orch.ask(BOSS, "Ship?", [])
    gw.pusher.cycle()
    assert cloud.pushes == []
    assert "UNAVAILABLE" in gw.route("GET", "/v1/health", {}, None, "127.0.0.1")[1]["push"]["error"]
    world.t += 20
    gw.pusher.cycle()
    [question] = cloud.pushes
    assert question["message"]["seq"] == 2 and question["message"]["sent"] == world.t


def test_without_a_service_account_nothing_is_pushed_and_health_says_why(world, cloud):
    world.serve.orch_push.SA_FILE.unlink()
    gw = world.serve.Gateway()
    status, out = pair(world, gw)
    assert status == 200 and out["test_push"]["seq"] is None
    assert "no service account key" in out["test_push"]["error"]
    gw.pusher.cycle()
    assert cloud.sends == 0
    assert "no service account key" in gw.route("GET", "/v1/health", {}, None,
                                                "127.0.0.1")[1]["push"]["error"]


def test_a_usage_alert_is_sent_once_per_window_even_days_later(world, cloud):
    gw = world.serve.Gateway()
    pair(world, gw)
    resets = world.t + 6.9 * 86400

    def burning():  # account 1 at 4 %/h: full long before its reset in days
        (world.root / "usage.json").write_text(json.dumps({"accounts": {"1": {
            "time": world.t - 30, "windows": {"seven_day": {"used_percentage": 80,
                                                        "resets_at": resets}}}}}))
        with open(world.root / "usage-history.jsonl", "a") as f:
            f.writelines(json.dumps({"time": world.t - 3600 + i * 900, "account": "1", "windows": {
                "seven_day": {"used_percentage": 77 + i, "resets_at": resets}}}) + "\n"
                for i in range(4))
    burning()
    gw.pusher.cycle()
    assert cloud.kinds().count("usage") == 1
    for _ in range(2):  # the second pass is the first after old entries were pruned
        world.t += 2.5 * 86400
        burning()
        gw.pusher.cycle()
    assert cloud.kinds().count("usage") == 1


def test_the_machine_is_named_by_orch_machine_else_tailscale_else_the_hostname(world, monkeypatch,
                                                                              capsys):
    start_run(world)
    gw = world.serve.Gateway()
    world.orch.emit("respawn", "Handoff takeover", "...", BOSS)  # the hooks log the hostname
    assert gw.fleet()[1]["machine"] == "box1" and gw.health(None)["machine"] == "box1"
    assert gw.fleet()[1]["orchestrators"][0]["machine"] == "box1" and gw.pusher.machine == "box1"
    assert {e["host"] for e in gw.history(BOSS, 0, 50)["events"]} == {"box1"}
    world.serve.write_env(dict(world.serve.read_env(), ORCH_MACHINE="box3"))
    assert gw.fleet()[1]["machine"] == "box3" and gw.health(None)["machine"] == "box3"
    assert gw.pusher.machine == "box3" and gw.usage(24)["machine"] == "box3"
    world.orch.main(["pair"])
    assert "orch://pair?machine=box3&" in capsys.readouterr().out
    world.serve.write_env({k: v for k, v in world.serve.read_env().items() if k != "ORCH_MACHINE"})
    monkeypatch.setattr(world.orch, "_TAILNET_NAME", [None])  # tailscale didn't answer
    assert gw.fleet()[1]["machine"] == socket.gethostname()
