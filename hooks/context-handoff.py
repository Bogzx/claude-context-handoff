#!/usr/bin/env python3
"""Context handoff hook for Claude Code (PostToolUse, Stop, SessionStart).

Past HANDOFF_SOFT_TOKENS the agent is told to write a handoff at its next logical
stopping point; past HANDOFF_URGENT_TOKENS (default: 50K before auto-compaction) it is
told to write or refresh it now. A Stop that ends a turn with the handoff still due is
blocked once per stage.

Inside herdr, the first Stop after the handoff is written replaces the agent: a detached
process exits it, starts a fresh claude in the same pane with the same name, launch
flags, live model and effort, and account (CLAUDE_CONFIG_DIR, as set by claude-acc), and
prompts it to continue from the (archived) handoff.
Outside herdr, SessionStart (clear/compact) injects the handoff into the fresh context.

Each piece of work gets a lineage folder that the agents taking it over share,
~/.claude/handoffs/<project>/<pane>-<started>-<session>/: handoff.md (the current state,
rewritten each time), decisions.md (an append-only decision log across all of them) and
gen-NN.md (consumed handoffs). A takeover passes the folder on through HANDOFF_LINEAGE; a
/clear or compaction that consumes a pending handoff keeps it; any other start begins a new
one at its first notice.

Thresholds can be overridden per project in that project's .claude/settings.local.json
"env" block. Respawns are logged to ~/.claude/handoffs/respawn.log.
"""
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def tokens_env(name, default):
    raw = os.environ.get(name, "").strip().lower()
    try:
        if raw.endswith("k"):
            return int(float(raw[:-1]) * 1_000)
        if raw.endswith("m"):
            return int(float(raw[:-1]) * 1_000_000)
        return int(raw) if raw else default
    except ValueError:
        return default


# Claude Code compacts about 33K tokens before the window (it reserves room for output):
# a 633K window compacts at ~600K, observed as a 566K compaction under a 600K window.
COMPACT = tokens_env("CLAUDE_CODE_AUTO_COMPACT_WINDOW", 633_000) - 33_000
SOFT = tokens_env("HANDOFF_SOFT_TOKENS", 400_000)
URGENT = tokens_env("HANDOFF_URGENT_TOKENS", COMPACT - 50_000)
INLINE_LIMIT = 9_000  # Claude Code caps injected context at 10,000 characters
ROOT = Path.home() / ".claude" / "handoffs"
SEALED = ".decisions.sealed"  # the log as of the last handoff, for the append-only check
POLICY = "Context handoff policy (configured by the user in ~/.claude/settings.json)"
IN_HERDR = os.environ.get("HERDR_ENV") == "1" and bool(os.environ.get("HERDR_PANE_ID"))

TEMPLATE = """\
The handoff is a Markdown file that lets an agent with no memory of this session continue \
the work at the same quality. Completeness matters more than brevity, because omissions are \
the most common way handoffs fail. Sections:
1. Goal and scope: the user's request and every constraint, quoted verbatim, and what done means.
2. Environment: repo, branch, worktree path, commits made, uncommitted changes, running \
processes, background jobs, scheduled loops (with their prompts), herdr agents being \
orchestrated (names, pane IDs, what each is doing), PRs and issues touched.
3. Progress: every work item marked done and verified, done but unverified, partial, or not \
started, with how it was verified (exact commands and results).
4. Specs and key facts: exact file paths, functions, line numbers, interfaces, data shapes, \
commands and config values the remaining work depends on. Structural detail rather than \
paraphrase; reference files instead of pasting large content.
5. Open questions, risks and blockers, including decisions that belong to the user.
6. Ideas: recommendations, suspicions and improvements noticed but not acted on.
7. Next steps: an ordered list whose first item is concrete and can be started immediately.
8. Verify first: the claims a fresh agent re-checks before building on them."""

LOG_RULES = """\
Decisions and ruled-out approaches go in the decision log at {log}, not in the handoff. The \
log covers this piece of work across every agent that takes it over, and each new agent reads \
it in full. It is append-only: earlier entries are never edited, shortened or removed, and a \
decision that changes gets a new entry naming the one it replaces. One entry per decision, \
numbered D1, D2, ... continuing from the last one: date and time, who decided (the user, \
quoted verbatim, or the agent), what was chosen or ruled out, and why, with the evidence \
behind a ruled-out approach. Before the handoff is written, the log holds every decision this \
session made or learned: the user's instructions and choices, the agent's own design \
decisions, and the approaches ruled out."""

