#!/usr/bin/env python3
"""Use only a temporary gateway with a deliberately nonexistent App channel."""
import argparse
import http.client
import json
import os
import plistlib
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bridge.local_control import rpc
from bridge.lifecycle import read_record, request_stop
from bridge.autostart import windows_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--gui-binary", type=Path)
    parser.add_argument("--online", action="store_true")
    args = parser.parse_args()
    binary = args.binary.resolve()
    flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    with tempfile.TemporaryDirectory(prefix="biandengbao-smoke-") as temporary:
        root = Path(temporary)
        config = root / "config.json"
        gateway = None
        def cli(command, *extra, input=None):
            result = subprocess.run([str(binary), command, "--config", str(config), *extra],
                input=input, capture_output=True, encoding="utf-8", errors="replace", timeout=35, **flags)
            if result.returncode:
                raise RuntimeError("Packaged CLI failed: " + result.stderr[-700:])
            return json.loads(result.stdout)["result"]
        def phone(path, body=None, cookie=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            headers = {"Origin": "http://127.0.0.1:%d" % port, "Content-Type": "application/json",
                       "User-Agent": "Synthetic iPhone Edge"}
            if cookie:
                headers["Cookie"] = cookie
            conn.request("POST" if body is not None else "GET", path,
                         json.dumps(body).encode() if body is not None else None, headers)
            response = conn.getresponse()
            value, cookie_out = json.loads(response.read()), response.getheader("Set-Cookie")
            status = response.status
            conn.close()
            return status, value, cookie_out.split(";")[0] if cookie_out else None
        try:
            status = cli("status")
            assert status["manager"]["revision"] != "development"
            if sys.platform == "darwin":
                info = plistlib.loads((binary.parent.parent / "Info.plist").read_bytes())
                assert info["CFBundleShortVersionString"] == status["manager"]["version"]
                assert info["CFBundleVersion"] == status["manager"]["version"]
            assert not status["configured"]
            cli("account", "--username", "smoke", "--password-stdin", "--yes", input="synthetic-password-123\n")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            ipc = r"\\.\pipe\biandengbao-nonexistent-smoke" if os.name == "nt" else str(root / "no-app.sock")
            gateway = subprocess.Popen([str(binary), "--gateway", "--config", str(config),
                "--port", str(port), "--codex-home", str(root / "no-codex-data"), "--ipc-path", ipc],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
            deadline = time.monotonic() + 15
            while not (root / "gateway-admin.json").exists():
                if gateway.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Packaged test gateway did not become ready")
                time.sleep(.1)
            manager_record = read_record(root / ".manager/control.json")
            source = subprocess.run([sys.executable, "-B", str(ROOT / "manager.py"),
                "status", "--config", str(config)], capture_output=True, encoding="utf-8",
                errors="replace", timeout=35, **flags)
            assert source.returncode == 0, source.stderr[-700:]
            assert json.loads(source.stdout)["result"]["gateway"]["pid"] == gateway.pid
            assert read_record(root / ".manager/control.json") == manager_record
            duplicate = subprocess.run([sys.executable, "-B", str(ROOT / "run.py"),
                "--config", str(config), "--port", str(port), "--codex-home", str(root / "no-codex-data"),
                "--ipc-path", ipc], capture_output=True, encoding="utf-8", errors="replace",
                timeout=15, **flags)
            assert duplicate.returncode == 0, duplicate.stderr[-700:]
            assert read_record(root / "gateway-control.json")["pid"] == gateway.pid
            first = phone("/api/login", {"username": "smoke", "password": "synthetic-password-123"})
            assert first[0] == 200
            devices = cli("devices")
            assert len(devices) == 1 and "csrf" not in devices[0]
            cli("revoke", "--id", devices[0]["id"], "--yes")
            assert phone("/api/auth", cookie=first[2])[1]["authenticated"] is False
            second = phone("/api/login", {"username": "smoke", "password": "synthetic-password-123"})
            assert second[0] == 200
            cli("account", "--username", "changed", "--password-stdin", "--yes", input="another-synthetic-password\n")
            assert phone("/api/auth", cookie=second[2])[1]["authenticated"] is False
            assert phone("/api/login", {"username": "changed", "password": "another-synthetic-password"})[0] == 200
            cli("stop", "--yes")
            assert gateway.wait(timeout=8) == 0
            gateway = subprocess.Popen([sys.executable, "-B", str(ROOT / "run.py"),
                "--config", str(config), "--port", str(port),
                "--codex-home", str(root / "no-codex-data"), "--ipc-path", ipc],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
            deadline = time.monotonic() + 15
            while True:
                record = read_record(root / "gateway-admin.json")
                if record and record["pid"] == gateway.pid:
                    break
                if gateway.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Source-first shared test gateway did not become ready")
                time.sleep(.1)
            assert cli("status")["gateway"]["pid"] == gateway.pid
            assert phone("/api/login", {"username": "changed", "password": "another-synthetic-password"})[0] == 200
            assert len(cli("devices")) == 1
            duplicate = subprocess.run([str(binary), "--gateway", "--config", str(config),
                "--port", str(port), "--codex-home", str(root / "no-codex-data"), "--ipc-path", ipc],
                capture_output=True, encoding="utf-8", errors="replace", timeout=15, **flags)
            assert duplicate.returncode == 0, duplicate.stderr[-700:]
            assert read_record(root / "gateway-control.json")["pid"] == gateway.pid
            cli("stop", "--yes")
            assert gateway.wait(timeout=8) == 0
            cli("appearance", "--mode", "dark")
            assert cli("status")["appearance"] == "dark"
            update = cli("check-update") if args.online else None
            gui = None
            if args.gui_binary:
                output = root / "gui-smoke.json"
                result = subprocess.run([str(args.gui_binary.resolve()), "gui", "--config", str(config),
                    "--smoke-gui", "--output", str(output)], timeout=75, **flags)
                if not output.exists():
                    runtime_log = root / ".manager/desktop-runtime.log"
                    detail = runtime_log.read_text(encoding="utf-8", errors="replace")[-3000:] if runtime_log.exists() else "No runtime log"
                    control = read_record(root / ".manager/control.json") or {}
                    if control.get("token"):
                        detail = detail.replace(control["token"], "[redacted]")
                    raise RuntimeError("Native GUI exited without a report (code %s): %s" % (result.returncode, detail))
                gui = json.loads(output.read_text(encoding="utf-8"))
                assert result.returncode == 0 and gui["passed"] and gui["width"] >= 700 and gui["theme"] == "dark", gui
            print(json.dumps({"passed": True, "packagedVersion": status["manager"],
                "deviceRevocation": True, "credentialRotation": True, "cooperativeStop": True,
                "sourceBinarySharedBackend": True, "bothGatewayStartOrders": True,
                "duplicateGatewayPrevented": True,
                "gui": gui, "onlineUpdate": update, "realAppOperations": 0}, ensure_ascii=False))
        finally:
            if gateway and gateway.poll() is None and read_record(root / "gateway-control.json"):
                request_stop(root)
                gateway.wait(timeout=8)
            control = read_record(root / ".manager/control.json")
            if control:
                started = windows_app.process_started(control["pid"])
                rpc(control, "manager/quit", {"confirm": True})
                deadline = time.monotonic() + 12
                while ((root / ".manager/control.json").exists() or
                       (started and windows_app.process_started(control["pid"]) == started)) and time.monotonic() < deadline:
                    time.sleep(.1)
                assert not (root / ".manager/control.json").exists()
                assert not started or windows_app.process_started(control["pid"]) != started


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    main()
