# claude-context-handoff

Claude Code hooks that hand a long session over to a fresh agent before its context fills up.

At 400K tokens the agent is asked to write a handoff document at its next logical stopping point. Inside
herdr, the agent is then replaced in the same pane by a new one with the same name, launch
flags, model, effort and account, and the new agent continues from the handoff. Auto-compaction at about 600K stays
as the backstop.

Decisions don't go in the handoff. They go in an append-only decision log that every agent taking over the same work
reads and adds to (see [Decision log](#decision-log)).

`orch` (in `hooks/orch.py`) keeps a herdr orchestration moving across those takeovers: a run registered per
orchestrator pane, one shared wait, parked workers and a watchdog timer (see [Orchestration runs](#orchestration-runs-orch)).

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

Each notice includes an 8-section template for the handoff: goal and scope (quoted verbatim), environment, progress,
specs and key facts, open questions, ideas, next steps, and claims to verify first. It also gives the rules for the
decision log.

## Takeover inside herdr

At the first Stop after the handoff has been written, the hook starts a detached process
(`context-handoff.py --respawn <job>`) that:

1. reads the herdr agent name of the pane and waits until the agent has really stopped working (up to 30 minutes:
   another Stop hook, such as `/goal`'s, can keep the turn going). If it is still working, is at a dialog, or started
   background tasks after the handoff, the takeover is postponed: the hook's state is reset, so the next turn end
   tries again;
2. sends `/exit` (then SIGTERM, then SIGKILL, only if the process is still `claude`);
3. waits for herdr to release the pane, archives the handoff as the next `gen-NN.md` in the lineage folder, and adds a
   generation line to the decision log;
4. types `HANDOFF_LINEAGE=<lineage folder> CLAUDE_CONFIG_DIR=… claude <original flags> --model <live model> --effort
   <live effort>` into the pane with `herdr pane run`;
5. waits until the new agent is idle, restores the old name, and prompts it to read the decision log (see below), then
   the archived handoff, and continue. The prompt also names the pane's `orch` run, if it has an active one;
6. checks that the new agent started working. If not, it presses Enter, then sends the prompt once more, and then
   fails loudly. On 2026-11-02 a takeover prompt was typed but never submitted, and the pane sat unstarted for 9 hours
   while the log said "respawned";
7. if the old session had an unmet `/goal`, sends `/goal <condition>` again. It is queued behind the takeover prompt,
   and Claude Code runs it as the command (checked live on 2026-11-05). A `/goal` is per session, so without this it
   ended at the first takeover.

The relaunch keeps the original command-line flags but drops the session identity (`--resume`, `--continue`,
`--session-id`, `--fork-session`) and the old initial prompt. The model is taken from the transcript, so a `/model`
switch carries over, and the effort comes from the hook input or `$CLAUDE_EFFORT`. The account carries over through
`CLAUDE_CONFIG_DIR`, which `claude-acc N` sets for accounts above 1 (`pibo2`). It goes in as a prefix on the
command only, so a later `pibo` in the same pane is not affected.

Every takeover is logged in `~/.claude/handoffs/respawn.log` and `events.jsonl`. A failure is logged there too,
raises a herdr notification and goes to the owner's notify command (see [Alerts](#alerts)). A postponed takeover is
logged as `postponed: <why>`.

### Background tasks

`/exit` would kill background tasks, so the takeover waits until none are running. The handoff notice asks the agent
to stop the background tasks and monitors it started before ending its turn, after recording any that should be
restarted (exact command or condition). If tasks are still running at the Stop, the hook blocks once with the list
and asks again. After that it only shows "the respawn waits for N background task(s)", and the takeover happens at
the first turn end after the last task is gone. Scheduled `/loop` jobs don't block; the template records them so
the new agent can recreate them.

For an orchestrator this means its `orch wait`: it stops it, and the new orchestrator re-arms it. The `orch` run
itself is keyed by pane and carries over.

## Decision log

Before 2026-10-26 each handoff carried its own Decisions and Ruled out sections, and every generation rewrote them from
memory, so they shrank with each takeover. The orchestrator in pane p6Z (#1016) listed the owner's decisions with
dates and nine ruled-out approaches with reasons in its first handoff. The second kept six of the names and none
of the reasons. The third pointed to "spec + build doc for the full list" and kept none of the nine. A worker in pane
p7X went from about 50 lines of design decisions in generation 4 to none of them in generation 5.

Now each piece of work gets a lineage folder, and its decisions go in `decisions.md` there:

- What a new agent reads: the whole log while it is up to 30 KB. Past that, `decisions-index.md` (the first 160
  characters of every entry, written at each takeover) and the log from the generation that just finished, plus the
  one before it if both fit in 60 KB. Older entries are looked up by D-number. Reading every log in full had made each
  new AlphaProve orchestrator start at 140–190K of its 400K tokens (177 KB log), and the bg3-ai log reached 587 KB.
  So the first line of an entry has to say on its own what was decided and by whom.

- One entry per decision, numbered D1, D2, … across generations: date and time, who decided (the user, quoted
  verbatim, or the agent), what was chosen or ruled out, and why, with the evidence for a ruled-out approach.
- Append-only. Earlier entries are never edited, shortened or removed; a changed decision gets a new entry that names
  the one it replaces.
- The hook creates the log with these rules at the top. At each takeover, `/clear` or compaction that consumes a
  handoff, it adds a `## Generation N` line and keeps a copy of the log (`.decisions.sealed`).
- When the handoff is written, the Stop is blocked once if the log no longer starts with that copy (entries were
  edited or removed) or has nothing new since it, and tells the agent to restore the entries or add this session's.

Which lineage a session belongs to:

- A takeover passes the folder to the new agent through `HANDOFF_LINEAGE`, read only at `startup`.
- A `/clear` that consumes a pending handoff, or a compaction, keeps the folder.
- Anything else, such as a `/clear` that gives a worker a new task or a fresh `claude`, starts a new lineage at its
  first notice. That keeps one pane's unrelated tasks out of the same log.

The log stays in `~/.claude/handoffs`, not in the repo. Several agents share a worktree, every repo would get
untracked files, and logs can mention prod details. Decisions the project needs beyond one piece of work still belong
in the repo's own docs.

## Orchestration runs (orch)

`hooks/orch.py`, installed as `~/.local/bin/orch`. It came from an audit of four orchestrator runs (2026-11-05). The
orchestrators sat idle for hours while their workers waited: watchers killed under memory pressure (14.4 h), a takeover
that never started (9 h), a home-made wait loop kept alive by a leftover shell (6.8 h), an approval question parking
the whole fleet overnight (11.8 h), and "today's work is done" with three workers on standby.

| Command | What it does |
| --- | --- |
| `orch run start --until 09:00 --goal "..." --workers w17:pB7=builder,...` | Registers the run of the orchestrator in this pane (`--until` takes `HH:MM`, `YYYY-MM-DD HH:MM` or `+4h`). `run set`, `run stop` and `run show` change, end or show it. |
| `orch wait` | The orchestrator's one background watcher. It returns when a worker's turn ended with nothing left running, a worker is blocked at a dialog, a pane lost its agent (3 min), a worker has waited 90 min on its own job, a `--check '<cmd>'` exits 0, or after the 30 min heartbeat. It prints one line per worker. An event is reported once: the next wait doesn't repeat it (`--reset` does). |
| `orch park <pane> --reason "..." [--until HH:MM]` | Standby with a reason. It ends at the worker's next turn end or at `--until`. `orch unpark` ends it. |
| `orch ask "<question>"` | Logs a question for the owner in the run and pushes it. |
| `orch status [--json]`, `orch last <pane>` | The fleet, and a worker's last message and background tasks. |
| `orch watchdog [--dry-run]` | One watchdog pass. A systemd user timer runs it every 5 minutes. |

What tells a finished worker from one waiting on its own job: inside herdr, the Stop hook writes
`~/.claude/handoffs/agents/<pane>.json` at every turn end, holding the session, the background tasks still running and
the last message. SessionStart writes it too. A file from another session than the pane's current agent is ignored.

The watchdog only looks at registered, unexpired runs, so it costs nothing elsewhere. When a run's orchestrator is idle,
it prompts it if any of these hold:

- an unparked worker has been idle for 10 minutes with nothing running;
- a worker has been at a dialog for 10 minutes, has had no agent for 5, or has waited 90 minutes on its own job;
- the orchestrator's last turn ended with no background task (nothing would wake it) 5 minutes ago and a worker is
  idle;
- the orchestrator has been idle for an hour.

It prompts at most every 15 minutes. After three prompts in a row about the same problem, it alerts the owner and
slows down to hourly. It also alerts the owner, without prompting, when the orchestrator pane has had no agent for 5
minutes or has been at a dialog for 10. When the run's end time passes, it marks the run ended and alerts once.

### Alerts

Everything goes to `~/.claude/handoffs/events.jsonl`, one JSON object per line (`time`, `host`, `kind`, `pane`,
`title`, `body`). Alerts for the owner (a failed takeover, an `orch ask`, an orchestrator gone, blocked or not acting,
a run that ended) also raise a herdr notification and run the notify command, if there is one: `$ORCH_NOTIFY_CMD`, else
an executable `~/.claude/handoffs/notify`. It is called as `<command> <title> <body>` with `ORCH_EVENT_KIND` set. That
is where a phone push plugs in. `orch notify <title> <body>` sends a test.

## Outside herdr

There is no takeover. The user is told the handoff is ready. On `/clear` or after a compaction, SessionStart injects
the handoff into the fresh context, archives it in its lineage folder, and points to the decision log. A handoff over
9,000 characters is not inlined; the agent is told to read the file first. A new session (`startup`) only gets a
one-line pointer to an unread handoff.

## Install

Requirements: Linux (`/proc`), Python 3.8 or newer (ob1 runs 3.8, so keep the script 3.8-compatible), and herdr for
the takeover.

```bash
git clone https://github.com/Bogzx/claude-context-handoff.git
cd claude-context-handoff
./install.sh                          # this machine
./install.sh bogdan@dev2 ubuntu@ob1   # other machines over ssh
```

`install.sh` copies `hooks/` into `~/.claude/hooks/`, links `~/.local/bin/orch` to `orch.py`, installs and starts the
`orch-watchdog.timer` systemd user unit, and runs `context-handoff-install.py`, which merges the hooks and env into
`~/.claude/settings.json`. For other hosts it copies the files to `~/.cache/claude-context-handoff` there and runs the
same script. It is idempotent and backs up the settings to `settings.json.bak-<timestamp>`
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

- `<project-path>/<pane>-<started>-<session>/`: one lineage folder, for example
  `home-bogdan-Repo-AlphaProve/w17-p8B-20261026-134442-45643f64/`. The pane part is `local` outside herdr; several
  workers often share one worktree, so lineages are kept apart by pane. The session part is the first 8 characters of
  the session that started it, which also finds its transcript.
  - `handoff.md`: the pending handoff.
  - `decisions.md`: the decision log, and `.decisions.sealed`, its copy as of the last handoff.
  - `gen-NN.md`: consumed handoffs, in order.
  - `decisions-index.md`: the first line of every entry, rewritten at each takeover once the log is over 30 KB.
- `state/<session-id>.json`: per-session lineage and stage timestamps (`dir`, `soft`, `urgent`, `stop_*`,
  `respawned`). Files older than 7 days are deleted.
- `respawn.log`: takeovers, postponements and failures.
- `agents/<pane>.json`: each herdr agent's latest turn end (session, background tasks, last message).
- `runs/<orchestrator pane>.json`: an `orch` run (end time, goal, workers, parked workers, questions), and
  `runs/<pane>.seen.json`, the events its waits already reported.
- `events.jsonl`: takeovers, failures, nudges, alerts and run changes. `watchdog.json`: the watchdog's memory.
- `archive/` and `<project-path>--<pane>.md`: the flat layout used before 2026-10-26. The hook no longer reads them.

## Checking it works

```bash
uv run --no-project --with pytest pytest -q                 # offline tests
uv run --python 3.8 --no-project --with pytest pytest -q    # the Python that ob1 runs
tail ~/.claude/handoffs/respawn.log ~/.claude/handoffs/events.jsonl
systemctl --user list-timers orch-watchdog.timer
orch watchdog --dry-run
sha256sum ~/.claude/hooks/context-handoff.py hooks/context-handoff.py
ssh bogdan@dev2 'sha256sum ~/.claude/hooks/context-handoff.py'
ssh ubuntu@ob1 'sha256sum ~/.claude/hooks/context-handoff.py'
jq .env ~/.claude/settings.json
```

Verified on 2026-10-24 in throwaway herdr panes: a named agent on each account was replaced with the same name,
flags, live model, effort and account, and the new agent read its handoff. Live takeovers happened the same day for
a Sonnet worker (`vid-extract`) and for the Opus session that built these hooks. The decision log and lineage folders were
verified live on 2026-10-26 after installing on all three machines: a Sonnet agent in a throwaway pane on account 2
logged two decisions and wrote its handoff, and its replacement got the same lineage through `HANDOFF_LINEAGE`, read
the log and then `gen-01.md`, and found the `## Generation 2` line the hook had added.

## Known gaps

- Inline env vars other than `CLAUDE_CONFIG_DIR` are not carried over, nor are mid-session `/fast` or
  permission-mode toggles.
- A flag that takes several values (`--add-dir a b`) loses all but the first.
- A hook can't run `/clear` or `/compact`, so outside herdr the user runs `/clear`.
- `herdr agent start` can't take an env prefix, which is why the relaunch uses `herdr pane run`.
- A `claude` started from inside a taken-over agent (for example `claude -p`) inherits `HANDOFF_LINEAGE` and would
  write into the same lineage if it ever reached the threshold.
- A session that was between its notice and its takeover when the new layout was installed gets the notice again
  with the new paths. A pending handoff it wrote in the flat layout stays where it is.
- When the takeover prompt doesn't start the new agent, it is sent a second time, and if the first copy was still in
  the input box the agent gets it twice.
- A `/goal` is carried over only through a herdr takeover, not through `/clear` or compaction.
- The watchdog sees what herdr and the status files show. An orchestrator that answers every prompt but never assigns
  work is reported to the owner after three prompts; it is not stopped.
- `orch` runs on one machine's herdr server. An orchestrator driving workers on another machine (`herdr --machine`)
  isn't covered.

## Why 400K and 600K

Research on long contexts (Chroma's "Context Rot", Du et al. at EMNLP 2025, Anthropic's context-engineering post) shows
quality dropping as context grows, and the common rule of thumb is to reset at 40–60% of the window. On that basis
the first recommendation was a handoff at about 120K and a hard stop at about 200K. The owner chose 400K and 600K
instead, because the current models hold up well at high context and the projects (AlphaProve in particular) are
large. Limits based on project size were considered and dropped: what fills the context is the task, not the size of
the repo. Per-project overrides cover the exceptions.
