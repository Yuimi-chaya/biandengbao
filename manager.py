#!/usr/bin/env python3
"""Desktop manager and JSON CLI. GUI dependencies are optional for scripts."""
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from bridge import autostart
from bridge.lifecycle import read_record
from bridge.local_control import LocalServer, rpc
from bridge.manager import Manager, default_config, runner, spawn

ROOT = Path(__file__).resolve().parent


def serve(config):
    config = Path(config).resolve()
    folder = config.parent / ".manager"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / "control.json"
    lock_profile = SimpleNamespace(config=folder / "manager.json", control=folder)
    with autostart.worker_lock(lock_profile) as acquired:
        if not acquired:
            return 0
        manager = Manager(ROOT, config)
        assets = {"/": (ROOT / "manager-web/index.html", "text/html; charset=utf-8"),
                  "/app.js": (ROOT / "manager-web/app.js", "text/javascript; charset=utf-8"),
                  "/style.css": (ROOT / "manager-web/style.css", "text/css; charset=utf-8"),
                  "/lucide.js": (ROOT / "web/vendor/lucide.js", "text/javascript; charset=utf-8")}

        def dispatch(action, body):
            if action == "manager/quit":
                if body.get("confirm") is not True:
                    raise ValueError("请确认退出管理接口")
                threading.Thread(target=server.shutdown, daemon=True).start()
                return {"stopped": True, "gatewayUnaffected": True}
            if action == "identity":
                return {"root": str(ROOT), "config": str(config)}
            return manager.dispatch(action, body)

        server = LocalServer(dispatch, assets)
        record = {"pid": os.getpid(), "port": server.server_port, "token": server.token,
                  "root": str(ROOT), "config": str(config)}
        autostart.write_json(path, record)
        try:
            server.serve_forever(poll_interval=.25)
        finally:
            manager.closed.set()
            server.server_close()
            if read_record(path) == record:
                path.unlink(missing_ok=True)
    return 0


def connect(config):
    config = Path(config).resolve()
    path = config.parent / ".manager/control.json"
    record = read_record(path)
    if record:
        try:
            identity = rpc(record, "identity", timeout=1)
        except (OSError, ValueError, RuntimeError):
            identity = None
        if identity:
            if identity != {"root": str(ROOT), "config": str(config)}:
                raise RuntimeError("另一份安装正在管理此配置；请先退出该管理端后台，不会自动接管")
            return record
    process = spawn(runner(ROOT, "serve", "--config", config), ROOT,
                    config.parent / ".manager/manager.log")
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        record = read_record(path)
        if record:
            try:
                if rpc(record, "identity", timeout=.5) == {"root": str(ROOT), "config": str(config)}:
                    return record
            except (OSError, ValueError, RuntimeError):
                pass
        if process.poll() is not None and process.returncode != 0:
            break
        time.sleep(.1)
    raise RuntimeError("管理后台未就绪；请检查配置目录的 .manager/manager.log，不要重复启动")


def gui(record, smoke_output=None):
    try:
        import webview
    except ImportError as error:
        raise RuntimeError("源码 GUI 需要 pywebview；发布版已内置。脚本命令不需要此依赖") from error
    preference = read_record(Path(record["config"]).parent / ".manager/preferences.json")
    appearance = preference.get("appearance") if isinstance(preference, dict) else "system"
    appearance = appearance if appearance in ("system", "light", "dark") else "system"
    url = "http://127.0.0.1:%d/?appearance=%s#%s" % (record["port"], appearance, record["token"])
    window = webview.create_window("便蹬宝", url, width=1120, height=790, min_size=(760, 560),
                                   background_color="#202226" if appearance == "dark" else "#F7F8FA", text_select=True)
    def smoke():
        result = {"passed": False}
        try:
            for _ in range(60):
                time.sleep(.25)
                result = window.evaluate_js("({passed: document.getElementById('health')?.textContent === '管理端已连接', width: innerWidth, height: innerHeight, title: document.title, theme: document.documentElement.dataset.theme})")
                if result and result.get("passed"):
                    break
            Path(smoke_output).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        finally:
            window.destroy()
    webview.start(smoke if smoke_output else None, gui="edgechromium" if sys.platform == "win32" else None, private_mode=True)


