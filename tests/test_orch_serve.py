"""Offline tests for hooks/orch_serve.py (orch serve): herdr, tailscale and the clock are fakes.
The shapes are checked against orch-app docs/API.md (v1, draft 2026-11-05)."""
import importlib.util
import json
import os
import re
import socket
import stat
import subprocess
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
NEW = "w1:p5"  # the pane `herdr tab create` opens
TRUST = """\
my-app on  main
❯ /home/me/.local/bin/claude-acc 2 --session-id 0e1f --model sonnet

────────────────────────────────────────────────────────────────────
 Accessing workspace:

 /home/me/Repo/my-app

 Quick safety check: Is this a project you created or one you trust? (Like your own code, a
 well-known open source project, or work from your team). If not, take a moment to review
 what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel"""  # Claude Code 2.x, as seen live on 2026-11-05
# Claude Code 2.1.318's permission prompts, captured with `herdr agent read --source visible` on
# 2026-11-05 in an 8-row pane: a short terminal shows only the option under the cursor.
BASH = """\
 Remove notes.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 rm notes.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to proceed?
 ❯ 1. Yes

 Esc to cancel · Tab to amend
"""
BASH_3 = """\
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 rm notes.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to proceed?
 ❯ 3. Yes, and switch to auto mode · auto mode handles
      these prompts for you

 Esc to cancel
"""
WRITE = """\
 hello.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  1 hi
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to create hello.txt?
 ❯ 1. Yes

 Esc to cancel · Tab to amend
"""
WRITE_2 = """\
  1 hi
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to create hello.txt?
 ❯ 2. Yes, and switch to accept edits (auto-approve file
      edits and common file commands) for this session
      (shift+tab)

 Esc to cancel
"""
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
        self.tab_ok = True  # whether `herdr tab create` opens NEW
        self.tab_cwd = None
        self.tab_alive = False  # whether the tab outlives its pane (someone split it)
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
                                   stdout=self.screen, stderr="")
        if args[:2] == ("tab", "create"):
            if not self.tab_ok:
                return SimpleNamespace(returncode=1, stdout="", stderr="no such workspace")
            self.tab_cwd = args[args.index("--cwd") + 1]
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                {"result": {"tab": {"tab_id": "w1:t9"}, "root_pane": {"pane_id": NEW}}}))
        if args[:2] == ("pane", "run"):  # Claude comes up idle in the new tab's folder
            self.agent(args[2], "idle")
            self.agents[args[2]]["cwd"] = self.tab_cwd
        if args[:2] == ("agent", "send-keys") and args[-1] == "enter" and "trust" in self.screen:
            lines = self.screen.split("\n")
            cursor = next(i for i, line in enumerate(lines)
                          if line.strip().startswith("❯ No") or line.strip().startswith("❯ Yes"))
            if "No, exit" in lines[cursor + args.count("down") - args.count("up")]:
                self.agents.pop(args[2], None)  # Claude quits
            else:
                self.agents[args[2]]["agent_status"] = "idle"
        if args[:2] == ("pane", "close"):
            self.agents.pop(args[2], None)
        if args[:2] == ("tab", "get"):
            return SimpleNamespace(returncode=0 if self.tab_alive else 1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def prompt(self, pane, text):
        self.prompts.append((pane, text))
        if text == "/exit":
            self.agents.pop(pane, None)
        return self.prompt_ok

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
    monkeypatch.setattr(orch, "STARTS", home / ".claude" / "orch" / "new")
    monkeypatch.setattr(orch, "PROJECTS", home / ".claude" / "projects")
    monkeypatch.setattr(orch, "now", lambda: w.t)
    monkeypatch.setattr(orch, "agents", lambda: dict(w.agents))
    monkeypatch.setattr(orch, "prompt", w.prompt)
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
STATE = {"working", "blocked", "idle", "own-job", "parked", "missing", "unknown"}
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
               "roadmap_time?": NUM, "risks?": [str], "milestones?": [dict],
               "suggestions?": [str], "source": {"report", "handoff"}}
WORKER_REPORT = {"time": NUM, "summary": str, "status?": {"done", "partial", "blocked"},
                 "tests?": [{"cmd": str, "passed?": int, "failed?": int}],
                 "changed_files?": [{"path": str, "added?": int, "removed?": int}],
                 "needs_live_test?": [str], "issues_for_others?": [str]}
EVIDENCE = {"kind": {"pr", "branch", "files", "tests"}, "label": str, "pane?": str,
            "tab?": {"report", "diff"}, "ok?": bool}
QUESTION = {"id": str, "pane": str, "time": NUM, "text": str,
            "options": [{"label": str, "danger": {"none", "deploy", "prod", "destructive"}}],
            "evidence": [EVIDENCE], "rule?": str, "kind?": {"plan"}, "answer?": str,
            "answered?": NUM, "default?": str, "default_at?": NUM, "by_default": bool}
DIALOG = {"pane": str, "label": str, "since?": NUM, "change": int, "tool?": str,
          "command?": str, "cwd?": str, "doing?": str, "danger": bool, "text": str,
          "kind?": {"trust"}}
STALL = {"nudged_at?": NUM, "reasons": [{"key": str, "kind": str, "pane?": str, "text": str,
                                         "fix?": str, "message?": str, "picked": bool}]}
WINDOW = {"used_percentage": NUM, "resets_at": NUM, "burn_per_hour?": NUM, "basis_minutes?": NUM,
          "projected_full_at?": NUM, "full_before_reset?": bool, "samples": [[NUM]]}
ACCOUNT = {"id": str, "name": str, "login?": str, "config_dir": str, "logged_in": bool,
           "updated?": NUM,
           "stale": bool, "pct_per_100k_uncached?": NUM, "windows": {"seven_day?": WINDOW,
                                                                      "five_hour?": WINDOW}}
ACTION_STATUS = {"id": str, "status": {"delivered", "picked", "rejected", "failed"},
                 "delivered_at?": NUM, "picked_at?": NUM, "error?": str, "pane?": str}
ORCHESTRATOR = {"pane": str, "name": str, "machine": str, "cwd?": str,
                "activity": {"working", "waiting", "stalled", "done", "offline", "starting"},
                "activity_since?": NUM, "agent": AGENT,
                "run?": {"goal": str, "created": NUM, "until": NUM, "ended?": NUM,
                         "settings?": dict, "cost_usd?": NUM, "week_points?": NUM},
                "report?": ORCH_REPORT, "questions": [QUESTION], "dialogs": [DIALOG],
                "stall?": STALL, "workers": [AGENT], "lanes?": dict,
                "start?": {"action": str, "goal": str, "plan_first": bool, "until": NUM,
                           "steps": [{"step": {"pane", "claude", "trust", "run", "read",
                                               "brief", "plan"},
                                      "status": {"doing", "ok", "failed"}, "time": NUM,
                                      "error?": str}]}}
SPILL = {"from": str, "at": NUM, "to": str, "time": NUM,
         "active?": {"window": {"seven_day", "five_hour"}, "used": NUM, "since": NUM,
                     "resets_at?": NUM}}
FLEET = {"version": int, "machine": str, "time": NUM, "gaming": bool,
         "orchestrators": [ORCHESTRATOR],
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
         "other": [{"label": str, "account?": str, "cost_usd": NUM, "points_last_hour?": NUM,
                    "points_today?": NUM, "points_7d?": NUM, "note": str}],
         "runs": dict}
RUN_USAGE = {"orchestrator": str, "cost": NUM, "week_points?": NUM, "account?": str, "hours": NUM,
             "max_workers": int}


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
    world.serve.write_env({"ORCH_START_ROOTS": "~/Repo"})  # settings, but not paired
    assert world.orch.main(["serve"]) == world.serve.NOT_PAIRED == 3  # orch-serve.service's
    assert "not paired: run `orch pair` first" in capsys.readouterr().err


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
        "read_at": w.t - 30, "capture_since": w.t - 30 * 86400,
        "windows": {"seven_day": {"used_percentage": 80, "resets_at": w.t + 86400}}}}}))
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
    world.orch.emit("pair", "Phone paired", "x", None)  # machine-wide, not the run's
    world.t += 60
    world.orch.emit("spill", "Account 1 at 98%: moving to 2", "its agents move", None)
    world.orch.emit("nudge", "Watchdog nudged", "go", BOSS)
    status, out = world.get(gw, f"/v1/orchestrators/{BOSS}/history")
    assert status == 200
    conforms(out, HISTORY)
    assert [e["kind"] for e in out["events"]] == ["nudge", "spill", "report", "run-start"]
    assert out["events"][1]["pane"] is None
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
    world.serve.warm_usage(world.serve.Gateway())
    assert (world.root / "usage-cache.json").exists()


def assistant(at, mid, out_tokens):  # opus 5.5: $4 / $20 per million in / out
    stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return json.dumps({"type": "assistant", "timestamp": stamp, "message": {
        "id": mid, "model": "claude-opus-5-5",
        "usage": {"input_tokens": 1000, "output_tokens": out_tokens}}}) + "\n"


def spend(world, session, *lines):
    project = world.home / ".claude" / "projects" / "-work-proj"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session}.jsonl").write_text("".join(lines))


def change_run(world, **fields):
    path = world.root / "runs" / "w1-p0.json"
    path.write_text(json.dumps(dict(json.loads(path.read_text()), **fields)))


def test_usage_has_the_api_shape_and_points_per_period(world):
    busy_fleet(world)
    usage = json.loads((world.root / "usage.json").read_text())
    usage["sessions"] = {"s-other": {"account": "1", "time": world.t}}
    (world.root / "usage.json").write_text(json.dumps(usage))
    spend(world, f"s-{W1}", assistant(world.t - 1800, "m1", 1_000_000),
          assistant(world.t - 3 * 86400, "m2", 3_000_000))
    spend(world, "s-other", assistant(world.t - 600, "m3", 500_000))
    change_run(world, created=world.t - 4 * 86400)
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
    assert other["cost_usd"] == 10.0  # rounded per agent; $10.004 in all
    assert other["note"].startswith("1 sessions outside a run")
    [run] = out["runs"].values()
    conforms(run, RUN_USAGE)
    assert run["week_points"] is None  # the orchestrator itself spent nothing here
    assert run["account"] is None and run["max_workers"] == 2  # builder and tester
    assert run["hours"] == 96
    assert out["accounts"]["1"]["windows"]["seven_day"]["machine_cost_usd"] == 90.01


