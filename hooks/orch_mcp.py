#!/usr/bin/env python3
"""orch MCP server (stdio): turn-end reports and questions for the owner, from herdr agents.

Registered at user scope by install.sh (`claude mcp add --scope user orch -- python3
~/.claude/hooks/orch_mcp.py`). The agent's pane comes from HERDR_PANE_ID; outside herdr the
server offers no tools. Tools:
  report     the agent's state at the end of a working turn, stored in
             ~/.claude/handoffs/agents/<pane>.json ("report") and events.jsonl (kind report)
  ask_owner  `orch ask` with options: logged, pushed to the owner, answered by `orch answer`
Newline-delimited JSON-RPC 2.0, stdlib only, Python 3.8 compatible (ob1).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import orch

VERSION = "1"
STR = {"type": "string"}
STRS = {"type": "array", "items": STR}
INT = {"type": "integer", "minimum": 0}


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


ORCH_FIELDS = ("state", "progress", "waiting_on", "next", "roadmap", "risks", "milestones")
REPORT = obj({
    "role": {"enum": ["orchestrator", "worker"]},
    "summary": {"type": "string", "description": "one line"},
    "state": {"enum": ["working", "waiting_on_workers", "waiting_on_owner", "done"]},
    "progress": obj({"done": INT, "total": INT, "label": STR}, ["done", "total"]),
    "waiting_on": {"type": "array", "items": obj({"who": STR, "what": STR, "eta": STR},
                                                 ["who", "what"])},
    "next": STRS,
    "roadmap": {"type": "array", "items": obj({
        "id": STR, "title": STR, "owner": STR, "eta": STR,
        "status": {"enum": ["done", "doing", "next", "later", "blocked"]}}, ["title", "status"])},
    "risks": STRS,
    "milestones": {"type": "array", "items": obj({
        "kind": {"enum": ["pr_merged", "deploy", "release", "other"]}, "ref": STR, "url": STR},
        ["kind", "ref"])},
    "status": {"enum": ["done", "partial", "blocked"]},
    "tests": {"type": "array", "items": obj({"cmd": STR, "passed": INT, "failed": INT}, ["cmd"])},
    "changed_files": STRS,
    "needs_live_test": STRS,
    "issues_for_others": STRS,
}, ["summary"])
ASK = obj({
    "question": STR,
    "options": {"type": "array", "items": obj({"label": STR, "danger": {"enum": list(orch.DANGER)}},
                                              ["label"])},
}, ["question"])
TOOLS = [
    {"name": "report",
     "description": ("End-of-turn status for the owner's app; call once at the end of each working "
                     "turn. Orchestrator: summary, state, progress, waiting_on, next, roadmap (the "
                     "plan), risks, milestones. Worker: summary, status, tests, changed_files, "
                     "needs_live_test, issues_for_others."),
     "inputSchema": REPORT},
    {"name": "ask_owner",
     "description": ("Ask the owner a question, with options shown as buttons (danger: what "
                     "picking it does). Returns the question id; the answer arrives later as a "
                     "prompt. Keep working on everything else meanwhile."),
     "inputSchema": ASK},
]


def check(schema, value, where="arguments"):
    """Errors in value against the small JSON Schema subset used above."""
    if "enum" in schema:
        return [] if value in schema["enum"] else [f"{where} must be one of {schema['enum']}"]
    kind = schema.get("type")
    if kind == "string":
        return [] if isinstance(value, str) else [f"{where} must be a string"]
    if kind == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        return [] if ok else [f"{where} must be a whole number >= 0"]
    if kind == "array":
        if not isinstance(value, list):
            return [f"{where} must be a list"]
        return [e for i, v in enumerate(value) for e in check(schema["items"], v, f"{where}[{i}]")]
    if not isinstance(value, dict):
        return [f"{where} must be an object"]
    errors = [f"{where}.{k} is required" for k in schema["required"] if k not in value]
    for k, v in value.items():
        if k not in schema["properties"]:
            errors.append(f"{where}.{k} is not a field")
        else:
            errors += check(schema["properties"][k], v, f"{where}.{k}")
    return errors


def report(args, pane):
    role = args.get("role") or orch.run_of(pane)[1] or (
        "orchestrator" if any(k in args for k in ORCH_FIELDS) else "worker")
    orch.write_report(pane, dict(args, role=role, time=orch.now()))
    return "Report recorded."


def ask_owner(args, pane):
    qid = orch.ask(pane, args["question"], args.get("options") or [])
    return (f"Sent to the owner as {qid}. Keep everything else going; the answer comes back as a "
            "prompt.")


HANDLERS = {"report": (REPORT, report), "ask_owner": (ASK, ask_owner)}


def call(params, pane):
    name, args = params.get("name"), params.get("arguments") or {}
    if name not in HANDLERS:
        return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
    schema, handler = HANDLERS[name]
    errors = check(schema, args)
    if errors:
        return {"content": [{"type": "text", "text": "; ".join(errors)}], "isError": True}
    try:
        text = handler(args, pane)
    except Exception as e:  # noqa: BLE001 the agent sees the error instead of a dead server
        return {"content": [{"type": "text", "text": f"failed: {e!r}"}], "isError": True}
    return {"content": [{"type": "text", "text": text}], "isError": False}


def handle(message, pane):
    """The JSON-RPC response to one message, or None for notifications."""
    method, mid = message.get("method"), message.get("id")
    if mid is None:
        return None
    params = message.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "orch", "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS if pane else []}
    elif method == "tools/call" and pane:
        result = call(params, pane)
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main(stdin=sys.stdin, stdout=sys.stdout):
    pane = os.environ.get("HERDR_PANE_ID") if os.environ.get("HERDR_ENV") == "1" else None
    while True:
        line = stdin.readline()
        if not line:
            return 0
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "parse error"}}
        else:
            response = handle(message, pane) if isinstance(message, dict) else None
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
