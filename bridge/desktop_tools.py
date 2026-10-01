"""Restricted access to the running App's existing local tools channel."""
import json
import os
import struct
import threading
import uuid
from pathlib import Path

from .ipc import DesktopIPC, IPCError
from .transport import connect_stream


class DesktopTools:
    ALLOWED = {"list_projects", "create_thread", "navigate_to_codex_page"}
    MAX_FRAME = 8 * 1024 * 1024

    def __init__(self, caller=None, pipe=None):
        self.caller = caller or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
        self.pipe = pipe or os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")
        self.catalog = None
        self.lock = threading.RLock()

    def _path(self):
        if self.pipe:
            return self.pipe
        if os.name == "nt":
            prefix = "\\\\.\\pipe\\"
            candidates = [prefix + name for name in os.listdir(prefix)
                          if name.startswith("codex-browser-use-")]
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise IPCError("发现多个桌面工具通道；请从原 Codex App 聊天重新启动网关")
        raise IPCError("未找到桌面工具通道；请从 Codex App 重新启动网关")

    def _request(self, method, params, timeout=30):
        stream = connect_stream(self._path())
        expired = threading.Event()

        def expire():
            expired.set()
            stream.close()

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        request_id = str(uuid.uuid4())
        try:
            body = json.dumps({"id": request_id, "jsonrpc": "2.0", "method": method, "params": params}).encode()
            stream.sendall(struct.pack("<I", len(body)) + body)
            size = struct.unpack("<I", DesktopIPC._exact(stream, 4))[0]
            if not 0 < size <= self.MAX_FRAME:
                raise IPCError("桌面工具返回的数据过大")
            response = json.loads(DesktopIPC._exact(stream, size))
            if response.get("id") != request_id:
                raise IPCError("桌面工具响应不匹配")
            if "error" in response:
                raise IPCError(response["error"].get("message", "桌面工具操作失败"))
            return response["result"]
        except (OSError, EOFError, ValueError, KeyError) as exc:
            message = "桌面工具响应超时；请先检查操作结果，勿重复提交" if expired.is_set() else "桌面工具连接已断开"
            raise IPCError(message) from exc
        finally:
            timer.cancel()
            stream.close()

    def tools(self):
        with self.lock:
            if self.catalog is None:
                result = self._request("tools/list", {"threadStartKind": "all"}, timeout=8)
                self.catalog = {tool["name"]: tool for tool in result["tools"] if tool["name"] in self.ALLOWED}
            return self.catalog

    def call(self, name, arguments):
        if name not in self.ALLOWED:
            raise ValueError("不允许此桌面工具")
        tool = self.tools().get(name)
        if not tool or not self.caller:
            raise IPCError("此 App 未提供所需的桌面工具或调用上下文")
        result = self._request("tools/call", {
            "arguments": arguments, "callerSource": "codex", "namespace": tool["namespace"],
            "threadId": self.caller, "tool": name,
            "callId": "mobile-" + str(uuid.uuid4()), "turnId": "mobile-" + str(uuid.uuid4()),
        }, timeout=45 if name == "create_thread" else 10)
        texts = [item["text"] for item in result.get("contentItems", []) if item.get("type") == "inputText"]
        if not result.get("success"):
            raise IPCError("\n".join(texts) or "桌面未完成操作")
        for text in texts:
            try:
                return json.loads(text)
            except ValueError:
                continue
        raise IPCError("桌面工具未返回可识别的结果")