def test_sessions_from_before_the_account_capture_have_no_account_and_their_cost(world):
    busy_fleet(world)
    spend(world, "s-old", assistant(world.t - 600, "m1", 1_000_000))
    spend(world, "s-older", assistant(world.t - 700, "m2", 2_000_000))
    out = world.get(world.serve.Gateway(), "/v1/usage?hours=168")[1]
    conforms(out, USAGE)
    [other] = out["other"]
    assert other["account"] is None and other["cost_usd"] == 60.0  # each rounded to cents
    assert other["note"].startswith("2 sessions outside a run")


def test_a_runs_hours_stop_at_its_end_and_max_workers_comes_from_its_settings(world):
    costly_run(world)
    change_run(world, settings={"max_workers": 5}, ended=world.t - 2 * 86400)
    spend(world, f"s-{BOSS}", assistant(world.t - 3 * 86400, "m1", 1_000_000))
    [run] = world.get(world.serve.Gateway(), "/v1/usage?hours=168")[1]["runs"].values()
    conforms(run, RUN_USAGE)
    assert run["hours"] == 48 and run["max_workers"] == 5 and run["account"] == "1"
    assert run["cost"] == 20.0


def test_a_running_runs_hours_count_to_now_not_its_end(world):
    costly_run(world)
    world.t += 3600
    [run] = world.get(world.serve.Gateway(), "/v1/usage?hours=168")[1]["runs"].values()
    assert run["hours"] == 97 and run["ended"] is None


def costly_run(world):
    """The run's orchestrator and builder spent $20.004 each, all of this machine's week so far
    (80%): the run has cost $40.01 and 80 points."""
    busy_fleet(world)
    spend(world, f"s-{BOSS}", assistant(world.t - 1800, "m1", 1_000_000))
    spend(world, f"s-{W1}", assistant(world.t - 600, "m2", 1_000_000))
    change_run(world, created=world.t - 4 * 86400)


def test_an_agent_herdr_cant_read_is_unknown_not_working(world):
    busy_fleet(world)
    world.agent(W1, "unknown", change=8)
    world.agent(BOSS, "unknown", name="Website")
    status, fleet = world.get(world.serve.Gateway(), "/v1/fleet")
    assert status == 200
    conforms(fleet, FLEET)
    lead = fleet["orchestrators"][0]
    builder = next(w for w in lead["workers"] if w["pane"] == W1)
    assert builder["state"] == "unknown" and builder["agent_status"] == "unknown"
    assert lead["agent"]["state"] == "unknown"
    assert lead["activity"] == "working" and lead["stall"] is None  # nothing shows it stopped


def test_the_fleet_keeps_a_plan_across_reports_and_says_where_it_came_from(world):
    """`source` says where the plan came from, so it goes with the plan and not with the report
    that happens to be the latest."""
    busy_fleet(world)
    gw = world.serve.Gateway()
    lead = lambda: gw.fleet()[1]["orchestrators"][0]["report"]
    written = world.t - 400
    assert lead()["roadmap_time"] == written and lead()["source"] == "report"
    conforms(lead(), ORCH_REPORT)

    world.t += 60
    world.orch.write_report(BOSS, {"role": "orchestrator", "time": world.t, "summary": "5 of 9"})
    assert [r["title"] for r in lead()["roadmap"]] == ["flaky test"]
    assert lead()["roadmap_time"] == written and lead()["time"] == world.t
    conforms(lead(), ORCH_REPORT)

    path = world.root / "agents" / "w1-p0.json"
    status = json.loads(path.read_text())
    status["report"]["source"] = "handoff"
    path.write_text(json.dumps(status))
    world.orch.write_report(BOSS, {"role": "orchestrator", "time": world.t, "summary": "6 of 9"})
    assert lead()["source"] == "handoff"

    world.orch.write_report(BOSS, {"role": "orchestrator", "time": world.t, "summary": "7 of 9",
                                   "roadmap": [{"title": "ship", "status": "next"}]})
    assert lead()["source"] == "report" and lead()["roadmap_time"] == world.t


PLAN = [{"title": "flaky test", "status": "next", "owner": "builder"}]


def old_session_reports(w, pane, role="orchestrator", **fields):
    """What the MCP server of a session started before plans were kept does: write_report as it
    loaded it, the whole report over the last one, and the same report in the log."""
    report = dict({"role": role, "time": w.t, "summary": "s"}, **fields)
    path = w.root / "agents" / f"{pane.replace(':', '-')}.json"
    w.orch.save(path, dict(w.orch.load(path) or {"pane": pane}, report=report))
    w.orch.emit("report", "Orchestrator report", report["summary"], pane, report=report)


def new_session_reports(w, pane, **fields):
    w.orch.write_report(pane, dict({"role": "orchestrator", "time": w.t, "summary": "s"}, **fields))


def lead_report(gw, pane=BOSS):
    return next(o for o in gw.fleet()[1]["orchestrators"] if o["pane"] == pane)["report"]


def test_the_fleet_gives_an_old_sessions_report_its_plan_back_from_the_log(world):
    start_run(world)
    gw = world.serve.Gateway()
    written = world.t
    old_session_reports(world, BOSS, roadmap=PLAN, next=["deploy"])
    assert lead_report(gw)["roadmap_time"] == written  # a report with its own plan has it too
    conforms(lead_report(gw), ORCH_REPORT)

    world.t += 60
    old_session_reports(world, BOSS, summary="no plan in this one")
    stored = json.loads((world.root / "agents" / "w1-p0.json").read_text())["report"]
    assert "roadmap" not in stored  # lost on disk, as on the machines today: nothing is rewritten

    report = lead_report(gw)
    assert report["roadmap"] == PLAN and report["roadmap_time"] == written
    assert report["time"] == world.t and report["summary"] == "no plan in this one"
    assert "next" not in report and report["source"] == "report"
    conforms(report, ORCH_REPORT)
    assert lead_report(world.serve.Gateway()) == report  # from the log: a restart changes nothing


def test_an_old_session_clears_the_plan_with_an_empty_roadmap(world):
    start_run(world)
    gw = world.serve.Gateway()
    old_session_reports(world, BOSS, roadmap=PLAN)
    world.t += 60
    cleared = world.t
    old_session_reports(world, BOSS, roadmap=[])
    assert lead_report(gw)["roadmap"] == [] and lead_report(gw)["roadmap_time"] == cleared
    world.t += 60
    old_session_reports(world, BOSS)
    assert lead_report(gw)["roadmap"] == [] and lead_report(gw)["roadmap_time"] == cleared


def test_old_and_new_sessions_on_a_pane_share_one_plan(world):
    """A takeover can put a new session after an old one, in either order of the two writers."""
    start_run(world)
    gw = world.serve.Gateway()
    first = world.t
    old_session_reports(world, BOSS, roadmap=PLAN)
    world.t += 60
    old_session_reports(world, BOSS)
    world.t += 60
    new_session_reports(world, BOSS)  # nothing in its status file to keep: the log still has it
    assert lead_report(gw)["roadmap"] == PLAN and lead_report(gw)["roadmap_time"] == first

    world.t += 60
    newer, second = [{"title": "ship", "status": "doing"}], world.t
    new_session_reports(world, BOSS, roadmap=newer)
    world.t += 60
    new_session_reports(world, BOSS)
    assert lead_report(gw)["roadmap"] == newer and lead_report(gw)["roadmap_time"] == second
    world.t += 60
    old_session_reports(world, BOSS)  # the whole report over the kept plan
    assert lead_report(gw)["roadmap"] == newer and lead_report(gw)["roadmap_time"] == second

    world.t += 60
    new_session_reports(world, BOSS, roadmap=[])
    world.t += 60
    old_session_reports(world, BOSS)
    assert lead_report(gw)["roadmap"] == []


def test_a_plan_stays_with_its_pane_and_a_worker_gets_none(world):
    busy_fleet(world)
    gw = world.serve.Gateway()
    old_session_reports(world, LOOSE)
    old_session_reports(world, W1, role="worker", roadmap=PLAN)
    old_session_reports(world, W1, role="worker", status="done")
    _, fleet = gw.fleet()
    assert not {"roadmap", "roadmap_time"} & set(lead_report(gw, LOOSE))
    [boss] = [o for o in fleet["orchestrators"] if o["pane"] == BOSS]
    [builder] = [w for w in boss["workers"] if w["pane"] == W1]
    assert "roadmap" not in builder["report"]


def test_a_lead_with_no_report_gets_no_plan_from_the_log(world):
    start_run(world)
    gw = world.serve.Gateway()
    old_session_reports(world, BOSS, roadmap=PLAN)
    (world.root / "agents" / "w1-p0.json").unlink()
    assert lead_report(gw) is None


def test_a_stall_fix_and_a_state_push_use_the_plan_from_the_log(world, cloud):
    busy_fleet(world)
    gw = world.serve.Gateway()
    pair(world, gw)
    world.t += 60
    old_session_reports(world, BOSS, state="waiting_on_workers")

    [boss] = [o for o in gw.fleet()[1]["orchestrators"] if o["pane"] == BOSS]
    [idle] = [r for r in boss["stall"]["reasons"] if r.get("pane") == W1]
    assert idle["fix"] == "Assign: flaky test"  # the next item of the plan, for the idle builder
    cloud.pushes.clear()
    gw.pusher.cycle()
    [state] = [p for p in cloud.pushes if p["message"]["kind"] == "state"
               and p["message"]["orchestrator"] == BOSS]
    assert [r["title"] for r in state["message"]["body"]["report"]["roadmap"]] == ["flaky test"]


