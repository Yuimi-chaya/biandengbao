"""Gateway-side management, reachable only via a local capability."""
import json
from pathlib import Path

from .auth import password_record
from .autostart import write_json


def account_record(body):
    username, password = body.get("username"), body.get("password")
    if not isinstance(username, str) or not username.strip() or len(username) > 100:
        raise ValueError("账号名需为 1–100 个字符")
    if not isinstance(password, str) or not 12 <= len(password) <= 1000:
        raise ValueError("密码需为 12–1000 个字符")
    return {"mode": "password", "username": username.strip(), **password_record(password)}


def save_account(path, auth):
    path = Path(path)
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"origins": []}
    config["auth"] = auth
    write_json(path, config)
    (path.parent / "首次登录.txt").unlink(missing_ok=True)


class GatewayAdmin:
    def __init__(self, server, config_path, status):
        self.server, self.config_path, self.status = server, config_path, status

    def __call__(self, action, body):
        if action == "status":
            return {**self.status(), "username": self.server.auth.config.get("username"),
                    "devices": self.server.auth.devices()}
        if action == "devices":
            return self.server.auth.devices()
        if action == "devices/revoke":
            device_id = body.get("id")
            if not isinstance(device_id, str) or len(device_id) != 32:
                raise ValueError("无效的登录设备标识")
            return {"revoked": self.server.auth.revoke(device_id)}
        if action == "devices/revoke-all":
            if body.get("confirm") is not True:
                raise ValueError("请确认退出所有设备")
            return {"revoked": self.server.auth.revoke()}
        if action == "account":
            auth = account_record(body)
            self.server.auth.replace_config(auth, lambda: save_account(self.config_path, auth))
            return {"username": auth["username"], "sessionsRevoked": True}
        raise ValueError("未知的网关管理操作")
