"""Shared entry points and installation ownership; synthetic fixtures only."""
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import configure
import manager
import run
import stop
from bridge.autostart import Profile, profile_from_settings, read_json, write_json, worker_lock
from bridge.manager import Manager
from bridge.service_profile import default_config


class SharedServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.config = self.root / "data/config.json"
        self.source.mkdir()

    def test_all_entry_points_use_same_default_config_function(self):
        self.assertIs(run.default_config, default_config)
        self.assertIs(stop.default_config, default_config)
        self.assertIs(configure.default_config, default_config)
        self.assertIs(manager.default_config, default_config)

    def test_installation_paths_do_not_fork_new_service_identity(self):
        self.assertEqual(Profile(self.source, self.config).key,
                         Profile(self.root / "binary", self.config).key)

    def test_competing_process_can_probe_an_occupied_lock_without_reading_it(self):
        profile = Profile(self.source, self.config)
        code = ("from pathlib import Path\nfrom types import SimpleNamespace\n"
                "from bridge.autostart import worker_lock\nimport sys\n"
                "p=SimpleNamespace(config=Path(sys.argv[1]),control=Path(sys.argv[2]))\n"
                "with worker_lock(p) as acquired:\n print(acquired)\n")
        with worker_lock(profile) as acquired:
            self.assertTrue(acquired)
            result = subprocess.run([sys.executable, "-B", "-c", code,
                                     str(profile.config), str(profile.control)],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "False")
        with worker_lock(profile) as acquired:
            self.assertTrue(acquired)

    def save_owner(self, folder=None, launcher=None):
        folder = folder or self.config.parent / "autostart-0123456789ab"
        owner = self.root / "binary/_internal"
        owner.mkdir(parents=True, exist_ok=True)
        executable = owner.parent / "Biandengbao.exe"
        executable.touch()
        options = {"schema": 1, "repository": str(owner), "config": str(self.config),
                   "port": 8788, "callerThread": "fixture", "codexHome": str(self.root / "fake-home"),
                   "network": {"mode": "lan"}}
        if launcher is not None:
            options["launcher"] = launcher
        write_json(folder / "options.json", options)
        return owner, executable, folder

    def test_existing_packaged_task_is_adopted_without_second_task(self):
        owner, executable, folder = self.save_owner()
        profile = Profile(self.source, self.config)
        self.assertEqual(profile.key, "0123456789ab")
        self.assertEqual(profile.root, owner)
        self.assertEqual(profile.control, folder)
        self.assertEqual(profile.launcher, {"kind": "packaged", "executable": str(executable)})
        self.assertEqual(profile.pythonw(), executable)

    def test_duplicate_owner_records_are_refused(self):
        self.save_owner()
        self.save_owner(self.config.parent / "autostart-1123456789ab")
        with self.assertRaisesRegex(RuntimeError, "多套"):
            Profile(self.source, self.config)

    def test_other_config_owner_is_not_adopted(self):
        owner, _, folder = self.save_owner()
        self.assertNotEqual(Profile(self.source, self.config.with_name("other.json")).control, folder)

    def test_removed_owner_does_not_block_new_installation(self):
        owner, _, folder = self.save_owner()
        value = read_json(folder / "options.json")
        write_json(folder / "options.json", {**value, "retired": True})
        profile = Profile(self.source, self.config)
        self.assertEqual(profile.root, self.source)
        self.assertNotEqual(profile.control, folder)

    def test_saved_source_launcher_is_preserved_across_installations(self):
        executable = str(self.root / "python.exe")
        owner, _, _ = self.save_owner(launcher={"kind": "source", "executable": executable})
        profile = Profile(self.source, self.config)
        self.assertEqual(profile.root, owner)
        self.assertEqual(profile.launcher["executable"], executable)

    def test_foreign_executing_worker_does_not_claim_owner(self):
        owner, _, folder = self.save_owner()
        with self.assertRaisesRegex(RuntimeError, "安装"):
            profile_from_settings(self.source, folder / "options.json")
        self.assertEqual(profile_from_settings(owner, folder / "options.json").root, owner)

    def test_equivalent_absolute_paths_are_not_foreign_owners(self):
        owner, _, folder = self.save_owner()
        options = read_json(folder / "options.json")
        options["repository"] = str(owner) + "/../_internal"
        options["config"] = str(self.config.parent) + "/../data/config.json"
        write_json(folder / "options.json", options)
        self.assertEqual(profile_from_settings(owner, folder / "options.json").root, owner)

    def test_manager_options_keep_saved_owner_not_gui_installation(self):
        owner, executable, _ = self.save_owner()
        value = Manager(self.source, self.config).validate_options({})
        self.assertEqual(value["repository"], str(owner))
        self.assertEqual(value["launcher"]["executable"], str(executable))

    def test_source_connect_reuses_frozen_manager_without_spawning(self):
        record = {"pid": 123, "port": 12345, "token": "fixture"}
        with patch.object(manager, "read_record", return_value=record), \
                patch.object(manager, "rpc", return_value={"root": "other-install", "config": str(self.config)}), \
                patch.object(manager, "spawn") as spawn:
            self.assertEqual(manager.connect(self.config), record)
            spawn.assert_not_called()

    def test_connect_refuses_other_config(self):
        with patch.object(manager, "read_record", return_value={"port": 12345, "token": "fixture"}), \
                patch.object(manager, "rpc", return_value={"root": "other-install", "config": str(self.root / "wrong.json")}), \
                patch.object(manager, "spawn") as spawn:
            with self.assertRaisesRegex(RuntimeError, "归属"):
                manager.connect(self.config)
            spawn.assert_not_called()

    def test_configure_disable_calls_shared_manager_instead_of_new_profile(self):
        with patch.object(sys, "argv", ["configure.py", "--autostart", "disable", "--config", str(self.config)]), \
                patch.object(configure, "connect", return_value={"port": 12345, "token": "fixture"}) as connect, \
                patch.object(configure, "rpc", return_value={"enabled": False}) as rpc, \
                patch("builtins.print"):
            configure.main()
        connect.assert_called_once_with(self.config)
        rpc.assert_called_once_with(connect.return_value, "autostart", {"enabled": False}, timeout=60)

    def test_repeated_configure_enable_keeps_caller_and_does_not_restart_gateway(self):
        current = {"port": 8788, "callerThread": "saved-caller", "network": {"mode": "lan"}}
        with patch.object(sys, "argv", ["configure.py", "--autostart", "enable", "--config", str(self.config)]), \
                patch.object(configure, "connect", return_value={}), \
                patch.object(configure, "rpc", side_effect=[{"settings": current}, {"enabled": True}]) as rpc, \
                patch.dict(configure.os.environ, {"CODEX_THREAD_ID": "different-caller"}), patch("builtins.print"):
            configure.main()
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ["status", "autostart"])

    def test_raw_source_reuses_running_gateway_without_initialization(self):
        profile = Profile(self.source, self.config)
        write_json(profile.options, {"port": 8788, "network": {"mode": "lan"}})
        with patch.object(sys, "argv", ["run.py", "--config", str(self.config)]), \
                patch.object(run, "worker_lock") as lock, \
                patch.object(run, "read_record", return_value={"port": 12345, "token": "fixture"}), \
                patch.object(run, "rpc", return_value={"running": True}), \
                patch.object(run, "run_gateway") as gateway, patch("builtins.print"):
            lock.return_value.__enter__.return_value = False
            run.main()
            gateway.assert_not_called()

    def test_source_manual_start_uses_saved_port_and_network(self):
        profile = Profile(self.source, self.config)
        write_json(profile.options, {"port": 8788, "network": {"mode": "lan"}})
        with patch.object(sys, "argv", ["run.py", "--config", str(self.config)]), \
                patch.object(run, "worker_lock") as lock, \
                patch.object(run, "run_gateway") as gateway:
            lock.return_value.__enter__.return_value = True
            run.main()
            args = gateway.call_args.args[0]
            self.assertEqual(args.port, 8788)
            self.assertTrue(args.lan)