def test_the_fleet_has_the_run_cost_from_the_background_pass_only(world, monkeypatch):
    costly_run(world)
    gw = world.serve.Gateway()
    assert "cost_usd" not in gw.fleet()[1]["orchestrators"][0]["run"]  # no pass yet
    world.serve.warm_usage(gw)

    def no_scan(*args, **kwargs):
        raise AssertionError("a fleet read scanned the transcripts")
    monkeypatch.setattr(world.serve.orch_usage, "report", no_scan)
    monkeypatch.setattr(world.serve.orch_usage, "scan", no_scan)
    status, fleet = world.get(gw, "/v1/fleet")
    assert status == 200
    conforms(fleet, FLEET)
    run = fleet["orchestrators"][0]["run"]
    assert run["cost_usd"] == 40.01 and run["week_points"] == pytest.approx(80, abs=0.01)
    assert run["goal"] == "ship the fixes"  # next to the run's own fields


def test_a_run_whose_lead_spent_long_before_the_default_period_still_has_week_points(world):
    costly_run(world)
    spend(world, f"s-{BOSS}", assistant(world.t - 3 * 86400, "m1", 1_000_000))
    gw = world.serve.Gateway()
    world.serve.warm_usage(gw)
    assert gw.fleet()[1]["orchestrators"][0]["run"]["week_points"] == pytest.approx(80, abs=0.01)
    assert gw.usage(1)["runs"][next(iter(gw.run_costs))]["week_points"] is None  # why: not 1 h


def test_a_questions_evidence_and_rule_reach_the_fleet_and_its_push(world, cloud):
    start_run(world)
    gw = world.serve.Gateway()
    pair(world, gw)
    gw.pusher.cycle()
    cloud.pushes.clear()
    chip = {"kind": "pr", "label": "#12 checks 14/14", "pane": W1, "tab": "diff", "ok": True}
    qid = world.orch.ask(W1, "Merge #12?", [{"label": "Merge"}], evidence=[chip], rule="D27")
    q = gw.fleet()[1]["orchestrators"][0]["questions"][0]
    conforms(q, QUESTION)
    assert q["id"] == qid and q["evidence"] == [chip] and q["rule"] == "D27"
    gw.pusher.cycle()
    [push] = [p["message"] for p in cloud.pushes if p["message"]["kind"] == "question"]
    assert push["body"]["evidence"] == [chip] and push["body"]["rule"] == "D27"


def test_an_earlier_session_with_no_account_doesnt_hide_the_agents_account(world):
    costly_run(world)
    # the orchestrator's previous generation: known from its lineage folder, not by an account
    spend(world, "zzzzzzzz-old", assistant(world.t - 7200, "m3", 1_000_000))
    (world.root / "w1-p0" / "w1-p0-20261105-080000-zzzzzzzz").mkdir(parents=True)
    out = world.get(world.serve.Gateway(), "/v1/usage?hours=168")[1]
    [lead] = [a for a in out["agents"] if a["pane"] == BOSS]
    assert lead["accounts"] == ["1", "unknown"]
    assert lead["account"] == "1" and lead["points_7d"] is not None
    [run] = out["runs"].values()
    assert run["account"] == "1" and run["week_points"] is not None


def set_reading(world, **fields):
    path = world.root / "usage.json"
    usage = json.loads(path.read_text())
    usage["accounts"]["1"].update(fields)
    path.write_text(json.dumps(usage))


def test_points_are_null_while_the_window_opened_before_the_capture_began(world):
    costly_run(world)
    spend(world, "s-other", assistant(world.t - 600, "m4", 500_000))
    usage = json.loads((world.root / "usage.json").read_text())
    usage["sessions"] = {"s-other": {"account": "1", "time": world.t}}
    (world.root / "usage.json").write_text(json.dumps(usage))
    set_reading(world, capture_since=world.t - 3600)  # the window opened 6 days ago
    gw = world.serve.Gateway()
    world.serve.warm_usage(gw)

    status, out = world.get(gw, "/v1/usage?hours=168")
    assert status == 200
    conforms(out, USAGE)
    periods = ("points_last_hour", "points_today", "points_7d")
    for row in [*out["agents"], *out["other"]]:
        assert [row[k] for k in periods] == [None, None, None]
    assert out["other"][0]["cost_usd"] == 10.0  # the dollars are still right
    for agent in out["accounts"]["1"]["windows"]["seven_day"]["agents"].values():
        assert agent["estimated_points"] is None
    run = next(iter(out["runs"].values()))
    assert run["week_points"] is None and run["cost"] == 40.01 and run["account"] == "1"
    fleet = gw.fleet()[1]
    conforms(fleet, FLEET)
    assert fleet["orchestrators"][0]["run"]["week_points"] is None
    assert fleet["orchestrators"][0]["run"]["cost_usd"] == 40.01


def test_points_come_back_with_the_first_window_opened_after_the_capture_began(world):
    costly_run(world)
    set_reading(world, capture_since=world.t - 3600)
    gw = world.serve.Gateway()
    assert gw.usage(168)["agents"][0]["points_7d"] is None
    set_reading(world, windows={"seven_day": {"used_percentage": 10,  # opened 30 minutes ago
                                              "resets_at": world.t + 7 * 86400 - 1800}})
    spend(world, f"s-{BOSS}", assistant(world.t - 1800, "m1", 1_000_000),
          assistant(world.t - 60, "m5", 1_000_000))
    [lead] = [a for a in gw.usage(168)["agents"] if a["pane"] == BOSS]
    assert lead["points_7d"] == pytest.approx(6.7, abs=0.05)  # 2 of its 3 messages' cost


def test_a_reading_with_no_capture_time_has_no_points(world):
    costly_run(world)
    set_reading(world, capture_since=None)
    out = world.serve.Gateway().usage(168)
    assert out["agents"][0]["points_7d"] is None
    assert next(iter(out["runs"].values()))["week_points"] is None


def test_a_state_push_carries_the_run_cost(world, cloud):
    costly_run(world)
    gw = world.serve.Gateway()
    pair(world, gw)
    world.serve.warm_usage(gw)
    cloud.pushes.clear()
    gw.pusher.cycle()
    [state] = [p["message"] for p in cloud.pushes
               if p["message"]["kind"] == "state" and p["message"]["orchestrator"] == BOSS]
    assert state["body"]["run"]["cost_usd"] == 40.01
    assert state["body"]["run"]["week_points"] == pytest.approx(80, abs=0.01)


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
    assert world.post(gw, id="d1", kind="dialog", pane=LOOSE, change=1, choice="allow")[0] == 400
    assert world.post(gw, id="r1", kind="run", orchestrator=W2, op="extend")[0] == 409
    assert world.post(gw, id="pk", kind="park", orchestrator=BOSS, pane=LOOSE)[0] == 409
    qid = world.orch.ask(W1, "Deploy?")
    assert world.post(gw, id="q1", kind="answer", question=qid, text="yes")[0] == 202
    again = world.post(gw, id="q2", kind="answer", question=qid, text="no")
    assert again[0] == 409 and "already answered" in again[1]["error"]
    rejected = [e for e in world.events() if e["kind"] == "action" and e["status"] != "delivered"]
    assert len(rejected) == 11 and all(e["source"] == "app" for e in rejected)


def test_the_fleet_shows_a_questions_default_and_that_it_was_taken(world):
    world.orch.main(["--orch", BOSS, "run", "start", "--until", "+8h", "--goal", "x"])
    qid = world.orch.ask(BOSS, "Both OK?", [{"label": "Yes"}, {"label": "No"}], default="Yes",
                         by="+30m")
    gw = world.serve.Gateway()

    def question():
        [lead] = world.get(gw, "/v1/fleet")[1]["orchestrators"]
        return next(q for q in lead["questions"] if q["id"] == qid)

    open_one = question()
    assert open_one["default"] == "Yes" and open_one["default_at"] == world.t + 1800
    assert open_one["by_default"] is False and open_one["answered"] is None

    world.t += 1800
    world.orch.default_answers(world.orch.agents(), False)

    closed = question()
    assert closed["by_default"] is True and closed["answer"] == "Yes"
    assert closed["answered"] == world.t


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


def test_accounts_carry_the_hash_of_their_login_for_the_app_to_merge_machines_by(world):
    (world.home / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "accountUuid": "u1", "organizationUuid": "o1", "emailAddress": "me@example.com"}}))
    gw = world.serve.Gateway()

    fleet = world.get(gw, "/v1/fleet")[1]

    assert fleet["accounts"]["1"]["login"] == world.orch.account_login(1)
    assert len(fleet["accounts"]["1"]["login"]) == 16
    assert fleet["accounts"]["2"]["login"] is None  # no login known: the app merges nothing
    assert "example" not in json.dumps(fleet["accounts"])
    assert world.get(gw, "/v1/usage?hours=24")[1]["accounts"]["1"]["login"] == (
        fleet["accounts"]["1"]["login"])


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


# --- starting a new orchestrator (the `start` action, orch_new.py) and the folders --------------

def allow_starts(w, monkeypatch, run_it=True, **env):
    """Starts from the app under ~/Repo; the detached start (orch_new.spawn) runs in-process,
    or not at all. Returns the folder ~/Repo/my-app, a git checkout."""
    w.serve.write_env(dict(w.serve.read_env(), ORCH_START_ROOTS="~/Repo", **env))
    new = w.serve.orch_new
    monkeypatch.setattr(new, "spawn", lambda sid: run_it and new.start(
        w.orch.load(w.orch.STARTS / sid / "new.json")))
    monkeypatch.setattr(w.orch, "sleep", lambda s: setattr(w, "t", w.t + s))
    app = w.home / "Repo" / "my-app"
    (app / ".git").mkdir(parents=True, exist_ok=True)
    return app


def lead_of(w, gw, pane):
    [lead] = [o for o in w.get(gw, "/v1/fleet")[1]["orchestrators"] if o["pane"] == pane]
    return lead


