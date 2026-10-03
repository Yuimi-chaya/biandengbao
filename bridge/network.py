"""Network profiles shared by the manager, CLI and autostart."""
from pathlib import Path
from urllib.parse import urlsplit


def validate_network(value, require_binary=False):
    if not isinstance(value, dict) or set(value) - {"mode", "cloudflared", "origin"}:
        raise ValueError("无效的连接配置")
    mode = value.get("mode", "lan")
    if mode not in ("lan", "tunnel", "proxy"):
        raise ValueError("连接模式必须为 lan、tunnel 或 proxy")
    result = {"mode": mode}
    if mode == "tunnel":
        executable = value.get("cloudflared", "")
        if not isinstance(executable, str) or not Path(executable).is_absolute():
            raise ValueError("请选择 cloudflared 可执行文件的绝对路径")
        if require_binary and not Path(executable).is_file():
            raise ValueError("cloudflared 文件不存在；管理端不会自动下载安装")
        result["cloudflared"] = executable
    if mode == "proxy":
        origin = value.get("origin", "")
        if not isinstance(origin, str):
            raise ValueError("请输入 HTTPS 域名")
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.path or
                parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("反向代理地址须为不含路径的 HTTPS 源")
        _ = parsed.port
        result["origin"] = origin
    return result


def network_arguments(value):
    value = validate_network(value, require_binary=True)
    if value["mode"] == "lan":
        return ["--lan"]
    if value["mode"] == "tunnel":
        return ["--tunnel", "--cloudflared", value["cloudflared"]]
    return ["--origin", value["origin"]]
