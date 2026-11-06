"""What differs between Linux and Windows, in one place: `WINDOWS` is detected here and the rest of
hooks/ calls these functions. Linux reads /proc as before; Windows uses the Win32 API through
ctypes, so there is still nothing to install. Python 3.8 compatible.
"""
import os
import shlex
import shutil
import struct
import subprocess
import time
from pathlib import Path

WINDOWS = os.name == "nt"

if WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    # herdr and the logs speak UTF-8 and Windows' default is cp1252. The installed commands pass
    # -X utf8; this carries it to the python children (a takeover, a start).
    os.environ.setdefault("PYTHONUTF8", "1")
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p,
                                                 ctypes.c_ulong, ctypes.c_void_p]
    _shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    _shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]

    class _ProcessEntry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
else:
    import fcntl


# --- processes ---------------------------------------------------------------------------------

def _table():
    """{pid: (parent pid, exe name without its extension)} of every process on this machine."""
    snapshot = _kernel32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    out = {}
    try:
        entry = _ProcessEntry(dwSize=ctypes.sizeof(_ProcessEntry))
        more = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            # split: Claude's updater renames its running exe to claude.exe.old.<time>
            out[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.split(".")[0])
            more = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return out


def _parameters(pid):
    """(command line, environment) from the process's memory. Windows has no other way to read
    another process's environment, and it only needs the same user.
    shortcut: 64-bit layouts of PEB and RTL_USER_PROCESS_PARAMETERS, a 32-bit python gets OSError."""
    if struct.calcsize("P") != 8:
        raise OSError("reading a process needs a 64-bit python")
    # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
    handle = _kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    def memory(address, size):
        buffer = ctypes.create_string_buffer(size)
        if not _kernel32.ReadProcessMemory(handle, address, buffer, size, None):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw

    def pointer(address):
        return struct.unpack("<Q", memory(address, 8))[0]

    try:
        info = ctypes.create_string_buffer(48)  # PROCESS_BASIC_INFORMATION
        if _ntdll.NtQueryInformationProcess(handle, 0, info, 48, None) != 0:
            raise OSError(f"can't query process {pid}")
        params = pointer(struct.unpack("<Q", info.raw[8:16])[0] + 0x20)
        length = struct.unpack("<H", memory(params + 0x70, 2))[0]
        command = memory(pointer(params + 0x78), length).decode("utf-16-le", "replace")
        size = pointer(params + 0x3F0)
        env = memory(pointer(params + 0x80), size).decode("utf-16-le", "replace")
    finally:
        _kernel32.CloseHandle(handle)
    return command, dict(x.split("=", 1) for x in env.split("\0") if "=" in x[1:])


def comm(pid):
    """The process's name, None when it is gone. No extension on Windows: "claude", "python"."""
    if WINDOWS:
        return _table().get(pid, (0, None))[1]
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return None


def parent(pid):
    """The parent's pid, 0 when unknown."""
    if WINDOWS:
        return _table().get(pid, (0, None))[0]
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def pids():
    if WINDOWS:
        return list(_table())
    return [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()]


def argv(pid):
    """The process's arguments, its program first. OSError when it can't be read."""
    if WINDOWS:
        return split(_parameters(pid)[0])
    return [a.decode(errors="replace")
            for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]


def environ(pid):
    """The process's environment as a dict. OSError when it can't be read."""
    if WINDOWS:
        return _parameters(pid)[1]
    return dict(x.split("=", 1) for x in
                Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace").split("\0")
                if "=" in x)


def running(pid):
    """Whether a process with this pid exists. Not os.kill(pid, 0) on Windows: signal 0 is
    CTRL_C_EVENT there, and any other number terminates the process."""
    if WINDOWS:
        return pid in _table()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def spawn_detached(command, **kw):
    """Start a process that outlives this one and the claude above it. On Windows it keeps a hidden
    console, so the herdr calls it makes don't each flash a window, and leaves claude's job object
    where it can, or the /exit of a takeover would end it."""
    if not WINDOWS:
        return subprocess.Popen(command, start_new_session=True, **kw)
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                                **kw)
    except PermissionError:  # a job that forbids leaving it
        return subprocess.Popen(command, creationflags=flags, **kw)


# --- files -------------------------------------------------------------------------------------

