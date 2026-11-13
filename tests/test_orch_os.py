"""Offline tests for hooks/orch_os.py, what differs between Linux and Windows."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import orch_os

windows = pytest.mark.skipif(not orch_os.WINDOWS, reason="Windows only")


def test_shell_lines_set_the_env_for_the_command_alone():
    env, command = {"A": "it's"}, ["claude", "--name", "a b"]
    assert orch_os.shell_line(env, command) == """A='it'"'"'s' claude --name 'a b'"""
    assert orch_os.shell_line(env, command, "bash") == orch_os.shell_line(env, command)
    assert orch_os.shell_line(env, command, "powershell") == (
        "$env:A='it''s'; & 'claude' '--name' 'a b'; Remove-Item Env:A")
    assert orch_os.shell_line(env, command, "pwsh") == orch_os.shell_line(env, command, "powershell")
    assert orch_os.shell_line(env, command, "cmd") == (
        '''set "A=it's" & claude --name "a b" & set "A="''')
    assert orch_os.shell_line({}, ["claude"], "powershell") == "& 'claude'"


def test_a_command_runs_without_a_variable_the_shell_exports():
    command = orch_os.without_env("ORCH_T", ["sh", "-c", "echo \"[$ORCH_T]\""])
    if orch_os.WINDOWS:  # shortcut: no unset for one command there
        assert command == ["sh", "-c", "echo \"[$ORCH_T]\""]
        return
    line = orch_os.shell_line({"A": "1"}, command)
    out = subprocess.run(["sh", "-c", line], env={**os.environ, "ORCH_T": "/home/u/.claude-2"},
                         capture_output=True, text=True, timeout=20, check=True).stdout
    assert out == "[]\n"


@windows
def test_a_pane_line_is_in_the_panes_own_shell(monkeypatch):
    for shell, start in (("powershell", "$env:A='1'; & 'claude'"), ("cmd", 'set "A=1" & claude'),
                         ("bash", "A=1 claude")):
        monkeypatch.setattr(orch_os, "pane_shell", lambda pane, s=shell: s)
        assert orch_os.pane_line("w1:p1", {"A": "1"}, ["claude"]).startswith(start)


def test_a_powershell_line_really_runs_with_the_env_set_and_unset_after():
    powershell = "powershell" if orch_os.WINDOWS else "pwsh"
    if not orch_os.shutil.which(powershell):
        pytest.skip("no PowerShell here")
    line = orch_os.shell_line({"ORCH_T": "it's"}, ["cmd" if orch_os.WINDOWS else "sh"]
                              + (["/c", "echo %ORCH_T%"] if orch_os.WINDOWS
                                 else ["-c", "echo $ORCH_T"]), "powershell")
    out = subprocess.run([powershell, "-NoProfile", "-Command", line + "; echo \"[$env:ORCH_T]\""],
                         capture_output=True, text=True, timeout=60, check=True).stdout.split()
    assert out == ["it's", "[]"]


@pytest.mark.parametrize("path,slug", [("/home/me/app", "home-me-app")]
                         + ([("E:\\work\\app", "E--work-app"), ("E:/work/app", "E--work-app")]
                            if orch_os.WINDOWS else []))
def test_a_project_folder_gets_one_flat_name(path, slug):
    assert orch_os.project_slug(path) == slug


def test_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(orch_os.Path, "home", lambda: tmp_path)
    assert orch_os.account_dir("1") == tmp_path / ".claude"
    assert orch_os.account_dir(2) == tmp_path / ".claude-2"
    assert orch_os.account_of(None) == "1"
    assert orch_os.account_of(str(tmp_path / ".claude")) == "1"
    assert orch_os.account_of(str(tmp_path / ".claude-2")) == "2"
    assert orch_os.account_of(str(tmp_path / "other")) == str(tmp_path / "other")


@windows
def test_the_windows_profiles_are_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(orch_os.Path, "home", lambda: tmp_path)
    profile = tmp_path / ".claude-accounts" / "acc2"
    profile.mkdir(parents=True)
    assert orch_os.account_dir(2) == profile
    assert orch_os.account_dir(3) == tmp_path / ".claude-3"
    assert orch_os.account_of(str(profile)) == "2"
    assert orch_os.account_of(str(tmp_path / ".CLAUDE")) == "1"
    env, command = orch_os.account_command(2, ["--resume", "s"])
    assert env == {"CLAUDE_CONFIG_DIR": str(profile)} and command == ["claude", "--resume", "s"]
    assert orch_os.account_command("1", []) == ({}, ["claude"])


def test_this_process_can_be_read():
    me = os.getpid()
    assert orch_os.comm(me) and me in orch_os.pids()
    assert orch_os.parent(me) == os.getppid()
    assert orch_os.running(me)
    os.environ["ORCH_OS_TEST"] = "x"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "two words"],
                             env=dict(os.environ, ORCH_OS_TEST="seen"))
    try:
        deadline = time.time() + 10
        while time.time() < deadline:  # the program has to get past its loader first
            try:
                if orch_os.argv(child.pid)[-1] == "two words":
                    break
            except (OSError, IndexError):
                pass
            time.sleep(0.05)
        assert orch_os.argv(child.pid)[-1] == "two words"
        assert orch_os.environ(child.pid)["ORCH_OS_TEST"] == "seen"
        assert orch_os.parent(child.pid) == me
    finally:
        child.kill()
        child.wait()
        del os.environ["ORCH_OS_TEST"]
    assert not orch_os.running(child.pid) and orch_os.comm(child.pid) is None


def test_children_and_names_are_found_also_through_cmd():
    me, code = os.getpid(), "import time; time.sleep(60)"
    child = subprocess.Popen([sys.executable, "-c", code])
    wrapped = None
    if orch_os.WINDOWS:  # as Claude starts an MCP server
        wrapped = subprocess.Popen(f'cmd /d /s /c ""{sys.executable}" -c "{code}" orch_mcp.py"')
    try:
        assert child.pid in orch_os.children(me)
        assert child.pid not in orch_os.children(child.pid)
        assert orch_os.comm(me) and me in orch_os.named(orch_os.comm(me))
        if wrapped:
            deadline = time.time() + 15
            while time.time() < deadline and not [
                    p for p in orch_os.children(me) if orch_os.parent(p) == wrapped.pid]:
                time.sleep(0.05)
            [inner] = [p for p in orch_os.children(me) if orch_os.parent(p) == wrapped.pid]
            assert orch_os.comm(inner) != "cmd"
    finally:
        child.kill()
        child.wait()
        if wrapped:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(wrapped.pid)], capture_output=True,
                           check=False)
            wrapped.wait()


def test_checking_a_process_does_not_signal_it():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert orch_os.running(child.pid)
        time.sleep(0.2)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_a_missing_process_has_no_parent_and_its_memory_raises():
    gone = subprocess.Popen([sys.executable, "-c", ""])
    gone.wait()
    assert orch_os.parent(gone.pid) == 0
    with pytest.raises(OSError):
        orch_os.argv(gone.pid)


def test_the_lock_makes_a_second_holder_wait(tmp_path):
    path = tmp_path / "lock"
    child = """
