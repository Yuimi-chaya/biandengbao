"""Per-user LaunchAgent registration. No administrator or App lifecycle actions."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path


def task(profile, mode):
    label = "com.biandengbao.gateway." + profile.key
    folder = Path.home() / "Library/LaunchAgents"
    path = folder / (label + ".plist")
    executable = profile.launcher["executable"]
    arguments = ([executable, "--worker", str(profile.options)] if profile.launcher["kind"] == "packaged" else
                 [executable, "-B", str(profile.worker), "--settings", str(profile.options)])
    previous = plistlib.loads(path.read_bytes()) if path.exists() else None
    if previous and (previous.get("Label") != label or previous.get("ProgramArguments") != arguments):
        raise RuntimeError("已有其他登录项使用此名称，未修改")
    domain, target = "gui/" + str(os.getuid()), "gui/%d/%s" % (os.getuid(), label)

    def run(*args, check=True):
        try:
            result = subprocess.run(["/bin/launchctl", *args], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=15, check=False)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("macOS 登录项操作超时；请先查询状态，不要重复执行") from error
        if check and result.returncode:
            raise RuntimeError("macOS 登录项操作失败：" + result.stderr.strip()[:250])
        return result

    loaded = run("print", target, check=False).returncode == 0
    if mode == "enable":
        folder.mkdir(parents=True, exist_ok=True)
        profile.control.mkdir(parents=True, exist_ok=True)
        value = {"Label": label, "ProgramArguments": arguments, "RunAtLoad": True,
                 "WorkingDirectory": str(profile.root), "ProcessType": "Background",
                 "StandardOutPath": str(profile.control / "supervisor.log"),
                 "StandardErrorPath": str(profile.control / "supervisor.err.log")}
        temporary = path.with_suffix(".plist.tmp")
        temporary.write_bytes(plistlib.dumps(value))
        temporary.chmod(0o600)
        temporary.replace(path)
        run("enable", target)
    elif mode == "start":
        if not path.is_file():
            raise RuntimeError("登录项尚未安装")
        if not loaded:
            run("bootstrap", domain, str(path))
        else:
            run("kickstart", target)
    elif mode == "disable" and previous:
        run("disable", target)
    elif mode == "remove" and previous:
        if loaded:
            state = run("print", target).stdout
            if "state = running" in state:
                raise RuntimeError("请等待监听程序退出后移除登录项")
            run("bootout", target)
        path.unlink()
    state = run("print", target, check=False)
    running = state.returncode == 0 and "state = running" in state.stdout
    return {"exists": path.is_file(), "enabled": path.is_file() and not profile.disabled.exists(),
            "state": "Running" if running else ("Ready" if path.is_file() else "NotInstalled"),
            "lastResult": None}