def start_action(**kw):
    return dict({"kind": "start", "folder": "~/Repo/my-app", "goal": "Ship the beta",
                 "until": "+8h", "account": "2", "model": "sonnet", "effort": "high",
                 "worker_model": "haiku", "max_workers": 3, "rules": ["No pushes to main"],
                 "plan_first": True}, **kw)


def test_a_start_answers_with_its_pane_and_the_fleet_shows_its_steps_until_the_plan_is_ok(
        world, monkeypatch):
    app = allow_starts(world, monkeypatch)
    gw = world.serve.Gateway()
    status, out = world.post(gw, **start_action(id="st1"))
    assert status == 202 and out["status"] == "delivered" and out["pane"] == NEW
    conforms(out, ACTION_STATUS)
    assert world.get(gw, "/v1/actions/st1")[1]["pane"] == NEW
    rec = world.orch.load(world.orch.STARTS / "st1" / "new.json")
    assert rec["by"] == "phone" and rec["folder"] == str(app) and rec["account"] == "2"
    assert world.prompts == [(NEW, f"Read {rec['brief']} and do it.")]
    [action] = [e for e in world.events() if e["kind"] == "action"]
    assert action["pane"] == NEW and action["status"] == "delivered"
    fleet = world.get(gw, "/v1/fleet")[1]
    conforms(fleet, FLEET)
    [lead] = [o for o in fleet["orchestrators"] if o["pane"] == NEW]
    assert lead["activity"] == "starting" and lead["activity_since"] == rec["time"]
    assert lead["name"] == "my-app" and lead["cwd"] == str(app) and lead["stall"] is None
    assert {k: lead["start"][k] for k in ("action", "goal", "plan_first", "until")} == {
        "action": "st1", "goal": "Ship the beta", "plan_first": True, "until": rec["until"]}
    assert [(s["step"], s["status"]) for s in lead["start"]["steps"]] == [
        ("pane", "ok"), ("claude", "ok"), ("run", "ok"), ("read", "doing")]
    assert lead["run"]["settings"] == {
        "folder": str(app), "worktree": False, "model": "sonnet", "effort": "high",
        "worker_model": "haiku", "max_workers": 3, "plan_first": True, "account": "2"}
    # the plan: the new orchestrator asks, the owner approves with the `answer` action
    qid = world.orch.ask(NEW, "Plan ready", [{"label": "Approve and start workers"},
                                             {"label": "Cancel the run"}], kind="plan")
    lead = lead_of(world, gw, NEW)
    [q] = lead["questions"]
    conforms(q, QUESTION)
    assert q["kind"] == "plan" and lead["activity"] == "starting"
    assert [(s["step"], s["status"]) for s in lead["start"]["steps"]][-2:] == [
        ("read", "ok"), ("plan", "doing")]
    assert world.post(gw, kind="answer", question=qid, text="Approve and start workers")[0] == 202
    lead = lead_of(world, gw, NEW)
    assert lead["pane"] == NEW and lead["activity"] != "starting" and lead["start"] is None


def test_a_start_is_refused_outside_the_roots_in_excluded_or_busy_folders(world, monkeypatch,
                                                                          tmp_path):
    gw = world.serve.Gateway()
    app = allow_starts(world, monkeypatch, ORCH_START_EXCLUDE="~/Repo/live")
    (world.home / "Repo" / "live").mkdir()
    (world.home / "Other").mkdir()
    world.serve.write_env({k: v for k, v in world.serve.read_env().items()
                           if k != "ORCH_START_ROOTS"})
    off = world.post(gw, **start_action(id="r0"))
    assert off[0] == 409 and "starts from the app are off on box1" in off[1]["error"]
    world.serve.write_env(dict(world.serve.read_env(), ORCH_START_ROOTS="~/Repo"))
    outside = world.post(gw, **start_action(id="r1", folder="~/Other"))
    assert outside[0] == 409 and outside[1]["error"] == (
        "~/Other is outside the start folders on box1 (~/Repo)")
    live = world.post(gw, **start_action(id="r2", folder="~/Repo/live"))
    assert live[0] == 409 and "~/Repo/live is excluded from starts on box1" in live[1]["error"]
    missing = world.post(gw, **start_action(id="r3", folder="~/Repo/nope"))
    assert missing[0] == 409 and "is not a folder on box1" in missing[1]["error"]
    monkeypatch.chdir(app)
    start_run(world)  # BOSS works in ~/Repo/my-app
    busy = world.post(gw, **start_action(id="r4"))
    assert busy[0] == 409 and busy[1]["error"].startswith(f"{BOSS} works in ~/Repo/my-app")
    bad = world.post(gw, **start_action(id="r5", folder="~/Other", max_workers=99))
    assert bad[0] == 409 and "max_workers 99" in bad[1]["error"]
    assert not [c for c in world.herdr_calls if c[:2] == ("tab", "create")]
    assert not world.orch.STARTS.exists()


def test_a_start_runs_once_per_id(world, monkeypatch):
    allow_starts(world, monkeypatch)
    gw = world.serve.Gateway()
    action = start_action(id="st1", time=world.t)
    first = world.post(gw, **action)
    assert world.post(gw, **action) == first  # the app's retry: the same body
    other = world.post(gw, **start_action(id="st1", goal="Something else"))
    assert other[0] == 409 and "already used" in other[1]["error"]
    (world.root / "gateway-actions.json").unlink()  # the action log forgot it: the start didn't
    busy = world.post(gw, **start_action(id="st1"))
    assert busy[0] == 409 and busy[1]["error"].startswith(f"{NEW} works in ~/Repo/my-app")
    world.orch.main(["--orch", NEW, "run", "stop"])
    (world.root / "gateway-actions.json").unlink()
    again = world.post(gw, **start_action(id="st1"))
    assert again[0] == 409 and again[1]["error"] == "a start st1 exists already on box1"
    assert len([c for c in world.herdr_calls if c[:2] == ("tab", "create")]) == 1


def test_a_start_without_a_pane_fails_or_answers_without_it(world, monkeypatch):
    allow_starts(world, monkeypatch)
    gw = world.serve.Gateway()
    world.tab_ok = False
    status, out = world.post(gw, **start_action(id="f1"))
    assert status == 502 and out["status"] == "failed" and out["pane"] is None
    assert out["delivered_at"] is None  # it never reached an agent
    assert out["error"] == "herdr couldn't open a tab: no such workspace"
    world.tab_ok = True
    allow_starts(world, monkeypatch, run_it=False)  # the start runs on but has no pane yet
    t0 = world.t
    status, out = world.post(gw, **start_action(id="s2"))
    assert status == 202 and out["pane"] is None
    assert world.t - t0 == pytest.approx(world.serve.PANE_WAIT, abs=0.5)


def test_a_start_at_the_trust_dialog_shows_a_trust_dialog(world, monkeypatch):
    app = allow_starts(world, monkeypatch, run_it=False)
    gw = world.serve.Gateway()
    world.post(gw, **start_action(id="st1"))
    rec = world.orch.load(world.orch.STARTS / "st1" / "new.json")
    rec["pane"] = NEW
    for step, status in [("pane", "ok"), ("claude", "doing"), ("trust", "doing")]:
        world.orch.start_step(rec, step, status)
    world.agent(NEW, "blocked", change=4)
    world.agents[NEW]["cwd"] = str(app)  # Claude runs in the start's folder
    lead = lead_of(world, gw, NEW)
    assert lead["activity"] == "starting" and lead["run"] is None
    [dialog] = lead["dialogs"]
    conforms(dialog, DIALOG)
    assert dialog["kind"] == "trust" and dialog["cwd"] == str(app) and dialog["change"] == 4
    trust_at = next(s["time"] for s in rec["steps"] if s["step"] == "trust")
    assert (dialog["label"], dialog["since"], dialog["doing"]) == ("my-app", trust_at,
                                                                  "Ship the beta")
    world.orch.start_step(rec, "trust", "ok")
    [dialog] = lead_of(world, gw, NEW)["dialogs"]
    assert "kind" not in dialog  # another dialog of the same agent


def test_a_starting_orchestrator_takes_no_typed_input_until_claude_runs_and_trust_is_answered(
        world, monkeypatch):
    allow_starts(world, monkeypatch, run_it=False)
    gw = world.serve.Gateway()
    world.post(gw, **start_action(id="st1"))
    rec = world.orch.load(world.orch.STARTS / "st1" / "new.json")
    rec["pane"] = NEW
    world.orch.start_step(rec, "pane", "ok")
    world.agent(NEW, "idle")  # herdr sees the pane's shell before Claude runs

    def typed(n):
        return [world.post(gw, id=f"p{n}", kind="prompt", pane=NEW, text="y"),
                world.post(gw, id=f"i{n}", kind="interrupt", pane=NEW),
                world.post(gw, id=f"n{n}", kind="nudge", orchestrator=NEW, text="y")]

    keys = len(world.herdr_calls)
    for status, out in typed(1):
        assert status == 409 and out["error"].endswith("whose Claude isn't running yet")
    world.orch.start_step(rec, "claude", "doing")
    world.orch.start_step(rec, "trust", "doing")
    world.agent(NEW, "blocked")
    for status, out in typed(2):
        assert status == 409 and "folder-trust question: answer that first" in out["error"]
    assert world.prompts == [] and world.herdr_calls[keys:] == []  # nothing reached the pane
    world.orch.start_step(rec, "trust", "ok")
    world.orch.start_step(rec, "claude", "ok")
    world.agent(NEW, "idle")
    assert [status for status, _ in typed(3)] == [202, 202, 202]
    world.orch.save(world.orch.STARTS / "st1" / "new.json", dict(rec, restart=True))  # retry
    assert world.post(gw, id="p4", kind="prompt", pane=NEW, text="y")[0] == 409