LOG_HEADER = """\
# Decision log

Started {started}. It covers this piece of work across every agent that takes it over, and \
each new agent reads it in full. Append-only: entries are never edited, shortened or removed, \
and a decision that changes gets a new entry naming the one it replaces. Each entry: D<n>, \
date and time, who decided (the user, quoted verbatim, or the agent), what was chosen or ruled \
out, and why.

## Generation 1
"""

if IN_HERDR:
    AFTER = ("After writing it, the agent ends its turn. A fresh agent with the same settings "
             "then takes over this herdr pane automatically and continues from the handoff. "
             "The takeover starts only once no background tasks are running, so before ending "
             "that turn the agent stops the background tasks and monitors it started, after "
             "recording in the handoff any the next agent should restart (exact command or "
             "condition). Background work still producing results it needs can finish first.")
else:
    AFTER = ("Once it is written, the user is told it is ready and that /clear starts a fresh "
             "context seeded with it; the file is updated at later stopping points if work "
             "continues.")

NEXT_PROMPT = (
    "Context handoff: the previous agent in this pane reached its context threshold. Read "
    "the decision log {log} in full, then its handoff {path}. Re-check the claims in the "
    "handoff's Verify first section, then continue from its next steps, adding entries to the "
    "decision log as decisions are made (append-only; the rules are at its top). Continue "
    "only work the user already asked for; if the work is complete or the next step needs "
    "the user's decision, summarize where things stand and wait.")

# claude flags dropped when relaunching: session identity, the old initial prompt, and the
# model/effort, which are re-added from the live session.
STRIP_VALUE = {"--model", "--effort", "--session-id", "--resume", "-r"}
STRIP_FLAG = {"--continue", "-c", "--fork-session"}
BOOLEAN = STRIP_FLAG | {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                        "--verbose", "--ide", "--strict-mcp-config", "--chrome", "--no-chrome"}


