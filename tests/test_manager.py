import http.client
import json
import os
import plistlib
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bridge.auth import Auth, password_record
from bridge.autostart import Profile, read_json, write_json
from bridge.gateway_admin import GatewayAdmin, account_record
from bridge.local_control import LocalServer, rpc
from bridge.manager import Manager, source_info
from bridge import manager, macos_app, macos_startup, autostart
from bridge.network import validate_network, network_arguments

ROOT = Path(__file__).resolve().parents[1]


class DeviceAuthTests(unittest.TestCase):
    def test_device_metadata_never_exposes_session_credentials(self):
        auth = Auth({"mode": "none"})
        token, session = auth.login("", "", "192.168.1.5", "iPhone Edge")
        devices = auth.devices()
        self.assertEqual(devices[0]["userAgent"], "iPhone Edge")
        self.assertNotIn("csrf", devices[0])
        self.assertNotIn(token, json.dumps(devices))
        self.assertNotEqual(session["id"], token)
        self.assertEqual(auth.revoke(session["id"]), 1)
        self.assertIsNone(auth.get(token))

    def test_revoke_one_does_not_revoke_others(self):
        auth = Auth({"mode": "none"})
        one, first = auth.login("", "", "one")
        two, _ = auth.login("", "", "two")
        auth.revoke(first["id"])
        self.assertIsNone(auth.get(one))
        self.assertIsNotNone(auth.get(two))
        self.assertEqual(auth.revoke(), 1)
        self.assertEqual(auth.devices(), [])

    def test_expired_devices_removed_and_metadata_bounded(self):
        auth = Auth({"mode": "none"})
        _, session = auth.login("", "", "x" * 200, "u" * 600)
        self.assertEqual(len(session["userAgent"]), 300)
        self.assertEqual(len(session["address"]), 100)
        session["expires"] = time.time() - 1
        self.assertEqual(auth.devices(), [])

    def test_old_password_cannot_finish_login_after_rotation(self):
        config = {"mode": "password", "username": "admin", **password_record("old-password-123")}
        new = {"mode": "password", "username": "new", **password_record("new-password-123")}
        auth = Auth(config)
        entered, release = threading.Event(), threading.Event()
        errors = []
        def hashing(*args, **kwargs):
            entered.set()
            release.wait(3)
            return bytes.fromhex(config["hash"])
        def login():
            try:
                auth.login("admin", "old-password-123", "test")
            except PermissionError as error:
                errors.append(str(error))
        with patch("bridge.auth.hashlib.pbkdf2_hmac", side_effect=hashing):
            thread = threading.Thread(target=login)
            thread.start()
            self.assertTrue(entered.wait(2))
            auth.replace_config(new, lambda: None)
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(auth.devices(), [])

    def test_persistence_failure_keeps_previous_auth_and_sessions(self):
        auth = Auth({"mode": "none"})
        token, _ = auth.login("", "", "one")
        with self.assertRaises(OSError):
            auth.replace_config({}, Mock(side_effect=OSError("disk full")))
        self.assertIsNotNone(auth.get(token))


class LocalControlTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.server = LocalServer(lambda action, data: self.calls.append((action, data)) or {"safe": True})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.record = {"port": self.server.server_port, "token": self.server.token}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, headers=None, body=b'{}', path="/api/v1/status"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        conn.request("POST", path, body, {"Authorization": "Bearer " + self.server.token,
                     "Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        value = json.loads(response.read())
        conn.close()
        return response.status, value

    def test_loopback_and_valid_capability(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(rpc(self.record, "status"), {"safe": True})
        self.assertEqual(self.calls, [("status", {})])

    def test_invalid_capability_origin_and_dns_rebinding_rejected(self):
        for headers in ({"Authorization": "Bearer bad"}, {"Origin": "https://evil.example"},
                        {"Host": "evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request(headers)[0], 403)
        self.assertEqual(self.calls, [])

    def test_body_shape_and_media_type_rejected(self):
        self.assertEqual(self.request(body=b'[]')[0], 400)
        self.assertEqual(self.request({"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.request(body=b'x' * 65537)[0], 400)
        self.assertEqual(self.calls, [])

    def test_unknown_path_and_query_rejected(self):
        self.assertEqual(self.request(path="/api/status")[0], 404)
        self.assertEqual(self.request(path="/api/v1/status?token=bad")[0], 404)


class NetworkTests(unittest.TestCase):
    def test_default_lan_and_explicit_loopback_external_arguments(self):
        self.assertEqual(network_arguments({"mode": "lan"}), ["--lan"])
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "cloudflared with space.exe"
            binary.touch()
            args = network_arguments({"mode": "tunnel", "cloudflared": str(binary)})
            self.assertEqual(args, ["--tunnel", "--cloudflared", str(binary)])
            self.assertNotIn("--lan", args)
        self.assertEqual(network_arguments({"mode": "proxy", "origin": "https://test.example"}),
                         ["--origin", "https://test.example"])

    def test_invalid_network_settings_fail_closed(self):
        for value in ({"mode": "unknown"}, {"mode": "lan", "no-auth": True},
                      {"mode": "tunnel", "cloudflared": "relative.exe"},
                      {"mode": "proxy", "origin": "http://example.com"},
                      {"mode": "proxy", "origin": "https://example.com/path"},
                      {"mode": "proxy", "origin": "https://user:secret@example.com"}):
            with self.assertRaises(ValueError):
                validate_network(value, require_binary=True)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "data/config.json"
        self.manager = Manager(self.root, self.config)
        self.active = patch.object(autostart, "active_instance", return_value=None)
        self.active.start()
        self.addCleanup(self.active.stop)

    def test_account_rotation_persists_hash_and_removes_plaintext(self):
        self.config.parent.mkdir()
        first = self.config.parent / "首次登录.txt"
        first.write_text("obsolete secret", encoding="utf-8")
        body = {"username": "local user", "password": "long password for test", "confirm": True}
        self.manager.dispatch("account", body)
        value = read_json(self.config)
        self.assertEqual(value["auth"]["username"], "local user")
        self.assertNotIn(body["password"], self.config.read_text())
        self.assertFalse(first.exists())
        Auth(value["auth"]).login(body["username"], body["password"], "local")

    def test_appearance_survives_new_manager_and_rejects_other_fields(self):
        self.manager.dispatch("appearance", {"mode": "dark"})
        replacement = Manager(self.root, self.config)
        self.assertEqual(replacement.appearance(), "dark")
        self.assertFalse(self.config.exists())
        for body in ({"mode": "anything"}, {"mode": "light", "password": "extra"}):
            with self.assertRaises(ValueError):
                self.manager.dispatch("appearance", body)
        self.assertEqual(replacement.appearance(), "dark")

    def test_gateway_live_account_rotation_revokes_logins(self):
        auth = Auth({"mode": "none"})
        token, _ = auth.login("", "", "phone")
        admin = GatewayAdmin(SimpleNamespace(auth=auth), self.config, lambda: {"running": True})
        admin("account", {"username": "new", "password": "long password 1234"})
        self.assertIsNone(auth.get(token))
        self.assertEqual(admin("status", {})["username"], "new")

    def test_confirmation_required_for_disruptive_actions(self):
        for action in ("account", "service/stop"):
            with self.assertRaises(ValueError):
                self.manager.dispatch(action, {})
        self.assertFalse(self.config.exists())

    def test_configure_changes_only_requested_local_profile(self):
        self.manager.dispatch("settings", {"port": 9888, "callerThread": "demo",
            "codexHome": str(self.root), "network": {"mode": "proxy", "origin": "https://test.example"}})
        options = self.manager.profile.load_options()
        self.assertEqual(options["port"], 9888)
        self.assertEqual(options["network"]["mode"], "proxy")
        self.assertFalse(self.config.exists())

    def test_invalid_settings_never_pause_existing_service(self):
        with patch.object(self.manager, "pause_worker") as pause:
            with self.assertRaises(ValueError):
                self.manager.configure({"port": 0, "confirmRestart": True})
            pause.assert_not_called()

    def test_unknown_setting_and_operation_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.configure({"shell": "execute"})
        with self.assertRaises(ValueError):
            self.manager.dispatch("run-command", {})

    def test_autostart_requires_password_mode(self):
        write_json(self.config, {"auth": {"mode": "none"}})
        with patch.object(self.manager.profile, "task") as task:
            with self.assertRaises(ValueError):
                self.manager.dispatch("autostart", {"enabled": True})
            task.assert_not_called()

    def test_gateway_control_identity_must_match(self):
        write_json(self.config.parent / "gateway-control.json", {"pid": 10, "token": "a"})
        write_json(self.config.parent / "gateway-admin.json", {"pid": 10, "token": "b", "port": 80})
        self.assertIsNone(self.manager.gateway_record())

    def test_update_check_distinguishes_ahead_and_diverged_without_writes(self):
        self.manager.build["revision"] = "a" * 40
        for relation in ("ahead", "behind", "diverged", "identical"):
            self.manager.last_update = None
            with patch.object(manager, "github_json", side_effect=[{"sha": "b" * 40,
                    "commit": {"message": "A change"}}, {"status": relation}]):
                result = self.manager.check_update()
            self.assertEqual(result["updateAvailable"], relation == "ahead")
        self.assertFalse(self.config.exists())

    def test_update_check_has_bounded_cache(self):
        self.manager.build["revision"] = "a" * 40
        with patch.object(manager, "github_json", return_value={"sha": "a" * 40}) as fetch:
            self.manager.check_update()
            self.manager.check_update()
            self.assertEqual(fetch.call_count, 1)


class NativeGuiVerificationTests(unittest.TestCase):
    def check_window(self, window):
        from manager import verify_gui_window
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            verify_gui_window(window, output)
            result = json.loads(output.read_text(encoding="utf-8"))
        window.destroy.assert_called_once()
        return result

    def test_waits_for_loaded_before_reading_page(self):
        window = Mock()
        window.events.loaded.wait.return_value = True
        window.evaluate_js.return_value = {"passed": True, "theme": "dark"}
        self.assertTrue(self.check_window(window)["passed"])
        window.events.loaded.wait.assert_called_once_with(30)
        window.evaluate_js.assert_called_once()

    def test_load_timeout_retains_failure_report(self):
        window = Mock()
        window.events.loaded.wait.return_value = False
        result = self.check_window(window)
        self.assertFalse(result["passed"])
        self.assertIn("30 seconds", result["error"])
        window.evaluate_js.assert_not_called()

    def test_javascript_error_retains_failure_report(self):
        window = Mock()
        window.events.loaded.wait.return_value = True
        window.evaluate_js.side_effect = RuntimeError("bridge unavailable")
        result = self.check_window(window)
        self.assertFalse(result["passed"])
        self.assertIn("bridge unavailable", result["error"])


class MacStartupTests(unittest.TestCase):
    def test_launchctl_timeout_is_actionable_and_never_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            profile = Profile(home, home / "data/config.json")
            with patch.object(Path, "home", return_value=home), patch.object(os, "getuid", return_value=501, create=True), \
                    patch.object(macos_startup.subprocess, "run", side_effect=subprocess.TimeoutExpired("launchctl", 15)) as run:
                with self.assertRaisesRegex(RuntimeError, "不要重复执行"):
                    macos_startup.task(profile, "enable")
                run.assert_called_once()
            self.assertFalse((home / "Library/LaunchAgents").exists())

    def test_launchagent_is_local_and_retains_frozen_worker_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            profile = Profile(home, home / "data/config.json")
            with patch.object(Path, "home", return_value=home), patch.object(os, "getuid", return_value=501, create=True), \
                    patch.object(macos_startup.subprocess, "run", return_value=Mock(returncode=1, stdout="", stderr="")) as run:
                # The status path is read-only and does not register anything.
                self.assertFalse(macos_startup.task(profile, "status")["exists"])
                self.assertFalse((home / "Library/LaunchAgents").exists())
            with patch.object(Path, "home", return_value=home), patch.object(os, "getuid", return_value=501, create=True), \
                    patch.object(macos_startup.sys, "frozen", True, create=True), \
                    patch.object(macos_startup.subprocess, "run", return_value=Mock(returncode=0, stdout="state = waiting", stderr="")) as run:
                macos_startup.task(profile, "enable")
            path = next((home / "Library/LaunchAgents").glob("*.plist"))
            value = plistlib.loads(path.read_bytes())
            self.assertEqual(value["ProgramArguments"][1:], ["--worker", str(profile.options)])
            self.assertTrue(value["RunAtLoad"])
            self.assertNotIn("KeepAlive", value)
            self.assertFalse(any("-k" in call.args[0] for call in run.call_args_list))

    def test_discovery_requires_unique_app_runtime_and_owned_ipc(self):
        rows = [(1, 0, "/Applications/Codex.app/Contents/MacOS/Codex"), (2, 1, "/app/bin/codex")]
        with patch.object(macos_app, "processes", return_value=rows), \
                patch.object(macos_app, "valid_app_image", side_effect=lambda image: image.endswith("/Codex")), \
                patch.object(macos_app, "owned_sockets", return_value=set()):
            self.assertIsNone(macos_app.discover())


if __name__ == "__main__":
    unittest.main()
