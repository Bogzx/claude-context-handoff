#!/usr/bin/env python3
"""Merge the context-handoff hooks and env into ~/.claude/settings.json (idempotent).

Deploy or update another machine from this one:
  scp ~/.claude/hooks/context-handoff*.py HOST:.claude/hooks/ &&
    ssh HOST python3 .claude/hooks/context-handoff-install.py
"""
import json
import shutil
import time
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
COMMAND = f"python3 {Path.home()}/.claude/hooks/context-handoff.py"
# A 633K window compacts at ~600K (Claude Code keeps ~33K in reserve).
ENV = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "633000", "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS": "400000"}
GROUPS = {"SessionStart": "^(startup|clear|compact)$", "PostToolUse": None, "Stop": None}

before = SETTINGS.read_text() if SETTINGS.exists() else "{}"
settings = json.loads(before)
settings.setdefault("env", {}).update(ENV)
hooks = settings.setdefault("hooks", {})
for event, matcher in GROUPS.items():
    groups = hooks.setdefault(event, [])
    if any(h.get("command") == COMMAND for g in groups for h in g.get("hooks", [])):
        continue
    group = {"hooks": [{"type": "command", "command": COMMAND, "timeout": 10}]}
    groups.append({"matcher": matcher, **group} if matcher else group)
after = json.dumps(settings, indent=2) + "\n"
if json.loads(after) == json.loads(before):
    print(f"{SETTINGS}: already up to date")
else:
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, SETTINGS.with_name(
            f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    SETTINGS.write_text(after)
    print(f"{SETTINGS}: updated")