def last_call(transcript):
    """(input-side tokens, model) of the latest main-chain API call in the transcript."""
    try:
        with open(transcript, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            chunk = 1 << 20
            while True:
                start = max(0, size - chunk)
                f.seek(start)
                lines = f.read(size - start).splitlines()
                if start:
                    lines = lines[1:]  # the first line may be cut mid-way
                for line in reversed(lines):
                    if b'"usage"' not in line and b'"compact_boundary"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("subtype") == "compact_boundary":
                        return 0, ""  # no API call since compacting; older usage is stale
                    if entry.get("type") != "assistant" or entry.get("isSidechain"):
                        continue
                    message = entry.get("message") or {}
                    model = message.get("model") or ""
                    if not model.startswith("claude"):
                        continue
                    usage = message.get("usage") or {}
                    return sum(usage.get(k) or 0 for k in (
                        "input_tokens", "cache_read_input_tokens",
                        "cache_creation_input_tokens")), model
                if start == 0:
                    return 0, ""
                chunk *= 8
    except OSError:
        return 0, ""


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def written_since(handoff, ts):
    return handoff.exists() and handoff.stat().st_mtime > ts


def clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def structure(lineage):
    return f"{TEMPLATE}\n\n{LOG_RULES.format(log=lineage / 'decisions.md')}"


def soft_message(tokens, lineage):
    return (f"{POLICY}: this conversation is at {tokens // 1000}K tokens, past the "
            f"{SOFT // 1000}K handoff threshold; auto-compaction runs at {COMPACT // 1000}K. "
            "The handoff belongs at the next logical stopping point, once the current unit of "
            "work is complete and verified, not in the middle of it. It is written to "
            f"{lineage / 'handoff.md'}, replacing any older version. {AFTER}\n\n"
            f"{structure(lineage)}")


def urgent_message(tokens, lineage, state):
    handoff = lineage / "handoff.md"
    if "soft" in state and written_since(handoff, state["soft"]):
        status = (f"The handoff was last written at {clock(handoff.stat().st_mtime)} and does "
                  "not cover work since then.")
    else:
        status = "No handoff has been written yet."
    return (f"{POLICY}: this conversation is at {tokens // 1000}K tokens and auto-compaction "
            f"runs at {COMPACT // 1000}K. {status} The handoff at {handoff} is due now, before "
            "the current task continues, so that it survives compaction. "
            f"{AFTER}\n\n{structure(lineage)}")


def slot():
    """Where this session runs: its herdr pane (agents often share a worktree), else local."""
    return os.environ["HERDR_PANE_ID"].replace(":", "-") if IN_HERDR else "local"


def new_lineage(base, session):
    # The session that starts it makes the name unique (a session starts at most one).
    lineage = base / f"{slot()}-{time.strftime('%Y%m%d-%H%M%S')}-{session[:8]}"
    lineage.mkdir(parents=True, exist_ok=True)
    (lineage / "decisions.md").write_text(
        LOG_HEADER.format(started=time.strftime("%Y-%m-%d %H:%M")))
    seal(lineage)
    return lineage


def seal(lineage):
    shutil.copyfile(lineage / "decisions.md", lineage / SEALED)


def log_problem(lineage):
    """What keeps the decision log from being ready for the next agent, if anything."""
    try:
        sealed = (lineage / SEALED).read_text(errors="replace").rstrip()
    except OSError:
        return None
    try:
        log = (lineage / "decisions.md").read_text(errors="replace")
    except OSError:
        log = ""
    if not log.startswith(sealed):
        return (f"no longer starts with its content as of the last handoff, kept in "
                f"{lineage / SEALED}, so earlier entries were edited or removed. They are "
                "restored from that copy, and a change to one goes in a new entry that names "
                "the entry it replaces.")
    if log.rstrip() == sealed:
        return ("has no entries from this session. Every decision this session made or "
                "learned goes in it, by the rules at its top; if there were none, one line "
                "says so.")
    return None


def pending(base):
    """The newest unconsumed handoff from this pane (outside herdr: from this project)."""
    found = sorted(base.glob(f"{slot()}-*/handoff.md"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def consume(lineage, how):
    """Archive the pending handoff as the next generation and mark the new one in the log."""
    n = len(list(lineage.glob("gen-*.md"))) + 1
    gen = lineage / f"gen-{n:02d}.md"
    (lineage / "handoff.md").rename(gen)
    with open(lineage / "decisions.md", "a") as f:
        f.write(f"\n## Generation {n + 1}: {how}, {time.strftime('%Y-%m-%d %H:%M')}, "
                f"from {gen.name}\n")
    seal(lineage)
    return gen


def claude_pid():
    """PID of the claude process that runs this hook (the nearest claude ancestor)."""
    pid = os.getppid()
    while pid > 1:
        try:
            if Path(f"/proc/{pid}/comm").read_text().strip() == "claude":
                return pid
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return None


def relaunch_args(argv, model, effort):
    out, found, i = [], {}, 0
    while i < len(argv):
        arg = argv[i]
        flag = arg.split("=", 1)[0]
        if flag in STRIP_VALUE:
            takes_next = "=" not in arg and i + 1 < len(argv) and not argv[i + 1].startswith("-")
            found[flag] = arg.split("=", 1)[1] if "=" in arg else argv[i + 1] if takes_next else ""
            i += 2 if takes_next else 1
            continue
        prev = out[-1] if out else ""
        is_value = prev.startswith("-") and "=" not in prev and prev not in BOOLEAN
        if flag in STRIP_FLAG or (not arg.startswith("-") and not is_value):
            i += 1  # a session flag, or a positional: the old session's initial prompt
            continue
        out.append(arg)
        i += 1
    model = model or found.get("--model")
    effort = effort or found.get("--effort")
    return out + (["--model", model] if model else []) + (["--effort", effort] if effort else [])


def start_respawn(data, lineage, model):
    pid = claude_pid()
    if not pid:
        return False
    argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[1:]
    effort = (data.get("effort") or {}).get("level") or os.environ.get("CLAUDE_EFFORT")
    env = {k: os.environ[k] for k in ("CLAUDE_CONFIG_DIR",) if os.environ.get(k)}
    job = {"pane": os.environ["HERDR_PANE_ID"], "pid": pid, "dir": str(lineage),
           "args": relaunch_args([a.decode() for a in argv if a], model, effort),
           "env": {"HANDOFF_LINEAGE": str(lineage), **env}}
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--respawn", json.dumps(job)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def respawn(job):
    """Detached: replace the agent in job["pane"] with a fresh one seeded with the handoff."""
    pane, pid = job["pane"], job["pid"]
    herdr_bin = shutil.which("herdr") or str(Path.home() / ".local" / "bin" / "herdr")
    log = ROOT / "respawn.log"

    def say(text):
        with open(log, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {pane} {text}\n")

    def herdr(*args, timeout=120):
        return subprocess.run([herdr_bin, *args], capture_output=True, text=True,
                              timeout=timeout)

    def alive():
        try:
            return Path(f"/proc/{pid}/comm").read_text().strip() == "claude"
        except OSError:
            return False

    def fail(text):
        say("FAILED: " + text)
        herdr("notification", "show", "Handoff respawn failed",
              "--body", f"{pane}: {text} (see {log})")

    def pane_agent():
        agents = json.loads(herdr("agent", "list").stdout)["result"]["agents"]
        return next((a for a in agents if a.get("pane_id") == pane), None)

    try:
        agent = json.loads(herdr("agent", "get", pane).stdout)["result"]["agent"]
        name = agent.get("name")
        time.sleep(3)  # let the remaining Stop hooks and the turn finish
        herdr("agent", "wait", pane, "--timeout", "60000")
        herdr("agent", "prompt", pane, "/exit")
        for sig, wait in ((None, 20), (signal.SIGTERM, 10), (signal.SIGKILL, 5)):
            if sig and alive():
                os.kill(pid, sig)
            for _ in range(wait):
                if not alive():
                    break
                time.sleep(1)
            if not alive():
                break
        else:
            return fail(f"claude pid {pid} would not exit")
        for _ in range(15):  # wait for herdr to release the pane
            if not pane_agent():
                break
            time.sleep(1)
        time.sleep(1)
        lineage = Path(job["dir"])
        if not (lineage / "handoff.md").exists():
            return fail(f"{lineage / 'handoff.md'} vanished before the respawn")
        saved = consume(lineage, f"takeover by {name or 'an unnamed agent'} in herdr pane {pane}")
        # Typed into the pane's shell, like the original launch; the env prefix keeps the
        # account that claude-acc selected, and the lineage, for this session only.
        prefix = "".join(f"{k}={shlex.quote(v)} " for k, v in job.get("env", {}).items())
        command = prefix + " ".join(shlex.quote(a) for a in ["claude", *job["args"]])
        herdr("pane", "run", pane, command)
        for _ in range(90):
            agent = pane_agent()
            if agent and agent.get("agent_status") in ("idle", "done"):
                break
            time.sleep(1)
        else:
            return fail(f"no ready agent after running {command}; handoff at {saved}")
        time.sleep(2)  # let the input box settle before prompting
        if name:
            herdr("agent", "rename", pane, name)
        herdr("agent", "prompt", pane,
              NEXT_PROMPT.format(log=lineage / "decisions.md", path=saved))
        say(f"respawned {name or '(unnamed)'}: {command}; handoff {saved}")
    except Exception as e:  # never leave the pane silently empty
        fail(repr(e))


def threshold(event, data, base, state_path):
    tokens, model = last_call(data.get("transcript_path") or "")
    state = load(state_path)
    if "path" in state:  # from the flat layout before lineages: notify again with the new paths
        state = {}
    lineage = Path(state["dir"]) if state.get("dir") else None  # fixed once per session
    now = time.time()

    def save():
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state))

    written = (lineage is not None and "soft" in state
               and written_since(lineage / "handoff.md", state["soft"]))
    problem = event == "Stop" and written and not state.get("stop_log") and log_problem(lineage)
    if problem:
        state["stop_log"] = True
        save()
        return {"decision": "block", "systemMessage": "Handoff written; the decision log is not.",
                "reason": f"{POLICY}: the decision log {lineage / 'decisions.md'} {problem}"}
    if event == "Stop" and IN_HERDR and written and "respawned" not in state:
        tasks = data.get("background_tasks") or []
        if tasks:  # exiting would kill them; the Stop after the last one ends retries
            waiting = (f"Handoff written; the respawn waits for {len(tasks)} background "
                       f"task(s) ({', '.join(sorted({str(t.get('type')) for t in tasks}))}).")
            if state.get("stop_tasks"):
                return {"systemMessage": waiting}
            state["stop_tasks"] = True
            save()
            running = "; ".join(
                f"{t.get('type')} {t.get('id')}: "
                f"{(t.get('command') or t.get('description') or '')[:200]}" for t in tasks)
            return {"decision": "block", "systemMessage": waiting, "reason": (
                f"{POLICY}: the handoff is written, but the takeover by a fresh agent waits "
                f"until no background tasks are running. Still running: {running}. Each task "
                "the next agent can restart is recorded in the handoff (exact command or "
                "condition) and then stopped. A task still producing results this work needs "
                "can finish instead; the takeover happens at the first turn end after the "
                "last task is gone.")}
        state["respawned"] = now
        save()
        if start_respawn(data, lineage, model):
            return {"systemMessage": "Handoff written: a fresh agent with the same settings "
                                     "takes over this pane in a few seconds."}
    if event == "Stop" and data.get("stop_hook_active"):
        return None
    due = ("urgent" if tokens >= URGENT and "urgent" not in state
           else "soft" if tokens >= SOFT and "soft" not in state else None)
    if due and lineage is None:
        lineage = new_lineage(base, data.get("session_id") or "unknown")
        state["dir"] = str(lineage)
    if due == "urgent":
        stage, message = "urgent", urgent_message(tokens, lineage, state)
        state.setdefault("soft", now)
        state["urgent"] = now
    elif due == "soft":
        stage, message = "soft", soft_message(tokens, lineage)
        state["soft"] = now
        if event == "Stop":
            message += "\n\nThe turn is ending, which is such a stopping point."
    elif event == "Stop":
        stage = "urgent" if "urgent" in state else "soft" if "soft" in state else None
        if (not stage or written_since(lineage / "handoff.md", state[stage])
                or state.get(f"stop_{stage}")):
            return None
        limit = URGENT if stage == "urgent" else SOFT
        message = (f"{POLICY}: the handoff requested at {limit // 1000}K tokens has not been "
                   f"written to {lineage / 'handoff.md'}, and the turn is ending, which is the "
                   "stopping point it was waiting for. It is due now, with the structure from "
                   f"the earlier notice. {AFTER}")
    else:
        return None
    if event == "Stop":
        state[f"stop_{stage}"] = True
    save()
    notice = f"Context at {tokens // 1000}K tokens: {stage} handoff requested ({lineage.name})"
    if event == "Stop":
        return {"decision": "block", "reason": message, "systemMessage": notice}
    return {"systemMessage": notice,
            "hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}


def session_start(source, base, state_path):
    for old in (ROOT / "state").glob("*.json"):
        if old.stat().st_mtime < time.time() - 7 * 86400:
            old.unlink(missing_ok=True)
    lineage = None
    if source == "compact":  # the same session: keep its lineage, re-arm the thresholds
        lineage = load(state_path).get("dir")
        handoff = Path(lineage) / "handoff.md" if lineage else None
    elif source == "startup" and os.environ.get("HANDOFF_LINEAGE"):
        lineage, handoff = os.environ["HANDOFF_LINEAGE"], None  # a takeover; it prompts next
    elif source in ("startup", "clear"):
        handoff = pending(base)
        if handoff and source == "clear":
            lineage = str(handoff.parent)
    else:
        return None
    if lineage:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"dir": lineage}))
    else:
        state_path.unlink(missing_ok=True)
    if not handoff or not handoff.exists():
        return None
    written = time.strftime("%Y-%m-%d %H:%M", time.localtime(handoff.stat().st_mtime))
    if source == "startup":
        context = (f"An unread handoff from an earlier session in this project, written "
                   f"{written}, is at {handoff}. It applies only if this session continues "
                   "that work.")
    else:
        text = handoff.read_text(errors="replace")
        saved = consume(handoff.parent, "after /clear" if source == "clear" else
                        "after compaction")
        after = ("; work after that time appears only in the compaction summary"
                 if source == "compact" else "")
        context = (f"Handoff from the previous context of this project, written {written}"
                   f"{after}. Its claims are re-checked before building on them. Archived "
                   f"copy: {saved}. The decision log for this work, "
                   f"{handoff.parent / 'decisions.md'}, is read in full too, and gets new "
                   "entries as decisions are made (append-only; the rules are at its top).")
        if len(context) + len(text) + 2 <= INLINE_LIMIT:
            context += "\n\n" + text
        else:
            context += (f" The handoff is {len(text):,} characters, over the inline limit, so "
                        "it is not shown here; reading that file in full comes before anything "
                        "else.")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}


def main():
    if sys.argv[1:2] == ["--respawn"]:
        return respawn(json.loads(sys.argv[2]))
    data = json.load(sys.stdin)
    if data.get("agent_id"):  # subagents have their own context; this tracks the main one
        return
    event = data.get("hook_event_name")
    project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    base = ROOT / project.strip("/").replace("/", "-").replace(":", "-")
    state_path = ROOT / "state" / f"{data.get('session_id') or 'unknown'}.json"
    if event == "SessionStart":
        out = session_start(data.get("source"), base, state_path)
    else:
        out = threshold(event, data, base, state_path)
    if out:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