def replace(source, target):
    """os.replace. Windows refuses while another process has the target open, as a reader may."""
    for _ in range(40 if WINDOWS else 0):
        try:
            return os.replace(source, target)
        except PermissionError:
            time.sleep(0.05)
    return os.replace(source, target)


def lock(file):
    """Hold an exclusive lock on the open file until it is closed; waits for another holder."""
    if not WINDOWS:
        fcntl.flock(file, fcntl.LOCK_EX)
        return
    while True:
        try:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)  # gives up after 10 s: try again
            return
        except OSError:
            continue


def project_slug(path):
    """The folder name of a project's handoffs: /home/me/app -> home-me-app, E:\\app -> E--app."""
    path = path.replace("\\", "/") if WINDOWS else path
    return path.strip("/").replace("/", "-").replace(":", "-")


# --- accounts ----------------------------------------------------------------------------------
# claude-acc N: account 1 is ~/.claude, account N is ~/.claude-N, or ~/.claude-accounts/accN where
# claude-accN.cmd keeps it on Windows.

def account_dir(account):
    home = Path.home()
    if str(account) == "1":
        return home / ".claude"
    profile = home / ".claude-accounts" / f"acc{account}"
    return profile if WINDOWS and profile.is_dir() else home / f".claude-{account}"


def account_of(config_dir):
    """The account a CLAUDE_CONFIG_DIR belongs to, as a string: unset is "1"."""
    if not config_dir:
        return "1"
    path = os.path.normpath(os.path.expanduser(config_dir))
    if os.path.normcase(path) == os.path.normcase(str(Path.home() / ".claude")):
        return "1"
    name = os.path.basename(path)
    if WINDOWS and name.startswith("acc") and name[3:].isdigit():
        return name[3:]
    return name[len(".claude-"):] if name.startswith(".claude-") else path


def account_command(account, args):
    """(env, command) that starts claude on an account. Windows has no claude-acc to go through,
    so nothing adds --dangerously-skip-permissions there: the flags are the caller's."""
    if WINDOWS:
        env = {} if str(account) == "1" else {"CLAUDE_CONFIG_DIR": str(account_dir(account))}
        return env, ["claude", *args]
    acc = shutil.which("claude-acc") or str(Path.home() / ".local" / "bin" / "claude-acc")
    return {}, [acc, str(account), *args]


# --- the shell in a pane -----------------------------------------------------------------------

def split(command):
    """A command line as arguments, by this OS's rules."""
    if not WINDOWS:
        return shlex.split(command)
    count = ctypes.c_int()
    args = _shell32.CommandLineToArgvW(command, ctypes.byref(count))
    if not args:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [args[i] for i in range(count.value)]
    finally:
        _kernel32.LocalFree(args)


def pane_shell(pane):
    """powershell, pwsh, cmd, bash...: the shell herdr started in the pane."""
    import json
    try:
        out = subprocess.run([shutil.which("herdr") or "herdr", "pane", "process-info", "--pane",
                              pane], capture_output=True, encoding="utf-8", errors="replace",
                             timeout=20, check=False)
        pid = json.loads(out.stdout)["result"]["process_info"]["shell_pid"]
        return (comm(pid) or "powershell").lower()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return "powershell"  # herdr's default on Windows


def shell_line(env, command, shell="sh"):
    """The line to type into a shell to run `command` with `env` set for it alone. Windows' shells
    have no `K=v command`, so they set it and unset it after the command.
    shortcut: PowerShell 5.1 drops a `"` inside an argument to a native program; no flag claude is
    relaunched with has one so far."""
    if shell in ("powershell", "pwsh"):
        def quote(text):
            return "'" + text.replace("'", "''") + "'"
        return ("".join(f"$env:{k}={quote(v)}; " for k, v in env.items())
                + "& " + " ".join(quote(a) for a in command)
                + "".join(f"; Remove-Item Env:{k}" for k in env))
    if shell == "cmd":
        return ("".join(f'set "{k}={v}" & ' for k, v in env.items())
                + subprocess.list2cmdline(command)
                + "".join(f' & set "{k}="' for k in env))
    return ("".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
            + " ".join(shlex.quote(a) for a in command))


def pane_line(pane, env, command):
    """shell_line for what runs in this herdr pane. Only Windows has to ask which shell that is."""
    return shell_line(env, command, pane_shell(pane) if WINDOWS else "sh")
