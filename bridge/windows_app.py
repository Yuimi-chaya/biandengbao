"""Read-only discovery of the running Store App and its tools channel."""
import ctypes
import os
import time
from ctypes import wintypes
from pathlib import PureWindowsPath

from .desktop_tools import DesktopTools
from .ipc import IPCError

PIPE_PREFIX = "\\\\.\\pipe\\"
REQUIRED_TOOLS = {"list_projects", "create_thread", "navigate_to_codex_page"}


def winapi():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetNamedPipeServerProcessId.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel


def pipe_owner(path):
    kernel = winapi()
    handle = kernel.CreateFileW(path, 0xC0000000, 0, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        return None
    try:
        pid = wintypes.ULONG()
        return pid.value if kernel.GetNamedPipeServerProcessId(
            handle, ctypes.byref(pid)) else None
    finally:
        kernel.CloseHandle(handle)


def process_image(pid):
    kernel = winapi()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        return buffer.value if kernel.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)) else None
    finally:
        kernel.CloseHandle(handle)


def process_started(pid):
    kernel = winapi()
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        values = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            return None
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def session_key(binding):
    return (binding["appPid"], binding["appStarted"], binding["runtimePid"],
            binding["runtimeStarted"])


def session_alive(binding):
    return (process_started(binding["appPid"]) == binding["appStarted"] and
            process_started(binding["runtimePid"]) == binding["runtimeStarted"] and
            valid_app_image(process_image(binding["appPid"])) and
            process_image(binding["runtimePid"]) == binding["runtime"])


def children(parent):
    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260)]

    kernel = winapi()
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == wintypes.HANDLE(-1).value:
        return []
    found = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        valid = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while valid:
            if entry.th32ParentProcessID == parent:
                found.append((entry.th32ProcessID, entry.szExeFile))
            valid = kernel.Process32NextW(handle, ctypes.byref(entry))
        return found
    finally:
        kernel.CloseHandle(handle)


def valid_app_image(image):
    if not image or not os.environ.get("PROGRAMFILES"):
        return False
    path = PureWindowsPath(image)
    root = PureWindowsPath(os.environ["PROGRAMFILES"]) / "WindowsApps"
    return (path.name.lower() == "chatgpt.exe" and root in path.parents and
            path.parent.name.lower() == "app" and
            path.parent.parent.name.startswith("OpenAI.Codex_"))


def valid_runtime_image(image):
    if not image or not os.environ.get("LOCALAPPDATA"):
        return False
    path = PureWindowsPath(image)
    root = PureWindowsPath(os.environ["LOCALAPPDATA"]) / "OpenAI/Codex/bin"
    return path.name.lower() == "codex.exe" and root in path.parents


def select_unique_tools(app_pid, names, get_owner, probe):
    names = [name for name in names if name.startswith("codex-browser-use-")]
    if len(names) > 64:
        return None
    deadline = time.monotonic() + 8
    found = []
    for name in names:
        if time.monotonic() >= deadline:
            return None
        path = PIPE_PREFIX + name
        if get_owner(path) != app_pid:
            continue
        try:
            if REQUIRED_TOOLS.issubset(probe(path)):
                found.append(path)
                if len(found) > 1:
                    return None
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, IPCError):
            continue
    return found[0] if len(found) == 1 else None


def discover():
    app_pid = pipe_owner(PIPE_PREFIX + "codex-ipc")
    if not app_pid or not valid_app_image(process_image(app_pid)):
        return None
    app_started = process_started(app_pid)
    runtimes = [(pid, process_image(pid)) for pid, name in children(app_pid)
                if name.lower() == "codex.exe"]
    runtimes = [(pid, image) for pid, image in runtimes if valid_runtime_image(image)]
    if len(runtimes) != 1:
        return None
    runtime_pid, runtime = runtimes[0]
    runtime_started = process_started(runtime_pid)
    if app_started is None or runtime_started is None:
        return None

    def probe(path):
        value = DesktopTools(pipe=path)._request(
            "tools/list", {"threadStartKind": "all"}, timeout=1)
        if not isinstance(value, dict) or not isinstance(value.get("tools"), list):
            raise ValueError("Invalid tools catalog")
        return {tool["name"] for tool in value["tools"] if isinstance(tool, dict)}

    pipe = select_unique_tools(app_pid, sorted(os.listdir(PIPE_PREFIX)),
                               pipe_owner, probe)
    if not pipe or pipe_owner(pipe) != app_pid:
        return None
    binding = {"appPid": app_pid, "appStarted": app_started,
               "runtimePid": runtime_pid, "runtimeStarted": runtime_started,
               "runtime": runtime, "pipe": pipe}
    return binding if session_alive(binding) else None


def binding_alive(binding):
    pid = binding["appPid"]
    return (pipe_owner(PIPE_PREFIX + "codex-ipc") == pid and
            pipe_owner(binding["pipe"]) == pid and
            session_alive(binding))
