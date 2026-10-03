"""Opt-in user startup on Windows/macOS. Never launch or stop the App."""
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

if sys.platform == "darwin":
    from . import macos_app as windows_app
else:
    from . import windows_app
from .lifecycle import request_stop
from .network import network_arguments, validate_network
from .service_profile import same_config


def supported():
    return sys.platform in ("win32", "darwin")


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
        identity = os.path.normcase(str(self.config))
        self.key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        candidates = []
        for folder in self.config.parent.glob("autostart-*"):
            suffix = folder.name.removeprefix("autostart-")
            if len(suffix) != 12 or any(char not in "0123456789abcdef" for char in suffix):
                continue
            options = read_json(folder / "options.json")
            if (isinstance(options, dict) and options.get("schema") == 1 and not options.get("retired")
                    and isinstance(options.get("config"), str)
                    and same_config(options["config"], self.config)):
                candidates.append((suffix, options))
        if len(candidates) > 1:
            raise RuntimeError("同一服务配置有多套自启动记录；请先停用旧安装，不会创建第三套")
        self.launcher = None
        if candidates:
            # Preserve an existing task's owner, including older root-based names.
            self.key, options = candidates[0]
            repository = options.get("repository")
            if not isinstance(repository, str) or not Path(repository).is_absolute():
                raise RuntimeError("已有自启动安装归属无效")
            self.root = Path(repository).resolve()
            self.launcher = options.get("launcher")
            if self.launcher is None:
                packaged = (self.root.parent / "Biandengbao.exe" if self.root.name == "_internal"
                            else self.root.parent / "MacOS/Biandengbao")
                if packaged.is_file():
                    self.launcher = {"kind": "packaged", "executable": str(packaged)}
                elif not getattr(sys, "frozen", False) or self.root == Path(root).resolve():
                    self.launcher = {"kind": "packaged" if getattr(sys, "frozen", False) else "source",
                                     "executable": str(Path(sys.executable).resolve())}
                else:
                    raise RuntimeError("旧源码自启动缺少启动程序记录；请从原安装停用或迁移")
        if self.launcher is None:
            self.launcher = {"kind": "packaged" if getattr(sys, "frozen", False) else "source",
                             "executable": str(Path(sys.executable).resolve())}
        if (not isinstance(self.launcher, dict)
                or self.launcher.get("kind") not in ("source", "packaged")
                or not isinstance(self.launcher.get("executable"), str)
                or not Path(self.launcher["executable"]).is_absolute()):
            raise RuntimeError("服务启动程序记录无效")
        self.task_name = "Biandengbao-LAN-" + self.key
        self.control = self.config.parent / ("autostart-" + self.key)
        self.options = self.control / "options.json"
        self.disabled = self.control / "disabled"
        self.worker = self.root / "autostart.py"

    def pythonw(self):
        if self.launcher["kind"] == "packaged":
            return Path(self.launcher["executable"])
        path = Path(self.launcher["executable"]).with_name("pythonw.exe")
        if not path.is_file():
            raise RuntimeError("当前 Python 安装缺少 pythonw.exe；未配置自启动")
        return path

    def task(self, mode):
        if sys.platform == "darwin":
            from .macos_startup import task
            return task(self, mode)
        powershell = (Path(os.environ["SystemRoot"]) / "System32" /
                      "WindowsPowerShell/v1.0/powershell.exe")
        try:
            result = subprocess.run([
                str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-File", str(self.root / "tools/configure-autostart.ps1"),
                "-Mode", mode, "-TaskName", self.task_name,
                "-Pythonw", str(self.pythonw()), "-Worker", str(self.worker),
                "-SettingsPath", str(self.options)] +
                (["-Packaged"] if self.launcher["kind"] == "packaged" else []),
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
                    "message": "登录自启动支持 Windows 和 macOS"}
        state = self.task("status")
        state["supported"] = True
        state["taskName"] = self.task_name
        state["worker"] = read_json(self.control / "status.json")
        return state

    def enable(self, port=None, codex_home=None, caller=None, network=None):
        if not supported():
            raise RuntimeError("登录自启动支持 Windows 和 macOS")
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
            raise RuntimeError("自启动任务仍在运行；请先关闭，待退出后再修改配置")
        value = {"schema": 1, "repository": str(self.root),
                 "config": str(self.config), "port": port, "launcher": self.launcher,
                 "codexHome": str(Path(codex_home or previous.get("codexHome") or
                     os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()),
                 "callerThread": caller.strip()}
        value["network"] = validate_network(network or previous.get("network") or {"mode": "lan"}, require_binary=True)
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
            raise RuntimeError("登录自启动支持 Windows 和 macOS")
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
        options = read_json(self.options)
        if isinstance(options, dict):
            write_json(self.options, {**options, "retired": True})
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
        validate_network(value.get("network", {"mode": "lan"}))
        return value

    def write_status(self, state, **fields):
        started = windows_app.process_started(os.getpid()) if sys.platform in ("win32", "darwin") else None
        write_json(self.control / "status.json", {
            "state": state, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "supervisorPid": os.getpid(), "supervisorStarted": started, **fields})


def profile_from_settings(root, settings):
    settings = Path(settings).resolve()
    value = read_json(settings)
    if (not isinstance(value, dict) or not isinstance(value.get("config"), str) or
            not Path(value["config"]).is_absolute()):
        raise RuntimeError("缺少自启动配置")
    profile = Profile(root, value["config"])
    if profile.root != Path(root).resolve():
        raise RuntimeError("监听程序不属于保存的服务安装；不会更改归属")
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
    packaged = profile.launcher["kind"] == "packaged"
    python = Path(profile.launcher["executable"])
    if not packaged and os.name == "nt":
        python = python.with_name("python.exe")
    if not python.is_file() or (not packaged and not (profile.root / "run.py").is_file()):
        raise RuntimeError("Python 或安装目录缺失；未启动进程")
    environment = os.environ.copy()
    environment["CODEX_APP_TOOLS_PIPE_PATH"] = binding["pipe"]
    environment["CODEX_THREAD_ID"] = options["callerThread"]
    environment["CODEX_HOME"] = options["codexHome"]
    if packaged:
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    arguments = ([str(python), "--gateway"] if packaged else
                 [str(python), "-B", str(profile.root / "run.py")])
    arguments += network_arguments(options.get("network", {"mode": "lan"}))
    if binding.get("ipc"):
        arguments += ["--ipc-path", binding["ipc"]]
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    stdout = profile.control / ("gateway-" + stamp + ".log")
    stderr = profile.control / ("gateway-" + stamp + ".err.log")
    with stdout.open("xb") as output, stderr.open("xb") as errors:
        process = subprocess.Popen(arguments + [
            "--port", str(options["port"]),
            "--config", str(profile.config),
            "--codex-home", options["codexHome"],
            "--codex-bin", binding["runtime"]],
            cwd=str(profile.root), env=environment, stdin=subprocess.DEVNULL,
            stdout=output, stderr=errors,
            creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0,
            start_new_session=os.name != "nt")
    return process, stdout, stderr


@contextmanager
def worker_lock(profile):
    if os.name == "nt":
        import msvcrt
    else:
        import fcntl
    profile.control.mkdir(parents=True, exist_ok=True)
    # Gateway lifecycle and attachment data are shared by the config directory.
    with (profile.config.parent / "autostart.lock").open("a+b") as lock:
        lock.seek(0)
        if lock.read(1) == b"":
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class Supervisor:
    """Own only children started here; a manual stop lasts until the next App session."""
    def __init__(self, profile, options):
        self.profile = profile
        self.options = options
        self.managed = None
        self.handled = None
        self.last_status = None
        self.next_discovery = 0

    def status(self, state, **fields):
        value = (state, fields)
        if value != self.last_status:
            self.profile.write_status(state, **fields)
            self.last_status = value

    def stop_owned(self):
        process, record, binding = self.managed
        if process.poll() is None:
            request_stop(self.profile.config.parent, expected_record=record)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("网关尚未退出；不强杀、不启动第二个服务") from error
        self.managed = None
        self.next_discovery = 0
        self.status("waiting_for_app")

    def start(self, binding):
        process, stdout, stderr = launch(self.profile, self.options, binding)
        for _ in range(50):
            if process.poll() is not None:
                break
            record = read_json(self.profile.config.parent / "gateway-control.json")
            if (isinstance(record, dict) and record.get("pid") == process.pid and
                    record.get("token") and port_busy(self.options["port"]) and
                    windows_app.binding_alive(binding)):
                self.managed = (process, record, binding)
                self.handled = windows_app.session_key(binding)
                self.status("started", gatewayPid=process.pid,
                    appPid=binding["appPid"], port=self.options["port"],
                    output=str(stdout), errors=str(stderr))
                return
            time.sleep(.2)
        self.status("startup_failed", output=str(stdout), errors=str(stderr))
        raise RuntimeError("启动未确认；请检查日志，不自动重放部分启动")

    def tick(self):
        if self.managed:
            process, record, binding = self.managed
            if process.poll() is not None:
                self.managed = None
                self.status("stopped_until_app_restart")
            elif read_json(self.profile.config.parent / "gateway-control.json") != record:
                # Normal shutdown removes its record just before the child exits.
                if read_json(self.profile.config.parent / "gateway-control.json") is not None:
                    raise RuntimeError("网关记录已变更；不停止未知服务")
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError("网关记录丢失但进程仍在运行；不接管、不重启") from error
                self.managed = None
                self.status("stopped_until_app_restart")
            elif not windows_app.session_alive(binding):
                self.stop_owned()
            elif windows_app.binding_alive(binding):
                self.status("monitoring", gatewayPid=process.pid,
                    appPid=binding["appPid"], port=self.options["port"])
                return 3
            elif time.monotonic() < self.next_discovery:
                return 3
            else:
                self.next_discovery = time.monotonic() + 15
                replacement = windows_app.discover()
                if replacement and (windows_app.session_key(replacement) != self.handled or
                                    replacement["pipe"] != binding["pipe"]):
                    self.stop_owned()
                    self.handled = None
                else:
                    self.status("waiting_for_channel", gatewayPid=process.pid)
                    return 3
        existing = active_instance(self.profile)
        if existing or port_busy(self.options["port"]):
            # Do not take over an unrelated/manual gateway based on a port or PID.
            self.status("existing_instance" if existing else "existing_listener",
                        **({"gatewayPid": existing} if existing else
                           {"port": self.options["port"]}))
            return 15
        binding = windows_app.discover()
        if self.profile.disabled.exists():
            return 0
        if not binding:
            self.status("waiting_for_app")
            return 15
        if windows_app.session_key(binding) == self.handled:
            self.status("stopped_until_app_restart")
            return 15
        self.start(binding)
        return 3


def run_worker(profile, once=False):
    if not supported():
        raise RuntimeError("登录自启动支持 Windows 和 macOS")
    options = profile.load_options()
    if sys.platform == "darwin":
        os.environ["CODEX_HOME"] = options["codexHome"]
    with worker_lock(profile) as acquired:
        if not acquired:
            profile.write_status("another_worker")
            return
        supervisor = Supervisor(profile, options)
        while not profile.disabled.exists():
            delay = supervisor.tick()
            if once and not profile.disabled.exists():
                return
            for _ in range(delay):
                if profile.disabled.exists():
                    break
                time.sleep(1)
        profile.write_status("disabled")
