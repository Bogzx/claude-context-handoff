#!/usr/bin/env bash
# Install or update the context-handoff hooks.
#   ./install.sh                  this machine
#   ./install.sh HOST [HOST ...]  other machines over ssh, e.g. ./install.sh bogdan@dev2 ubuntu@ob1
set -euo pipefail
cd "$(dirname "$0")"
files=(hooks/context-handoff.py hooks/context-handoff-install.py)
if [ $# -eq 0 ]; then
  mkdir -p ~/.claude/hooks
  cp "${files[@]}" ~/.claude/hooks/
  python3 ~/.claude/hooks/context-handoff-install.py
  exit
fi
for host in "$@"; do
  echo "== $host"
  ssh "$host" 'mkdir -p ~/.claude/hooks'
  scp -q "${files[@]}" "$host:.claude/hooks/"
  ssh "$host" 'python3 ~/.claude/hooks/context-handoff-install.py'
done