def test_permission_prompts_parse_from_captured_screens(world):
    serve = world.serve
    bash = serve.permission(BASH.split("\n"))
    assert bash == {"question": "Do you want to proceed?", "tool": None,
                    "command": "rm notes.txt", "options": ["1. Yes"], "cursor": "1. Yes"}
    assert serve.permission_keys(bash, "allow") == ["enter"]
    assert serve.permission_keys(bash, "deny") == ["esc"]
    third = serve.permission(BASH_3.split("\n"))
    assert third["cursor"] == ("3. Yes, and switch to auto mode · auto mode handles these "
                               "prompts for you")
    assert serve.permission_keys(third, "allow") is None  # never Enter on another option
    write = serve.permission(WRITE.split("\n"))
    assert (write["tool"], write["command"], write["cursor"]) == ("Write", "hello.txt", "1. Yes")
    assert serve.permission(WRITE_2.split("\n"))["cursor"].endswith("for this session "
                                                                     "(shift+tab)")
    # the same prompt in a tall terminal (the captured options, all on screen at once)
    tall = ("Bash command\n\n  rm notes.txt\n  Remove notes.txt\n\n Do you want to proceed?\n"
            " ❯ 1. Yes\n   2. Yes, and always allow access to tmp/ from this project\n"
            "   3. Yes, and switch to auto mode\n   4. No\n\n Esc to cancel · Tab to amend")
    found = serve.permission(tall.split("\n"))
    assert (found["tool"], found["command"]) == ("Bash", "rm notes.txt\nRemove notes.txt")
    assert len(found["options"]) == 4 and found["cursor"] == "1. Yes"
    for other in (TRUST, world.screen, "❯ \n────\n  ? for shortcuts", ""):
        assert serve.permission(other.split("\n")) is None


def test_the_app_answers_a_permission_prompt_of_an_agent_in_a_run(world):
    gw = world.serve.Gateway()
    start_run(world)
    world.agent(W1, "blocked", change=7)
    world.screen = BASH
    [dialog] = lead_of(world, gw, BOSS)["dialogs"]
    assert (dialog["kind"], dialog["tool"], dialog["command"]) == ("permission", None,
                                                                    "rm notes.txt")
    assert gw.route("GET", "/v1/health", {}, None, "127.0.0.1")[1]["dialogs"] == [
        "trust", "permission"]

    stale = from_phone(world, gw, kind="dialog", pane=W1, change=6, choice="allow")
    assert stale[0] == 409 and "the dialog changed" in stale[1]["error"]
    assert from_phone(world, gw, kind="dialog", pane=W1, change=7, choice="allow")[0] == 202
    assert world.herdr_calls[-1] == ("agent", "send-keys", W1, "enter")
    assert from_phone(world, gw, kind="dialog", pane=W1, change=7, choice="deny")[0] == 202
    assert world.herdr_calls[-1] == ("agent", "send-keys", W1, "esc")
    assert from_phone(world, gw, kind="dialog", pane=W1, change=7, choice="delegate")[0] == 202
    lead, text = world.prompts[-1]
    assert lead == BOSS and "builder (w1:p1)" in text and "rm notes.txt" in text
    assert f"herdr agent send-keys {W1} enter" in text and f"send-keys {W1} esc" in text
    answered = [(e["choice"], e["by"]) for e in world.events() if e["kind"] == "permission"]
    assert answered == [("allow", "nPHONE"), ("deny", "nPHONE"), ("delegate", "nPHONE")]

    keys = len(world.herdr_calls)
    world.screen = BASH_3  # someone moved the cursor in the pane
    moved = from_phone(world, gw, kind="dialog", pane=W1, change=7, choice="allow")
    assert moved[0] == 409 and "not \"1. Yes\"" in moved[1]["error"]
    world.screen = "Bash command\n  rm x\nWhat should Claude do?\n❯ 1. Retry\n"
    assert from_phone(world, gw, kind="dialog", pane=W1, change=7, choice="allow")[0] == 400
    world.screen = BASH
    world.agent(BOSS, "blocked", change=3)  # the orchestrator itself: nobody to delegate to
    assert from_phone(world, gw, kind="dialog", pane=BOSS, change=3, choice="delegate")[0] == 409
    assert from_phone(world, gw, kind="dialog", pane=W1, choice="allow")[0] == 400  # no change
    world.agent(W1, "working", change=8)
    assert from_phone(world, gw, kind="dialog", pane=W1, change=8, choice="allow")[0] == 409
    assert world.herdr_calls[keys:] == [c for c in world.herdr_calls[keys:]
                                        if c[:2] != ("agent", "send-keys")]  # no keys sent


def from_phone(w, gw, **action):
    """An action from the paired phone (127.0.0.1 is its tailnet node in the world)."""
    action.setdefault("id", f"a{len(w.events())}-{action.get('kind')}")
    action.setdefault("time", w.t)
    return gw.route("POST", "/v1/actions", {}, action, "127.0.0.1")


def test_reads_go_on_while_a_start_waits_for_its_pane(world, monkeypatch):
    allow_starts(world, monkeypatch, run_it=False)  # the detached start has no tab yet
    gw = world.serve.Gateway()
    seen = []

    def sleep(seconds):  # the start's request waits here; meanwhile the phone reads
        assert not gw.lock.locked()
        if not seen:
            seen.append(world.get(gw, "/v1/actions/st1")[1])
            seen.append(world.get(gw, "/v1/fleet")[0])
            seen.append(gw.route("GET", "/v1/health", {}, None, "127.0.0.1")[0])
            path = world.orch.STARTS / "st1" / "new.json"
            world.orch.save(path, dict(world.orch.load(path), pane=NEW))  # the tab opens
            seen.append(world.get(gw, "/v1/actions/st1")[1]["pane"])
        world.t += seconds

    monkeypatch.setattr(world.orch, "sleep", sleep)
    status, out = world.post(gw, **start_action(id="st1"))
    assert status == 202 and out["pane"] == NEW
    assert seen[0]["status"] == "delivered" and seen[0]["pane"] is None
    assert seen[1:] == [200, 200, NEW]
    [event] = [e for e in world.events() if e["kind"] == "action"]  # logged once, when answered
    assert event["status"] == "delivered" and event["pane"] == NEW


def test_cancel_quits_the_new_orchestrator_and_closes_only_its_pane(world, monkeypatch):
    allow_starts(world, monkeypatch)
    world.serve.orch_push.save_phones({"nPHONE": {"node": "phone", "device": "OnePlus 12"}})
    gw = world.serve.Gateway()
    world.post(gw, **start_action(id="st1"))  # plan first: it reads, the run is registered
    qid = world.orch.ask(NEW, "Plan ready", [{"label": "Approve and start workers"}], "plan")
    lineage = Path(world.orch.load(world.orch.STARTS / "st1" / "new.json")["lineage"])
    assert lineage.is_dir()
    status, out = from_phone(world, gw, id="c1", kind="start_cancel", action="st1")
    assert status == 202 and out["status"] == "picked" and out["picked_at"] == world.t
    assert (NEW, "/exit") in world.prompts and NEW not in world.agents
    assert ("pane", "close", NEW) in world.herdr_calls
    assert not [c for c in world.herdr_calls if c[:2] == ("tab", "close")]
    assert world.orch.load_run(NEW)["ended"] == world.t
    lead = lead_of(world, gw, NEW)  # the stopped run, no longer starting
    assert lead["activity"] == "done" and lead["start"] is None
    [event] = [e for e in world.events() if e["kind"] == "start-cancel"]
    assert event["pane"] == NEW and event["action"] == "st1" and event["by"] == "phone (OnePlus 12)"
    assert event["body"] == (f"by phone (OnePlus 12): run stopped; pane {NEW} closed; "
                             "its unused decision log removed")
    assert not lineage.exists() and not lineage.parent.exists()  # nothing was written to it
    [plan] = world.orch.load_run(NEW)["questions"]
    assert (plan["id"], plan["answer"], plan["answered"]) == (
        qid, "(the start was cancelled by phone (OnePlus 12))", world.t)
    again = world.post(gw, id="c2", kind="start_cancel", action="st1")
    assert again[0] == 409 and again[1]["error"] == "the start st1 was cancelled already"
    assert world.post(gw, id="c3", kind="start_cancel", action="nope")[0] == 404
    (world.home / "Repo" / "two" / ".git").mkdir(parents=True)
    world.tab_alive = True  # someone split the new tab
    world.post(gw, **start_action(id="st2", folder="~/Repo/two", plan_first=False))
    done = world.post(gw, id="c4", kind="start_cancel", action="st2")
    assert done[0] == 409 and "is done" in done[1]["error"]  # a running orchestrator now
    world.agent(NEW, "idle")
    rec = world.orch.load(world.orch.STARTS / "st2" / "new.json")
    world.orch.save(world.orch.STARTS / "st2" / "new.json", dict(rec, steps=rec["steps"][:-1]))
    world.post(gw, id="c5", kind="start_cancel", action="st2")
    assert "tab w1:t9 kept: it has other panes" in world.events()[-2]["body"]


def test_retry_runs_a_failed_start_again_from_its_failed_step(world, monkeypatch):
    allow_starts(world, monkeypatch)
    gw = world.serve.Gateway()
    world.tab_ok = False
    assert world.post(gw, **start_action(id="st1"))[0] == 502
    world.tab_ok = True
    at = world.t
    assert world.post(gw, id="r1", kind="start_retry", action="st1")[0] == 202
    rec = world.orch.load(world.orch.STARTS / "st1" / "new.json")
    assert [(s["step"], s["status"]) for s in rec["steps"]] == [
        ("pane", "ok"), ("claude", "ok"), ("run", "ok"), ("read", "doing")]
    assert rec["retries"] == [{"time": at, "by": "the app", "step": "pane", "account": "2"}]
    again = world.post(gw, id="r2", kind="start_retry", action="st1")
    assert again[0] == 409 and "has not failed" in again[1]["error"]


