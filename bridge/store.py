"""Read-only discovery and history for existing desktop chats."""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


class SessionStore:
    def __init__(self, codex_home):
        self.home = Path(codex_home).resolve()
        self.usage_cache = {}
        self.summary_cache = {}

    def _rollout_path(self, value):
        text = str(value)
        if os.name == "nt" and text.startswith("\\\\?\\"):
            text = "\\\\" + text[8:] if text[4:8].upper() == "UNC\\" else text[4:]
        path = Path(text).resolve()
        if not any(root.resolve() in path.parents for root in
                   (self.home / "sessions", self.home / "archived_sessions")):
            raise ValueError("会话记录路径不在 Codex 数据目录中")
        return path

    def usage(self, thread_id):
        return self.summary(thread_id)["usage"]

    def summary(self, thread_id):
        meta = self.get(thread_id)
        try:
            path = self._rollout_path(meta["rollout_path"])
        except ValueError:
            return {"usage": None, "latestPrompt": None}
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            cached = self.summary_cache.get(thread_id)
            if cached and cached[0] == stamp:
                return cached[1]
            with path.open("rb") as stream:
                offset = max(0, stat.st_size - 2 * 1024 * 1024)
                stream.seek(offset)
                lines = stream.read().decode("utf-8", errors="replace").splitlines()
            if offset:
                lines = lines[1:]
            info, prompt = None, None
            for line in reversed(lines):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if info is None and record.get("type") == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                if prompt is None:
                    text = None
                    if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                        text = payload.get("message")
                    elif record.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                        text = "\n".join(item.get("text", "") for item in payload.get("content", [])
                                         if isinstance(item, dict) and item.get("type") in ("input_text", "text", "inputText"))
                    if isinstance(text, str):
                        prompt = prompt_preview(text)
                if info is not None and prompt is not None:
                    break
            result = {"usage": info, "latestPrompt": prompt}
            if len(self.summary_cache) >= 256 and thread_id not in self.summary_cache:
                self.summary_cache.pop(next(iter(self.summary_cache)))
            self.summary_cache[thread_id] = (stamp, result)
            self.usage_cache[thread_id] = (stamp, info)
            return result
        except OSError:
            return {"usage": None, "latestPrompt": None}

    def _connect(self):
        databases = sorted(self.home.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not databases:
            raise RuntimeError("找不到 Codex 会话数据库")
        conn = sqlite3.connect(databases[0].as_uri() + "?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        return conn

    def list(self, query="", limit=100, offset=0, archived=False):
        with closing(self._connect()) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
            fields = [name for name in ("id", "name", "title", "cwd", "updated_at", "updated_at_ms", "recency_at", "recency_at_ms", "model_provider", "model", "originator", "source", "archived", "is_pinned") if name in columns]
            where = ["archived = ?"]
            params = [int(archived)]
            if "originator" in columns:
                # Older desktop imports have no originator but retain their app source.
                where.append("(originator IN ('Codex Desktop', 'codex_work_desktop') OR (originator IS NULL AND source = 'vscode'))")
            if "thread_source" in columns:
                where.append("COALESCE(thread_source, '') != 'subagent'")
            if "source" in columns:
                where.append("COALESCE(source, '') NOT LIKE '%\"subagent\"%'")
            if query:
                search_fields = [name for name in ("name", "title", "cwd") if name in columns]
                where.append("(" + " OR ".join(name + " LIKE ?" for name in search_fields) + ")")
                params += ["%" + query + "%"] * len(search_fields)
            recency = "COALESCE(recency_at_ms, recency_at * 1000, updated_at * 1000)" if "recency_at_ms" in columns else "COALESCE(recency_at, updated_at)" if "recency_at" in columns else "updated_at"
            rows = conn.execute("SELECT " + ",".join(fields) + " FROM threads WHERE " + " AND ".join(where) + " ORDER BY " + recency + " DESC, id DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
            return [dict(row) for row in rows]

    def get(self, thread_id):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if not row:
                raise KeyError("找不到这个桌面会话")
            result = dict(row)
            desktop = result.get("originator") in ("Codex Desktop", "codex_work_desktop") or (result.get("originator") is None and result.get("source") == "vscode")
            if not desktop or result.get("thread_source") == "subagent" or '"subagent"' in (result.get("source") or ""):
                raise KeyError("不是桌面 App 会话")
            return result

    def history(self, thread_id):
        meta = self.get(thread_id)
        resolved = self._rollout_path(meta["rollout_path"])
        items, turns, current = [], [], None
        model, effort = meta.get("model"), None
        with resolved.open(encoding='utf-8') as stream:
            for ordinal, line in enumerate(stream):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "turn_context":
                    model = payload.get("model") or model
                    if "effort" in payload:
                        effort = payload["effort"]
                    elif "reasoning_effort" in payload:
                        effort = payload["reasoning_effort"]
                elif record.get("type") == "event_msg" and payload.get("type") == "task_started":
                    current = {"turnId": payload.get("turn_id") or "saved-" + str(ordinal), "status": "inProgress", "items": [],
                               "turnStartedAtMs": record_time(record)}
                    turns.append(current)
                    items = current["items"]
                elif record.get("type") == "event_msg" and payload.get("type") in ("task_complete", "turn_aborted"):
                    if current:
                        current["status"] = "completed" if payload["type"] == "task_complete" else "interrupted"
                        current["completedAtMs"] = record_time(record)
                        if current["completedAtMs"] is not None and current.get("turnStartedAtMs") is not None:
                            current["durationMs"] = max(0, current["completedAtMs"] - current["turnStartedAtMs"])
                elif record.get("type") == "response_item":
                    if current is None:
                        current = {"turnId": "saved-" + str(ordinal), "status": "completed", "items": []}
                        turns.append(current)
                        items = current["items"]
                    if payload.get("type") == "message" and payload.get("role") in ("user", "assistant"):
                        role = payload["role"]
                        if role == "user":
                            items.append({"id": str(len(items)), "type": "userMessage", "content": payload.get("content", [])})
                        else:
                            items.append({"id": str(len(items)), "type": "agentMessage", "text": "\n".join(x.get("text", "") for x in payload.get("content", []) if isinstance(x, dict)), "phase": payload.get("phase")})
                    elif payload.get("type") in ("agent_message", "agentMessage"):
                        items.append({"id": payload.get("id", str(len(items))), "type": "agentMessage", "text": payload.get("text", "")})
                    elif payload.get("type") == "reasoning":
                        items.append({"id": payload.get("id", str(len(items))), "type": "reasoning",
                                      "summary": payload.get("summary", []), "content": payload.get("content", [])})
                    elif payload.get("type") in ("function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output"):
                        items.append({"id": payload.get("call_id", str(len(items))), "type": "storedToolEvent", **payload})
        return {"id": thread_id, "title": meta.get("name") or meta.get("title"), "cwd": meta["cwd"],
                "latestModel": model, "latestReasoningEffort": effort, "modelProvider": meta.get("model_provider"),
                "latestTokenUsageInfo": self.usage(thread_id),
                "turns": turns, "requests": [], "threadRuntimeStatus": {"type": "notLoaded"}}


def record_time(record):
    try:
        value = record.get("timestamp")
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000 if isinstance(value, str) else None
    except (ValueError, OverflowError):
        return None


def prompt_preview(text):
    text = " ".join(text.split())
    return text[:240] + ("..." if len(text) > 240 else "")
