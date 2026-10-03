"""One management API for desktop UI and agent-friendly CLI clients."""
import json
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import autostart
from .gateway_admin import account_record, save_account
from .lifecycle import read_record, request_stop
from .local_control import rpc
from .network import validate_network
from .store import SessionStore
from .version import VERSION, REVISION, REPOSITORY
from .service_profile import default_config


def runner(root, mode, *args):
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        gui = executable.with_name("Biandengbao.exe")
        if sys.platform == "win32" and gui.is_file():
            executable = gui
        return [str(executable), mode, *map(str, args)]
    return [sys.executable, "-B", str(Path(root) / "manager.py"), mode, *map(str, args)]


def spawn(command, root, log):
    environment = os.environ.copy()
    if getattr(sys, "frozen", False):
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        return subprocess.Popen(command, cwd=str(root), env=environment,
            stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
            creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0,
            start_new_session=os.name != "nt")


def source_info(root):
    info = {"version": VERSION, "revision": REVISION, "dirty": False}
    path = Path(root) / "build-info.json"
    if path.is_file():
        value = json.loads(path.read_text(encoding="utf-8"))
        info.update({key: value[key] for key in ("version", "revision", "dirty", "builtAt", "platform") if key in value})
    elif (Path(root) / ".git").exists():
        flags = {"capture_output": True, "text": True, "timeout": 3,
                 "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        try:
            result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], **flags)
            if result.returncode == 0:
                info["revision"] = result.stdout.strip()
            result = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], **flags)
            info["dirty"] = bool(result.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    return info


def app_status():
    native = autostart.windows_app
    if sys.platform == "win32":
        pid = native.pipe_owner(native.PIPE_PREFIX + "codex-ipc")
        image = native.process_image(pid) if pid else None
        running = bool(pid and native.valid_app_image(image))
        version = None
        if image:
            match = re.search(r"OpenAI[.]Codex_([0-9.]+)_", image)
            version = match[1] if match else None
        return {"running": running, "pid": pid if running else None, "version": version}
    if sys.platform == "darwin":
        pid, image = native.app_identity()
        version = None
        if image:
            try:
                info = plistlib.loads((Path(image).parent.parent / "Info.plist").read_bytes())
                version = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
            except (OSError, ValueError):
                pass
        return {"running": bool(pid), "pid": pid, "version": version}
    return {"running": False, "pid": None, "version": None, "supported": False}


def github_json(path):
    request = Request("https://api.github.com/repos/" + REPOSITORY + path, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "Biandengbao-Manager/" + VERSION})
    try:
        with urlopen(request, timeout=8) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise RuntimeError("更新检查响应过大")
        return json.loads(raw)
    except HTTPError as error:
        raise RuntimeError("GitHub 更新检查失败（HTTP %s）；未修改本机文件" % error.code) from error
    except (OSError, ValueError) as error:
        raise RuntimeError("暂时无法连接 GitHub；未修改本机文件") from error