def test_retry_on_another_account_restarts_claude_after_nobody_trusted_the_folder(
        world, monkeypatch):
    allow_starts(world, monkeypatch)
    gw = world.serve.Gateway()
    world.screen = TRUST
    run = world.herdr

    def herdr(*args, **kw):  # Claude on account 2 stops at the trust question, on 1 it doesn't
        out = run(*args, **kw)
        if args[:2] == ("pane", "run") and " 2 --session-id " in args[3]:
            world.agents[args[2]]["agent_status"] = "blocked"
        elif args[:2] == ("pane", "run"):
            world.screen = ""
        return out

    monkeypatch.setattr(world.orch, "herdr", herdr)
    status, out = world.post(gw, **start_action(id="st1"))
    assert status == 202 and out["pane"] == NEW  # answered once the pane was there
    rec = world.orch.load(world.orch.STARTS / "st1" / "new.json")
    assert rec["status"] == "failed" and rec["step"] == "trust"  # 15 minutes, nobody answered
    first = rec["session"]
    assert world.post(gw, id="r1", kind="start_retry", action="st1", account="1")[0] == 202
    rec = world.orch.load(world.orch.STARTS / "st1" / "new.json")
    assert [(s["step"], s["status"]) for s in rec["steps"]] == [
        ("pane", "ok"), ("claude", "ok"), ("run", "ok"), ("read", "doing")]
    assert ("agent", "send-keys", NEW, "enter") in world.herdr_calls  # "No, exit", the default
    second = [c[3] for c in world.herdr_calls if c[:2] == ("pane", "run")][-1]
    assert f" 1 --session-id {rec['session']} " in second and rec["session"] != first
    assert world.orch.load_run(NEW)["settings"]["account"] == "1"
    assert rec["account"] == "1" and "restart" not in rec
    bad = world.post(gw, id="r2", kind="start_retry", action="st1", account=["1"])
    assert bad[0] == 400


def trust_step(world, gw):
    """A phone start whose new agent waits at the trust question (the detached start is
    elsewhere)."""
    world.post(gw, **start_action(id="st1"))
    path = world.orch.STARTS / "st1" / "new.json"
    rec = dict(world.orch.load(path), pane=NEW)
    for step, status in [("pane", "ok"), ("claude", "doing")]:
        world.orch.start_step(rec, step, status)
    world.agent(NEW, "blocked", change=4)
    world.screen = TRUST
    return rec


def test_retry_picks_up_a_start_whose_process_died(world, monkeypatch):
    allow_starts(world, monkeypatch, run_it=False)
    gw = world.serve.Gateway()
    world.orch.start_step(trust_step(world, gw), "trust", "doing")
    path = world.orch.STARTS / "st1" / "new.json"
    world.orch.save(path, dict(world.orch.load(path), pid=os.getpid()))  # still waiting
    busy = world.post(gw, id="r1", kind="start_retry", action="st1")
    assert busy[0] == 409 and "has not failed (trust doing)" in busy[1]["error"]
    gone = subprocess.Popen(["true"])
    gone.wait()
    world.orch.save(path, dict(world.orch.load(path), pid=gone.pid))  # killed meanwhile
    world.agents[NEW]["agent_status"] = "idle"  # and the question was answered in the pane
    world.screen = ""
    allow_starts(world, monkeypatch)  # the retry runs in-process
    assert world.post(gw, id="r2", kind="start_retry", action="st1")[0] == 202
    rec = world.orch.load(path)
    assert [(s["step"], s["status"]) for s in rec["steps"]] == [
        ("pane", "ok"), ("claude", "ok"), ("trust", "ok"), ("run", "ok"), ("read", "doing")]
    assert rec["pid"] == os.getpid() and not [c for c in world.herdr_calls
                                              if c[:2] == ("pane", "run")]  # Claude was up


def test_the_app_answers_only_a_new_orchestrators_trust_question(world, monkeypatch):
    allow_starts(world, monkeypatch, run_it=False)
    world.serve.orch_push.save_phones({"nPHONE": {"node": "phone", "device": "OnePlus 12"}})
    gw = world.serve.Gateway()
    rec = trust_step(world, gw)
    answer = {"kind": "dialog", "pane": NEW, "change": 4, "choice": "allow"}
    world.orch.start_step(rec, "claude", "ok")  # the start is past its trust question
    late = from_phone(world, gw, **answer)
    assert late[0] == 400 and "answer this dialog in the pane" in late[1]["error"]
    world.orch.start_step(rec, "claude", "doing")
    world.orch.start_step(rec, "trust", "doing")
    world.agent(W2, "blocked", change=2)
    assert from_phone(world, gw, **dict(answer, pane=W2, change=2))[0] == 400  # not a start's
    assert from_phone(world, gw, **dict(answer, choice="delegate"))[0] == 400
    stale = from_phone(world, gw, **dict(answer, change=3))
    assert stale[0] == 409 and "changed since" in stale[1]["error"]
    world.screen = "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. No\n"
    other = from_phone(world, gw, **answer)
    assert other[0] == 409 and "doesn't show Claude Code's folder-trust question" in other[1]["error"]
    world.screen = TRUST
    assert not [c for c in world.herdr_calls if c[:2] == ("agent", "send-keys")]
    assert from_phone(world, gw, **answer)[0] == 202
    assert ("agent", "send-keys", NEW, "down", "enter") in world.herdr_calls  # from "No, exit"
    assert world.agents[NEW]["agent_status"] == "idle"
    [event] = [e for e in world.events() if e["kind"] == "trust"]
    assert event["pane"] == NEW and event["action"] == "st1" and event["choice"] == "allow"
    assert event["by"] == "phone (OnePlus 12)" and event["source"] == "app"
    assert event["body"] == "~/Repo/my-app on account 2, by phone (OnePlus 12)"


def test_the_trust_question_is_marked_and_answerable_as_soon_as_it_shows(world, monkeypatch):
    allow_starts(world, monkeypatch, run_it=False)
    gw = world.serve.Gateway()
    rec = trust_step(world, gw)  # Claude starts; the start hasn't seen it blocked for 3 s yet
    world.agents[NEW]["cwd"] = str(world.home / "Repo" / "my-app")
    claude_at = next(s["time"] for s in rec["steps"] if s["step"] == "claude")
    [dialog] = lead_of(world, gw, NEW)["dialogs"]
    assert (dialog["kind"], dialog["since"], dialog["label"]) == ("trust", claude_at, "my-app")
    world.screen = BASH  # another dialog of that pane is not the trust question
    assert lead_of(world, gw, NEW)["dialogs"][0]["kind"] == "permission"
    world.screen = TRUST
    assert world.post(gw, kind="dialog", pane=NEW, change=4, choice="allow")[0] == 202
    assert ("agent", "send-keys", NEW, "down", "enter") in world.herdr_calls


def test_denying_the_trust_question_quits_claude_and_cancels_the_start(world, monkeypatch):
    allow_starts(world, monkeypatch, run_it=False)
    gw = world.serve.Gateway()
    world.orch.start_step(trust_step(world, gw), "trust", "doing")
    assert world.post(gw, kind="dialog", pane=NEW, change=4, choice="deny")[0] == 202
    assert ("agent", "send-keys", NEW, "enter") in world.herdr_calls
    assert NEW not in world.agents and ("pane", "close", NEW) in world.herdr_calls
    assert world.orch.start_cancelled({"id": "st1"})["by"] == "the app"
    assert [e["kind"] for e in world.events()][-3:] == ["trust", "start-cancel", "action"]
    assert NEW not in world.serve.Gateway.starts(world.t)


def test_health_says_whether_a_start_can_work_here(world, monkeypatch, tmp_path):
    gw = world.serve.Gateway()
    acc = tmp_path / "claude-acc"
    monkeypatch.setattr(world.serve.orch_new, "claude_acc", lambda: str(acc))

    def health():
        return gw.route("GET", "/v1/health", {}, None, "127.0.0.1")[1]

    assert health()["can_start"] is False
    assert "ORCH_START_ROOTS is not set" in health()["cannot_start"]
    world.serve.write_env(dict(world.serve.read_env(), ORCH_START_ROOTS="~/Repo"))
    assert health()["cannot_start"] == "claude-acc is not installed"
    acc.write_text("#!/bin/sh\n")
    acc.chmod(0o755)
    out = health()
    assert out["can_start"] is True and "cannot_start" not in out
    monkeypatch.setattr(world.orch, "agents", lambda: None)
    assert health()["cannot_start"] == "herdr isn't running"


def git_checkout(path, head="ref: refs/heads/main", at=None, worktree=None):
    """A checkout's .git (a folder, or a worktree's .git file) with HEAD written at `at`."""
    path.mkdir(parents=True, exist_ok=True)
    gd = path / ".git"
    if worktree:
        worktree.mkdir(parents=True)
        (path / ".git").write_text(f"gitdir: {worktree}\n")
        gd = worktree
    else:
        gd.mkdir()
    (gd / "HEAD").write_text(head + "\n")
    if at:
        os.utime(gd / "HEAD", (at, at))
    return gd


