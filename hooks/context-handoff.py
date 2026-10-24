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

Thresholds can be overridden per project in that project's .claude/settings.local.json
"env" block. Handoffs live in ~/.claude/handoffs/, one file per project directory and
herdr pane (several agents often share one worktree); respawns are logged to
~/.claude/handoffs/respawn.log.
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
5. Decisions: what was chosen and why, including user-approved choices that are settled.
6. Ruled out: approaches and hypotheses already rejected, one line each with the reason.
7. Open questions, risks and blockers, including decisions that belong to the user.
8. Ideas: recommendations, suspicions and improvements noticed but not acted on.
9. Next steps: an ordered list whose first item is concrete and can be started immediately.
10. Verify first: the claims a fresh agent re-checks before building on them."""

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
    "Context handoff: the previous agent in this pane reached its context threshold and "
    "wrote a handoff to {path}. Read it in full, re-check the claims in its Verify first "
    "section, then continue from its next steps. Continue only work the user already asked "
    "for; if the work is complete or the next step needs the user's decision, summarize "
    "where things stand and wait.")

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


def soft_message(tokens, handoff):
    return (f"{POLICY}: this conversation is at {tokens // 1000}K tokens, past the "
            f"{SOFT // 1000}K handoff threshold; auto-compaction runs at {COMPACT // 1000}K. "
            "The handoff belongs at the next logical stopping point, once the current unit of "
            "work is complete and verified, not in the middle of it. It is written to "
            f"{handoff}, replacing any older version. {AFTER}\n\n{TEMPLATE}")


def urgent_message(tokens, handoff, state):
    if "soft" in state and written_since(handoff, state["soft"]):
        status = (f"The handoff was last written at {clock(handoff.stat().st_mtime)} and does "
                  "not cover work since then.")
    else:
        status = "No handoff has been written yet."
    return (f"{POLICY}: this conversation is at {tokens // 1000}K tokens and auto-compaction "
            f"runs at {COMPACT // 1000}K. {status} The handoff at {handoff} is due now, before "
            f"the current task continues, so that it survives compaction. {AFTER}\n\n{TEMPLATE}")


def archive(handoff):
    target = ROOT / "archive" / f"{handoff.stem}.{time.strftime('%Y%m%d-%H%M%S')}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    handoff.rename(target)
    return target


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


def start_respawn(data, handoff, model):
    pid = claude_pid()
    if not pid:
        return False
    argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[1:]
    effort = (data.get("effort") or {}).get("level") or os.environ.get("CLAUDE_EFFORT")
    job = {"pane": os.environ["HERDR_PANE_ID"], "pid": pid, "handoff": str(handoff),
           "args": relaunch_args([a.decode() for a in argv if a], model, effort),
           "env": {k: os.environ[k] for k in ("CLAUDE_CONFIG_DIR",) if os.environ.get(k)}}
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
        handoff = Path(job["handoff"])
        saved = archive(handoff) if handoff.exists() else None
        if not saved:
            return fail(f"{handoff} vanished before the respawn")
        # Typed into the pane's shell, like the original launch; the env prefix keeps the
        # account that claude-acc selected for this session only.
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
        herdr("agent", "prompt", pane, NEXT_PROMPT.format(path=saved))
        say(f"respawned {name or '(unnamed)'}: {command}; handoff {saved}")
    except Exception as e:  # never leave the pane silently empty
        fail(repr(e))


def threshold(event, data, handoff, state_path):
    tokens, model = last_call(data.get("transcript_path") or "")
    state = load(state_path)
    handoff = Path(state.setdefault("path", str(handoff)))  # fixed once per session
    now = time.time()

    def save():
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state))

    if (event == "Stop" and IN_HERDR and "soft" in state and "respawned" not in state
            and written_since(handoff, state["soft"])):
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
        if start_respawn(data, handoff, model):
            return {"systemMessage": "Handoff written: a fresh agent with the same settings "
                                     "takes over this pane in a few seconds."}
    if event == "Stop" and data.get("stop_hook_active"):
        return None
    if tokens >= URGENT and "urgent" not in state:
        stage, message = "urgent", urgent_message(tokens, handoff, state)
        state.setdefault("soft", now)
        state["urgent"] = now
    elif tokens >= SOFT and "soft" not in state:
        stage, message = "soft", soft_message(tokens, handoff)
        state["soft"] = now
        if event == "Stop":
            message += "\n\nThe turn is ending, which is such a stopping point."
    elif event == "Stop":
        stage = "urgent" if "urgent" in state else "soft" if "soft" in state else None
        if not stage or written_since(handoff, state[stage]) or state.get(f"stop_{stage}"):
            return None
        limit = URGENT if stage == "urgent" else SOFT
        message = (f"{POLICY}: the handoff requested at {limit // 1000}K tokens has not been "
                   f"written to {handoff}, and the turn is ending, which is the stopping point "
                   f"it was waiting for. It is due now, with the structure from the earlier "
                   f"notice. {AFTER}")
    else:
        return None
    if event == "Stop":
        state[f"stop_{stage}"] = True
    save()
    notice = f"Context at {tokens // 1000}K tokens: {stage} handoff requested ({handoff.name})"
    if event == "Stop":
        return {"decision": "block", "reason": message, "systemMessage": notice}
    return {"systemMessage": notice,
            "hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}


def session_start(source, handoff, state_path):
    for old in (ROOT / "state").glob("*.json"):
        if old.stat().st_mtime < time.time() - 7 * 86400:
            old.unlink(missing_ok=True)
    if source == "compact":
        handoff = Path(load(state_path).get("path", handoff))
        state_path.unlink(missing_ok=True)  # re-arm the thresholds for the shrunk context
    if not handoff.exists():
        return None
    written = time.strftime("%Y-%m-%d %H:%M", time.localtime(handoff.stat().st_mtime))
    if source == "startup":
        context = (f"An unread handoff from an earlier session in this project, written "
                   f"{written}, is at {handoff}. It applies only if this session continues "
                   "that work.")
    elif source in ("clear", "compact"):
        text = handoff.read_text(errors="replace")
        saved = archive(handoff)
        after = ("; work after that time appears only in the compaction summary"
                 if source == "compact" else "")
        context = (f"Handoff from the previous context of this project, written {written}"
                   f"{after}. Its claims are re-checked before building on them. Archived "
                   f"copy: {saved}.")
        if len(context) + len(text) + 2 <= INLINE_LIMIT:
            context += "\n\n" + text
        else:
            context += (f" It is {len(text):,} characters, over the inline limit, so it is not "
                        "shown here; reading that file in full comes before anything else.")
    else:
        return None
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}


def main():
    if sys.argv[1:2] == ["--respawn"]:
        return respawn(json.loads(sys.argv[2]))
    data = json.load(sys.stdin)
    if data.get("agent_id"):  # subagents have their own context; this tracks the main one
        return
    event = data.get("hook_event_name")
    project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    if IN_HERDR:
        project += "--" + os.environ["HERDR_PANE_ID"]
    handoff = ROOT / (project.replace("/", "-").replace(":", "-") + ".md")
    state_path = ROOT / "state" / f"{data.get('session_id') or 'unknown'}.json"
    if event == "SessionStart":
        out = session_start(data.get("source"), handoff, state_path)
    else:
        out = threshold(event, data, handoff, state_path)
    if out:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
