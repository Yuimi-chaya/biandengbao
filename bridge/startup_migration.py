"""Explicit Windows legacy-task handover to an already running desktop manager."""
import ctypes
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from . import windows_app
from .autostart import read_json, write_json, worker_lock
from .lifecycle import read_record, request_stop
from .local_control import rpc


def same_path(one, two):
    return os.path.normcase(str(Path(one).resolve())) == os.path.normcase(str(Path(two).resolve()))


def command_arguments(command):
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    count = ctypes.c_int()
    arguments = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not arguments:
        raise RuntimeError("Cannot read process arguments.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return [arguments[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(arguments)


class WindowsOperations:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Legacy task migration currently supports Windows only.")
        self.script = Path(__file__).resolve().parents[1] / "tools/migration-task.ps1"

    def powershell(self, command=None, arguments=()):
        executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        invocation = [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive"]
        invocation += (["-Command", command] if command else
                       ["-ExecutionPolicy", "Bypass", "-File", str(self.script), *arguments])
        result = subprocess.run(invocation, stdin=subprocess.DEVNULL, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            # Do not emit raw PowerShell output, which can include private paths/data.
            raise RuntimeError("Windows ownership/task query failed; no forced shutdown.")
        return json.loads(result.stdout)

    def task(self, mode, name, worker):
        return self.powershell(arguments=["-Mode", mode, "-TaskName", name,
                                         "-Worker", str(worker)])

    def process(self, pid):
        if type(pid) is not int or pid <= 0:
            return None
        value = self.powershell(
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
            "$p=Get-CimInstance Win32_Process -Filter 'ProcessId=" + str(pid) + "'; "
            "if($p){@{image=$p.ExecutablePath;command=$p.CommandLine}|ConvertTo-Json -Compress}"
            "else{'null'}")
        if not value:
            return None
        started = windows_app.process_started(pid)
        if not started or not value.get("image") or not value.get("command"):
            raise RuntimeError("Process identity is unavailable; refusing migration.")
        return {"pid": pid, "started": started, "image": value["image"],
                "arguments": command_arguments(value["command"])}

    def alive(self, process):
        return bool(process and windows_app.process_started(process["pid"]) == process["started"]
                    and same_path(windows_app.process_image(process["pid"]) or "", process["image"]))

    def listeners(self, port):
        return self.powershell(
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
            "$ErrorActionPreference='Stop'; "
            "$p=@(Get-NetTCPConnection -State Listen -ErrorAction Stop | "
            "Where-Object {$_.LocalPort -eq " + str(port) + "} | "
            "Select-Object -ExpandProperty OwningProcess -Unique); "
            "ConvertTo-Json -InputObject $p -Compress")

    def manager(self, config, action, body=None):
        record = read_record(config.parent / ".manager/control.json")
        if not record:
            raise RuntimeError("Open the target desktop manager first.")
        identity = rpc(record, "identity", timeout=3)
        if not same_path(identity.get("config", ""), config):
            raise RuntimeError("Target manager config identity does not match.")
        return rpc(record, action, body, timeout=60)

    def stop_gateway(self, config, record):
        request_stop(config.parent, expected_record=record)


class StartupMigration:
    def __init__(self, legacy_config, legacy_control, legacy_worker, target_config,
                 task="Biandengbao-LAN", operations=None, timeout=45):
        self.legacy_config = Path(legacy_config).resolve()
        self.legacy_control = Path(legacy_control).resolve()
        self.legacy_worker = Path(legacy_worker).resolve()
        self.target_config = Path(target_config).resolve()
        self.task_name = task
        self.ops = operations or WindowsOperations()
        self.timeout = timeout

    def wait(self, predicate, description):
        deadline = time.monotonic() + self.timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise RuntimeError(description + "; no process was killed. Inspect before retrying.")
            time.sleep(.25)

    def plan(self):
        if same_path(self.legacy_config.parent, self.target_config.parent):
            raise ValueError("Legacy and target configs must have separate directories.")
        for path in (self.legacy_config, self.target_config, self.legacy_worker):
            if not path.is_file():
                raise ValueError("A required configuration/legacy worker file is missing.")
        for config in (self.legacy_config, self.target_config):
            value = read_json(config)
            if not isinstance(value, dict) or value.get("auth", {}).get("mode") != "password":
                raise ValueError("Both installations must use password authentication.")
        task = self.ops.task("status", self.task_name, self.legacy_worker)
        target = self.ops.manager(self.target_config, "status")
        if not target.get("configured") or not target.get("app", {}).get("running"):
            raise RuntimeError("Target account and running Codex App are required.")
        port = target.get("settings", {}).get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Target manager port is invalid.")
        if not target.get("settings", {}).get("callerThread"):
            raise RuntimeError("Choose an existing chat in the manager connection settings first.")
        gateway = read_record(self.legacy_config.parent / "gateway-control.json")
        supervisor = read_json(self.legacy_control / "status.json") or {}
        old_worker = self.ops.process(supervisor.get("supervisorPid"))
        if old_worker:
            args = old_worker["arguments"]
            if len(args) != 3 or args[1] != "-B" or not same_path(args[2], self.legacy_worker):
                raise RuntimeError("Legacy supervisor does not match the explicitly selected worker.")
        old_gateway = self.ops.process(gateway.get("pid")) if isinstance(gateway, dict) else None
        if old_gateway:
            args = old_gateway["arguments"]
            if (not gateway.get("token") or "--config" not in args or
                    args.index("--config") + 1 >= len(args) or
                    not same_path(args[args.index("--config") + 1], self.legacy_config) or
                    not any(Path(arg).name == "run.py" for arg in args)):
                raise RuntimeError("Legacy gateway process/config ownership does not match.")
        if old_worker and Path(old_worker["image"]).name.lower() not in ("python.exe", "pythonw.exe"):
            raise RuntimeError("Legacy supervisor is not the selected Python deployment.")
        if old_gateway and Path(old_gateway["image"]).name.lower() != "python.exe":
            raise RuntimeError("Legacy gateway is not the selected Python deployment.")
        listeners = set(self.ops.listeners(port))
        target_pid = target.get("gateway", {}).get("pid")
        complete = (not task.get("enabled") and not old_worker and not old_gateway
                    and target.get("gateway", {}).get("running") and listeners == {target_pid})
        if complete:
            return {"state": "already_migrated", "port": port}, None
        if target.get("gateway", {}).get("running"):
            raise RuntimeError("Target gateway is already active while legacy ownership remains; inspect manually.")
        if listeners and (not old_gateway or listeners != {old_gateway["pid"]}):
            raise RuntimeError("The target port belongs to an unknown process; refusing migration.")
        if old_gateway and not listeners:
            raise RuntimeError("Legacy gateway is not listening on the target port.")
        if old_worker and supervisor.get("port") != port:
            raise RuntimeError("Legacy and target ports differ; no implicit port changes.")
        target_worker = target.get("worker", {})
        if target_worker.get("running"):
            pid = target_worker.get("supervisorPid")
            process = self.ops.process(pid)
            if not process or process["started"] != target_worker.get("supervisorStarted"):
                raise RuntimeError("Target worker identity changed.")
        else:
            process = None
        if not target.get("autostart", {}).get("supported"):
            raise RuntimeError("Target startup adapter is unavailable.")
        public = {"state": "ready", "port": port, "legacyTask": self.task_name,
                  "targetTask": target["autostart"].get("taskName"),
                  "accountPolicy": "keep_target", "network": target["settings"]["network"].get("mode"),
                  "targetAutostart": bool(target["autostart"].get("enabled")),
                  "requiresPhoneLogin": True}
        private = {"task": task, "target": target, "gateway": gateway,
                   "oldGateway": old_gateway, "oldWorker": old_worker, "targetWorker": process}
        return public, private

    def backup(self, destination, plan, private):
        destination = Path(destination).resolve()
        if any((ancestor / ".git").exists() for ancestor in (destination, *destination.parents)):
            raise ValueError("Choose a backup directory outside Git repositories; backups contain credentials.")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder = destination / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        folder.mkdir(mode=0o700)
        files = [("legacy-config.json", self.legacy_config),
                 ("target-config.json", self.target_config),
                 ("legacy-options.json", self.legacy_control / "options.json"),
                 ("target-options.json", Path(private["target"]["logDirectory"]) / "options.json")]
        hashes = {}
        for name, source in files:
            if source.is_file():
                data = source.read_bytes()
                path = folder / name
                with path.open("xb") as output:
                    output.write(data)
                path.chmod(0o600)
                hashes[name] = hashlib.sha256(data).hexdigest()
        xml = private["task"].get("xml")
        if xml:
            (folder / "legacy-task.xml").write_text(xml, encoding="utf-8")
        write_json(folder / "manifest.json", {
            **plan, "stage": "backed_up", "legacyConfig": str(self.legacy_config),
            "legacyControl": str(self.legacy_control), "legacyWorker": str(self.legacy_worker),
            "targetConfig": str(self.target_config), "legacyEnabled": private["task"].get("enabled"),
            "legacyDisabledMarker": (self.legacy_control / "disabled").exists(), "sha256": hashes})
        return folder

    def apply(self, backup_directory):
        # A separate migration lock does not claim the supervisors' startup locks.
        lock = type("LockProfile", (), {"config": self.target_config.parent / ".migration/lock.json",
                                       "control": self.target_config.parent / ".migration"})()
        with worker_lock(lock) as acquired:
            if not acquired:
                raise RuntimeError("Another migration is running.")
            plan, private = self.plan()
            if private is None:
                return plan
            folder = self.backup(backup_directory, plan, private)
            manifest = read_json(folder / "manifest.json")

            def stage(value):
                manifest["stage"] = value
                write_json(folder / "manifest.json", manifest)

            try:
                # Pause the target before releasing the legacy listener.
                stage("pausing_target")
                self.ops.manager(self.target_config, "autostart", {"enabled": False})
                self.wait(lambda: not self.ops.alive(private["targetWorker"]), "Target worker did not exit")
                if self.ops.manager(self.target_config, "status").get("worker", {}).get("running"):
                    raise RuntimeError("A new target worker appeared; inspect manually.")
                stage("disabling_legacy")
                self.ops.task("disable", self.task_name, self.legacy_worker)
                self.legacy_control.mkdir(parents=True, exist_ok=True)
                (self.legacy_control / "disabled").touch()
                self.wait(lambda: not self.ops.alive(private["oldWorker"]), "Legacy supervisor did not exit")
                current = read_json(self.legacy_control / "status.json") or {}
                replacement = self.ops.process(current.get("supervisorPid"))
                if replacement and self.ops.alive(replacement):
                    raise RuntimeError("A new legacy supervisor appeared; inspect manually.")
                stage("stopping_legacy")
                if private["oldGateway"]:
                    if not self.ops.alive(private["oldGateway"]):
                        raise RuntimeError("Legacy gateway changed before shutdown; inspect manually.")
                    if set(self.ops.listeners(plan["port"])) != {private["oldGateway"]["pid"]}:
                        raise RuntimeError("Port ownership changed before shutdown.")
                    self.ops.stop_gateway(self.legacy_config, private["gateway"])
                    self.wait(lambda: not self.ops.alive(private["oldGateway"]), "Legacy gateway did not exit")
                if self.ops.listeners(plan["port"]):
                    raise RuntimeError("Port was claimed by another service; target remains paused.")
                stage("starting_target")
                if plan["targetAutostart"]:
                    self.ops.manager(self.target_config, "autostart", {"enabled": True})
                else:
                    self.ops.manager(self.target_config, "service/start")

                def ready():
                    value = self.ops.manager(self.target_config, "status")
                    gateway = value.get("gateway", {})
                    return bool(gateway.get("running") and
                                set(self.ops.listeners(plan["port"])) == {gateway.get("pid")})

                self.wait(ready, "Target gateway did not become ready")
                for name, config in (("legacy-config.json", self.legacy_config),
                                     ("target-config.json", self.target_config)):
                    if hashlib.sha256(config.read_bytes()).hexdigest() != manifest["sha256"][name]:
                        raise RuntimeError("Account/config changed during migration; inspect manually.")
                if self.ops.task("status", self.task_name, self.legacy_worker).get("enabled"):
                    raise RuntimeError("Legacy startup was re-enabled during migration.")
                stage("complete")
                return {**plan, "state": "migrated", "backupDirectory": str(folder)}
            except Exception:
                # Never resurrect an old owner automatically after a partial handover.
                manifest["failed"] = True
                write_json(folder / "manifest.json", manifest)
                raise RuntimeError("Migration stopped at " + manifest["stage"] +
                                   ". Backup: " + str(folder) +
                                   ". Inspect status before retrying; no automatic rollback.") from None