def test_folders_lists_the_checkouts_under_the_roots_newest_work_first(world, tmp_path,
                                                                        monkeypatch):
    gw = world.serve.Gateway()
    repo, work = world.home / "Repo", world.home / "work"
    assert world.get(gw, "/v1/folders") == (200, [])  # no roots: starts are off
    world.serve.write_env(dict(world.serve.read_env(), ORCH_START_ROOTS="~/Repo, ~/work",
                               ORCH_START_EXCLUDE="~/Repo/live"))
    git_checkout(repo / "app", at=world.t - 3600)
    git_checkout(repo / "app-wt", head="0123456789abcdef", at=world.t - 7200,
                 worktree=repo / "app" / ".git" / "worktrees" / "app-wt")
    git_checkout(repo / "live", at=world.t)  # excluded
    (repo / "live-link").symlink_to(repo / "live")  # the same checkout under another name
    (repo / "notes").mkdir()  # not a checkout
    git_checkout(work / "deep" / "lib", at=world.t - 9000)  # too deep, but an agent works there
    (work / "deep" / "lib" / "src").mkdir()
    world.agent(LOOSE, "working")
    world.agents[LOOSE]["cwd"] = str(work / "deep" / "lib" / "src")
    git_checkout(work / "old", at=world.t - 86400)
    transcripts = world.orch.PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(work / "old"))
    transcripts.mkdir(parents=True)
    (transcripts / "s1.jsonl").write_text("{}\n")
    os.utime(transcripts / "s1.jsonl", (world.t - 60, world.t - 60))  # Claude worked there
    monkeypatch.chdir(repo / "app")
    start_run(world)  # BOSS ("Website") works in ~/Repo/app
    (world.home / ".claude.json").write_text(json.dumps({"projects": {
        str(repo / "app"): {"hasTrustDialogAccepted": True},
        str(work): {"hasTrustDialogAccepted": True},  # not its children: Claude asks there again
        str(work / "old"): {"hasTrustDialogAccepted": True},
        str(repo / "app-wt"): {"hasTrustDialogAccepted": False}}}))
    (world.home / ".claude-2" / ".claude.json").write_text(json.dumps({"projects": {}}))
    status, folders = world.get(gw, "/v1/folders")
    assert status == 200
    conforms(folders, [{"path": str, "branch": str, "last_activity": NUM,
                        "orchestrator?": {"pane": str, "name": str}, "trusted": bool}])
    assert [(f["path"], f["branch"], f["last_activity"], f["trusted"]) for f in folders] == [
        ("~/work/old", "main", world.t - 60, True),
        ("~/Repo/app", "main", world.t - 3600, True),
        ("~/Repo/app-wt", "01234567", world.t - 7200, False),
        ("~/work/deep/lib", "main", world.t - 9000, False)]
    assert folders[1]["orchestrator"] == {"pane": BOSS, "name": "Website"}
    assert [f["orchestrator"] for f in folders if f["path"] != "~/Repo/app"] == [None] * 3
    assert [f["path"] for f in world.get(gw, "/v1/folders?q=APP&limit=1")[1]] == ["~/Repo/app"]
    assert world.get(gw, "/v1/folders?limit=x")[0] == 400
    # a new agent would start on account 2 (the default): its trust list counts
    (world.root / "accounts.json").write_text(json.dumps(dict(
        world.orch.load(world.root / "accounts.json"), default="2")))
    assert not any(f["trusted"] for f in world.get(gw, "/v1/folders")[1])


# --- pairing, health, pushes and the relay (orch_push.py; needs `cryptography`) ------------------

