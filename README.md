# claude-context-handoff

Claude Code hooks that hand a long session over to a fresh agent before its context fills up.

At 400K tokens the agent is asked to write a handoff document at its next logical stopping point. Inside
herdr, the agent is then replaced in the same pane by a new one with the same name, launch
flags, model, effort and account, and the new agent continues from the handoff. Auto-compaction at about 600K stays
as the backstop.

Installed on dev1, dev2 and ob1 since 2026-10-24.

## Stages

| Context | What happens |
| --- | --- |
| 400K (`HANDOFF_SOFT_TOKENS`) | Soft notice: write the handoff once the current unit of work is complete and verified. |
| 550K (`HANDOFF_URGENT_TOKENS`, default 50K before compaction) | Urgent notice: write or refresh the handoff now. |
| ~600K | Claude Code auto-compacts (`CLAUDE_CODE_AUTO_COMPACT_WINDOW=633000`). |

The notices are added as context after a tool call (PostToolUse) or block the Stop at the end of a turn. A Stop is
blocked at most once per stage while the handoff is still unwritten. Subagents are ignored; only the main
conversation is tracked.

The token count is the input side of the latest main-chain API call in the transcript
(`input_tokens + cache_read_input_tokens + cache_creation_input_tokens`). After a compaction it reads 0 until the
next call.

Each notice includes a 10-section template: goal and scope (quoted verbatim), environment, progress, specs and key
facts, decisions, ruled out, open questions, ideas, next steps, and claims to verify first.

## Takeover inside herdr

At the first Stop after the handoff has been written, the hook starts a detached process
(`context-handoff.py --respawn <job>`) that:

1. reads the herdr agent name of the pane, waits for the turn to finish, and sends `/exit`
   (then SIGTERM, then SIGKILL, only if the process is still `claude`);
2. waits for herdr to release the pane and moves the handoff to `archive/`;
3. types `CLAUDE_CONFIG_DIR=… claude <original flags> --model <live model> --effort <live effort>` into the pane
   with `herdr pane run`;
4. waits until the new agent is idle, restores the old name, and prompts it to read the archived handoff and
   continue.

The relaunch keeps the original command-line flags but drops the session identity (`--resume`, `--continue`,
`--session-id`, `--fork-session`) and the old initial prompt. The model is taken from the transcript, so a `/model`
switch carries over, and the effort comes from the hook input or `$CLAUDE_EFFORT`. The account carries over through
`CLAUDE_CONFIG_DIR`, which `claude-acc N` sets for accounts above 1 (`pibo2`). It goes in as a prefix on the
command only, so a later `pibo` in the same pane is not affected.

Every takeover is logged in `~/.claude/handoffs/respawn.log`. A failure is logged there too and raises a herdr
notification.

### Background tasks

`/exit` would kill background tasks, so the takeover waits until none are running. The handoff notice asks the agent
to stop the background tasks and monitors it started before ending its turn, after recording any that should be
restarted (exact command or condition). If tasks are still running at the Stop, the hook blocks once with the list
and asks again. After that it only shows "the respawn waits for N background task(s)", and the takeover happens at
the first turn end after the last task is gone. Scheduled `/loop` jobs don't block; the template records them so
the new agent can recreate them.

For an orchestrator this means its per-worker `herdr agent wait` loops: it records and stops them, and the new
orchestrator re-arms them.

## Outside herdr

There is no takeover. The user is told the handoff is ready. On `/clear` or after a compaction, SessionStart injects
the handoff into the fresh context and archives it. A handoff over 9,000 characters is not inlined; the agent is told
to read the file first. A new session (`startup`) only gets a one-line pointer to an unread handoff.

## Install

Requirements: Linux (`/proc`), Python 3.8 or newer (ob1 runs 3.8, so keep the script 3.8-compatible), and herdr for
the takeover.

```bash
git clone https://github.com/Bogzx/claude-context-handoff.git
cd claude-context-handoff
./install.sh                          # this machine
./install.sh bogdan@dev2 ubuntu@ob1   # other machines over ssh
```

`install.sh` copies `hooks/` into `~/.claude/hooks/` and runs `context-handoff-install.py`, which merges the hooks and
env into `~/.claude/settings.json`. It is idempotent and backs up the settings to `settings.json.bak-<timestamp>`
whenever it changes them. It adds:

- `env`: `CLAUDE_CODE_AUTO_COMPACT_WINDOW=633000`, `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS=400000`
- `hooks`: `python3 ~/.claude/hooks/context-handoff.py` on SessionStart (matcher `^(startup|clear|compact)$`),
  PostToolUse and Stop. Existing hooks are left alone.

Running sessions pick up the settings at their next start.

Account 2 needs nothing extra: `claude-acc` symlinks `~/.claude-2/settings.json` and `~/.claude-2/hooks` to
`~/.claude`.

To change the hook, edit it here, commit, then run both install commands above.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `HANDOFF_SOFT_TOKENS` | `400000` | Soft notice threshold. Accepts `400k`, `1m`. |
| `HANDOFF_URGENT_TOKENS` | compaction point − 50K | Urgent notice threshold. |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | `633000` | Claude Code's window; it compacts about 33K below it. |

For a per-project override, put the variable in that project's `.claude/settings.local.json` under `env`. For a
quick test, set it inline: `HANDOFF_SOFT_TOKENS=1000 claude …`.

## Runtime files

All under `~/.claude/handoffs/`:

- `<project-path>--<herdr-pane>.md`: the pending handoff (`<project-path>.md` outside herdr). It is keyed by pane
  because several workers often share one worktree. The path is fixed once per session.
- `archive/<name>.<timestamp>.md`: consumed handoffs.
- `state/<session-id>.json`: per-session stage timestamps (`soft`, `urgent`, `stop_*`, `respawned`, `path`).
  Files older than 7 days are deleted.
- `respawn.log`: takeovers and failures.

## Checking it works

```bash
tail ~/.claude/handoffs/respawn.log
sha256sum ~/.claude/hooks/context-handoff.py hooks/context-handoff.py
ssh bogdan@dev2 'sha256sum ~/.claude/hooks/context-handoff.py'
ssh ubuntu@ob1 'sha256sum ~/.claude/hooks/context-handoff.py'
jq .env ~/.claude/settings.json
```

Verified on 2026-10-24 in throwaway herdr panes: a named agent on each account was replaced with the same name,
flags, live model, effort and account, and the new agent read its handoff. Live takeovers happened the same day for
a Sonnet worker (`vid-extract`) and for the Opus session that built these hooks.

## Known gaps

- Inline env vars other than `CLAUDE_CONFIG_DIR` are not carried over, nor are mid-session `/fast` or
  permission-mode toggles.
- A flag that takes several values (`--add-dir a b`) loses all but the first.
- A hook can't run `/clear` or `/compact`, so outside herdr the user runs `/clear`.
- `herdr agent start` can't take an env prefix, which is why the relaunch uses `herdr pane run`.

## Why 400K and 600K

Research on long contexts (Chroma's "Context Rot", Du et al. at EMNLP 2025, Anthropic's context-engineering post) shows
quality dropping as context grows, and the common rule of thumb is to reset at 40–60% of the window. On that basis
the first recommendation was a handoff at about 120K and a hard stop at about 200K. The owner chose 400K and 600K
instead, because the current models hold up well at high context and the projects (AlphaProve in particular) are
large. Limits based on project size were considered and dropped: what fills the context is the task, not the size of
the repo. Per-project overrides cover the exceptions.
