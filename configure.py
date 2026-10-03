#!/usr/bin/env python3
"""Local deployment configuration; never exposed through the phone gateway."""
import argparse
import json
import os
import sys
from pathlib import Path

from bridge.autostart import supported
from bridge.local_control import rpc
from bridge.service_profile import default_config
from manager import connect

ROOT = Path(__file__).resolve().parent


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="便蹬宝 · 本机配置")
    parser.add_argument("--autostart", choices=["enable", "disable", "status", "remove"],
                        help="可选：启用、关闭、查看或移除 Windows/macOS 登录自启动")
    parser.add_argument("--config", type=Path, default=default_config())
    parser.add_argument("--port", type=int, help="启用时保存的局域网端口，首次默认 8787")
    parser.add_argument("--codex-home", type=Path, help="启用时保存的 Codex 数据目录")
    parser.add_argument("--network-mode", choices=["lan", "tunnel", "proxy"], help="保存自启动连接模式；默认保留原配置")
    parser.add_argument("--cloudflared", type=Path, help="临时隧道的程序绝对路径")
    parser.add_argument("--origin", help="已有反向代理的 HTTPS 源")
    parser.add_argument("--yes", action="store_true", help="允许更改运行配置时只重启网关")
    args = parser.parse_args()
    mode = args.autostart
    if not mode:
        if not supported():
            print("登录自启动支持 Windows 和 macOS；其他平台仍可手动启动。")
            return
        if not sys.stdin.isatty():
            parser.error("请指定 --autostart enable、disable、status 或 remove")
        try:
            state = rpc(connect(args.config), "status")["autostart"]
        except (RuntimeError, OSError, ValueError) as error:
            parser.error(str(error))
        print("登录自启动：" + ("已启用" if state["enabled"] else "未启用"))
        print("1. 启用（等待 Codex App 后恢复保存的连接模式）")
        print("2. 关闭自启动  3. 查看状态  4. 移除任务  回车取消")
        try:
            answer = input("选择：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消；未修改配置。")
            return
        if not answer:
            return
        mode = {"1": "enable", "2": "disable", "3": "status", "4": "remove"}.get(answer)
        if not mode:
            parser.error("无效选项；未修改配置")
    try:
        connection = connect(args.config)
        if mode == "enable":
            current = rpc(connection, "status")["settings"]
            settings = {}
            if args.port is not None:
                settings["port"] = args.port
            if args.codex_home is not None:
                settings["codexHome"] = str(args.codex_home.resolve())
            caller = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
            if caller and not current.get("callerThread"):
                settings["callerThread"] = caller
            if args.network_mode:
                network = {"mode": args.network_mode}
                if args.network_mode == "tunnel":
                    network["cloudflared"] = str(args.cloudflared.resolve()) if args.cloudflared else ""
                elif args.network_mode == "proxy":
                    network["origin"] = args.origin or ""
                settings["network"] = network
            settings = {key: value for key, value in settings.items() if current.get(key) != value}
            if settings:
                settings["confirmRestart"] = args.yes
                rpc(connection, "settings", settings, timeout=60)
            state = rpc(connection, "autostart", {"enabled": True}, timeout=60)
        elif mode == "disable":
            state = rpc(connection, "autostart", {"enabled": False}, timeout=60)
        elif mode == "remove":
            state = rpc(connection, "autostart/remove", {"confirm": True}, timeout=60)
        else:
            state = rpc(connection, "status")["autostart"]
    except (RuntimeError, OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if mode == "enable":
        print("已启用：登录电脑后等待 Codex App，并恢复保存的连接模式。不会开启免密。")
    elif mode in ("disable", "remove"):
        print("已关闭自启动；当前网关和 Codex App 不受影响。")


if __name__ == "__main__":
    main()
