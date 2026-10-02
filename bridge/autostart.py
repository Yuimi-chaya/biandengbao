"""Opt-in, current-user Windows logon startup. Never launch or stop the App."""
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import windows_app


def supported():
    return sys.platform == "win32"


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Profile:
    def __init__(self, root, config):
        self.root = Path(root).resolve()
        self.config = Path(config).resolve()
        identity = os.path.normcase(str(self.root) + "\0" + str(self.config))
        self.key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        self.task_name = "Biandengbao-LAN-" + self.key
        self.control = self.config.parent / ("autostart-" + self.key)
        self.options = self.control / "options.json"
        self.disabled = self.control / "disabled"
        self.worker = self.root / "autostart.py"

    def pythonw(self):
        path = Path(sys.executable).with_name("pythonw.exe")
        if not path.is_file():
            raise RuntimeError("当前 Python 安装缺少 pythonw.exe；未配置自启动")
        return path

    def task(self, mode):
        powershell = (Path(os.environ["SystemRoot"]) / "System32" /
                      "WindowsPowerShell/v1.0/powershell.exe")
        try:
            result = subprocess.run([
                str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-File", str(self.root / "tools/configure-autostart.ps1"),
                "-Mode", mode, "-TaskName", self.task_name,
                "-Pythonw", str(self.pythonw()), "-Worker", str(self.worker),
                "-SettingsPath", str(self.options)],
                stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                errors="replace", timeout=30, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("计划任务操作超时；请先查询状态，不要直接重复启用") from error
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Windows 计划任务操作失败")
        return json.loads(result.stdout)

    def status(self):
        if not supported():
            return {"supported": False, "enabled": False, "exists": False,
                    "message": "登录自启动目前仅支持 Windows"}
        state = self.task("status")
        state["supported"] = True
        state["taskName"] = self.task_name
        state["worker"] = read_json(self.control / "status.json")
        return state

    def enable(self, port=None, codex_home=None, caller=None):
        if not supported():
            raise RuntimeError("登录自启动目前仅支持 Windows")
        previous = read_json(self.options) or {}
        port = port if port is not None else previous.get("port", 8787)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("端口必须为 1-65535")
        config = read_json(self.config)
        if not config or config.get("auth", {}).get("mode") != "password":
            raise RuntimeError("请先运行 run.py --lan 完成密码配置，再启用自启动")
        caller = caller or os.environ.get("CODEX_THREAD_ID") or \
            os.environ.get("CODEX_SESSION_ID") or previous.get("callerThread")
        if not isinstance(caller, str) or not caller.strip():
            raise RuntimeError("首次启用请从 Codex App 当前聊天执行，需要调用上下文")
        state = self.task("status")
        if state.get("state") == "Running":
            raise RuntimeError("自启动任务仍在等待；请先关闭，待退出后再修改配置")
        value = {"schema": 1, "repository": str(self.root),
                 "config": str(self.config), "port": port,
                 "codexHome": str(Path(codex_home or previous.get("codexHome") or
                     os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()),
                 "callerThread": caller.strip()}
        self.control.mkdir(parents=True, exist_ok=True)
        self.disabled.touch()
        try:
            write_json(self.options, value)
            self.task("enable")
            self.disabled.unlink(missing_ok=True)
            self.task("start")
        except Exception:
            self.disabled.touch()
            raise
        return self.status()

    def disable(self):
        if not supported():
            raise RuntimeError("登录自启动目前仅支持 Windows")
        self.task("status")  # Refuse a foreign task before writing any marker.
        self.control.mkdir(parents=True, exist_ok=True)
        self.disabled.touch()
        self.task("disable")
        return self.status()

    def remove(self):
        self.disable()
        deadline = time.monotonic() + 10
        while self.task("status").get("state") == "Running":
            if time.monotonic() >= deadline:
                raise RuntimeError("等待程序尚未退出；自启动已关闭，稍后再移除")
            time.sleep(.5)
        self.task("remove")
        # Keep options/status for diagnosis, but leave the disable marker.
        return self.status()

    def load_options(self):
        value = read_json(self.options)
        if (not isinstance(value, dict) or value.get("schema") != 1 or
                value.get("repository") != str(self.root) or
                value.get("config") != str(self.config) or
                type(value.get("port")) is not int or
                not 1 <= value["port"] <= 65535 or
                not isinstance(value.get("callerThread"), str) or
                not value["callerThread"].strip() or
                not isinstance(value.get("codexHome"), str) or
                not Path(value["codexHome"]).is_absolute()):
            raise RuntimeError("自启动配置无效；请在当前安装目录重新启用")
        return value

    def write_status(self, state, **fields):
        write_json(self.control / "status.json", {
            "state": state, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "supervisorPid": os.getpid(), **fields})


def profile_from_settings(root, settings):
    settings = Path(settings).resolve()
    value = read_json(settings)
    if (not isinstance(value, dict) or not isinstance(value.get("config"), str) or
            not Path(value["config"]).is_absolute()):
        raise RuntimeError("缺少自启动配置")
    profile = Profile(root, value["config"])
    if settings != profile.options:
        raise RuntimeError("自启动配置文件不属于当前安装")
    profile.load_options()
    return profile


def port_busy(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def active_instance(profile):
    record = read_json(profile.config.parent / "gateway-control.json")
    if (isinstance(record, dict) and type(record.get("pid")) is int and
            record["pid"] > 0 and record.get("token") and
            windows_app.process_image(record["pid"])):
        return record["pid"]
    return None


def launch(profile, options, binding):
    config = read_json(profile.config)
    if not config or config.get("auth", {}).get("mode") != "password":
        raise RuntimeError("自启动要求已有密码配置；未启动进程")
    if profile.disabled.exists() or active_instance(profile):
        raise RuntimeError("自启动已关闭或同配置目录已有服务；未启动进程")
    if not windows_app.binding_alive(binding):
        raise RuntimeError("App 连接已变化；未启动进程")
    python = Path(sys.executable).with_name("python.exe")
    if not python.is_file() or not (profile.root / "run.py").is_file():
        raise RuntimeError("Python 或安装目录缺失；未启动进程")
    environment = os.environ.copy()
    environment["CODEX_APP_TOOLS_PIPE_PATH"] = binding["pipe"]
    environment["CODEX_THREAD_ID"] = options["callerThread"]
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    stdout = profile.control / ("gateway-" + stamp + ".log")
    stderr = profile.control / ("gateway-" + stamp + ".err.log")
    with stdout.open("xb") as output, stderr.open("xb") as errors:
        process = subprocess.Popen([
            str(python), "-B", str(profile.root / "run.py"),
            "--lan", "--port", str(options["port"]),
            "--config", str(profile.config),
            "--codex-home", options["codexHome"],
            "--codex-bin", binding["runtime"]],
            cwd=str(profile.root), env=environment, stdin=subprocess.DEVNULL,
            stdout=output, stderr=errors,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
    return process, stdout, stderr


@contextmanager
def worker_lock(profile):
    import msvcrt
    profile.control.mkdir(parents=True, exist_ok=True)
    # Gateway lifecycle and attachment data are shared by the config directory.
    with (profile.config.parent / "autostart.lock").open("a+b") as lock:
        lock.seek(0)
        if lock.read(1) == b"":
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def run_worker(profile):
    if not supported():
        raise RuntimeError("登录自启动目前仅支持 Windows")
    options = profile.load_options()
    with worker_lock(profile) as acquired:
        if not acquired:
            profile.write_status("another_worker")
            return
        while not profile.disabled.exists():
            existing = active_instance(profile)
            if existing:
                profile.write_status("existing_instance", gatewayPid=existing)
                return
            if port_busy(options["port"]):
                profile.write_status("existing_listener", port=options["port"])
                return
            binding = windows_app.discover()
            if profile.disabled.exists():
                break
            if binding:
                process, stdout, stderr = launch(profile, options, binding)
                for _ in range(50):
                    if process.poll() is not None:
                        break
                    # The lifecycle record must belong to our child, not a race winner.
                    record = read_json(profile.config.parent / "gateway-control.json")
                    if (record and record.get("pid") == process.pid and
                            port_busy(options["port"])):
                        profile.write_status("started", gatewayPid=process.pid,
                            port=options["port"], output=str(stdout), errors=str(stderr))
                        return
                    time.sleep(.2)
                profile.write_status("startup_failed", output=str(stdout),
                                     errors=str(stderr))
                return  # Never kill or replay a partially started process.
            profile.write_status("waiting_for_app")
            for _ in range(15):
                if profile.disabled.exists():
                    break
                time.sleep(1)
        profile.write_status("disabled")
