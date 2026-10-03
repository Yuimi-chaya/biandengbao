"""Local capability RPC. Not reachable through the phone gateway or tunnel."""
import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, dispatch, assets=None, token=None):
        self.dispatch = dispatch
        self.assets = assets or {}
        self.token = token or secrets.token_urlsafe(32)
        self.slots = threading.BoundedSemaphore(12)
        super().__init__(("127.0.0.1", 0), LocalHandler)
        self.origin = "http://127.0.0.1:" + str(self.server_port)

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass


class LocalHandler(BaseHTTPRequestHandler):
    server_version = "BiandengbaoManager"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def output(self, status, value, content_type="application/json; charset=utf-8"):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'")
        self.end_headers()
        self.wfile.write(body)

    def guard(self, authenticated):
        if (self.client_address[0] != "127.0.0.1" or
                len(self.headers.get_all("Host", [])) != 1 or
                self.headers.get("Host") != self.server.origin.removeprefix("http://") or
                self.headers.get("Sec-Fetch-Site") == "cross-site" or
                self.headers.get("Origin", self.server.origin) != self.server.origin):
            raise PermissionError("仅允许本机同源管理请求")
        if authenticated and not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + self.server.token):
            raise PermissionError("管理授权已失效，请重新打开管理端")

    def do_GET(self):
        try:
            self.guard(False)
            asset = self.server.assets.get(urlsplit(self.path).path)
            if not asset:
                return self.output(404, {"ok": False, "error": "Not found"})
            path, mime = asset
            self.output(200, Path(path).read_bytes(), mime)
        except PermissionError as error:
            self.output(403, {"ok": False, "error": str(error)})
        except OSError:
            self.output(404, {"ok": False, "error": "资源不可用"})

    def do_POST(self):
        try:
            self.guard(True)
            if not self.path.startswith("/api/v1/") or "?" in self.path:
                return self.output(404, {"ok": False, "error": "Not found"})
            if (self.headers.get("Transfer-Encoding") or
                    len(self.headers.get_all("Content-Length", [])) != 1 or
                    self.headers.get_content_type() != "application/json"):
                raise ValueError("仅接受定长 JSON 请求")
            size = int(self.headers["Content-Length"])
            if not 0 < size <= 65536:
                raise ValueError("请求大小无效")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("请求未完整接收")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("请求必须为 JSON 对象")
            result = self.server.dispatch(self.path[len("/api/v1/"):], body)
            self.output(200, {"ok": True, "result": result})
        except PermissionError as error:
            self.output(403, {"ok": False, "error": str(error)})
        except (ValueError, RuntimeError, OSError) as error:
            self.output(400, {"ok": False, "error": str(error)[:500]})
        except Exception:
            self.output(500, {"ok": False, "error": "本机管理操作失败，请检查本机日志"})


def rpc(record, action, data=None, timeout=20):
    if (not isinstance(record, dict) or type(record.get("port")) is not int or
            not 1 <= record["port"] <= 65535 or not isinstance(record.get("token"), str)):
        raise RuntimeError("本机管理接口尚未就绪")
    request = Request("http://127.0.0.1:%d/api/v1/%s" % (record["port"], action),
                      json.dumps(data or {}).encode(), method="POST",
                      headers={"Authorization": "Bearer " + record["token"],
                               "Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
            value = json.loads(response.read(2 * 1024 * 1024))
    except HTTPError as error:
        try:
            message = json.loads(error.read(65536)).get("error", "管理请求失败")
        except (ValueError, AttributeError):
            message = "管理请求失败"
        raise RuntimeError(message) from error
    if not value.get("ok"):
        raise RuntimeError(value.get("error", "管理请求失败"))
    return value["result"]