class Manager:
    def __init__(self, root, config):
        self.root = Path(root).resolve()
        self.profile = autostart.Profile(self.root, config)
        self.config = self.profile.config
        self.lock = threading.Lock()
        self.build = source_info(self.root)
        self.child = None
        self.cache = {}
        self.last_update = None
        self.closed = threading.Event()

    def cached(self, key, seconds, read):
        entry = self.cache.get(key)
        if entry and time.monotonic() - entry[0] < seconds:
            return entry[1]
        result = read()
        self.cache[key] = (time.monotonic(), result)
        return result

    def options(self):
        value = autostart.read_json(self.profile.options) or {}
        return {"port": value.get("port", 8787),
                "codexHome": value.get("codexHome", os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
                "callerThread": value.get("callerThread", os.environ.get("CODEX_THREAD_ID", os.environ.get("CODEX_SESSION_ID", ""))),
                "network": value.get("network", {"mode": "lan"})}

    def appearance(self):
        try:
            value = autostart.read_json(self.config.parent / ".manager/preferences.json") or {}
            mode = value.get("appearance")
            return mode if mode in ("system", "light", "dark") else "system"
        except (OSError, ValueError, AttributeError):
            return "system"

    def worker_alive(self):
        if self.child and self.child.poll() is None:
            return True
        status = autostart.read_json(self.profile.control / "status.json") or {}
        pid, started = status.get("supervisorPid"), status.get("supervisorStarted")
        return bool(pid and started and autostart.windows_app.process_started(pid) == started)

    def gateway_record(self):
        control = read_record(self.config.parent / "gateway-control.json")
        admin = read_record(self.config.parent / "gateway-admin.json")
        if (control and admin and admin.get("pid") == control.get("pid") and
                admin.get("token") == control.get("token")):
            return admin
        return None

    def gateway(self, action, body=None):
        record = self.gateway_record()
        if not record:
            raise RuntimeError("网关未启动或为旧版本；请使用新版启动后再管理登录设备")
        return rpc(record, action, body, timeout=4 if action == "status" else 15)

    def status(self):
        config = autostart.read_json(self.config) or {}
        gateway = {"running": False, "devices": [], "addresses": []}
        record = self.gateway_record()
        if record:
            try:
                gateway = rpc(record, "status", timeout=1)
            except (OSError, RuntimeError, ValueError):
                gateway["message"] = "网关管理接口暂不可达"
        elif read_record(self.config.parent / "gateway-control.json"):
            gateway["message"] = "此配置有旧版网关记录；不会自动接管或强制停止"
        worker = autostart.read_json(self.profile.control / "status.json") or {}
        worker["running"] = self.worker_alive()
        try:
            startup = self.cached("startup", 20, self.profile.status)
        except (RuntimeError, OSError, ValueError) as error:
            startup = {"enabled": None, "message": str(error)}
        return {"manager": self.build, "platform": sys.platform,
                "configPath": str(self.config), "logDirectory": str(self.profile.control),
                "configured": config.get("auth", {}).get("mode") == "password",
                "username": config.get("auth", {}).get("username", "admin"),
                "app": self.cached("app", 5, app_status), "gateway": gateway,
                "worker": worker, "autostart": startup, "settings": self.options(),
                "update": self.last_update, "appearance": self.appearance()}

    def validate_options(self, body):
        if set(body) - {"port", "codexHome", "callerThread", "network", "confirmRestart"}:
            raise ValueError("包含不支持的设置字段")
        value = self.options()
        value.update({k: v for k, v in body.items() if k != "confirmRestart"})
        if type(value["port"]) is not int or not 1 <= value["port"] <= 65535:
            raise ValueError("端口必须为 1–65535")
        if not isinstance(value["codexHome"], str) or not Path(value["codexHome"]).is_absolute():
            raise ValueError("Codex 数据目录必须为绝对路径")
        if not isinstance(value["callerThread"], str) or len(value["callerThread"]) > 100:
            raise ValueError("调用上下文格式不正确")
        value["network"] = validate_network(value["network"], require_binary=True)
        return {"schema": 1, "repository": str(self.profile.root), "config": str(self.config),
                "launcher": self.profile.launcher, **value}

    def pause_worker(self):
        self.profile.control.mkdir(parents=True, exist_ok=True)
        self.profile.disabled.touch()
        deadline = time.monotonic() + 12
        while self.worker_alive():
            if time.monotonic() > deadline:
                raise RuntimeError("监听程序尚未退出；未强制停止，请稍后检查状态")
            time.sleep(.2)
        self.child = None

    def start(self):
        if not autostart.supported():
            raise RuntimeError("桌面管理端目前支持 Windows 和 macOS")
        config = autostart.read_json(self.config) or {}
        if config.get("auth", {}).get("mode") != "password":
            raise RuntimeError("请先设置登录账号和至少 12 位密码")
        if autostart.active_instance(self.profile):
            return {"state": "already_running"}
        options = self.validate_options({})
        if not options["callerThread"].strip():
            raise RuntimeError("请先在连接设置中选择一个现有 Codex 聊天作为调用上下文")
        SessionStore(options["codexHome"]).get(options["callerThread"])
        if self.worker_alive():
            self.pause_worker()
        self.profile.control.mkdir(parents=True, exist_ok=True)
        autostart.write_json(self.profile.options, options)
        self.profile.disabled.unlink(missing_ok=True)
        command = [self.profile.launcher["executable"]]
        if self.profile.launcher["kind"] == "source":
            command += ["-B", str(self.profile.root / "autostart.py"), "--settings", str(self.profile.options)]
        else:
            command += ["--worker", str(self.profile.options)]
        self.child = spawn(command, self.profile.root, self.profile.control / "manager-worker.log")
        return {"state": "waiting_for_app", "supervisorPid": self.child.pid}

    def stop(self):
        record = read_record(self.config.parent / "gateway-control.json")
        if record:
            request_stop(self.config.parent, expected_record=record)
        elif self.worker_alive():
            self.pause_worker()
            self.profile.disabled.unlink(missing_ok=True)
        return {"state": "stopped", "note": "已停止网关；监听中的自启动仍会在下次打开 App 时运行"}

    def configure(self, body):
        value = self.validate_options(body)
        running = self.worker_alive() or bool(autostart.active_instance(self.profile))
        if running:
            if not value["callerThread"].strip():
                raise ValueError("请先选择调用上下文，再重启网关")
            SessionStore(value["codexHome"]).get(value["callerThread"])
        if running and body.get("confirmRestart") is not True:
            raise ValueError("更改运行配置需要确认重启网关；不会重启 Codex App")
        if running:
            self.pause_worker()
            self.stop()
        autostart.write_json(self.profile.options, value)
        if running:
            self.start()
        self.cache.pop("startup", None)
        return {"saved": True, "restarted": running}

    def check_update(self):
        if self.last_update and time.time() - self.last_update["checkedAt"] < 60:
            return self.last_update
        remote = github_json("/commits/main")
        sha = remote.get("sha", "")
        if not re.fullmatch("[a-f0-9]{40}", sha):
            raise RuntimeError("GitHub 返回了无效版本标识")
        local = self.build["revision"]
        relation = "unknown"
        if local == sha:
            relation = "identical"
        elif re.fullmatch("[a-f0-9]{40}", local):
            comparison = github_json("/compare/" + local + "..." + sha)
            relation = comparison.get("status", "unknown")
        self.last_update = {"checkedAt": time.time(), "remoteSha": sha,
            "relation": relation, "updateAvailable": relation == "ahead",
            "message": (str(remote.get("commit", {}).get("message", "")).splitlines() or [""])[0][:180],
            "url": "https://github.com/" + REPOSITORY + "/commits/main"}
        return self.last_update

    def dispatch(self, action, body):
        if action == "status":
            return self.status()
        if action == "contexts":
            rows = SessionStore(self.options()["codexHome"]).list(limit=40, query=str(body.get("query", ""))[:200])
            return [{"id": row["id"], "title": row.get("title") or row.get("name") or "未命名聊天"} for row in rows]
        if action == "devices":
            return self.gateway("devices")
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("另一项管理操作正在进行，请稍后再试")
        try:
            if action == "appearance":
                mode = body.get("mode")
                if mode not in ("system", "light", "dark") or set(body) != {"mode"}:
                    raise ValueError("外观仅支持 system、light 或 dark")
                autostart.write_json(self.config.parent / ".manager/preferences.json", {"appearance": mode})
                return {"appearance": mode}
            if action == "service/start":
                return self.start()
            if action == "service/stop":
                if body.get("confirm") is not True:
                    raise ValueError("请确认停止网关")
                return self.stop()
            if action == "settings":
                return self.configure(body)
            if action == "account":
                if body.get("confirm") is not True:
                    raise ValueError("请确认保存账号并使现有设备退出登录")
                if self.gateway_record():
                    return self.gateway("account", body)
                if autostart.active_instance(self.profile):
                    raise RuntimeError("旧版网关仍在运行，请先正常停止它再更改账号")
                auth = account_record(body)
                save_account(self.config, auth)
                return {"username": auth["username"], "sessionsRevoked": True}
            if action in ("devices/revoke", "devices/revoke-all"):
                return self.gateway(action, body)
            if action == "autostart":
                if type(body.get("enabled")) is not bool:
                    raise ValueError("enabled 必须为布尔值")
                if body["enabled"]:
                    options = self.validate_options({})
                    if not options["callerThread"] or (autostart.read_json(self.config) or {}).get("auth", {}).get("mode") != "password":
                        raise ValueError("请先设置账号和调用上下文")
                    SessionStore(options["codexHome"]).get(options["callerThread"])
                    self.profile.task("status")
                    autostart.write_json(self.profile.options, options)
                    self.profile.task("enable")
                    self.profile.disabled.unlink(missing_ok=True)
                    if not self.worker_alive():
                        self.profile.task("start")
                else:
                    self.profile.disable()
                self.cache.pop("startup", None)
                return self.profile.status()
            if action == "autostart/remove":
                if body.get("confirm") is not True:
                    raise ValueError("请确认移除登录自启动")
                self.profile.remove()
                self.cache.pop("startup", None)
                return self.profile.status()
            if action == "updates/check":
                return self.check_update()
            raise ValueError("未知的管理操作")
        finally:
            self.lock.release()
