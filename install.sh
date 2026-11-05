#!/usr/bin/env bash
# Install or update the context-handoff hooks, orch, its MCP server and its watchdog timer.
#   ./install.sh                  this machine
#   ./install.sh HOST [HOST ...]  other machines over ssh, e.g. ./install.sh user@host2 user@host3
set -euo pipefail
cd "$(dirname "$0")"
hooks=(hooks/context-handoff.py hooks/context-handoff-install.py hooks/orch.py hooks/orch_mcp.py
       hooks/orch_usage.py hooks/orch_serve.py hooks/orch_push.py hooks/orch_new.py)
units=(systemd/orch-watchdog.service systemd/orch-watchdog.timer systemd/orch-serve.service)
files=("${hooks[@]}" "${units[@]}" install.sh)
if [ $# -eq 0 ]; then
  export PATH="$HOME/.local/bin:$PATH"  # where claude lives; not on a non-interactive ssh PATH
  mkdir -p ~/.claude/hooks ~/.local/bin ~/.config/systemd/user
  cp "${hooks[@]}" ~/.claude/hooks/
  chmod +x ~/.claude/hooks/orch.py ~/.claude/hooks/orch_mcp.py
  ln -sfn ~/.claude/hooks/orch.py ~/.local/bin/orch
  cp "${units[@]}" ~/.config/systemd/user/
  python3 ~/.claude/hooks/context-handoff-install.py
  # The orch MCP server, at user scope in every account (claude-acc N keeps its own config).
  if command -v claude >/dev/null; then
    for dir in default ~/.claude-[0-9]*; do
      if [ "$dir" = default ]; then acct=(env -u CLAUDE_CONFIG_DIR claude)
      elif [ -f "$dir/.claude.json" ]; then acct=(env CLAUDE_CONFIG_DIR="$dir" claude)
      else continue; fi
      if ! "${acct[@]}" mcp get orch >/dev/null 2>&1; then
        "${acct[@]}" mcp add --scope user orch -- python3 ~/.claude/hooks/orch_mcp.py
      fi
    done
  else
    echo "claude not on PATH: register the MCP server with" \
         "'claude mcp add --scope user orch -- python3 ~/.claude/hooks/orch_mcp.py'" >&2
  fi
  if systemctl --user daemon-reload 2>/dev/null; then
    systemctl --user enable --now orch-watchdog.timer
    # The gateway is enabled but not started: it needs `orch pair` and then
    # `systemctl --user start orch-serve`. A running one is restarted onto the new code.
    systemctl --user enable orch-serve.service
    systemctl --user try-restart orch-serve.service
  else
    echo "no systemd user manager: run 'orch watchdog' every 5 minutes another way (cron)" >&2
  fi
  exit
fi
for host in "$@"; do
  echo "== $host"
  ssh "$host" 'mkdir -p ~/.cache/claude-context-handoff/hooks ~/.cache/claude-context-handoff/systemd'
  for f in "${files[@]}"; do scp -q "$f" "$host:.cache/claude-context-handoff/$f"; done
  ssh "$host" 'cd ~/.cache/claude-context-handoff && bash install.sh'
done
