"""Repeat App sessions without touching a real App, task or gateway."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bridge import autostart, lifecycle, windows_app

SESSION_ALIVE = windows_app.session_alive


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.profile = autostart.Profile(temporary.name,
                                        Path(temporary.name) / ".local/config.json")
        self.profile.control.mkdir(parents=True)
        self.options = {"port": 8788}
        self.supervisor = autostart.Supervisor(self.profile, self.options)
        self.binding = {"appPid": 10, "appStarted": 100, "runtimePid": 11,
                        "runtimeStarted": 101, "runtime": "runtime", "pipe": "pipe"}
        self.process = Mock(pid=21)
        self.process.poll.return_value = None
        self.record = {"pid": 21, "token": "synthetic-token"}
        self.control = self.profile.config.parent / "gateway-control.json"
        self.stack = []
        for target, kwargs in (
            ("active_instance", {"return_value": None}),
            ("port_busy", {"return_value": False}),
            ("request_stop", {}),
        ):
            mocked = patch.object(autostart, target, **kwargs)
            self.stack.append(mocked.start())
            self.addCleanup(mocked.stop)
        self.stop = self.stack[-1]
        for target, value in (("discover", self.binding), ("session_alive", True),
                              ("binding_alive", True)):
            mocked = patch.object(windows_app, target, return_value=value)
            setattr(self, target, mocked.start())
            self.addCleanup(mocked.stop)
        sleeper = patch.object(autostart.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def own(self):
        autostart.write_json(self.control, self.record)
        self.supervisor.managed = (self.process, self.record, self.binding)
        self.supervisor.handled = windows_app.session_key(self.binding)

    def state(self):
        return autostart.read_json(self.profile.control / "status.json")["state"]

    def test_success_keeps_monitoring_without_discovery_or_duplicate(self):
        self.own()
        with patch.object(autostart, "launch") as launch:
            self.assertEqual(self.supervisor.tick(), 3)
            self.supervisor.tick()
        launch.assert_not_called()
        self.discover.assert_not_called()
        self.stop.assert_not_called()
        self.assertEqual(self.state(), "monitoring")

    def test_app_closed_stops_owned_gateway_then_waits(self):
        self.own()
        self.session_alive.return_value = False
        self.discover.return_value = None
        self.supervisor.tick()
        self.stop.assert_called_once_with(self.profile.config.parent,
                                          expected_record=self.record)
        self.process.wait.assert_called_once_with(timeout=5)
        self.process.kill.assert_not_called()
        self.assertIsNone(self.supervisor.managed)
        self.assertEqual(self.state(), "waiting_for_app")

    def test_same_boot_app_restart_starts_new_binding_after_owned_stop(self):
        self.own()
        replacement = {**self.binding, "appPid": 30, "appStarted": 200,
                       "runtimePid": 31, "runtimeStarted": 201, "pipe": "new-pipe"}
        self.session_alive.return_value = False
        self.discover.return_value = replacement
        with patch.object(self.supervisor, "start") as start:
            self.supervisor.tick()
        self.stop.assert_called_once()
        start.assert_called_once_with(replacement)

    def test_manual_stop_is_not_replayed_until_app_changes(self):
        self.own()
        self.process.poll.return_value = 0
        with patch.object(self.supervisor, "start") as start:
            self.supervisor.tick()
            self.supervisor.tick()
            start.assert_not_called()
            self.discover.return_value = {**self.binding, "pipe": "new-pipe"}
            self.supervisor.tick()
            start.assert_not_called()
            self.discover.return_value = {**self.binding, "appStarted": 200}
            self.supervisor.tick()
            start.assert_called_once()
        self.stop.assert_not_called()

    def test_pid_reuse_is_a_different_session(self):
        self.assertNotEqual(windows_app.session_key(self.binding),
                            windows_app.session_key({**self.binding, "appStarted": 999}))
        self.assertNotEqual(windows_app.session_key(self.binding),
                            windows_app.session_key({**self.binding, "runtimeStarted": 999}))
        with patch.object(windows_app, "process_started", return_value=999):
            self.assertFalse(SESSION_ALIVE(self.binding))

    def test_record_removed_during_manual_shutdown_waits_for_child(self):
        self.own()
        self.control.unlink()
        with patch.object(self.supervisor, "start") as start:
            self.supervisor.tick()
        self.process.wait.assert_called_once_with(timeout=5)
        self.stop.assert_not_called()
        start.assert_not_called()
        self.assertEqual(self.state(), "stopped_until_app_restart")

    def test_missing_record_with_live_child_refuses_restart(self):
        self.own()
        self.control.unlink()
        self.process.wait.side_effect = subprocess.TimeoutExpired("gateway", 5)
        with patch.object(self.supervisor, "start") as start:
            with self.assertRaises(RuntimeError):
                self.supervisor.tick()
        self.stop.assert_not_called()
        start.assert_not_called()

    def test_native_runtime_restart_also_rebinds(self):
        self.own()
        self.session_alive.return_value = False
        replacement = {**self.binding, "runtimePid": 32, "runtimeStarted": 300}
        self.discover.return_value = replacement
        with patch.object(self.supervisor, "start") as start:
            self.supervisor.tick()
        start.assert_called_once_with(replacement)

    def test_transient_channel_unavailable_does_not_stop_live_app(self):
        self.own()
        self.binding_alive.return_value = False
        self.discover.return_value = None
        self.supervisor.tick()
        self.supervisor.tick()
        self.stop.assert_not_called()
        self.discover.assert_called_once()
        self.assertEqual(self.state(), "waiting_for_channel")

    def test_new_valid_channel_rebinds_only_owned_gateway(self):
        self.own()
        self.binding_alive.return_value = False
        replacement = {**self.binding, "pipe": "new-pipe"}
        self.discover.return_value = replacement
        with patch.object(self.supervisor, "start") as start:
            self.supervisor.tick()
        self.stop.assert_called_once()
        start.assert_called_once_with(replacement)

    def test_foreign_record_refused_without_stop_or_launch(self):
        self.own()
        autostart.write_json(self.control, {**self.record, "token": "other-token"})
        with patch.object(self.supervisor, "start") as start:
            with self.assertRaises(RuntimeError):
                self.supervisor.tick()
        self.stop.assert_not_called()
        start.assert_not_called()

    def test_shutdown_timeout_never_force_kills_or_starts_second_instance(self):
        self.own()
        self.session_alive.return_value = False
        self.process.wait.side_effect = subprocess.TimeoutExpired("gateway", 5)
        with patch.object(self.supervisor, "start") as start:
            with self.assertRaises(RuntimeError):
                self.supervisor.tick()
        start.assert_not_called()
        self.process.kill.assert_not_called()

    def test_external_listener_stays_monitored_but_is_not_taken_over(self):
        self.stack[1].return_value = True
        with patch.object(self.supervisor, "start") as start:
            self.assertEqual(self.supervisor.tick(), 15)
            self.supervisor.tick()
        self.stop.assert_not_called()
        start.assert_not_called()
        self.discover.assert_not_called()
        self.assertEqual(self.state(), "existing_listener")

    def test_disable_during_discovery_never_starts(self):
        def discover():
            self.profile.disabled.touch()
            return self.binding
        self.discover.side_effect = discover
        with patch.object(self.supervisor, "start") as start:
            self.supervisor.tick()
        start.assert_not_called()

    def test_readiness_requires_app_still_bound(self):
        autostart.write_json(self.control, self.record)
        self.stack[1].return_value = True
        self.binding_alive.return_value = False
        with patch.object(autostart, "launch",
                          return_value=(self.process, "log", "errors")) as launch:
            with self.assertRaises(RuntimeError):
                self.supervisor.start(self.binding)
        launch.assert_called_once()
        self.process.kill.assert_not_called()
        self.assertEqual(self.state(), "startup_failed")

    def test_lifecycle_expected_token_refuses_replacement_before_write(self):
        autostart.write_json(self.control, {**self.record, "token": "replacement"})
        with self.assertRaises(RuntimeError):
            lifecycle.request_stop(self.profile.config.parent, expected_record=self.record)
        self.assertFalse(self.control.with_name("gateway.stop").exists())


if __name__ == "__main__":
    unittest.main()
