"""Same-origin HTTP/SSE gateway. Desktop RPC is never exposed directly."""
import hmac
import gzip
import hashlib
import json
import logging
import mimetypes
import re
import socket
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, urlsplit, quote, unquote

from .auth import Auth
from .ipc import IPCError
from .catalog import CatalogError
from .remote import RemoteUnavailable
from .uploads import MAX_FILE
from .history import state_delta

LOG = logging.getLogger(__name__)
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/enhancements.js": ("enhancements.js", "text/javascript; charset=utf-8"),
          "/enhancements.css": ("enhancements.css", "text/css; charset=utf-8"),
          "/chat.css": ("chat.css", "text/css; charset=utf-8"),
          "/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
          "/thread-ui.js": ("thread-ui.js", "text/javascript; charset=utf-8"),
          "/reading.js": ("reading.js", "text/javascript; charset=utf-8"),
          "/connection.js": ("connection.js", "text/javascript; charset=utf-8"),
          "/workspace.css": ("workspace.css", "text/css; charset=utf-8"),
          "/vendor/marked.js": ("vendor/marked.js", "text/javascript; charset=utf-8"),
          "/vendor/purify.js": ("vendor/purify.js", "text/javascript; charset=utf-8"),
          "/vendor/highlight.js": ("vendor/highlight.js", "text/javascript; charset=utf-8"),
          "/vendor/lucide.js": ("vendor/lucide.js", "text/javascript; charset=utf-8"),
          "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/icon.svg": ("icon.svg", "image/svg+xml")}
