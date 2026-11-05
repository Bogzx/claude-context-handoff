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

In daily use on three machines since 2026-10-24.

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

1. reads the herdr agent name of the pane and waits until the agent is no longer working (up to 30 minutes:
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
`CLAUDE_CONFIG_DIR`, which `claude-acc N` sets for accounts above 1. It goes in as a prefix on the
command only, so a later command in the same pane is not affected.

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
  new orchestrator of one large project start at 140–190K of its 400K tokens (177 KB log), and another project's
  log reached 587 KB. So the first line of an entry has to say on its own what was decided and by whom.

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
| `orch ask "<question>" [--option "Deploy" --danger deploy --option "Not yet"]` | Logs a question for the owner in the run and pushes it, with an id and the options the owner's app shows as buttons. `--danger` (`none`, `deploy`, `prod`, `destructive`) applies to the option before it. `--kind plan` marks a new orchestrator's plan question (see orch new); it replaces the pane's open plan question. |
| `orch answer <id> "<text>"` | Records the owner's answer and prompts the agent that asked. An answered question can't be answered again. |
| `orch status [--json]`, `orch last <pane>` | The fleet, and a worker's last message and background tasks. `--json` also has the open questions, each agent's latest report and account, and pending account switches. |
| `orch decisions [--pane P] [--json]` | The pane's newest decision log, parsed: id, time, who (user or agent), the user's quote, what, why (only from an explicit "Why:" or "because"; everything else stays in what), and `superseded_by` when a later entry says "replaces D7", "instead of D7" or "supersedes D7". In log order. |
| `orch account [<pane> <N>] [--now \| --when-idle \| --at-handoff]`, `orch account --default N`, `orch account --name N NAME` | Which Claude account (`claude-acc N`) an agent runs on, and moving it to another one (see Accounts). |
| `orch usage [--json] [--hours 24]`, `orch usage capture` | Account limits and tokens per agent (see Usage). |
| `orch watchdog [--dry-run]` | One watchdog pass. A systemd user timer runs it every 5 minutes. |
| `orch new --cwd DIR --goal "..." --until +8h [--model M] [--effort E] [--account N\|auto] [--plan-first] [--rules "..."] [--id ID] [--dry-run]` | Starts a new orchestrator on this machine (see New orchestrator). |
| `orch serve`, `orch pair [--allow NODE] [--new]` | The HTTP gateway for the owner's phone app, and its pairing token (see Gateway). |

Inside herdr, the Stop hook writes `~/.claude/handoffs/agents/<pane>.json` at every turn end, with the session, the
background tasks still running and the last message. SessionStart writes it too. This is how `orch` tells a finished
worker from one waiting on its own job. A file from another session than the pane's current agent is ignored.

The watchdog only looks at registered, unexpired runs and does nothing for any other pane. When a run's orchestrator is idle,
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

Everything goes to `~/.claude/handoffs/events.jsonl`, one JSON object per line: `time`, `host` (the hostname; the
gateway's history shows the machine name instead), `kind`, `pane` (null when no agent is concerned), `title`, `body`,
plus the fields of its kind:

| Kind | Written when | `pane` | Extra fields | Alert |
| --- | --- | --- | --- | --- |
| `run-start`, `run-set`, `run-stop` | `orch run` changes a run | orchestrator | | |
| `run-ended` | the watchdog finds the run past its end | orchestrator | | yes |
| `orch-missing`, `orch-blocked` | the orchestrator pane has no agent, or sits at a dialog | orchestrator | | yes |
| `nudge` | the watchdog prompts an idle orchestrator | orchestrator | | |
| `orch-not-acting` | the same problem after several nudges, or the nudge could not be sent | orchestrator | | yes |
| `ask` | `orch ask` or the MCP `ask_owner` | the agent that asks | `id`, `question`, `options` (`label`, `danger`), `question_kind` (`plan` or null) | yes |
| `answer` | `orch answer` or the app answers | the agent that asked | `id` | |
| `report` | the MCP `report` tool | the agent | `report` | |
| `account` | a switch is queued, asked, resumed or done, or the default changes | the agent; null for the default | `account`; `when` (`handoff`, `idle`, `now`); `how` (`handoff`, `resume`) when a switch starts; `reason` (`spill`, `limit`) when the spill rule queued it; `session` after a resume | |
| `account-failed`, `respawn-failed` | a switch or takeover failed | the agent | | yes |
| `respawn` | a takeover started its fresh agent | the agent | | |
| `respawn-dialog` | a takeover's or `orch new`'s new agent waits at a dialog (not for a start from the app, which has its `start` push) | the agent | `action` for `orch new` | yes |
| `spill` | the spill rule is set, turned off, fires or clears | null | when it fires: `window`, `used`, `since` | when it fires |
| `start` | a step of `orch new` or the gateway's `start` action is doing, ok or failed | the new orchestrator, once its tab exists | `action` (the start's id), `step`, `status`, `error`; the `pane` step also `folder`, `goal`, `by` (`cli`, `phone`) | when it fails |
| `test` | `orch notify` | null | | yes |
| `pair` | a phone pairs or unpairs with the gateway | null | | |
| `action` | the gateway handles an app action | its agent or orchestrator, else null | `action` (its kind), `id`, `status` (`delivered`, `rejected`, `failed`), `error`, `source` (`app`, `relay`), `fields` (the action's other fields) | |
| `push-failed` | a push could not be sent | the orchestrator, or null | | |
| `push-token` | FCM refused a phone's token | null | | |
| `relay-rejected` | a relay message does not open with this machine's key | null | | |

The alerts for the owner also raise a herdr notification and run the notify command, if there is one:
`$ORCH_NOTIFY_CMD`, else an executable `~/.claude/handoffs/notify`. It is called as `<command> <title> <body>` with
`ORCH_EVENT_KIND` set and the whole event as JSON in `ORCH_EVENT`. That is where a phone push plugs in.
`orch notify <title> <body>` sends a test.

### Turn-end reports (MCP)

`hooks/orch_mcp.py` is a small MCP server (stdio, standard library only) that `install.sh` registers at user scope
as `orch`. It takes the agent's pane from `HERDR_PANE_ID` and offers no tools outside herdr. Its two tools:

- `report`: the agent's state at the end of a working turn. It goes into `report` in
  `~/.claude/handoffs/agents/<pane>.json` (the Stop hook keeps it there when it rewrites the file) and into
  events.jsonl as kind `report`. `summary` (one line) is required; everything else is optional:

  | Role | Fields |
  | --- | --- |
  | orchestrator | `state` (`working`, `waiting_on_workers`, `waiting_on_owner`, `done`), `progress` {`done`, `total`, `label`}, `waiting_on` [{`who`, `what`, `eta`}], `next` [text], `roadmap` [{`id`, `title`, `status` (`done`, `doing`, `next`, `later`, `blocked`), `owner`, `eta`, `why`}], `risks` [text], `milestones` [{`kind` (`pr_merged`, `deploy`, `release`, `other`), `ref`, `url`}], `suggestions` [text: messages the owner might send next] |
  | worker | `status` (`done`, `partial`, `blocked`), `tests` [{`cmd`, `passed`, `failed`}], `changed_files`, `needs_live_test`, `issues_for_others` [text] |

  `role` is taken from the pane's run (orchestrator or worker) unless the call sets it. A report that doesn't fit the
  schema is refused with the reason, so the agent can fix it.
- `ask_owner`: `question` and `options` [{`label`, `danger`}], the same as `orch ask` with options. It returns the
  question id; the answer comes back as a prompt once `orch answer` runs.

The Stop hook asks for the report. When an agent is the orchestrator or a worker of an active run, its turn used
tools, and that turn didn't call `mcp__orch__report`, the hook blocks the turn's end once with a one-line reason.
Turns that only chatted are left alone, and so are turns while a handoff is due or a takeover is starting, and
sessions that don't run the `orch` server: the hook looks for an `orch_mcp.py` child of the session's claude
process, so a session started before the install is never asked for a tool it doesn't have. A turn is never blocked
twice in a row.

For the orchestrator's CLAUDE.md:

> End each working turn with the orch `report` tool: a one-line summary, then your state, progress, who you are
> waiting on, next steps and roadmap. Ask the owner with `ask_owner` (or `orch ask --option ...`) and keep working.

### Accounts

`claude-acc N` runs Claude Code as account N: account 1 is `~/.claude`, account N is `CLAUDE_CONFIG_DIR=~/.claude-N`,
which shares everything with `~/.claude` except the login and `.claude.json`. Each account has its own 5-hour and
7-day limits. The status file records which account each agent runs on.

`orch account <pane> N` moves an agent to account N in one of three ways:

- `--at-handoff` (the default): the next takeover starts under account N. It costs nothing extra but may be hours
  away.
- `--when-idle`: at the agent's next turn end (the Stop hook) or, if it is already idle, at the next watchdog pass.
  An agent with 60K tokens of context or more is asked for its handoff, and the takeover starts the fresh agent on
  account N, which reads the handoff instead of re-reading the whole conversation there. A smaller context, an agent
  that hit its usage limit, or a handoff still unwritten 30 minutes after the request gets a `--now` switch instead
  (the watchdog spends up to 200 s per pass on those). Each choice is logged with its reason.
- `--now`: only for an idle agent with no background tasks. The agent exits, `claude-acc N --resume <session>` starts
  in the same pane with the same flags, model and effort, and orch checks that it came up. The first turn after it
  re-reads the whole context uncached on the new account.

Pending switches are kept in `accounts.json` and shown by `orch status --json`. Every switch is logged as kind
`account`; a failed one is pushed. When the new account hasn't trusted the folder yet, the new agent stops at Claude
Code's trust question, and the question has to be answered in the pane. A `--now` switch reports that at once. A
takeover pushes a `respawn-dialog` alert and waits up to 15 minutes for the answer, then sends its prompt.
`orch account --default N` records the account for new agents. `orch account --name N NAME` gives account N a short
name (default "Acct N"; `""` resets it), shown by `orch account`, `orch status --json` (`account_names`), `orch usage`
and the gateway.

`orch account --spill 1:98:2` uses account 1 until it is nearly full, then moves its agents to account 2. When
account 1's week or 5-hour window reaches 98%, every agent on account 1 gets a pending `--when-idle` switch to
account 2. Only fresh data counts: a stale `usage.json` or a window that has already reset is treated as unknown.
The trigger is pushed once (kind `spill`). An agent on account 1 whose last turn ended with Claude Code's limit
message ("You've hit your weekly limit · resets ...", an API error `rate_limit` in the transcript) is resumed on
account 2 at the next pass. The account of each agent is read from its claude process (`CLAUDE_CONFIG_DIR`).
`--spill off` turns the rule off and drops the switches it queued. The rule and whether it has fired show in
`orch account --json`, `orch status --json` and the gateway's fleet.

On a machine with a spill rule, a takeover starts on the pending switch's account if there is one. Otherwise it
starts on account 2 while the rule is active, and on the default account (`orch account --default`, 1 unless set)
once account 1 is back under the threshold. New agents should start with `claude-acc $(orch account --for-new) ...`;
an agent started on account 1 while the rule is active is moved by the watchdog anyway.

The rule's progress shows as `spill` in `orch status --json` and the gateway's fleet (when it fired, `moved` of
`total`). Each agent also gets a `spill` entry:
- `moved_idle` or `moved_limit`, with how (handoff or resume) and when;
- `waiting`, with what on: working, own job running, writing its handoff, or the next watchdog pass;
- `cant_move`, with the error. A resume that failed is not retried until the rule fires again.

### Usage

The statusline command gets the account's limits on stdin (`rate_limits.five_hour` and `rate_limits.seven_day`, each
`used_percentage` and `resets_at` in epoch seconds, present while the API reports them). Add this line to
`~/.claude/statusline.sh`, after it has read its input into `$input`:

```bash
printf '%s' "$input" | python3 ~/.claude/hooks/orch.py usage capture >/dev/null 2>&1 &
```

`orch usage capture` keeps the latest limits per account in `usage.json` and adds a line to `usage-history.jsonl`
at most every 5 minutes per account. It prints nothing and ignores input it can't read.

`orch usage --json` reports, per account and window, the percentage used, the reset time, the burn rate over the last
1-2 hours of history, the projected time to 100% and whether that comes before the reset. It also counts tokens
(input, output, cache read, cache write) and an API-equivalent cost per agent, orchestrator and worker, run, day,
account and machine, from the transcripts in `~/.claude/projects`. Each window lists the agents that spent in it
(`agents`): their share of this machine's cost there, the points that share stands for (`estimated_points`), and their
points in the last hour and today (`points_last_hour`, `points_today`: their cost then times the window's points per
dollar). Every account orch knows is listed, with `windows: {}` until a statusline has run on it. Sessions are
matched to panes through the status files, herdr and the lineage folders, and to accounts through the status files
and the statusline. The cost is an estimate from list prices (`PRICES` in `hooks/orch_usage.py`), not a bill, and the
shares only count this machine. Transcripts are read incrementally (`usage-cache.json` keeps the offsets and 10-minute
buckets for 8 days, and older files are skipped). On one machine, with 4.4 GB of transcripts, the first pass took 7 s and
the next one under a second.

### New orchestrator (orch new)

`orch new --cwd DIR --goal TEXT --until T` starts an orchestrator in a new herdr tab in `DIR` and prints its pane
first. The gateway's `start` action runs the same code. Each step is `doing`, `ok` or `failed`:

1. `pane`: with `--worktree`, `git worktree add <DIR>-<name> -b orch/<name>` first. Then a herdr tab in the folder
   (its root pane is the orchestrator's), a new decision log for that pane that starts with the goal (D1) and each
   `--rules` text (D2, ...) as the owner's entries, and a brief at `~/.claude/orch/new/<id>/brief.md` with the goal,
   the rules, the run and the worker limits (`--worker-model`, `--max-workers`).
2. `claude`: `claude-acc N` in the pane (`--account auto`, the default, is the account for new agents: the spill
   target while the rule is active, else the default), with a session id chosen up front, `--model`, `--effort` and
   `HANDOFF_LINEAGE` pointing at the new log. `--name` names the herdr agent.
3. `trust`, only when the new agent stops at a dialog (Claude Code's folder trust question on that account): it
   waits up to 15 minutes for an answer in the pane, as a takeover does. From the CLI it also pushes
   `respawn-dialog`.
4. `run`: the run is registered (goal, `--until`, cwd, no workers) with the start's settings in `run.settings`.
5. `brief`, or `read` with `--plan-first`: the agent is prompted to read the brief, with the same Enter-and-resend
   check as a takeover prompt. With `--plan-first` the brief asks for a roadmap in a `report` and
   `orch ask --kind plan` with the options "Approve and start workers" and "Cancel the run", before any other work.
   That question makes `read` ok and `plan` doing; the owner's answer (`orch answer` or the app) makes `plan` ok.
   While the plan question is open the watchdog doesn't nudge the run. A revised plan asked again replaces the open
   question.

Each step is a `start` event, and the start's state is kept in `~/.claude/orch/new/<id>/new.json`. The same `--id`
again starts nothing and prints that state. `--dry-run` prints every step, the brief and the command, and changes
nothing.

### Gateway (orch serve)

`hooks/orch_serve.py` is the HTTP gateway the phone app (orch-app) calls on each machine. The contract is orch-app's
`docs/API.md` (v1), and `docs/PUSH.md` for pushes and the relay. Its HTTP side uses only the standard library and runs
on Python 3.8; the push and relay code (`hooks/orch_push.py`) also needs the `cryptography` package (see Pushes).

- It listens on port 7433, on the machine's Tailscale IPv4 (`tailscale ip -4`) only, never on 0.0.0.0, and won't
  start without one.
- A request must come from a tailnet node in the allow-list, checked with `tailscale whois` against the node's
  StableID, short name or full name. It must also carry the pairing token as `Authorization: Bearer <token>`. An
  unknown node gets 403 and a missing or wrong token gets 401, both with an empty body.
- `orch pair --allow <phone's tailnet name>` writes the token and the allow-list to `~/.config/orch/.env` (mode 600,
  directory 700) and prints the token and a pairing string for a QR code
  (`orch://pair?machine=...&addr=...&port=7433&token=...&key=...&project=...`). `key` is the AES-256 key of pushes and
  relay messages; `project` is the Firebase project, printed once the service account key is installed. Run it again to
  change the list; `--new` replaces the token and the key. The gateway reads the file on every request, so changes take
  effect without a restart.
- The machine's name in the API, pushes and the relay is `ORCH_MACHINE` from that file, else its Tailscale device
  name (`tailscale status --json`, `Self.HostName`), else its hostname.
- `orch serve --allow-self` also admits the machine's own node. It is off by default and meant for a local test.
- `GET /v1/health` (version, clock, push and relay state; the `Server` header is `orch/<version>`), `POST /v1/pair`
  (the phone's FCM token and relay uid; a node of the machine's own Tailscale user with the right token joins the
  allow-list by StableID, then gets a `test` push) and `DELETE /v1/pair` (leaves the allow-list, token forgotten).
  The gateway keeps the phones in `~/.config/orch/phones.json` (600).
- Reads: `GET /v1/fleet` (runs with their orchestrator and workers, reports, open questions, blocked dialogs with the
  screen text, stall reasons with proposed fixes, accounts with today's samples, pending switches, recent actions),
  `/v1/usage?hours=168`, `/v1/orchestrators/<pane>/decisions`, `/v1/orchestrators/<pane>/history?since=&limit=`,
  `/v1/panes/<pane>/screen?lines=15`, `/v1/actions/<id>` and `/v1/folders?q=&limit=50` (see Starts from the app).
- Writes: `POST /v1/actions`, one JSON action with the app's `id` (a UUID), `time` and `kind`: `prompt`, `answer`,
  `interrupt` (Esc), `park`, `unpark`, `run` (`extend` from the current end, or `stop`), `nudge`, `account` (`now`,
  `idle`, `handoff`), `default_account`, `spill`, `ack` (decisions seen), `push_token` (a rotated FCM token) or
  `start` (a new orchestrator, see Starts from the app). An id runs once: a retry with the same body gets the
  first result, and the same id with a different body is refused. Actions older than 10 minutes are refused. The
  answer is 202 `delivered`, 4xx `rejected` or 502 `failed`, with a sentence the app can show. An action becomes
  `picked` once its target agent starts a turn (for an agent that was already working, at its next state change). A
  switch `now` runs in the background, and its action ends `picked` or `failed`.
- The gateway appends every action, refused ones included, to `events.jsonl` as kind `action` with `source: "app"`
  (or `"relay"`).
- `install.sh` installs `orch-serve.service` (systemd user unit) enabled but not started. It only runs once
  `~/.config/orch/.env` has the token: `orch pair`, then `systemctl --user start orch-serve`. Before that the file
  may hold other settings (`ORCH_START_ROOTS`), and a start of the unit is skipped.

Not in the gateway yet: answering a permission dialog (`dialog` is refused), the diff view, the activity lanes and the
per-agent model.

#### Starts from the app

- They are off until `ORCH_START_ROOTS` is set in `~/.config/orch/.env`: comma-separated folders, `~` allowed.
  `ORCH_START_EXCLUDE` lists folders a start may never use, such as live production checkouts. Both stay in that
  file, never in the repo.
- `start` takes `{folder, worktree?, name?, goal, until ("+4h" or a time), account ("auto" or N), model, effort,
  worker_model, max_workers (0-16), rules: [text], plan_first}` and runs `orch new` with them. It is refused (409,
  with the reason) for a folder outside the roots, under an excluded folder, or one an active run works in already
  (unless `worktree`). The action's id is the start's id. The gateway runs the start detached and answers 202 with
  `pane` once the new tab exists (or `pane: null` after 20 s; the action lock is held meanwhile), or 502 `failed`
  when the start failed before it had a pane. The steps then arrive as `start` pushes.
- `/v1/fleet` shows an unfinished start (the last day's, until `brief` or `plan` is ok) as an orchestrator with
  activity `starting` and `start: {action, goal, plan_first, until, steps: [{step, status, time, error?}]}`; its
  agent's dialog during the `trust` step has `kind: "trust"` and the folder as `cwd`. Questions carry `kind`
  (`"plan"`), runs started this way `run.settings`, and a `start` action's status its `pane`.
- `/v1/folders` lists the git checkouts that are a root or directly under one, plus the herdr agents' checkouts
  deeper under a root, never an excluded one, newest work first: `path` (`~` form), `branch` (a short sha when
  detached), `last_activity` (git index, HEAD or reflog, or a Claude transcript of the folder), `orchestrator`
  (`{pane, name}` of an active run there) and `trusted` (the folder or a parent is trusted in the `.claude.json` of
  the account a new agent would use). `q` filters on the path, `limit` is 1-200.
- Not yet: answering the trust dialog from the app (so a start in a folder the account hasn't trusted waits for an
  answer in the pane), retrying or cancelling a failed start, `Health.can_start`, closing the tab of a start whose
  run stopped with no worker.

### Pushes and the relay (orch_push.py)

The gateway's background loop also pushes to the paired phones and polls the relay mailbox, so notifications and quick
answers work off the tailnet. The app side is orch-app `docs/PUSH.md`.

- Down: FCM HTTP v1 data messages sent by the machine itself with the `orch-push` service account
  (`~/.config/orch/firebase-sa.json`, 600). The payload is zlib-compressed JSON sealed with AES-256-GCM under the
  pairing key (`ORCH_KEY`), so Google relays only `{v, machine, nonce, ct}`. What is pushed: questions and dialogs open
  30 s (high priority); stalls 5 minutes after the watchdog's nudge, ended runs, usage running out before its reset,
  spill events, failures (`system`), action statuses, the steps of a start (`start`, high priority only when one
  failed); a state snapshot per orchestrator on change (at most one a minute), accounts on a whole-percent change (at
  most every 5 minutes) and a heartbeat every 10 minutes (collapsible).
  The gateway trims a message over FCM's 4 KB in fixed steps and lists them in its `trimmed`. A passing FCM
  failure is retried on the next pass; a dead token stops pushes to that phone until the app sends a new one.
- Up: the phone writes sealed actions to Firestore at `mailboxes/<its anonymous uid>/<machine>/<id>`. The gateway
  polls its paired phones' mailboxes (every 30 s, every 5 s for 10 minutes after a push that may get an answer), runs
  each action through the same code as `POST /v1/actions` (`source: "relay"`) and deletes it.
- The crypto uses the `cryptography` package from the system (Debian/Ubuntu: python3-cryptography; 2.8, Ubuntu
  20.04's, is enough). Nothing else imports it: without it the gateway still serves HTTP and `/v1/health` says
  why pushes are off.
- `cloud/firebase_setup.py` sets up the Firebase project (`--project`, else the one in
  `~/.config/orch/google-services.json`) once, idempotently (`--dry-run` shows what it would change):
  APIs, the Firestore database (eur3 unless `--location`), Anonymous sign-in, the `orchPush` role (FCM send;
  Firestore get, list and delete) held by the `orch-push` service account, its key, and `cloud/firestore.rules`.
  `--copy-to host2 user@host3` copies the key. It uses the Firebase CLI's own login and keeps no secret.

## Outside herdr

There is no takeover. The user is told the handoff is ready. On `/clear` or after a compaction, SessionStart injects
the handoff into the fresh context, archives it in its lineage folder, and points to the decision log. A handoff over
9,000 characters is not inlined; the agent is told to read the file first. A new session (`startup`) only gets a
one-line pointer to an unread handoff.

## Install

Requirements: Linux (`/proc`), Python 3.8 or newer (Ubuntu 20.04 has 3.8, so keep the scripts 3.8-compatible), and herdr for
the takeover.

```bash
git clone https://github.com/Bogzx/claude-context-handoff.git
cd claude-context-handoff
./install.sh                          # this machine
./install.sh user@host2 user@host3   # other machines over ssh
```

`install.sh` copies `hooks/` into `~/.claude/hooks/`, links `~/.local/bin/orch` to `orch.py`, installs and starts the
`orch-watchdog.timer` systemd user unit, installs `orch-serve.service` enabled but not started (see Gateway), registers the `orch` MCP server at user scope in every account
(`claude mcp add --scope user orch -- python3 ~/.claude/hooks/orch_mcp.py`, skipped where it is already there), and
runs `context-handoff-install.py`, which merges the hooks and env into `~/.claude/settings.json`. For other hosts it copies the files to `~/.cache/claude-context-handoff` there and runs the
same script. It is idempotent and backs up the settings to `settings.json.bak-<timestamp>`
whenever it changes them. It adds:

- `env`: `CLAUDE_CODE_AUTO_COMPACT_WINDOW=633000`, `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS=400000`
- `hooks`: `python3 ~/.claude/hooks/context-handoff.py` on SessionStart (matcher `^(startup|clear|compact)$`),
  PostToolUse and Stop. Existing hooks are left alone.

Running sessions pick up the settings at their next start.

Account 2 shares the hooks and settings: `claude-acc` symlinks `~/.claude-2/settings.json` and `~/.claude-2/hooks`
to `~/.claude`. Its MCP servers live in its own `~/.claude-2/.claude.json`, which is why `install.sh` registers `orch`
in each account.

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
  `home-me-Repo-myapp/w17-p8B-20261026-134442-45643f64/`. The pane part is `local` outside herdr; several
  workers often share one worktree, so lineages are kept apart by pane. The session part is the first 8 characters of
  the session that started it, which also finds its transcript.
  - `handoff.md`: the pending handoff.
  - `decisions.md`: the decision log, and `.decisions.sealed`, its copy as of the last handoff.
  - `gen-NN.md`: consumed handoffs, in order.
  - `decisions-index.md`: the first line of every entry, rewritten at each takeover once the log is over 30 KB.
- `state/<session-id>.json`: per-session lineage and stage timestamps (`dir`, `soft`, `urgent`, `stop_*`,
  `respawned`). Files older than 7 days are deleted.
- `respawn.log`: takeovers, postponements and failures.
- `agents/<pane>.json`: each herdr agent's latest turn end (session, background tasks, last message, account, pid,
  relaunch flags) and its latest `report`.
- `runs/<orchestrator pane>.json`: an `orch` run (end time, goal, workers, parked workers, questions with their ids,
  options and answers), and `runs/<pane>.seen.json`, the events its waits already reported.
- `~/.claude/orch/new/<id>/`: one `orch new` or app start: `new.json` (its request and steps), `brief.md`, and
  `start.log` (the detached run's output, for a start from the app).
- `accounts.json`: pending account switches, the default account, the spill rule and the account names.
- `gateway-actions.json`: the gateway's actions of the last 24 h by id (result, status), and `decisions-seen.json`,
  the decisions the app marked as seen.
- `usage.json` (latest limits per account, and each session's account), `usage-history.jsonl`, `usage-cache.json`.
- `events.jsonl`: takeovers, failures, nudges, alerts and run changes. `watchdog.json`: the watchdog's memory.
- `archive/` and `<project-path>--<pane>.md`: the flat layout used before 2026-10-26. The hook no longer reads them.

## Checking it works

```bash
uv run --no-project --with pytest --with cryptography pytest -q        # offline tests
uv run --python 3.8 --no-project --with pytest --with cryptography==2.8 pytest -q  # the oldest Python and cryptography
tail ~/.claude/handoffs/respawn.log ~/.claude/handoffs/events.jsonl
systemctl --user list-timers orch-watchdog.timer
orch watchdog --dry-run
sha256sum ~/.claude/hooks/context-handoff.py hooks/context-handoff.py
ssh user@host2 'sha256sum ~/.claude/hooks/context-handoff.py'
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
instead, because the current models hold up well at high context and the projects (one in particular) are
large. Limits based on project size were considered and dropped: what fills the context is the task, not the size of
the repo. Per-project overrides cover the exceptions.
