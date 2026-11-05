#!/usr/bin/env bash
# Install or update the context-handoff hooks, orch and its watchdog timer.
#   ./install.sh                  this machine
#   ./install.sh HOST [HOST ...]  other machines over ssh, e.g. ./install.sh bogdan@dev2 ubuntu@ob1
set -euo pipefail
cd "$(dirname "$0")"
files=(hooks/context-handoff.py hooks/context-handoff-install.py hooks/orch.py
       systemd/orch-watchdog.service systemd/orch-watchdog.timer install.sh)
if [ $# -eq 0 ]; then
  mkdir -p ~/.claude/hooks ~/.local/bin ~/.config/systemd/user
  cp hooks/context-handoff.py hooks/context-handoff-install.py hooks/orch.py ~/.claude/hooks/
  chmod +x ~/.claude/hooks/orch.py
  ln -sfn ~/.claude/hooks/orch.py ~/.local/bin/orch
  cp systemd/orch-watchdog.service systemd/orch-watchdog.timer ~/.config/systemd/user/
  python3 ~/.claude/hooks/context-handoff-install.py
  if systemctl --user daemon-reload 2>/dev/null; then
    systemctl --user enable --now orch-watchdog.timer
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
