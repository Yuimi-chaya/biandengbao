"""Read-only macOS App discovery; bind only uniquely owned UNIX channels."""
import os
import plistlib
import subprocess
import time
from pathlib import Path

from .desktop_tools import DesktopTools
from .ipc import IPCError


def command(args, timeout=3):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=timeout, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def process_image(pid):
    return command(["/bin/ps", "-p", str(int(pid)), "-o", "comm="]) or None


def process_started(pid):
    return command(["/bin/ps", "-p", str(int(pid)), "-o", "lstart="]) or None


def valid_app_image(image):
    if not image:
        return False
    path = Path(image)
    if path.name not in ("Codex", "ChatGPT") or path.parent.name != "MacOS":
        return False
    try:
        info = plistlib.loads((path.parent.parent / "Info.plist").read_bytes())
        return str(info.get("CFBundleIdentifier", "")).lower().startswith("com.openai.")
    except (OSError, ValueError):
        return False


def processes():
    result = []
    for line in command(["/bin/ps", "-axo", "pid=,ppid=,comm="]).splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            result.append((int(parts[0]), int(parts[1]), parts[2]))
    return result


def app_identity():
    matches = [(pid, path) for pid, _, path in processes() if valid_app_image(path)]
    return matches[0] if len(matches) == 1 else (None, None)


def owned_sockets(pid):
    output = command(["/usr/sbin/lsof", "-n", "-a", "-p", str(pid), "-U", "-Fn"])
    paths = set()
    for line in output.splitlines():
        if line.startswith("n/"):
            value = line[1:].split(" type=", 1)[0]
            path = Path(value)
            try:
                if path.is_socket() and path.stat().st_uid == os.getuid():
                    paths.add(value)
            except OSError:
                pass
    return paths


def session_key(binding):
    return tuple(binding[key] for key in ("appPid", "appStarted", "runtimePid", "runtimeStarted"))


def session_alive(binding):
    return (process_started(binding["appPid"]) == binding["appStarted"] and
            process_started(binding["runtimePid"]) == binding["runtimeStarted"] and
            valid_app_image(process_image(binding["appPid"])) and
            process_image(binding["runtimePid"]) == binding["runtime"])


def discover():
    rows = processes()
    apps = [(pid, image) for pid, _, image in rows if valid_app_image(image)]
    if len(apps) != 1:
        return None
    pid, _ = apps[0]
    runtimes = [(child, path) for child, parent, path in rows
                if parent == pid and Path(path).name == "codex"]
    if len(runtimes) != 1:
        return None
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    ipc = str(home / "ipc/ipc.sock")
    sockets = owned_sockets(pid)
    if ipc not in sockets or len(sockets) > 64:
        return None
    found, deadline = [], time.monotonic() + 8
    for path in sorted(sockets - {ipc}):
        if time.monotonic() >= deadline:
            return None
        try:
            catalog = DesktopTools(pipe=path)._request("tools/list", {"threadStartKind": "all"}, timeout=1)
            names = {item["name"] for item in catalog["tools"]}
            if {"list_projects", "create_thread", "navigate_to_codex_page"}.issubset(names):
                found.append(path)
        except (OSError, ValueError, KeyError, TypeError, IPCError):
            continue
    if len(found) != 1:
        return None
    runtime_pid, runtime = runtimes[0]
    binding = {"appPid": pid, "appStarted": process_started(pid),
               "runtimePid": runtime_pid, "runtimeStarted": process_started(runtime_pid),
               "runtime": runtime, "pipe": found[0], "ipc": ipc}
    return binding if all(binding.values()) and session_alive(binding) else None


def binding_alive(binding):
    return session_alive(binding) and {binding["pipe"], binding["ipc"]}.issubset(owned_sockets(binding["appPid"]))
