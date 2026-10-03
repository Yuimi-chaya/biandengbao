"""Synthetic startup/configuration tests: no task registration or App changes."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from bridge import autostart, windows_app
from bridge.ipc import IPCError

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def unlocked(profile):
    yield True


class WindowsDiscoveryTests(unittest.TestCase):
    def select(self, names, owners, tools):
        def probe(path):
            value = tools[path]
            if isinstance(value, Exception):
                raise value
            return value
        return windows_app.select_unique_tools(10, names, owners.get, probe)

    def test_owned_supported_channel_selected_not_other_interfaces(self):
        names = ["codex-browser-use-a", "codex-browser-use-b"]
        paths = [windows_app.PIPE_PREFIX + name for name in names]
        self.assertEqual(self.select(names, dict.fromkeys(paths, 10), {
            paths[0]: windows_app.REQUIRED_TOOLS, paths[1]: IPCError("not tools")}),
            paths[0])

    def test_multiple_valid_channels_refused(self):
        names = ["codex-browser-use-a", "codex-browser-use-b"]
        paths = [windows_app.PIPE_PREFIX + name for name in names]
        self.assertIsNone(self.select(names, dict.fromkeys(paths, 10),
                                     dict.fromkeys(paths, windows_app.REQUIRED_TOOLS)))

    def test_foreign_owner_never_probed(self):
        path = windows_app.PIPE_PREFIX + "codex-browser-use-a"
        self.assertIsNone(self.select(["codex-browser-use-a"], {path: 11}, {}))

    def test_missing_capability_refused(self):
        path = windows_app.PIPE_PREFIX + "codex-browser-use-a"
        self.assertIsNone(self.select(["codex-browser-use-a"], {path: 10},
                                     {path: {"list_projects"}}))

    def test_large_channel_inventory_not_silently_truncated(self):
        names = ["codex-browser-use-" + str(index) for index in range(65)]
        probe = Mock()
        self.assertIsNone(windows_app.select_unique_tools(10, names, Mock(), probe))
        probe.assert_not_called()

    def test_scan_deadline_refuses_partial_result(self):
        with patch.object(windows_app.time, "monotonic", side_effect=[0, 9]):
            self.assertIsNone(self.select(["codex-browser-use-a"], {}, {}))

    def test_store_app_paths_not_arbitrary_executables(self):
        with patch.dict(os.environ, {"PROGRAMFILES": r"C:\Program Files"}):
            self.assertTrue(windows_app.valid_app_image(
                r"C:\Program Files\WindowsApps\OpenAI.Codex_1\app\ChatGPT.exe"))
            self.assertFalse(windows_app.valid_app_image(
                r"C:\Other\OpenAI.Codex_1\app\ChatGPT.exe"))
            self.assertFalse(windows_app.valid_app_image(
                r"C:\Program Files\WindowsApps\Other_1\app\ChatGPT.exe"))

    def test_runtime_must_be_inside_app_runtime_root(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Local"}):
            self.assertTrue(windows_app.valid_runtime_image(
                r"C:\Local\OpenAI\Codex\bin\version\codex.exe"))
            self.assertFalse(windows_app.valid_runtime_image(r"C:\Other\codex.exe"))
            self.assertFalse(windows_app.valid_runtime_image(None))

    def test_missing_app_does_not_probe_children(self):
        with patch.object(windows_app, "pipe_owner", return_value=None), \
                patch.object(windows_app, "children") as children:
            self.assertIsNone(windows_app.discover())
            children.assert_not_called()

    def test_unique_result_revalidated(self):
        with patch.object(windows_app, "pipe_owner", side_effect=[10, 10, 11]), \
                patch.object(windows_app, "process_image", side_effect=["app", "runtime"]), \
                patch.object(windows_app, "valid_app_image", return_value=True), \
                patch.object(windows_app, "valid_runtime_image", return_value=True), \
                patch.object(windows_app, "children", return_value=[(12, "codex.exe")]), \
                patch.object(windows_app, "process_started", return_value=1), \
                patch.object(windows_app.os, "listdir", return_value=["codex-browser-use-a"]), \
                patch("bridge.desktop_tools.DesktopTools._request",
                      return_value={"tools": [{"name": name}
                                    for name in windows_app.REQUIRED_TOOLS]}):
            self.assertIsNone(windows_app.discover())


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo with spaces"
        self.root.mkdir()
        self.config = self.root / ".local" / "bedroom.json"
        self.profile = autostart.Profile(self.root, self.config)
        self.support = patch.object(autostart, "supported", return_value=True)
        self.support.start()
        self.addCleanup(self.support.stop)
        self.image = patch.object(windows_app, "process_image", return_value=None)
        self.image.start()
        self.addCleanup(self.image.stop)
        self.backend = patch.object(self.profile, "task", return_value={
            "exists": False, "enabled": False, "state": "NotInstalled"})
        self.task = self.backend.start()
        self.addCleanup(self.backend.stop)

    def auth(self, mode="password"):
        autostart.write_json(self.config, {"auth": {"mode": mode}})

    def options(self, port=8790):
        self.auth()
        self.profile.enable(port=port, codex_home=self.root / "fake home", caller="demo")
        return self.profile.load_options()

    def test_default_status_does_not_create_configuration_or_task(self):
        status = self.profile.status()
        self.assertFalse(status["enabled"])
        self.assertFalse(self.profile.control.exists())
        self.task.assert_called_once_with("status")

    def test_profiles_separate_configs_and_installations(self):
        other = autostart.Profile(self.root, self.root / ".local/other.json")
        moved = autostart.Profile(self.root.parent / "other repo", self.config)
        self.assertNotEqual(self.profile.task_name, other.task_name)
        self.assertNotEqual(self.profile.task_name, moved.task_name)
        self.assertEqual(self.profile.task_name,
                         autostart.Profile(self.root, self.config).task_name)

    def test_enable_saves_port_config_context_not_password_or_pipe(self):
        self.options()
        text = self.profile.options.read_text(encoding="utf-8")
        value = json.loads(text)
        self.assertEqual(value["port"], 8790)
        self.assertEqual(value["config"], str(self.config.resolve()))
        self.assertNotIn("auth", value)
        self.assertNotIn("password", value)
        self.assertNotIn("pipe", value)
        self.assertNotIn("runtime", value)
        self.assertFalse(self.profile.disabled.exists())
        self.assertIn(("enable",), [call.args for call in self.task.call_args_list])

    def test_enable_does_not_rewrite_gateway_config(self):
        self.auth()
        before = self.config.read_bytes()
        self.profile.enable(caller="demo")
        self.assertEqual(self.config.read_bytes(), before)

    def test_port_and_home_preserved_on_reenable(self):
        previous = self.options()
        self.profile.enable(caller="demo")
        value = self.profile.load_options()
        self.assertEqual(value["port"], previous["port"])
        self.assertEqual(value["codexHome"], previous["codexHome"])

    def test_invalid_port_is_rejected_before_task_access(self):
        for port in (0, 65536, True, "8787"):
            with self.assertRaises(ValueError):
                self.profile.enable(port=port, caller="demo")
        self.task.assert_not_called()

    def test_missing_auth_does_not_install_task(self):
        with self.assertRaises(RuntimeError):
            self.profile.enable(caller="demo")
        self.task.assert_not_called()
        self.assertFalse(self.profile.control.exists())

    def test_passwordless_mode_does_not_install_task(self):
        self.auth("none")
        with self.assertRaises(RuntimeError):
            self.profile.enable(caller="demo")
        self.task.assert_not_called()

    def test_missing_caller_does_not_guess_chat(self):
        self.auth()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                self.profile.enable()
        self.task.assert_not_called()
        self.assertFalse(self.profile.control.exists())

    def test_current_context_has_priority_over_saved_context(self):
        self.options()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "new-demo"}):
            self.profile.enable()
        self.assertEqual(self.profile.load_options()["callerThread"], "new-demo")

    def test_running_worker_must_be_disabled_before_reconfiguration(self):
        self.auth()
        self.task.return_value = {"state": "Running"}
        with self.assertRaises(RuntimeError):
            self.profile.enable(caller="demo")
        self.assertFalse(self.profile.control.exists())

    def test_foreign_task_failure_has_no_configuration_side_effect(self):
        self.auth()
        self.task.side_effect = RuntimeError("foreign task")
        with self.assertRaises(RuntimeError):
            self.profile.enable(caller="demo")
        self.assertFalse(self.profile.control.exists())
        with self.assertRaises(RuntimeError):
            self.profile.disable()
        self.assertFalse(self.profile.control.exists())

    def test_registration_failure_leaves_worker_disabled(self):
        self.auth()
        def task(mode):
            if mode == "enable":
                raise RuntimeError("registration failed")
            return {"state": "NotInstalled"}
        self.task.side_effect = task
        with self.assertRaises(RuntimeError):
            self.profile.enable(caller="demo")
        self.assertTrue(self.profile.disabled.exists())
        self.assertNotIn(("start",), [call.args for call in self.task.call_args_list])

    def test_disable_keeps_auth_and_does_not_stop_gateway(self):
        self.options()
        before = self.config.read_bytes()
        with patch.object(autostart.subprocess, "Popen") as popen:
            self.profile.disable()
        self.assertTrue(self.profile.disabled.exists())
        self.assertEqual(self.config.read_bytes(), before)
        popen.assert_not_called()
        self.assertIn(("disable",), [call.args for call in self.task.call_args_list])

    def test_remove_retains_diagnostics_and_disables_first(self):
        self.options()
        self.task.reset_mock()
        self.profile.remove()
        self.assertTrue(self.profile.options.exists())
        self.assertTrue(self.profile.disabled.exists())
        modes = [call.args[0] for call in self.task.call_args_list]
        self.assertLess(modes.index("disable"), modes.index("remove"))

    def test_remove_never_terminates_running_worker(self):
        self.options()
        self.task.side_effect = lambda mode: {"state": "Running"}
        with patch.object(autostart.time, "monotonic", side_effect=[0, 11]):
            with self.assertRaises(RuntimeError):
                self.profile.remove()
        self.assertNotIn(("remove",), [call.args for call in self.task.call_args_list])
        self.assertTrue(self.profile.disabled.exists())

    def test_moved_repository_options_refused(self):
        self.options()
        value = autostart.read_json(self.profile.options)
        value["repository"] = str(self.root.parent / "different")
        autostart.write_json(self.profile.options, value)
        with self.assertRaises(RuntimeError):
            self.profile.load_options()

    def test_worker_requires_canonical_settings_path(self):
        self.options()
        other = self.root / "wrong.json"
        other.write_bytes(self.profile.options.read_bytes())
        with self.assertRaises(RuntimeError):
            autostart.profile_from_settings(self.root, other)
        actual = autostart.profile_from_settings(self.root, self.profile.options)
        self.assertEqual(actual.options, self.profile.options)

    def test_malformed_options_refused(self):
        self.options()
        for key, invalid in (("schema", 2), ("port", True), ("callerThread", ""),
                             ("codexHome", "relative/path")):
            value = autostart.read_json(self.profile.options)
            value[key] = invalid
            autostart.write_json(self.profile.options, value)
            with self.assertRaises(RuntimeError):
                self.profile.load_options()
            self.options()

    def test_unsupported_platform_does_not_touch_startup_settings(self):
        self.backend.stop()
        with patch.object(autostart, "supported", return_value=False), \
                patch.object(autostart.subprocess, "run") as run:
            self.assertFalse(self.profile.status()["supported"])
            for method in (self.profile.enable, self.profile.disable, self.profile.remove):
                with self.assertRaises(RuntimeError):
                    method()
            run.assert_not_called()
        self.assertFalse(self.profile.control.exists())

    def test_existing_listener_never_launches_duplicate(self):
        self.options()
        with patch.object(autostart, "worker_lock", unlocked), \
                patch.object(autostart, "port_busy", return_value=True), \
                patch.object(windows_app, "discover") as discover, \
                patch.object(autostart, "launch") as launch:
            autostart.run_worker(self.profile, once=True)
        discover.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(autostart.read_json(self.profile.control / "status.json")["state"],
                         "existing_listener")

    def test_disabled_worker_never_launches(self):
        self.options()
        self.profile.disabled.touch()
        with patch.object(autostart, "worker_lock", unlocked), \
                patch.object(autostart, "launch") as launch:
            autostart.run_worker(self.profile)
        launch.assert_not_called()
        self.assertEqual(autostart.read_json(self.profile.control / "status.json")["state"],
                         "disabled")

    def test_wait_for_app_and_disable_cooperatively(self):
        self.options()
        def sleep(seconds):
            self.profile.disabled.touch()
        with patch.object(autostart, "worker_lock", unlocked), \
                patch.object(autostart, "port_busy", return_value=False), \
                patch.object(windows_app, "discover", return_value=None), \
                patch.object(autostart.time, "sleep", side_effect=sleep), \
                patch.object(autostart, "launch") as launch:
            autostart.run_worker(self.profile)
        launch.assert_not_called()
        self.assertEqual(autostart.read_json(self.profile.control / "status.json")["state"],
                         "disabled")

    def test_disable_during_discovery_prevents_launch(self):
        self.options()
        def discover():
            self.profile.disabled.touch()
            return {"appPid": 10}
        with patch.object(autostart, "worker_lock", unlocked), \
                patch.object(autostart, "port_busy", return_value=False), \
                patch.object(windows_app, "discover", side_effect=discover), \
                patch.object(autostart, "launch") as launch:
            autostart.run_worker(self.profile)
        launch.assert_not_called()

    def test_changed_app_prevents_launch(self):
        options = self.options()
        with patch.object(windows_app, "binding_alive", return_value=False), \
                patch.object(autostart.subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError):
                autostart.launch(self.profile, options, {})
        popen.assert_not_called()

    def test_passwordless_config_changed_after_enable_prevents_launch(self):
        options = self.options()
        self.auth("none")
        with patch.object(autostart.subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError):
                autostart.launch(self.profile, options, {})
        popen.assert_not_called()

    def test_worker_does_not_accept_another_gateways_lifecycle_record(self):
        self.options()
        (self.config.parent / "gateway-control.json").write_text(
            json.dumps({"pid": 22, "token": "synthetic"}), encoding="utf-8")
        process = Mock(pid=21)
        process.poll.return_value = None
        with patch.object(autostart, "worker_lock", unlocked), \
                patch.object(autostart, "port_busy", side_effect=[False] + [True] * 50), \
                patch.object(windows_app, "discover", return_value={
                    "appPid": 10, "appStarted": 1, "runtimePid": 11,
                    "runtimeStarted": 2, "pipe": "demo"}), \
                patch.object(autostart, "launch", return_value=(process, "log", "errors")) as launch, \
                patch.object(autostart.time, "sleep"):
            with self.assertRaises(RuntimeError):
                autostart.run_worker(self.profile, once=True)
        launch.assert_called_once()
        process.kill.assert_not_called()
        self.assertEqual(autostart.read_json(self.profile.control / "status.json")["state"],
                         "startup_failed")

    def test_worker_success_requires_own_child_record(self):
        self.options()
        (self.config.parent / "gateway-control.json").write_text(
            json.dumps({"pid": 21, "token": "synthetic"}), encoding="utf-8")
        process = Mock(pid=21)
        process.poll.return_value = None
        with patch.object(autostart, "worker_lock", unlocked), \
                patch.object(autostart, "port_busy", side_effect=[False, True]), \
                patch.object(windows_app, "discover", return_value={
                    "appPid": 10, "appStarted": 1, "runtimePid": 11,
                    "runtimeStarted": 2, "pipe": "demo"}), \
                patch.object(windows_app, "binding_alive", return_value=True), \
                patch.object(autostart, "launch", return_value=(process, "log", "errors")):
            autostart.run_worker(self.profile, once=True)
        state = autostart.read_json(self.profile.control / "status.json")
        self.assertEqual(state["state"], "started")
        self.assertEqual(state["gatewayPid"], 21)

    def test_launch_arguments_use_profile_not_machine_paths_or_tunnel(self):
        options = self.options()
        (self.root / "run.py").touch()
        python = self.root / "python.exe"
        python.touch()
        binding = {"appPid": 10, "pipe": "fake-pipe", "runtime": "fake-runtime"}
        with patch.object(windows_app, "binding_alive", return_value=True), \
                patch.object(autostart.sys, "executable", str(python)), \
                patch.object(subprocess, "DETACHED_PROCESS", 0, create=True), \
                patch.object(subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                patch.object(autostart.subprocess, "Popen") as popen:
            autostart.launch(self.profile, options, binding)
        arguments = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(arguments[arguments.index("--config") + 1], str(self.config.resolve()))
        self.assertEqual(arguments[arguments.index("--port") + 1], "8790")
        self.assertEqual(arguments[arguments.index("--codex-home") + 1], options["codexHome"])
        self.assertIn("--lan", arguments)
        self.assertNotIn("--tunnel", arguments)
        self.assertNotIn("--no-auth", arguments)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["env"]["CODEX_THREAD_ID"], "demo")
        self.assertEqual(kwargs["env"]["CODEX_APP_TOOLS_PIPE_PATH"], "fake-pipe")

    def test_task_command_uses_argument_array_not_shell_command(self):
        self.backend.stop()
        with patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}), \
                patch.object(self.profile, "pythonw", return_value=self.root / "pythonw.exe"), \
                patch.object(autostart.subprocess, "run",
                             return_value=Mock(returncode=0, stdout='{"exists":false}')) as run:
            self.assertFalse(self.profile.task("status")["exists"])
        args = run.call_args.args[0]
        self.assertIsInstance(args, list)
        self.assertEqual(args[args.index("-SettingsPath") + 1], str(self.profile.options))
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_same_config_directory_other_port_does_not_start_second_gateway(self):
        self.options()
        autostart.write_json(self.config.parent / "gateway-control.json",
                             {"pid": 12, "token": "synthetic"})
        with patch.object(autostart, "worker_lock", unlocked), \
                patch.object(windows_app, "process_image", return_value="some-process"), \
                patch.object(autostart, "port_busy", return_value=False), \
                patch.object(autostart, "launch") as launch:
            autostart.run_worker(self.profile, once=True)
        launch.assert_not_called()
        self.assertEqual(autostart.read_json(self.profile.control / "status.json")["state"],
                         "existing_instance")

    def test_stale_record_never_signals_or_claims_live_process(self):
        self.options()
        autostart.write_json(self.config.parent / "gateway-control.json",
                             {"pid": 12, "token": "synthetic"})
        self.assertIsNone(autostart.active_instance(self.profile))

    def test_disabled_marker_rechecked_immediately_before_launch(self):
        options = self.options()
        self.profile.disabled.touch()
        with patch.object(autostart.subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError):
                autostart.launch(self.profile, options, {})
        popen.assert_not_called()

    def test_task_timeout_not_automatically_replayed(self):
        self.backend.stop()
        with patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}), \
                patch.object(self.profile, "pythonw", return_value=self.root / "pythonw.exe"), \
                patch.object(autostart.subprocess, "run",
                             side_effect=subprocess.TimeoutExpired("task", 30)) as run:
            with self.assertRaises(RuntimeError):
                self.profile.task("enable")
        run.assert_called_once()


class ConfigurationEntrypointTests(unittest.TestCase):
    def test_help_is_utf8_and_does_not_launch_gateway(self):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "configure.py"), "--help"],
            cwd=ROOT, capture_output=True, timeout=10,
            env={**os.environ, "PYTHONIOENCODING": "ascii"})
        self.assertEqual(result.returncode, 0)
        text = result.stdout.decode("utf-8")
        for option in ("--autostart", "--config", "--port", "--codex-home"):
            self.assertIn(option, text)

    def test_no_option_in_noninteractive_run_is_not_enable(self):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "configure.py")],
            cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        self.assertIn(result.returncode, (0, 2))
        self.assertNotIn(b'"enabled": true', result.stdout)

    def test_task_script_has_no_process_kill_or_app_launch(self):
        text = (ROOT / "tools/configure-autostart.ps1").read_text(encoding="utf-8")
        self.assertIn("-LogonType Interactive -RunLevel Limited", text)
        self.assertIn("-AtLogOn -User $user", text)
        self.assertNotIn("Stop-ScheduledTask", text)
        self.assertNotIn("Stop-Process", text)
        self.assertNotIn("Start-Process", text)
        self.assertIn("$task.Actions.Count -ne 1", text)


if __name__ == "__main__":
    unittest.main()