def main():
    parser = argparse.ArgumentParser(description="便蹬宝管理端；所有脚本命令返回 JSON")
    parser.add_argument("command", nargs="?", default="gui", choices=[
        "gui", "serve", "status", "contexts", "start", "stop", "devices",
        "revoke", "revoke-all", "account", "configure", "autostart", "appearance", "check-update", "quit-manager"])
    parser.add_argument("--config", type=Path, default=default_config())
    parser.add_argument("--yes", action="store_true", help="确认退出设备、停止服务或重启网关")
    parser.add_argument("--id", help="登录设备会话 ID")
    parser.add_argument("--username")
    parser.add_argument("--password-stdin", action="store_true", help="从标准输入读取密码，不放入进程参数")
    parser.add_argument("--json-stdin", action="store_true", help="从标准输入读取设置 JSON")
    parser.add_argument("--enabled", choices=["true", "false"])
    parser.add_argument("--mode", choices=["system", "light", "dark"], help="管理端外观")
    parser.add_argument("--output", type=Path, help="可选：将 JSON 结果写入指定文件")
    parser.add_argument("--smoke-gui", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.command == "serve":
        return serve(args.config)
    if args.command == "gui":
        if args.smoke_gui and not args.output:
            parser.error("GUI smoke verification requires --output")
        gui(connect(args.config), args.output if args.smoke_gui else None)
        return 0
    actions = {"start": "service/start", "stop": "service/stop",
               "revoke": "devices/revoke", "revoke-all": "devices/revoke-all",
               "configure": "settings", "check-update": "updates/check", "quit-manager": "manager/quit"}
    body = {}
    if args.command == "account":
        if not args.password_stdin or not args.username or not args.yes:
            parser.error("account 需要 --username、--password-stdin 和 --yes")
        body = {"username": args.username, "password": sys.stdin.readline(4096).rstrip('\r\n'), "confirm": True}
    elif args.command == "configure":
        if not args.json_stdin:
            parser.error("configure 需要 --json-stdin")
        body = json.loads(sys.stdin.read(65537))
        if not isinstance(body, dict):
            parser.error("设置必须为 JSON 对象")
        body["confirmRestart"] = args.yes
    elif args.command == "appearance":
        if args.mode is None:
            parser.error("appearance 需要 --mode system、light 或 dark")
        body = {"mode": args.mode}
    elif args.command == "autostart":
        if args.enabled is None:
            parser.error("autostart 需要 --enabled true 或 false")
        body = {"enabled": args.enabled == "true"}
    elif args.command == "revoke":
        if not args.id or not args.yes:
            parser.error("revoke 需要 --id 和 --yes")
        body = {"id": args.id}
    elif args.command in ("stop", "revoke-all", "quit-manager"):
        if not args.yes:
            parser.error(args.command + " 需要 --yes")
        body = {"confirm": True}
    result = rpc(connect(args.config), actions.get(args.command, args.command), body, timeout=60)
    output = json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output + '\n', encoding="utf-8")
    if sys.stdout:
        print(output)
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    hidden_log = None
    if not sys.stdout or not sys.stderr:
        data = default_config().parent / ".manager"
        if "--config" in sys.argv:
            data = Path(sys.argv[sys.argv.index("--config") + 1]).resolve().parent / ".manager"
        elif len(sys.argv) > 2 and sys.argv[1] == "--worker":
            data = Path(sys.argv[2]).resolve().parent
        data.mkdir(parents=True, exist_ok=True)
        hidden_log = open(data / "desktop-runtime.log", "a", encoding="utf-8", buffering=1)
    if not sys.stdout:
        sys.stdout = hidden_log
    if not sys.stderr:
        sys.stderr = hidden_log
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--gateway":
            sys.argv.pop(1)
            from run import main as gateway_main
            gateway_main()
        elif len(sys.argv) > 2 and sys.argv[1] == "--worker":
            profile = autostart.profile_from_settings(ROOT, Path(sys.argv[2]))
            try:
                autostart.run_worker(profile)
            except Exception as error:
                profile.write_status("error", error=str(error)[:300])
                raise
        else:
            sys.exit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
