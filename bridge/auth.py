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

    def login(self, username, password, address):
        now = time.time()
        with self.lock:
            recent = [t for t in self.failures.get(address, []) if now - t < 300]
            self.failures[address] = recent
            if len(recent) >= 8:
                raise PermissionError("尝试次数过多，请 5 分钟后再试")
            # Reserve the attempt before hashing, so parallel attempts cannot bypass the limit.
            recent.append(now)
        if self.config.get("mode", "password") != "none":
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(self.config["salt"]), self.config["iterations"], dklen=32)
            valid = hmac.compare_digest(digest.hex(), self.config["hash"]) and hmac.compare_digest(username.encode(), self.config["username"].encode())
            if not valid:
                raise PermissionError("账号或密码不正确")
        with self.lock:
            self.failures.pop(address, None)
            self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
            token = secrets.token_urlsafe(32)
            session = {"csrf": secrets.token_urlsafe(32), "expires": now + 12 * 3600}
            self.sessions[token] = session
            return token, session

    def get(self, token):
        with self.lock:
            session = self.sessions.get(token)
            if session and session["expires"] > time.time():
                return session
            self.sessions.pop(token, None)
            return None

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)