import sys, time
sys.path.insert(0, sys.argv[1])
import orch_os
with open(sys.argv[2], "w") as f:
    orch_os.lock(f)
    print(time.time())
"""
    with open(path, "w") as held:
        orch_os.lock(held)
        waiter = subprocess.Popen([sys.executable, "-c", child, str(Path(orch_os.__file__).parent),
                                   str(path)], stdout=subprocess.PIPE, text=True)
        time.sleep(1)
        assert waiter.poll() is None
        released = time.time()
    assert float(waiter.communicate(timeout=30)[0]) >= released - 0.1


def test_replace_goes_on_after_a_reader_lets_go(tmp_path):
    source, target = tmp_path / "new", tmp_path / "old"
    source.write_text("new")
    target.write_text("old")
    orch_os.replace(source, target)
    assert target.read_text() == "new" and not source.exists()


def test_split_follows_the_os():
    assert orch_os.split('notify "a b" c') == ["notify", "a b", "c"]
    if orch_os.WINDOWS:
        assert orch_os.split(r'C:\tools\notify.exe x') == [r"C:\tools\notify.exe", "x"]


def test_a_detached_process_runs_on(tmp_path):
    out = tmp_path / "out"
    code = f"import pathlib; pathlib.Path({str(out)!r}).write_text('ran')"
    orch_os.spawn_detached([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 15
    while not out.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert out.read_text() == "ran"
