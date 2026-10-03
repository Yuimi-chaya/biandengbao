import hashlib
import hmac
import secrets
import threading
import time


def password_record(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000, dklen=32)
    return {"algorithm": "pbkdf2-sha256", "iterations": 600000, "salt": salt.hex(), "hash": digest.hex()}


class Auth:
    COOKIE = "codex_mobile_session"

    def __init__(self, config):
        self.config = config
        self.sessions = {}
        self.failures = {}
        self.lock = threading.Lock()
        self.generation = 0

    def login(self, username, password, address, user_agent=""):
        now = time.time()
        with self.lock:
            recent = [t for t in self.failures.get(address, []) if now - t < 300]
            self.failures[address] = recent
            if len(recent) >= 8:
                raise PermissionError("尝试次数过多，请 5 分钟后再试")
            # Reserve the attempt before hashing, so parallel attempts cannot bypass the limit.
            recent.append(now)
            config, generation = dict(self.config), self.generation
        if config.get("mode", "password") != "none":
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(config["salt"]), config["iterations"], dklen=32)
            valid = hmac.compare_digest(digest.hex(), config["hash"]) and hmac.compare_digest(username.encode(), config["username"].encode())
            if not valid:
                raise PermissionError("账号或密码不正确")
        with self.lock:
            if generation != self.generation:
                raise PermissionError("账号配置已更新，请重新登录")
            self.failures.pop(address, None)
            self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
            token = secrets.token_urlsafe(32)
            session = {"csrf": secrets.token_urlsafe(32), "expires": now + 12 * 3600,
                       "id": secrets.token_hex(16), "createdAt": now, "lastSeen": now,
                       "address": str(address)[:100], "userAgent": str(user_agent)[:300]}
            self.sessions[token] = session
            return token, session

    def get(self, token):
        with self.lock:
            session = self.sessions.get(token)
            if session and session["expires"] > time.time():
                session["lastSeen"] = time.time()
                return session
            self.sessions.pop(token, None)
            return None

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)

    def devices(self):
        with self.lock:
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
            return [{k: v for k, v in session.items() if k != "csrf"}
                    for session in sorted(self.sessions.values(),
                                          key=lambda item: item["lastSeen"], reverse=True)]

    def revoke(self, device_id=None):
        with self.lock:
            before = len(self.sessions)
            self.sessions = {token: value for token, value in self.sessions.items()
                             if device_id is not None and value["id"] != device_id}
            return before - len(self.sessions)

    def replace_config(self, config, persist):
        # Invalidation and persistence are atomic with respect to in-flight logins.
        with self.lock:
            persist()
            self.config = dict(config)
            self.generation += 1
            self.sessions.clear()
            self.failures.clear()