THREAD_ROUTE = re.compile(r"^/api/sessions/([0-9a-f-]{36})(?:/(events|send|stop|history|respond|reconnect|activate|queue|catalog|settings|poll|compact|upload))?$")


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, bridge, config, web_dir):
        self.bridge = bridge
        self.auth = Auth(config["auth"])
        self.origins = set(config["origins"])
        self.hosts = {urlsplit(o).netloc for o in self.origins}
        self.secure_hosts = {urlsplit(o).netloc for o in self.origins if o.startswith("https://")}
        self.web_dir = Path(web_dir)
        self.slots = threading.BoundedSemaphore(48)
        self.upload_slots = threading.BoundedSemaphore(2)
        self.static_cache = {}
        self.static_lock = threading.Lock()
        super().__init__(address, Handler)

    def server_bind(self):
        # HTTPServer resolves the listening IP with getfqdn(), which can stall
        # startup on machines without working reverse DNS. Origins are explicit.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        LOG.warning("HTTP connection failed")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CodexMobile/0.1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, format, *args):
        # Request bodies, cookies, query strings and conversation IDs are private.
        pass

    def headers_common(self, cache="no-store"):
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def accepts_gzip(self):
        for part in self.headers.get("Accept-Encoding", "").lower().split(","):
            values = part.strip().split(";")
            if values[0] == "gzip":
                try:
                    return float(next((v.strip()[2:] for v in values[1:] if v.strip().startswith("q=")), "1")) > 0
                except ValueError:
                    return False
        return False

    def static(self, name, content_type):
        path = self.server.web_dir / name
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        with self.server.static_lock:
            cached = self.server.static_cache.get(name)
            if not cached or cached[0] != stamp:
                body = path.read_bytes()
                cached = (stamp, body, gzip.compress(body, compresslevel=5, mtime=0),
                          'W/"' + hashlib.sha256(body).hexdigest() + '"')
                self.server.static_cache[name] = cached
        _, raw, compressed, etag = cached
        cached_resource = name != "index.html"
        not_modified = cached_resource and self.headers.get("If-None-Match") == etag
        encoded = self.accepts_gzip()
        body = compressed if encoded else raw
        self.send_response(304 if not_modified else 200)
        self.headers_common("private, no-cache" if cached_resource else "no-store")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("ETag", etag)
        self.send_header("Content-Type", content_type)
        if not not_modified:
            if encoded:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not not_modified:
            self.wfile.write(body)

    def output(self, status, data, content_type="application/json; charset=utf-8", cookie=None):
        body = json.dumps(data, ensure_ascii=False).encode() if not isinstance(data, bytes) else data
        encoded = len(body) > 1024 and self.accepts_gzip()
        if encoded:
            body = gzip.compress(body, compresslevel=3, mtime=0)
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Vary", "Accept-Encoding")
        if encoded:
            self.send_header("Content-Encoding", "gzip")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def cookie(self, token, clear=False):
        secure = "; Secure" if self.headers.get("Host") in self.server.secure_hosts else ""
        return f"{Auth.COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={0 if clear else 43200}{secure}"

    def token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[Auth.COOKIE].value if Auth.COOKIE in cookie else ""
        except Exception:
            return ""

    def check_request(self, write=False):
        if len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") not in self.server.hosts:
            raise PermissionError("此访问地址未在网关配置中允许")
        origin = self.headers.get("Origin")
        if (origin and origin not in self.server.origins) or (write and not origin):
            raise PermissionError("不允许跨站请求")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("不允许跨站请求")

    def authorized(self, write=False):
        session = self.server.auth.get(self.token())
        if not session:
            self.close_connection = True
            self.output(401, {"error": "请登录", "code": "unauthenticated"})
            return None
        if write and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
            raise PermissionError("登录验证已失效，请刷新页面")
        return session

    def read_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持分块请求体")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("需要 JSON 请求")
        sizes = self.headers.get_all("Content-Length", [])
        if len(sizes) != 1 or not sizes[0].isdigit():
            raise ValueError("请求长度无效")
        size = int(sizes[0])
        if not 0 < size <= 512000:
            raise ValueError("请求过大或为空")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("请求内容必须为对象")
        return value

    def do_GET(self):
        self.handle_method(False)

    def do_POST(self):
        self.handle_method(True)

    def handle_method(self, write):
        try:
            self.check_request(write)
            path = urlsplit(self.path).path
            query = parse_qs(urlsplit(self.path).query)
            if not write and path in STATIC:
                name, content_type = STATIC[path]
                return self.static(name, content_type)
            if not write and path == "/api/auth":
                session = self.server.auth.get(self.token())
                return self.output(200, {"authenticated": bool(session), "csrf": session["csrf"] if session else None,
                                         "passwordless": self.server.auth.config.get("mode") == "none",
                                         "transport": "poll" if self.headers.get("Host", "").endswith(".trycloudflare.com") else "sse"})
            if write and path == "/api/login":
                body = self.read_json()
                username, password = body.get("username", ""), body.get("password", "")
                if not isinstance(username, str) or not isinstance(password, str) or len(username) > 200 or len(password) > 1000:
                    raise ValueError("账号或密码格式不正确")
                token, session = self.server.auth.login(username, password, self.client_address[0])
                self.server.auth.logout(self.token())
                return self.output(200, {"csrf": session["csrf"]}, cookie=self.cookie(token))
            auth = self.authorized(write)
            if not auth:
                return
            if write and path == "/api/logout":
                self.read_json()
                self.server.auth.logout(self.token())
                return self.output(200, {"ok": True}, cookie=self.cookie("", clear=True))
            if not write and path == "/api/sessions":
                rows = self.server.bridge.list(query=query.get("q", [""])[0][:200], offset=max(0, int(query.get("offset", [0])[0])), archived=query.get("archived", ["false"])[0] == "true")
                return self.output(200, {"sessions": rows, "unavailableHosts": self.server.bridge.host_errors})
            if not write and path == "/api/create-options":
                return self.output(200, self.server.bridge.creation_options())
            if write and path == "/api/sessions":
                return self.output(200, self.server.bridge.create(self.read_json()))
            bridge = self.server.bridge.for_host(query.get("host", ["local"])[0])
            if not write and path == "/api/contexts":
                ids = query.get("ids", [""])[0].split(",")
                return self.output(200, {"contexts": bridge.contexts(ids)})
            file_match = re.fullmatch(r"/api/sessions/([0-9a-f-]{36})/files/([a-f0-9]{64})", path)
            if not write and file_match:
                return self.download(bridge, *file_match.groups())
            match = THREAD_ROUTE.fullmatch(path)
            if not match:
                return self.output(404, {"error": "页面不存在"})
            thread_id, action = match.groups()
            if not write:
                if action is None:
                    return self.output(200, bridge.view(thread_id, background=True))
                if action == "catalog":
                    return self.output(200, bridge.catalog(thread_id, refresh=query.get("refresh") == ["true"]))
                if action == "history":
                    return self.output(200, bridge.history_page(
                        thread_id, before=query.get("before", [None])[0],
                        turn_id=query.get("turn", [None])[0], message_id=query.get("message", [None])[0]))
                if action == "poll":
                    return self.poll(bridge, thread_id, int(query.get("after", ["-1"])[0]))
                if action == "events":
                    return self.stream(bridge, thread_id, delta=query.get("delta") == ["true"])
                return self.output(404, {"error": "接口不存在"})
            if action == "upload":
                sizes = self.headers.get_all("Content-Length", [])
                if self.headers.get("Transfer-Encoding") or len(sizes) != 1 or not sizes[0].isdigit() or not 0 < int(sizes[0]) <= MAX_FILE:
                    raise ValueError("附件须为 1 字节至 10 MB")
                if self.headers.get_content_type() != "application/octet-stream":
                    raise ValueError("附件请求格式无效")
                if not self.server.upload_slots.acquire(blocking=False):
                    raise ValueError("其他附件正在上传，请稍后重试")
                try:
                    size = int(sizes[0])
                    data = self.rfile.read(size)
                    if len(data) != size:
                        raise ValueError("附件上传不完整")
                    return self.output(200, bridge.upload(thread_id, self.token(), unquote(self.headers.get("X-Filename", "")), data))
                finally:
                    self.server.upload_slots.release()
            body = self.read_json()
            if action == "send":
                result = bridge.send(thread_id, body.get("text"), body.get("id", ""), body.get("mode", "send"), body.get("skills", []),
                                     body.get("attachments", []), self.token())
            elif action == "settings":
                result = bridge.settings(thread_id, body.get("model"), body.get("effort"))
            elif action == "compact":
                result = bridge.compact(thread_id, body.get("id", ""))
            elif action == "stop":
                expected = body.get("expectedTurnId")
                if not isinstance(expected, str) or not expected:
                    raise ValueError("请确认要停止的任务轮次")
                result = bridge.interrupt(thread_id, expected)
            elif action == "history":
                result = bridge.full_history(thread_id)
            elif action == "respond":
                response = body.get("response")
                if not isinstance(response, dict):
                    raise ValueError("请求回应格式不正确")
                result = bridge.respond(thread_id, body.get("requestId"), response)
            elif action == "reconnect":
                result = bridge.view(thread_id, background=True, force=True)
            elif action == "activate":
                result = bridge.activate(thread_id)
            elif action == "queue":
                result = bridge.cancel_queued(thread_id, body.get("id", ""))
            else:
                return self.output(404, {"error": "接口不存在"})
            self.output(200, result)
        except PermissionError as exc:
            self.close_connection = True
            self.output(403, {"error": str(exc)})
        except KeyError:
            self.close_connection = True
            self.output(404, {"error": "找不到这个会话"})
        except ValueError as exc:
            self.close_connection = True
            self.output(400, {"error": str(exc)})
        except (IPCError, CatalogError, RemoteUnavailable) as exc:
            self.close_connection = True
            self.output(409, {"error": str(exc), "code": "desktop_unavailable"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            self.close_connection = True
        except Exception:
            LOG.exception("Gateway operation failed")
            self.close_connection = True
            self.output(500, {"error": "网关操作失败，请查看本机日志"})

    def download(self, bridge, thread_id, artifact_id):
        artifact = bridge.artifact(thread_id, artifact_id)
        data = artifact["path"].read_bytes()
        self.send_response(200)
        self.headers_common()
        mime = mimetypes.guess_type(artifact["name"])[0] if artifact["image"] else "application/octet-stream"
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        disposition = "inline" if artifact["image"] else "attachment"
        self.send_header("Content-Disposition", disposition + "; filename*=UTF-8''" + quote(artifact["name"]))
        self.end_headers()
        self.wfile.write(data)

    def poll(self, bridge, thread_id, after):
        session = bridge.session(thread_id, background=True)
        with session.condition:
            session.viewers += 1
        try:
            with session.condition:
                session.condition.wait_for(lambda: session.sequence != after or bridge.closed.is_set(), timeout=12)
                changed = session.sequence != after
            if not self.authorized():
                return
            self.output(200, {"state": bridge.view(thread_id, attach=False, background=True) if changed else None})
        finally:
            with session.condition:
                session.viewers -= 1
                session.touched = time.monotonic()

    def stream(self, bridge, thread_id, delta=False):
        session = bridge.session(thread_id, background=True)
        token = self.token()
        with session.condition:
            session.viewers += 1
        self.send_response(200)
        self.headers_common()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        sequence = -1
        previous = None
        try:
            while not bridge.closed.is_set():
                if not self.server.auth.get(token):
                    self.wfile.write(b'event: logout\ndata: {}\n\n')
                    self.wfile.flush()
                    break
                with session.condition:
                    session.condition.wait_for(lambda: session.sequence != sequence or bridge.closed.is_set(), timeout=12)
                    updated = session.sequence != sequence
                    sequence = session.sequence
                if updated:
                    view = bridge.view(thread_id, attach=False, background=True)
                    payload = json.dumps(state_delta(previous, view) if delta and previous else view,
                                         ensure_ascii=False, separators=(",", ":"))
                    previous = view
                    sequence = view["sequence"]
                    self.wfile.write(f"id: {sequence}\nevent: state\ndata: {payload}\n\n".encode())
                else:
                    self.wfile.write(b"event: heartbeat\ndata: {}\n\n")
                self.wfile.flush()
                bridge.closed.wait(0.2)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            pass
        finally:
            with session.condition:
                session.viewers -= 1
                session.touched = time.monotonic()