KEY = bytes(range(32))
MACHINE = "box1"  # the tailnet name the world fixture gives this machine
HEALTH = {"machine": str, "version": str, "time": NUM, "addr?": str, "orchestrators": [str],
          "can_start": bool, "cannot_start?": str,
          "accounts": dict, "push": {"registered": bool, "seq": int, "last_sent?": NUM,
                                     "last_heartbeat?": NUM, "error?": str},
          "capacity?": {"agents": int, "mem_free_gb": NUM, "mem_total_gb": NUM,
                        "load_avg": [NUM], "cores": int},
          "gaming": bool, "gaming_since?": NUM,
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
        assert refused.value.code == 401 and refused.value.headers["Server"] == "orch/1.1.0"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.headers["Server"] == "orch/1.1.0"
            health = json.loads(r.read())
    finally:
        server.shutdown()
        server.server_close()
    conforms(health, HEALTH)
    assert health["version"] == "1.1.0" and health["addr"] == "100.64.0.1"
    assert health["accounts"] == {"1": "main", "2": "spare"} and "relay" not in health
    assert health["push"]["registered"] is False and health["push"]["seq"] == 0


def fake_proc(tmp_path, monkeypatch, serve, meminfo, comms):
    proc = tmp_path / "proc"
    proc.mkdir()
    if meminfo is not None:
        (proc / "meminfo").write_text(meminfo)
    for pid, comm in comms.items():
        (proc / pid).mkdir()
        if comm:
            (proc / pid / "comm").write_text(comm + "\n")
    (proc / "sys").mkdir()  # no pid: never counted
    (proc / "sys" / "comm").write_text("claude\n")
    monkeypatch.setattr(serve.orch, "PROC", proc)
    monkeypatch.setattr(serve.os, "getloadavg", lambda: (1.234, 0.5, 0.25))
    monkeypatch.setattr(serve.os, "cpu_count", lambda: 16)


MEMINFO = """MemTotal:       31457280 kB
MemFree:         1048576 kB
MemAvailable:    9961472 kB
HugePages_Total:       0
"""


def test_health_says_how_full_the_machine_is(world, cloud, tmp_path, monkeypatch):
    # 1048576 kB is 1 GiB. MemAvailable, not MemFree: the page cache can be given back.
    fake_proc(tmp_path, monkeypatch, world.serve, MEMINFO,
              {"10": "claude", "11": "claude", "12": "bash", "13": ""})
    gw = world.serve.Gateway()
    status, health = world.get(gw, "/v1/health")
    assert status == 200
    conforms(health, HEALTH)
    assert health["capacity"] == {"agents": 2, "mem_free_gb": 9.5, "mem_total_gb": 30.0,
                                  "load_avg": [1.23, 0.5, 0.25], "cores": 16}


@pytest.mark.parametrize("meminfo", [None, "MemTotal: 31457280 kB\n",
                                     "MemTotal: x kB\nMemAvailable: y kB\n",
                                     "MemTotal:\nMemAvailable:\n", "no colon here\n"])
def test_a_machine_without_a_readable_meminfo_has_no_capacity(world, cloud, tmp_path, monkeypatch,
                                                              meminfo):
    fake_proc(tmp_path, monkeypatch, world.serve, meminfo, {"10": "claude"})
    status, health = world.get(world.serve.Gateway(), "/v1/health")
    assert status == 200 and health["capacity"] is None
    conforms(health, HEALTH)


def gaming_events(world):
    return [e for e in world.events() if e["kind"] == "gaming"]


def test_the_gaming_flag_set_here_shows_in_health_and_the_fleet(world, cloud, capsys):
    gw = world.serve.Gateway()
    assert world.get(gw, "/v1/health")[1]["gaming"] is False
    assert world.get(gw, "/v1/fleet")[1]["gaming"] is False
    world.orch.main(["machine"])
    assert capsys.readouterr().out == "not gaming\n"

    world.orch.main(["machine", "--gaming", "on"])
    capsys.readouterr()
    health = world.get(gw, "/v1/health")[1]
    conforms(health, HEALTH)
    assert health["gaming"] is True and health["gaming_since"] == world.t
    fleet = world.get(gw, "/v1/fleet")[1]
    conforms(fleet, FLEET)
    assert fleet["gaming"] is True
    [event] = gaming_events(world)
    assert event["gaming"] is True and event["by"] == "orch machine"
    assert event["title"] == "Gaming on on box1"

    world.t += 600  # on again: still the first time, and no second event
    world.orch.main(["machine", "--gaming", "on"])
    world.orch.main(["machine", "--json"])
    capsys.readouterr()
    assert world.get(gw, "/v1/health")[1]["gaming_since"] == world.t - 600
    assert len(gaming_events(world)) == 1
    world.orch.main(["machine"])
    assert capsys.readouterr().out.startswith("gaming since 10:00, by orch machine")

    world.orch.main(["machine", "--gaming", "off", "--json"])
    assert json.loads(capsys.readouterr().out) == {"gaming": False, "gaming_since": None}
    health = world.get(gw, "/v1/health")[1]
    assert health["gaming"] is False and health["gaming_since"] is None
    assert [e["gaming"] for e in gaming_events(world)] == [True, False]


def test_the_phone_sets_the_gaming_flag_and_a_retry_changes_nothing(world, cloud):
    gw = world.serve.Gateway()
    pair(world, gw)
    action = {"id": "g1", "time": world.t, "kind": "gaming", "on": True}
    status, out = gw.route("POST", "/v1/actions", {}, action, "127.0.0.1")
    assert status == 202 and out["status"] == "delivered"
    assert world.get(gw, "/v1/health")[1]["gaming"] is True
    [event] = gaming_events(world)
    assert event["by"] == "phone (OnePlus 12)"

    world.t += 60
    assert gw.route("POST", "/v1/actions", {}, action, "127.0.0.1")[0] == 202
    assert world.get(gw, "/v1/health")[1]["gaming_since"] == world.t - 60
    assert len(gaming_events(world)) == 1

    off = {"id": "g2", "time": world.t, "kind": "gaming", "on": False}
    assert gw.route("POST", "/v1/actions", {}, off, "127.0.0.1")[0] == 202
    assert world.get(gw, "/v1/health")[1]["gaming"] is False


@pytest.mark.parametrize("on", [None, "on", 1])
def test_the_gaming_action_needs_a_true_or_false(world, cloud, on):
    gw = world.serve.Gateway()
    status, out = world.post(gw, kind="gaming", **({} if on is None else {"on": on}))
    assert status == 400 and out["error"] == "on is missing"
    assert world.get(gw, "/v1/health")[1]["gaming"] is False


def test_pairing_stores_the_token_and_sends_a_test_push(world, cloud):
    gw = world.serve.Gateway()
    status, paired = pair(world, gw)
    assert status == 200
    conforms(paired, PAIRED)
    assert paired["test_push"] == {"seq": 0, "sent": world.t}
    [push] = cloud.pushes
    assert push["token"] == "fcm-1" and push["android"] == {"priority": "NORMAL", "ttl": "300s"}
    assert push["message"] == {"seq": 0, "sent": world.t, "kind": "test", "orchestrator": None,
                               "body": None}
    phones = world.serve.orch_push.PHONES
    assert stat.S_IMODE(os.stat(phones).st_mode) == 0o600
    assert json.loads(phones.read_text())["nPHONE"] == {
        "paired": world.t, "updated": world.t, "node": "phone", "fcm_token": "fcm-1",
        "token_error": None, "device": "OnePlus 12", "app_version": "0.3.0", "relay_uid": "uid1"}
    health = gw.route("GET", "/v1/health", {}, None, "127.0.0.1")[1]
    conforms(health, HEALTH)
    assert health["push"] == {"registered": True, "seq": 0, "last_sent": world.t,
                              "last_heartbeat": None}
    assert health["relay"] == {"last_poll": None, "error": None}
    world.t += 60
    assert pair(world, gw, token="fcm-2")[1]["test_push"]["seq"] == 0  # a new token replaces it
    assert json.loads(phones.read_text())["nPHONE"]["fcm_token"] == "fcm-2"
    assert cloud.pushes[-1]["token"] == "fcm-2"


def from_the_phone(world, gw, **action):
    return gw.route("POST", "/v1/actions", {}, dict(action, kind="test_push"), "127.0.0.1")


def test_a_test_push_goes_once_to_the_phone_that_asks_at_normal_priority(world, cloud):
    gw = world.serve.Gateway()
    pair(world, gw)
    phones = world.serve.orch_push.load_phones()
    phones["nOTHER"] = {"fcm_token": "fcm-other", "token_error": None, "relay_uid": "uid2"}
    world.serve.orch_push.save_phones(phones)
    cloud.pushes.clear()
    action = {"id": "t1", "time": world.t}

    first = from_the_phone(world, gw, **action)

    assert first == (202, {"id": "t1", "status": "delivered", "delivered_at": world.t,
                           "picked_at": None, "error": None})
    [push] = cloud.pushes
    assert push["token"] == "fcm-1" and push["android"] == {"priority": "NORMAL", "ttl": "300s"}
    assert push["message"] == {"seq": 0, "sent": world.t, "kind": "test", "orchestrator": None,
                               "body": None}

    assert from_the_phone(world, gw, **action) == first  # a retry: same answer, no second push
    assert len(cloud.pushes) == 1
    assert from_the_phone(world, gw, id="t2", time=world.t)[0] == 202  # a new tap, a new action
    assert [p["message"]["seq"] for p in cloud.pushes] == [0, 0]


def test_a_test_push_leaves_no_number_for_another_phone_to_wait_for(world, cloud):
    """A gap in seq is a lost push to the app, and a `test` reaches one phone only."""
    gw = world.serve.Gateway()
    pair(world, gw)
    phones = world.serve.orch_push.load_phones()
    phones["nOTHER"] = {"fcm_token": "fcm-other", "token_error": None}
    world.serve.orch_push.save_phones(phones)
    gw.pusher.cycle()
    cloud.pushes.clear()

    world.orch.emit("spill", "Account 1 at 98%: moving to 2", "its week reached 98%", None)
    gw.pusher.cycle()
    assert from_the_phone(world, gw, id="t1", time=world.t)[0] == 202
    assert pair(world, gw, token="fcm-2")[1]["test_push"]["seq"] == 1  # the current number
    world.t += 1
    world.orch.emit("spill", "Account 1 back under 98%", "its week reset", None)
    gw.pusher.cycle()

    other = [(p["message"]["kind"], p["message"]["seq"]) for p in cloud.pushes
             if p["token"] == "fcm-other"]
    assert other == [("spill", 1), ("spill", 2), ("actions", 3)]  # no number missing
    mine = [(p["message"]["kind"], p["message"]["seq"]) for p in cloud.pushes
            if p["token"] != "fcm-other"]
    assert mine == [("spill", 1), ("test", 1), ("test", 1), ("spill", 2), ("actions", 3)]


def test_a_test_push_from_a_phone_that_never_paired_is_refused(world, cloud):
    gw = world.serve.Gateway()

    status, out = from_the_phone(world, gw, id="t1", time=world.t)

    assert status == 409 and out["status"] == "rejected" and "pair it again" in out["error"]
    assert cloud.sends == 0


def test_a_test_push_without_a_service_account_says_why(world, cloud):
    world.serve.orch_push.SA_FILE.unlink()
    gw = world.serve.Gateway()
    pair(world, gw)

    status, out = from_the_phone(world, gw, id="t1", time=world.t)

    assert status == 502 and out["status"] == "failed" and out["delivered_at"] is None
    assert "no service account key" in out["error"] and cloud.sends == 0


def test_a_test_push_that_fcm_refuses_says_why_and_a_retry_changes_nothing(world, cloud):
    gw = world.serve.Gateway()
    pair(world, gw)
    cloud.pushes.clear()
    cloud.fcm = [(404, {"error": {"status": "NOT_FOUND", "message": "token gone",
                                  "details": [{"errorCode": "UNREGISTERED"}]}})]
    sends = cloud.sends

    first = from_the_phone(world, gw, id="t1", time=world.t)

    assert first[0] == 502 and first[1]["status"] == "failed" and "FCM" in first[1]["error"]
    assert cloud.pushes == []
    assert from_the_phone(world, gw, id="t1", time=world.t) == first
    assert cloud.sends == sends + 1


def test_a_test_push_is_sent_outside_the_action_lock(world, cloud, monkeypatch):
    gw = world.serve.Gateway()
    pair(world, gw)
    held = []

    def test(pid):
        held.append(gw.lock.locked())
        return {"seq": 9, "sent": world.t}

    monkeypatch.setattr(gw.pusher, "test", test)

    assert from_the_phone(world, gw, id="t1", time=world.t)[0] == 202
    assert held == [False]  # reads go on, and the push loop can't deadlock with it


def test_a_test_push_by_relay_reaches_the_phone_that_wrote_it(world, cloud):
    gw = world.serve.Gateway()
    pair(world, gw)
    cloud.pushes.clear()
    cloud.write("uid1", {"id": "t1", "time": world.t - 20, "kind": "test_push"})

    assert gw.pusher.poll() is True

    [push] = cloud.pushes
    assert push["token"] == "fcm-1" and push["message"]["kind"] == "test"


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
            for p in cloud.pushes] == [("state", BOSS, 0), ("state", LOOSE, 0),
                                       ("usage", None, 1), ("accounts", None, 1),
                                       ("heartbeat", None, 1)]
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
    assert dialog["message"]["kind"] == "dialog" and dialog["message"]["seq"] == 2
    assert dialog["android"] == {"priority": "HIGH", "ttl": "86400s"}
    assert dialog["message"]["orchestrator"] == BOSS and dialog["message"]["body"]["pane"] == W2
    cloud.pushes.clear()
    qid = world.orch.ask(W1, "Merge #12?", [{"label": "Merge"}, {"label": "Not yet"}])
    gw.pusher.cycle()
    [question] = cloud.pushes
    assert question["message"]["kind"] == "question" and question["message"]["seq"] == 3
    assert question["android"]["priority"] == "HIGH"
    assert question["message"]["orchestrator"] == BOSS
    conforms(question["message"]["body"], QUESTION)
    assert question["message"]["body"]["id"] == qid
    cloud.pushes.clear()
    world.t += 300  # the watchdog's nudge was 5 min ago and the run is still stalled
    gw.pusher.cycle()
    assert sorted(cloud.kinds()) == ["stall", "state"]
    [stall] = [p for p in cloud.pushes if p["message"]["kind"] == "stall"]
    assert stall["message"]["seq"] == 4 and stall["message"]["orchestrator"] == BOSS
    conforms(stall["message"]["body"], STALL)
    cloud.pushes.clear()
    world.t += 600
    gw.pusher.cycle()
    [beat] = [p for p in cloud.pushes if p["message"]["kind"] == "heartbeat"]
    assert beat["android"]["collapse_key"] == f"{MACHINE}:heartbeat:"
    assert beat["message"]["seq"] == 4 and set(beat["message"]["body"]["states"]) == {BOSS, LOOSE}
    assert "dialog" not in cloud.kinds() and "stall" not in cloud.kinds()  # each went once
    health = gw.route("GET", "/v1/health", {}, None, "127.0.0.1")[1]["push"]
    assert health["seq"] == 4 and health["last_heartbeat"] == world.t


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
            for p in cloud.pushes] == [("system", BOSS, 1), ("spill", None, 2)]
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


def test_start_steps_become_start_pushes_high_only_when_one_failed(world, cloud, monkeypatch):
    allow_starts(world, monkeypatch)
    gw = world.serve.Gateway()
    pair(world, gw)
    gw.pusher.cycle()
    cloud.pushes.clear()
    at = world.t
    world.post(gw, **start_action(id="st1", plan_first=False))
    gw.pusher.cycle()
    starts = [p for p in cloud.pushes if p["message"]["kind"] == "start"]
    assert [(p["message"]["body"]["step"], p["message"]["body"]["status"]) for p in starts] == [
        ("pane", "ok"), ("claude", "doing"), ("claude", "ok"), ("run", "ok"), ("brief", "ok")]
    assert {p["android"]["priority"] for p in starts} == {"NORMAL"}
    assert starts[0]["android"] == {"priority": "NORMAL", "ttl": "86400s"}  # not collapsed
    assert starts[0]["message"]["orchestrator"] == NEW
    assert starts[0]["message"]["body"] == {"action": "st1", "pane": NEW, "step": "pane",
                                            "status": "ok", "error": None, "time": at}
    seqs = [p["message"]["seq"] for p in starts]
    assert seqs == sorted(set(seqs))  # each counts
    cloud.pushes.clear()
    world.tab_ok = False
    (world.home / "Repo" / "two" / ".git").mkdir(parents=True)  # my-app has its orchestrator
    assert world.post(gw, **start_action(id="st2", folder="~/Repo/two"))[0] == 502
    gw.pusher.cycle()
    [failed] = [p for p in cloud.pushes if p["message"]["kind"] == "start"]
    assert failed["android"]["priority"] == "HIGH" and failed["message"]["orchestrator"] is None
    assert failed["message"]["body"]["status"] == "failed"
    assert failed["message"]["body"]["error"] == "herdr couldn't open a tab: no such workspace"
    assert "system" not in cloud.kinds()  # the failure goes once, as a start push


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
    assert acts["message"]["seq"] == 2 and acts["android"]["priority"] == "NORMAL"
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
    assert question["message"]["seq"] == 1 and question["message"]["sent"] == world.t


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
            "read_at": world.t - 30, "windows": {"seven_day": {"used_percentage": 80,
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
