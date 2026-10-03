"""Synthetic handover tests. No real tasks, App calls, accounts or processes."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.autostart import read_json, write_json
from bridge.startup_migration import StartupMigration, command_arguments


class FakeOperations:
    def __init__(self, case):
        self.case, self.calls = case, []
        self.enabled = True
        self.fail = None
        self.processes = {
            10: {"pid": 10, "started": 100, "image": "pythonw.exe",
                 "arguments": ["pythonw.exe", "-B", str(case.worker)]},
            11: {"pid": 11, "started": 110, "image": "python.exe",
                 "arguments": ["python.exe", "-B", str(case.root / "run.py"),
                               "--config", str(case.legacy)]},
            20: {"pid": 20, "started": 200, "image": "Biandengbao.exe",
                 "arguments": ["Biandengbao.exe", "--worker", "options.json"]}}
        self.port_owners = [11]
        self.target = {"configured": True, "app": {"running": True},
                       "gateway": {"running": False},
                       "settings": {"port": 8788, "callerThread": "existing",
                                    "network": {"mode": "lan"}},
                       "worker": {"running": True, "supervisorPid": 20, "supervisorStarted": 200},
                       "autostart": {"enabled": True, "supported": True, "taskName": "target-task"},
                       "logDirectory": str(case.target_control)}

    def task(self, mode, name, worker):
        self.calls.append("task/" + mode)
        if self.fail == "foreign_task":
            raise RuntimeError("foreign task")
        if mode == "disable":
            self.enabled = False
            if self.fail != "old_worker":
                self.processes.pop(10, None)
        return {"enabled": self.enabled, "xml": "<task/>"}

    def process(self, pid):
        return copy.deepcopy(self.processes.get(pid))

    def alive(self, process):
        return process is not None and self.processes.get(process["pid"]) == process

    def listeners(self, port):
        return self.port_owners.copy()

    def manager(self, config, action, body=None):
        self.calls.append("manager/" + action + ("/" + str(body["enabled"]) if action == "autostart" else ""))
        if action == "status":
            return copy.deepcopy(self.target)
        if action == "autostart" and not body["enabled"]:
            self.target["autostart"]["enabled"] = False
            if self.fail != "target_worker":
                self.processes.pop(20, None)
                self.target["worker"]["running"] = False
        elif action in ("autostart", "service/start"):
            if self.fail == "start":
                raise RuntimeError("start failure")
            if action == "autostart":
                self.target["autostart"]["enabled"] = True
            self.target["gateway"] = {"running": True, "pid": 30}
            self.port_owners = [30]

    def stop_gateway(self, config, record):
        self.calls.append("stop_gateway")
        if self.fail == "stop":
            raise RuntimeError("stop timeout")
        self.processes.pop(11, None)
        self.port_owners = []
        (config.parent / "gateway-control.json").unlink()


class StartupMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.legacy = self.root / "old/config.json"
        self.target = self.root / "new/config.json"
        self.control = self.root / "legacy-control"
        self.target_control = self.target.parent / "autostart-target"
        self.worker = self.root / "legacy worker.py"
        self.worker.write_text("# fixture", encoding="utf-8")
        for config in (self.legacy, self.target):
            write_json(config, {"auth": {"mode": "password", "username": "admin", "hash": "fixture-secret"}})
        write_json(self.legacy.parent / "gateway-control.json", {"pid": 11, "token": "private-control"})
        write_json(self.control / "status.json", {"supervisorPid": 10, "port": 8788})
        write_json(self.control / "options.json", {"callerThread": "existing"})
        write_json(self.target_control / "options.json", {"port": 8788})
        self.ops = FakeOperations(self)
        self.migration = StartupMigration(self.legacy, self.control, self.worker, self.target,
                                          operations=self.ops, timeout=0)

    def manifest(self):
        files = list((self.root / "backups").glob("*/manifest.json"))
        self.assertEqual(len(files), 1)
        return read_json(files[0])

    def apply(self):
        return self.migration.apply(self.root / "backups")

    def test_preflight_is_read_only_and_redacts_credentials(self):
        result, _ = self.migration.plan()
        self.assertEqual(result["state"], "ready")
        self.assertNotIn("private-control", json.dumps(result))
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertEqual(self.ops.calls, ["task/status", "manager/status"])
        self.assertFalse((self.root / "backups").exists())

    def test_handover_preserves_accounts_and_startup_policy(self):
        original = self.target.read_bytes()
        result = self.apply()
        self.assertEqual(result["state"], "migrated")
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(self.manifest()["stage"], "complete")
        self.assertFalse(self.ops.enabled)
        self.assertTrue((self.control / "disabled").exists())
        self.assertEqual(self.ops.port_owners, [30])
        calls = self.ops.calls
        self.assertLess(calls.index("manager/autostart/False"), calls.index("task/disable"))
        self.assertLess(calls.index("task/disable"), calls.index("stop_gateway"))
        self.assertLess(calls.index("stop_gateway"), calls.index("manager/autostart/True"))

    def test_disabled_target_autostart_is_not_enabled(self):
        self.ops.target["autostart"]["enabled"] = False
        self.apply()
        self.assertIn("manager/service/start", self.ops.calls)
        self.assertNotIn("manager/autostart/True", self.ops.calls)

    def test_backup_includes_exact_configs_and_task_but_no_tokens_in_manifest(self):
        result = self.apply()
        folder = Path(result["backupDirectory"])
        self.assertEqual((folder / "target-config.json").read_bytes(), self.target.read_bytes())
        self.assertEqual((folder / "legacy-config.json").read_bytes(), self.legacy.read_bytes())
        self.assertEqual((folder / "legacy-task.xml").read_text(), "<task/>")
        self.assertNotIn("private-control", (folder / "manifest.json").read_text())
        self.assertNotIn("fixture-secret", (folder / "manifest.json").read_text())

    def test_backup_inside_git_is_rejected_before_any_mutation(self):
        (self.root / ".git").mkdir()
        with self.assertRaisesRegex(ValueError, "outside Git"):
            self.apply()
        self.assertNotIn("manager/autostart/False", self.ops.calls)

    def test_unknown_port_owner_is_refused(self):
        self.ops.port_owners = [99]
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.migration.plan()
        self.assertNotIn("task/disable", self.ops.calls)

    def test_two_listener_owners_are_refused(self):
        self.ops.port_owners = [11, 99]
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.migration.plan()

    def test_foreign_gateway_config_refused(self):
        self.ops.processes[11]["arguments"][-1] = str(self.target)
        with self.assertRaisesRegex(RuntimeError, "ownership"):
            self.migration.plan()

    def test_foreign_supervisor_refused(self):
        self.ops.processes[10]["arguments"][-1] = str(self.root / "foreign.py")
        with self.assertRaisesRegex(RuntimeError, "supervisor"):
            self.migration.plan()

    def test_foreign_task_refused_before_mutation(self):
        self.ops.fail = "foreign_task"
        with self.assertRaisesRegex(RuntimeError, "foreign task"):
            self.migration.plan()
        self.assertEqual(self.ops.calls, ["task/status"])

    def test_missing_password_auth_refused(self):
        write_json(self.target, {"auth": {"mode": "none"}})
        with self.assertRaisesRegex(ValueError, "password"):
            self.migration.plan()
        self.assertEqual(self.ops.calls, [])

    def test_same_config_directory_refused(self):
        self.migration.target_config = self.legacy.with_name("other-config.json")
        with self.assertRaisesRegex(ValueError, "separate"):
            self.migration.plan()

    def test_equivalent_config_directory_paths_refused(self):
        self.migration.target_config = Path(str(self.legacy.parent) + "/../old/other-config.json")
        with self.assertRaisesRegex(ValueError, "separate"):
            self.migration.plan()

    def test_changed_target_worker_birth_refused(self):
        self.ops.target["worker"]["supervisorStarted"] = 999
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.migration.plan()

    def test_missing_app_refused(self):
        self.ops.target["app"]["running"] = False
        with self.assertRaisesRegex(RuntimeError, "Codex App"):
            self.migration.plan()

    def test_missing_caller_refused(self):
        self.ops.target["settings"]["callerThread"] = ""
        with self.assertRaisesRegex(RuntimeError, "existing chat"):
            self.migration.plan()

    def test_mismatched_ports_refused(self):
        write_json(self.control / "status.json", {"supervisorPid": 10, "port": 9999})
        with self.assertRaisesRegex(RuntimeError, "ports differ"):
            self.migration.plan()

    def test_target_pause_timeout_does_not_touch_legacy(self):
        self.ops.fail = "target_worker"
        with self.assertRaisesRegex(RuntimeError, "pausing_target"):
            self.apply()
        self.assertNotIn("task/disable", self.ops.calls)
        self.assertTrue(self.manifest()["failed"])

    def test_legacy_worker_timeout_does_not_stop_gateway(self):
        self.ops.fail = "old_worker"
        with self.assertRaisesRegex(RuntimeError, "disabling_legacy"):
            self.apply()
        self.assertNotIn("stop_gateway", self.ops.calls)
        self.assertNotIn("manager/autostart/True", self.ops.calls)

    def test_stop_timeout_never_starts_target_or_reenables_legacy(self):
        self.ops.fail = "stop"
        with self.assertRaisesRegex(RuntimeError, "stopping_legacy"):
            self.apply()
        self.assertFalse(self.ops.enabled)
        self.assertNotIn("manager/autostart/True", self.ops.calls)

    def test_target_start_failure_retains_backup_and_disabled_legacy(self):
        self.ops.fail = "start"
        with self.assertRaisesRegex(RuntimeError, "starting_target"):
            self.apply()
        self.assertFalse(self.ops.enabled)
        self.assertTrue(self.manifest()["failed"])

    def test_repeat_success_is_noop_without_second_backup(self):
        self.apply()
        self.ops.calls.clear()
        self.assertEqual(self.apply()["state"], "already_migrated")
        self.assertEqual(self.ops.calls, ["task/status", "manager/status"])
        self.manifest()

    def test_second_migration_lock_refused(self):
        with patch("bridge.startup_migration.worker_lock") as lock:
            lock.return_value.__enter__.return_value = False
            with self.assertRaisesRegex(RuntimeError, "Another migration"):
                self.apply()
        self.assertEqual(self.ops.calls, [])

    @unittest.skipUnless(os.name == "nt", "Windows argument parser")
    def test_windows_arguments_preserve_spaces_and_quoted_paths(self):
        self.assertEqual(command_arguments('"C:\\Program Files\\Python\\pythonw.exe" -B "C:\\My App\\worker.py"'),
                         ["C:\\Program Files\\Python\\pythonw.exe", "-B", "C:\\My App\\worker.py"])
