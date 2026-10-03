import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path

from .ipc import DesktopIPC, IPCError
from .transport import ipc_endpoint
from .model import apply_patches, normalize_state, normalize_request, normalize_item, items_array, ordered_turns, async_requests, context_usage, thread_settings
from .store import SessionStore, prompt_preview
from .files import artifact_paths, MARKDOWN_PATH
from .catalog import Catalog
from .remote import AppHosts, RemoteStore, RemoteCatalog, RemoteUnavailable
from .desktop_tools import DesktopTools
from .uploads import Uploads
from .history import merged_turns, page, present_turn, keyed_items

CONNECTED_IDLE_TTL = 30 * 60
MAX_IDLE_SESSIONS = 32


def latest_prompt(state):
    for turn in reversed(ordered_turns(state or {})):
        for item in reversed(items_array(turn.get("items", []))):
            if item.get("type") in ("userMessage", "steeringUserMessage") and item.get("status") not in ("rejected", "pending"):
                return prompt_preview(normalize_item(item).get("text", ""))
        opening = turn.get("params", {}).get("input")
        if opening:
            return prompt_preview(normalize_item({"type": "userMessage", "content": opening}).get("text", ""))
    return None


class LiveSession:
    def __init__(self, thread_id):
        self.id = thread_id
        self.owner = None
        self.discovering = False
        self.state = None
        self.revision = None
        self.sequence = 0
        self.sync_id = uuid.uuid4().hex
        self.connected = False
        self.connecting = False
        self.retry_at = 0
        self.error = None
        self.viewers = 0
        self.touched = time.monotonic()
        self.condition = threading.Condition(threading.RLock())
        self.attach_lock = threading.Lock()
        self.action_lock = threading.Lock()
        self.activation_lock = threading.Lock()
        self.activating = False
        self.activation_required = False
        self.archived = False
        self.compaction_pending = False
        self.compaction_baseline = None
        self.saved_state = None
        self.history_loading = False
        self.history_error = None
        self.history_retry_at = 0
        self.history_read_at = 0
        self.view_cache = None
        self.artifact_signature = None
        self.artifact_loading = False
        self.files = []
        self.artifact_read_at = 0
        self.artifact_scan_at = 0
        self.artifact_source_sequence = -1
        self.native_read_at = 0

    def display_turns(self):
        prefer_native = self.connected or self.native_read_at > self.history_read_at
        return merged_turns(self.saved_state, self.state, prefer_native=prefer_native)

    def changed(self):
        self.view_cache = None
        self.sequence += 1
        self.condition.notify_all()

    def view(self):
        with self.condition:
            if self.view_cache is not None:
                return dict(self.view_cache)
            raw = (self.state if self.connected else self.saved_state) or self.state or {"id": self.id}
            # Normalize only metadata here; collapsed process payloads are read on demand.
            requested_items = {request.get("params", {}).get("itemId") for request in raw.get("requests", [])}
            related = [{"items": [item for item in items_array(turn.get("items", []))
                                  if item.get("id") in requested_items]}
                       for turn in ordered_turns(raw)] if requested_items else []
            result = normalize_state({**raw, "turns": related, "turnHistory": {}}, self.connected)
            result["requests"] += async_requests(raw)
            all_turns = self.display_turns()
            turns, cursor = page(all_turns)
            start = len(all_turns) - len(turns)
            result["turns"] = [{**present_turn(turn), "historyIndex": start + index} for index, turn in enumerate(turns)]
            result["hasSavedHistory"] = self.saved_state is not None
            result["historyCursor"] = cursor
            result["historyLoading"] = self.history_loading
            result["historyError"] = self.history_error
            result["historyComplete"] = self.saved_state is not None or raw.get(
                "turnHistory", {}).get("history", {}).get(
                    "isComplete", raw.get("turnsPagination", {}).get("hasLoadedOldest", True))
            result["sequence"] = self.sequence
            result["syncId"] = self.sync_id
            result["connectionError"] = self.error
            result["connecting"] = self.connecting
            result["loadingHistory"] = self.state is None and self.saved_state is None
            result["compactionPending"] = self.compaction_pending
            result["activating"] = self.activating
            result["activationRequired"] = self.activation_required
            self.view_cache = result
            return dict(result)


class Bridge:
    def __init__(self, codex_home, data_dir, host="local", alias=None, ipc_path=None, codex_bin=None):
        self.host = host
        self.codex_home = Path(codex_home)
        self.data_dir = Path(data_dir)
        self.hosts = AppHosts(codex_home)
        self.remote_bridges = {}
        self.host_errors = []
        self.store = RemoteStore(alias) if alias else SessionStore(codex_home)
        self.catalog_reader = RemoteCatalog(alias) if alias else Catalog(codex_home, codex_bin)
        self.live = {}
        self.lock = threading.RLock()
        self.ipc = DesktopIPC(ipc_path or ipc_endpoint(codex_home), self._event, self._disconnected)
        self.closed = threading.Event()
        Path(data_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ledger_path = Path(data_dir) / "submissions.json"
        self.submit_lock = threading.Lock()
        self.submissions = json.loads(self.ledger_path.read_text(encoding='utf-8')) if self.ledger_path.exists() else {}
        self.summary_workers = ThreadPoolExecutor(max_workers=3, thread_name_prefix="context")
        self.artifact_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="artifacts")
        self.history_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="saved-history")
        self.desktop_tools = DesktopTools()
        self.operations_path = self.data_dir / "operations.json"
        self.operations = json.loads(self.operations_path.read_text(encoding='utf-8')) if self.operations_path.exists() else {}
        self.operations_lock = threading.RLock()
        self.desktop_activation_lock = threading.Lock()
        self.creation_cache = None
        self.uploads = Uploads(data_dir)
        for key, value in self.submissions.items():
            if value.get("status") == "queued":
                self.live.setdefault(key.split(":")[0], LiveSession(key.split(":")[0]))
        threading.Thread(target=self._maintain, daemon=True).start()

    def for_host(self, host):
        if host == self.host:
            return self
        available = self.hosts.hosts()
        if self.host != "local" or host not in available:
            raise KeyError("未知 SSH 主机")
        with self.lock:
            if host not in self.remote_bridges:
                folder = self.data_dir / 'hosts' / hashlib.sha256(host.encode()).hexdigest()[:16]
                self.remote_bridges[host] = Bridge(self.codex_home, folder, host, available[host]['alias'], ipc_path=self.ipc.path)
            return self.remote_bridges[host]

    def list(self, query="", limit=100, offset=0, archived=False):
        sources = [(self, "此电脑")]
        if self.host == 'local':
            sources.extend((self.for_host(host), info.get('displayName') or info['alias']) for host, info in self.hosts.hosts().items())
        def read(source):
            bridge, label = source
            try:
                rows = bridge.store.list(limit=limit + offset, archived=archived, query=query)
                rows = self.hosts.decorate(rows, bridge.host, label)
                with bridge.lock:
                    for row in rows:
                        session = bridge.live.get(row['id'])
                        row['connected'] = bool(session and session.connected)
                        row['title'] = row.get('name') or row.get('title') or '未命名聊天'
                return rows, None
            except RemoteUnavailable as exc:
                return [], {'host': bridge.host, 'label': label, 'error': str(exc)}
        with ThreadPoolExecutor(max_workers=min(4, len(sources))) as pool:
            results = list(pool.map(read, sources))
        self.host_errors = [error for _, error in results if error]
        rows = [row for group, _ in results for row in group]
        rows.sort(key=lambda row: (row['recency'], row['id'], row['host']), reverse=True)
        page = rows[offset:offset + limit]
        by_host = {bridge.host: bridge for bridge, _ in sources}
        for row in page:
            source = by_host[row['host']]
            with source.lock:
                session = source.live.get(row['id'])
            with session.condition if session else threading.RLock():
                row['status'] = (session.state or {}).get('threadRuntimeStatus', {}).get('type', 'notLoaded') if session else 'notLoaded'
                row['contextUsage'] = context_usage((session.state or {}).get('latestTokenUsageInfo'), session.connected) if session else context_usage(None)
                row['latestPrompt'] = latest_prompt(session.state) if session else None
            if not row['contextUsage']['available'] and source.host == 'local':
                cached = source.store.usage_cache.get(row['id'])
                row['contextUsage'] = context_usage(cached[1] if cached else None)
            if row['latestPrompt'] is None and source.host == 'local':
                cached = source.store.summary_cache.get(row['id'])
                row['latestPrompt'] = cached[1]['latestPrompt'] if cached else None
        return page

    def contexts(self, ids):
        if len(ids) > 40:
            raise ValueError("一次最多读取 40 条线程状态")
        result = []
        for thread_id in dict.fromkeys(ids):
            session = self.session(thread_id, attach=False, background=True)
            with session.condition:
                if not session.connected and not session.connecting and not session.activating and time.monotonic() >= session.retry_at:
                    session.connecting = True
                    self.summary_workers.submit(self._context_attach, session)
                usage = context_usage((session.state or {}).get("latestTokenUsageInfo"), session.connected)
                prompt = latest_prompt(session.state)
                if self.host == "local" and (not usage["available"] or prompt is None):
                    saved = self.store.summary(thread_id)
                    if not usage["available"]:
                        usage = context_usage(saved["usage"])
                    if prompt is None:
                        prompt = saved["latestPrompt"]
                result.append({"id": thread_id, "host": self.host, "contextUsage": usage,
                               "latestPrompt": prompt,
                               "connected": session.connected,
                               "status": (session.state or {}).get("threadRuntimeStatus", {}).get("type", "notLoaded")})
        return result

    def _context_attach(self, session):
        try:
            if not self.closed.is_set():
                self._attach(session)
        finally:
            with session.condition:
                session.connecting = False
                session.retry_at = time.monotonic() + 60
                session.changed()

    def creation_options(self, refresh=False):
        with self.operations_lock:
            if self.creation_cache and not refresh and time.monotonic() - self.creation_cache[0] < 60:
                return self.creation_cache[1]
            if not self.desktop_tools.caller:
                recent = self.store.list(limit=1)
                self.desktop_tools.caller = recent[0]["id"] if recent else None
            value = self.desktop_tools.call("list_projects", {})
            projects = [dict(project) for project in value.get("projects", [])
                        if project.get("projectKind") != "chatgpt"]
            catalog = self.catalog_reader.get(str(self.codex_home.parent))
            result = {"projects": projects, "models": catalog["models"],
                      "canCreate": "create_thread" in self.desktop_tools.tools()}
            self.creation_cache = (time.monotonic(), result)
            return result

    def _operation(self, key, payload, callback):
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.operations_lock:
            previous = self.operations.get(key)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("同一操作 ID 不能用于不同请求")
                return previous["result"] if previous["status"] == "accepted" else {"status": "unknown"}
            self.operations[key] = {"fingerprint": fingerprint, "status": "unknown"}
            self._save_operations()
        result = callback()
        with self.operations_lock:
            self.operations[key].update(status="accepted", result=result)
            self._save_operations()
        return result

    def _save_operations(self):
        temp = self.operations_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.operations, ensure_ascii=False), encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(self.operations_path)

    def create(self, body):
        if not isinstance(body.get("id"), str):
            raise ValueError("操作 ID 无效")
        operation_id = str(uuid.UUID(body.get("id", "")))
        prompt, model, effort = body.get("prompt"), body.get("model"), body.get("effort")
        if not isinstance(prompt, str) or not 0 < len(prompt.strip()) <= 100000:
            raise ValueError("请输入首条消息，最多 100000 字符")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,199}", model):
            raise ValueError("请选择有效模型")
        if effort not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            raise ValueError("请选择有效推理强度")
        options = self.creation_options()
        known = next((item for item in options["models"] if item["id"] == model), None)
        if known and effort not in known["efforts"]:
            raise ValueError("模型不支持所选推理强度")
        project_id = body.get("projectId")
        if project_id is None:
            target = {"type": "projectless"}
        else:
            project = next((p for p in options["projects"] if p["projectId"] == project_id), None)
            if not project:
                raise ValueError("项目不可用，请刷新后重新选择")
            target = {"type": "project", "projectId": project_id, "environment": {"type": "local"}}
        arguments = {"prompt": prompt, "model": model, "thinking": effort, "target": target}
        title = body.get("title")
        if title:
            if not isinstance(title, str) or len(title) > 200:
                raise ValueError("线程名称过长")
            arguments["title"] = title
        return self._operation("create:" + operation_id, arguments,
                               lambda: self.desktop_tools.call("create_thread", arguments))

    def compact(self, thread_id, operation_id):
        if not isinstance(operation_id, str):
            raise ValueError("操作 ID 无效")
        operation_id = str(uuid.UUID(operation_id))
        session = self._target(thread_id)
        with session.action_lock:
            key = "compact:" + thread_id + ":" + operation_id
            with self.operations_lock:
                previous = self.operations.get(key)
                if previous:
                    return previous["result"] if previous["status"] == "accepted" else {"status": "unknown"}
            with session.condition:
                if session.view()["status"] != "idle":
                    raise ValueError("请等待当前任务结束后再压缩")
                if session.compaction_pending and key not in self.operations:
                    raise ValueError("此线程已有压缩请求，请等待桌面更新")
                session.compaction_pending = True
                session.compaction_baseline = copy.deepcopy((session.state or {}).get("latestTokenUsageInfo"))
                session.changed()
            return self._operation(key, {"thread": thread_id},
                                   lambda: self._call(session, "thread-follower-compact-thread", {}, timeout=30))

    def session(self, thread_id, attach=True, background=False, force=False):
        uuid.UUID(thread_id)
        with self.lock:
            session = self.live.get(thread_id)
            if session:
                with session.condition:
                    session.touched = time.monotonic()
        if session is None:
            # SSH/SQLite reads must not block IPC events for other chats.
            meta = self.store.get(thread_id)
            with self.lock:
                session = self.live.setdefault(thread_id, LiveSession(thread_id))
                session.archived = bool(meta.get("archived"))
                with session.condition:
                    session.touched = time.monotonic()
        if background:
            if attach:
                self._history_async(session, force)
                self._refresh_async(session, force)
            return session
        if attach and not session.connected:
            self._attach(session)
        if session.saved_state is None and session.state is None:
            try:
                fallback = self.store.history(thread_id)
            except (OSError, ValueError, KeyError, RemoteUnavailable):
                fallback = None
            with session.condition:
                if fallback is not None and session.saved_state is None:
                    session.saved_state = fallback
                    session.changed()
        return session

    def _history_async(self, session, force=False):
        with session.condition:
            if session.history_loading or self.closed.is_set():
                return
            if session.saved_state is not None and not force and time.monotonic() - session.history_read_at < 30:
                return
            if not force and time.monotonic() < session.history_retry_at:
                return
            session.history_loading = True
            session.changed()
        def read():
            try:
                saved = self.store.history(session.id)
                with session.condition:
                    session.saved_state = saved
                    # Never replace a native patch base with saved rollout data.
                    session.history_error = None
                    session.history_read_at = time.monotonic()
            except (OSError, ValueError, KeyError, RemoteUnavailable):
                with session.condition:
                    session.history_error = "已保存历史暂不可读，可重试加载"
            finally:
                with session.condition:
                    session.history_loading = False
                    session.history_retry_at = time.monotonic() + 30
                    session.changed()
        self.history_workers.submit(read)

    def _refresh_async(self, session, force=False):
        with session.condition:
            if session.connected or session.connecting or session.activating or self.closed.is_set():
                return
            if not force and time.monotonic() < session.retry_at:
                return
            session.connecting = True
            session.changed()
        def refresh():
            try:
                if not self.closed.is_set():
                    self._attach(session)
            except Exception:
                logging.getLogger(__name__).exception("Background session read failed")
                with session.condition:
                    session.error = "读取会话失败，请重新连接或查看网关日志。"
            finally:
                with session.condition:
                    session.connecting = False
                    session.retry_at = time.monotonic() + 15
                    session.changed()
        threading.Thread(target=refresh, daemon=True).start()

    def _attach(self, session):
        with session.attach_lock:
            if session.connected:
                return
            try:
                if self.host == "local":
                    owner = self.ipc.owner(session.id, self.host)
                    with session.condition:
                        session.owner = owner
                        session.revision = None
                    self.ipc.follow(session.id, owner, host=self.host)
                else:
                    # Remote owners publish snapshots but are not registered by local discovery.
                    self.ipc.connect()
                    with session.condition:
                        session.owner = None
                        session.revision = None
                        session.discovering = True
                    self.ipc.follow(session.id, None, host=self.host)
                with session.condition:
                    if not session.condition.wait_for(lambda: session.connected or self.closed.is_set(), timeout=8):
                        raise IPCError("尚未收到桌面实时快照。请在电脑 App 打开此聊天后重新连接。")
            except IPCError as exc:
                if self.host != "local":
                    try:
                        self.ipc.follow(session.id, None, False, host=self.host)
                    except IPCError:
                        pass
                with session.condition:
                    session.discovering = False
                    session.connected = False
                    session.activation_required = self.host == "local" and "no-client-found" in str(exc)
                    session.error = "此线程尚未加载，可按需连接，无需在电脑逐条打开。" if session.activation_required else str(exc)
                    session.changed()

    def activate(self, thread_id):
        if self.host != "local":
            raise ValueError("此入口只支持此电脑的线程")
        session = self.session(thread_id, attach=False, background=True)
        if self.store.get(thread_id).get("archived"):
            raise ValueError("已归档线程只能阅读历史，请先在桌面恢复")
        with session.activation_lock:
            with session.condition:
                session.activating = True
                session.error = None
                session.changed()
            try:
                self._attach(session)
                if not session.connected:
                    if not session.activation_required:
                        raise IPCError(session.error or "无法连接桌面 App")
                    # Navigation lets the existing App load its original executor.
                    # Passive list/history/context reads never call this path.
                    with self.desktop_activation_lock:
                        if not session.connected:
                            if not self.desktop_tools.caller:
                                recent = self.store.list(limit=1)
                                self.desktop_tools.caller = recent[0]["id"] if recent else None
                            self.desktop_tools.call("navigate_to_codex_page", {"threadId": thread_id})
                            deadline = time.monotonic() + 20
                            while not self.closed.is_set():
                                self._attach(session)
                                if session.connected or time.monotonic() >= deadline:
                                    break
                                self.closed.wait(.5)
                    if not session.connected:
                        raise IPCError("桌面已收到加载请求，但尚未连接；请点击连接此线程重试")
            except IPCError as exc:
                with session.condition:
                    session.error = str(exc)
                raise
            finally:
                with session.condition:
                    session.activating = False
                    session.changed()
        return self.view(thread_id, attach=False, background=True)

    def _event(self, message):
        if message.get("method") == "client-status-changed":
            params = message.get("params", {})
            if params.get("status") == "disconnected":
                with self.lock:
                    sessions = list(self.live.values())
                for session in sessions:
                    if session.owner == params.get("clientId"):
                        self._invalidate(session, "桌面会话连接已断开，正在等待重连")
            return
        if message.get("method") != "thread-stream-state-changed":
            return
        if message.get("version") != 11:
            self._disconnected()
            return
        params = message.get("params", {})
        if params.get("hostId") != self.host:
            return
        with self.lock:
            session = self.live.get(params.get("conversationId"))
        if session is None:
            return
        change = params.get("change", {})
        with session.condition:
            if session.discovering and change.get("type") == "snapshot" and (change.get("conversationState") or {}).get("id") == session.id:
                session.owner = message.get("sourceClientId")
                session.discovering = False
            if not session.owner or message.get("sourceClientId") != session.owner:
                return
            try:
                if change.get("type") == "snapshot":
                    state = change["conversationState"]
                    if state.get("id") != session.id:
                        raise ValueError("Session mismatch")
                    session.state = state
                elif change.get("type") == "patches":
                    if session.revision is None or change.get("baseRevision") != session.revision:
                        raise ValueError("Revision mismatch")
                    session.state = apply_patches(session.state, change["patches"])
                else:
                    return
                session.revision = change["revision"]
                session.native_read_at = time.monotonic()
                session.connected = True
                session.activation_required = False
                session.error = None
                if session.compaction_pending and session.state.get("latestTokenUsageInfo") != session.compaction_baseline:
                    session.compaction_pending = False
                session.changed()
            except (ValueError, KeyError, IndexError, TypeError):
                session.connected = False
                session.revision = None
                session.error = "同步状态发生变化，正在重新读取桌面快照"
                session.changed()

    def _invalidate(self, session, message):
        with session.condition:
            session.connected = False
            session.revision = None
            session.error = message
            session.changed()

    def _disconnected(self):
        with self.lock:
            sessions = list(self.live.values())
        for session in sessions:
            self._invalidate(session, "桌面 App 连接已断开，正在等待重连")

    def _maintain(self):
        delay = 3
        while not self.closed.wait(delay):
            with self.lock:
                sessions = list(self.live.values())
            for session in sessions:
                with self.submit_lock:
                    queued = [(k, dict(v)) for k, v in self.submissions.items() if k.startswith(session.id + ":") and v["status"] == "queued"]
                if queued and session.connected and session.view().get("status") == "idle":
                    key, entry = queued[0]
                    try:
                        self._send_queued(session, key, entry)
                    except Exception:
                        pass  # Unknown outcomes stay recorded and are never automatically replayed.
                if (session.viewers > 0 or queued) and not session.connected:
                    self._refresh_async(session)
                if session.viewers > 0:
                    self._artifacts_async(session)
            self._trim_sessions()
            delay = 3 if self.ipc.client_id else min(30, delay * 2)

    def _trim_sessions(self, now=None):
        now = time.monotonic() if now is None else now
        with self.submit_lock:
            queued_ids = {key.split(":")[0] for key, value in self.submissions.items()
                          if value["status"] == "queued"}
        retired = []
        with self.lock:
            candidates = []
            for session in self.live.values():
                with session.condition:
                    active = (session.state or {}).get("threadRuntimeStatus", {}).get("type") == "active"
                    if (session.viewers or session.id in queued_ids or active or
                            session.connecting or session.activating or session.compaction_pending or session.history_loading):
                        continue
                    candidates.append(session)
            candidates.sort(key=lambda session: session.touched)
            excess = max(0, len(candidates) - MAX_IDLE_SESSIONS)
            for index, session in enumerate(candidates):
                with session.condition:
                    ttl = CONNECTED_IDLE_TTL if session.connected else 300
                    if index < excess or now - session.touched > ttl:
                        self.live.pop(session.id, None)
                        if session.connected:
                            retired.append((session.id, session.owner))
            # Pair removal/unfollow under the same lock so a returning viewer cannot
            # attach a new session before its old subscription is removed.
            for thread_id, owner in retired:
                try:
                    self.ipc.follow(thread_id, owner, False, host=self.host)
                except IPCError:
                    pass

    def _target(self, thread_id):
        session = self.session(thread_id)
        with session.condition:
            if not session.connected or not session.owner:
                raise IPCError(session.error or "请先在桌面 App 打开此聊天")
        return session

    def _call(self, session, method, params, timeout=30):
        return self.ipc.request(method, {"conversationId": session.id, **params}, target=session.owner, host=self.host, timeout=timeout)["result"]

    def _save_ledger(self):
        # Keep accepted or uncertain submissions durable across process restarts.
        target = self.ledger_path.with_suffix(".tmp")
        target.write_text(json.dumps(self.submissions, ensure_ascii=False), encoding='utf-8')
        target.chmod(0o600)
        target.replace(self.ledger_path)

    def view(self, thread_id, attach=True, background=False, force=False):
        session = self.session(thread_id, attach=attach, background=background, force=force)
        view = session.view()
        view["host"] = self.host
        view["canActivate"] = self.host == "local" and not session.archived
        view["hostLabel"] = "此电脑" if self.host == "local" else self.hosts.hosts().get(self.host, {}).get("displayName", self.host)
        self._artifacts_async(session)
        with session.condition:
            view["files"] = session.files
        with self.submit_lock:
            view["submissions"] = [{"id": k.split(":")[1], "text": v["text"], "status": v["status"]}
                                   for k, v in self.submissions.items()
                                   if k.startswith(thread_id + ":") and v["status"] in ("queued", "unknown")]
        return view

    def _artifact_source(self, session):
        # Copy only text references and attachment descriptors under the state lock.
        # Regex, hashing and filesystem work must not delay native state events.
        with session.condition:
            snapshot = [(item.get("text", ""),
                         [dict(value) for value in item.get("content", [])
                          if isinstance(value, dict) and value.get("type") in ("localImage", "image", "file")]
                         if isinstance(item.get("content"), list) else [])
                        for turn in session.display_turns()
                        for item in items_array(turn.get("items", []))]
            raw = (session.state if session.connected else session.saved_state) or session.state or {}
            cwd = raw.get("cwd")
        items = []
        for text, content in snapshot:
            links = [match.group(0) for match in MARKDOWN_PATH.finditer(text)] if isinstance(text, str) else []
            if links or content:
                items.append({"type": "agentMessage", "text": "\n".join(links), "content": content})
        return {"cwd": cwd, "turns": [{"items": items}]}

    def _artifacts_async(self, session):
        if self.host != "local" or self.closed.is_set():
            return
        with session.condition:
            if session.artifact_loading or time.monotonic() - session.artifact_scan_at < 1:
                return
            if session.artifact_source_sequence == session.sequence and time.monotonic() - session.artifact_read_at < 15:
                return
            session.artifact_source_sequence = session.sequence
            session.artifact_scan_at = time.monotonic()
            session.artifact_loading = True
        def read():
            try:
                source = self._artifact_source(session)
                signature = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).digest()
                with session.condition:
                    if signature == session.artifact_signature and time.monotonic() - session.artifact_read_at < 15:
                        return
                artifacts = artifact_paths(source, self.store.home)
                files = [{"id": key, "name": value["name"], "reference": value["reference"],
                          "image": value["image"]} for key, value in artifacts.items()]
                with session.condition:
                    session.artifact_signature = signature
                    session.artifact_read_at = time.monotonic()
                    if session.files != files:
                        session.files = files
                        session.changed()
            finally:
                with session.condition:
                    session.artifact_loading = False
        self.artifact_workers.submit(read)

    def history_page(self, thread_id, before=None, turn_id=None, message_id=None):
        session = self.session(thread_id, attach=False, background=True)
        self._history_async(session)
        with session.condition:
            turns = session.display_turns()
            if turn_id:
                turn = next((turn for turn in turns if turn.get("turnId") == turn_id), None)
                if turn is None:
                    raise KeyError("轮次不可用")
                if message_id:
                    item = next((item for key, item in keyed_items(turn) if key == message_id), None)
                    if item is None:
                        raise KeyError("详情不可用")
                    return {"message": normalize_item(item), "sequence": session.sequence}
                return {"turn": present_turn(turn, fold=False), "sequence": session.sequence}
            rows, cursor = page(turns, before)
            positions = {id(turn): index for index, turn in enumerate(turns)}
            return {"id": thread_id, "host": self.host,
                    "turns": [{**present_turn(turn), "historyIndex": positions[id(turn)]} for turn in rows],
                    "historyCursor": cursor, "historyLoading": session.history_loading,
                    "historyError": session.history_error, "sequence": session.sequence, "syncId": session.sync_id}

    def artifact(self, thread_id, artifact_id):
        session = self.session(thread_id, attach=False)
        source = self._artifact_source(session)
        files = artifact_paths(source, self.store.home) if self.host == "local" else {}
        if artifact_id not in files:
            raise KeyError("文件不属于此聊天的工作目录")
        return files[artifact_id]

    def catalog(self, thread_id, refresh=False):
        session = self.session(thread_id)
        with session.condition:
            raw = session.state or session.saved_state or {}
            cwd = raw.get("cwd")
            settings = thread_settings(raw)
            model, effort = settings["model"], settings["effort"]
        catalog = self.catalog_reader.get(cwd, refresh=refresh)
        return {**catalog, "currentModel": model, "currentEffort": effort}

    def _resolve_skills(self, session, skills):
        if not isinstance(skills, list) or len(skills) > 8 or not all(isinstance(s, str) for s in skills):
            raise ValueError("最多选择 8 个 Skill")
        if not skills:
            return []
        catalog = self.catalog(session.id)
        by_id = {s["id"]: s for s in catalog["skills"]}
        selected = []
        for key in sorted(set(skills)):
            skill = by_id.get(key)
            if not skill or (self.host == "local" and not Path(skill["path"]).is_file()):
                raise ValueError("Skill 不可用，请刷新列表")
            selected.append({"id": key, "name": skill["name"], "path": skill["path"]})
        return selected

    def settings(self, thread_id, model, effort):
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,199}", model):
            raise ValueError("模型 ID 格式不正确")
        if effort not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            raise ValueError("请选择有效的推理强度")
        session = self._target(thread_id)
        known = next((m for m in self.catalog(thread_id)["models"] if m["id"] == model), None)
        if known and effort not in known["efforts"]:
            raise ValueError("这个模型不支持所选推理强度")
        with session.action_lock:
            result = self._call(session, "thread-follower-update-thread-settings", {"threadSettings": {"model": model, "effort": effort}})
        if not result.get("applied"):
            raise IPCError("桌面未应用模型设置，请刷新后重试")
        with session.condition:
            confirmed = session.condition.wait_for(lambda: session.state.get("latestModel") == model and session.view().get("effort") == effort, timeout=5)
        return {"applied": True, "confirmed": confirmed, "model": model, "effort": effort}

    def cancel_queued(self, thread_id, submission_id):
        uuid.UUID(submission_id)
        session = self.session(thread_id, attach=False)
        with session.action_lock:
            with self.submit_lock:
                key = thread_id + ":" + submission_id
                prior = self.submissions.get(key)
                if not prior or prior["status"] != "queued":
                    raise ValueError("此消息已离开队列，请查看会话")
                prior["status"] = "cancelled"
                self._save_ledger()
            with session.condition:
                session.changed()
        return {"status": "cancelled"}

    def upload(self, thread_id, owner, name, data):
        if self.host != "local":
            raise ValueError("附件暂仅支持本机线程")
        self.store.get(thread_id)
        return self.uploads.put(thread_id, owner, name, data)

    def send(self, thread_id, text, submission_id, mode="send", skills=None, attachments=None, upload_owner=""):
        uuid.UUID(submission_id)
        if not isinstance(text, str) or (not text.strip() and not attachments) or len(text) > 100000:
            raise ValueError("请输入 1–100000 字的消息")
        if mode not in ("send", "steer", "queue"):
            raise ValueError("未知发送方式")
        if attachments and self.host != "local":
            raise ValueError("附件暂仅支持本机线程")
        attached = self.uploads.resolve(thread_id, upload_owner, [] if attachments is None else attachments)
        session = self._target(thread_id)
        selected = self._resolve_skills(session, [] if skills is None else skills)
        key = thread_id + ":" + submission_id
        with session.action_lock:
            with self.submit_lock:
                prior = self.submissions.get(key)
                if prior:
                    if prior["text"] != text or prior["mode"] != mode or prior.get("skills", []) != selected or prior.get("attachments", []) != attached:
                        raise ValueError("同一消息标识不能用于不同内容")
                    return {"status": prior["status"], "duplicate": True, "id": submission_id}
            with session.condition:
                active = session.state.get("threadRuntimeStatus", {}).get("type") == "active"
                if active and mode == "send":
                    raise ValueError("Codex 正在执行。请选择「排队发送」或「补充当前任务」。")
                if not active and mode == "steer":
                    raise ValueError("当前任务已经结束，请使用普通发送")
            with self.submit_lock:
                entry = {"text": text, "mode": mode, "skills": selected, "attachments": attached,
                         "status": "queued" if mode == "queue" else "unknown", "at": time.time()}
                self.submissions[key] = entry
                self._save_ledger()
            with session.condition:
                session.changed()
            if mode == "queue":
                return {"status": "queued", "id": submission_id}
            return self._dispatch(session, key, entry)

    def _send_queued(self, session, key, entry):
        with session.action_lock:
            with session.condition:
                if not session.connected or session.state.get("threadRuntimeStatus", {}).get("type") != "idle":
                    return
            with self.submit_lock:
                if self.submissions[key]["status"] != "queued":
                    return
                self.submissions[key]["status"] = "unknown"
                self._save_ledger()
            try:
                self._dispatch(session, key, entry)
            finally:
                with session.condition:
                    session.changed()

    def _dispatch(self, session, key, entry):
        submission_id = key.split(":")[1]
        request = {"threadId": session.id, "input": [{"type": "text", "text": entry["text"], "text_elements": []}], "clientUserMessageId": submission_id}
        request["input"].extend({"type": "skill", "name": s["name"], "path": s["path"]} for s in entry.get("skills", []))
        attached = entry.get("attachments", [])
        descriptors = [{"label": a["name"], "path": a["path"], "fsPath": a["path"],
                        "isImageAttachment": a["image"]} for a in attached]
        for attachment in attached:
            if attachment["image"]:
                request["input"].append({"type": "localImage", "path": attachment["path"]})
        if attached:
            references = "\n\n# Files mentioned by the user:\n" + "".join(
                "\n## " + a["name"] + ": " + a["path"] + "\n" for a in attached)
            request["input"].append({"type": "text", "text": references, "text_elements": []})
        if entry["mode"] != "steer":
            response = self._call(session, "thread-follower-start-turn", {
                "turnStart": {"request": request, "context": {"inheritThreadSettings": True, "attachments": descriptors, "commentAttachments": []}}}, timeout=90)
        else:
            response = self._call(session, "thread-follower-steer-turn", {
                "input": request["input"], "clientUserMessageId": submission_id,
                "restoreMessage": {"request": request, "context": {"attachments": descriptors, "commentAttachments": []}},
                "attachments": descriptors}, timeout=30)
        with self.submit_lock:
            self.submissions[key]["status"] = "accepted"
            self._save_ledger()
        with session.condition:
            session.changed()
        return {"status": "accepted", "id": submission_id, "result": response}

    def interrupt(self, thread_id, expected_turn_id=None):
        session = self._target(thread_id)
        with session.condition:
            active = next((t for t in reversed(ordered_turns(session.state)) if t.get("status") == "inProgress"), None)
            if not active or not active.get("turnId"):
                raise ValueError("当前没有可停止的任务")
            turn_id = active["turnId"]
            if expected_turn_id is not None and expected_turn_id != turn_id:
                raise ValueError("任务轮次已变化，请重新确认停止")
        return self._call(session, "thread-follower-interrupt-turn", {"mode": "user-stop", "expectedTurnId": turn_id})

    def full_history(self, thread_id):
        session = self._target(thread_id)
        return self._call(session, "thread-follower-load-complete-history", {}, timeout=180)

    def respond(self, thread_id, request_id, response):
        session = self._target(thread_id)
        with session.condition:
            async_pending = next((r for r in async_requests(session.state) if r["id"] == request_id), None)
        if async_pending:
            questions = async_pending["params"]["questions"]
            answers = response.get("answers", {})
            if set(answers) != {q["id"] for q in questions} or any(not isinstance(v, list) or len(v) != 1 or not isinstance(v[0], str) or not v[0].strip() or len(v[0]) > 20000 for v in answers.values()):
                raise ValueError("请回答所有问题")
            replies = [{"questionItemId": q["id"], "question": q["question"], "answer": answers[q["id"]][0]} for q in questions]
            text = "<send_user_message_question_reply>\n" + json.dumps(replies, ensure_ascii=False, separators=(",", ":")) + "\n</send_user_message_question_reply>"
            submission = str(uuid.uuid5(uuid.UUID(thread_id), request_id + text))
            with session.condition:
                active = session.state.get("threadRuntimeStatus", {}).get("type") == "active"
            return self.send(thread_id, text, submission, "steer" if active else "send")
        with session.condition:
            pending = next((r for r in session.state.get("requests", []) if str(r.get("id")) == str(request_id)), None)
            if not pending:
                raise ValueError("此请求已处理或已过期")
            pending = copy.deepcopy(pending)
        if not normalize_request(pending)["supported"]:
            raise ValueError("此类请求请在桌面 App 中处理")
        method = pending.get("method", "")
        params = pending.get("params", {})
        mapping = {
            "item/commandExecution/requestApproval": "thread-follower-command-approval-decision",
            "item/fileChange/requestApproval": "thread-follower-file-approval-decision",
            "item/permissions/requestApproval": "thread-follower-permissions-request-approval-response",
            "item/tool/requestUserInput": "thread-follower-submit-user-input",
            "tool/requestUserInput": "thread-follower-submit-user-input",
            "mcpServer/elicitation/request": "thread-follower-submit-mcp-server-elicitation-response",
        }
        if method not in mapping:
            raise ValueError("此类请求请在桌面 App 中处理")
        payload = {"requestId": pending["id"]}
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            decision = response.get("decision")
            if decision not in ("accept", "decline", "cancel"):
                raise ValueError("只支持本次批准、拒绝或取消")
            available = params.get("availableDecisions")
            if available and decision not in available:
                raise ValueError("该审批不支持所选操作")
            payload["decision"] = decision
        elif method == "item/permissions/requestApproval":
            if response.get("decision") not in ("accept", "decline"):
                raise ValueError("请选择批准或拒绝")
            payload["response"] = {"permissions": params.get("permissions", {}) if response["decision"] == "accept" else {}, "scope": "turn"}
        elif method in ("item/tool/requestUserInput", "tool/requestUserInput"):
            answers = response.get("answers")
            if not isinstance(answers, dict):
                raise ValueError("请填写问题答案")
            allowed_ids = {q["id"] for q in params.get("questions", [])}
            if not allowed_ids or set(answers) != allowed_ids or any(not isinstance(v, list) or not v or not all(isinstance(s, str) and len(s) <= 20000 for s in v) for v in answers.values()):
                raise ValueError("答案不完整或格式不正确")
            payload["response"] = {"answers": {k: {"answers": v} for k, v in answers.items()}}
        else:
            action = response.get("action")
            if action not in ("accept", "decline", "cancel"):
                raise ValueError("未知确认操作")
            content = response.get("content")
            if action == "accept":
                validate_form(content, params.get("requestedSchema", {}))
            payload["response"] = {"action": action, "content": content if action == "accept" else None}
        return self._call(session, mapping[method], payload)

    def close(self):
        self.closed.set()
        self.summary_workers.shutdown(wait=False, cancel_futures=True)
        self.artifact_workers.shutdown(wait=False, cancel_futures=True)
        self.history_workers.shutdown(wait=False, cancel_futures=True)
        for bridge in list(self.remote_bridges.values()):
            bridge.close()
        with self.lock:
            sessions = list(self.live.values())
        for session in sessions:
            try:
                if session.connected:
                    self.ipc.follow(session.id, session.owner, False, host=self.host)
            except IPCError:
                pass
        self.ipc.close()


def validate_form(value, schema):
    """Validate the small JSON-schema form subset that the phone can render."""
    from .model import form_supported
    if not form_supported(schema):
        raise ValueError("此表单需要在桌面填写")
    if not isinstance(value, dict):
        raise ValueError("请填写表单")
    properties = schema.get("properties", {})
    if set(value) - set(properties) or set(schema.get("required", [])) - set(value):
        raise ValueError("表单字段不完整")
    for key, item in value.items():
        field = properties[key]
        kind = field.get("type", "string")
        valid = ((kind == "string" and isinstance(item, str)) or
                 (kind == "boolean" and isinstance(item, bool)) or
                 (kind in ("number", "integer") and type(item) in (int, float) and (kind != "integer" or int(item) == item)))
        if not valid or ("enum" in field and item not in field["enum"]):
            raise ValueError("表单字段格式不正确：" + key)
        if isinstance(item, str) and not field.get("minLength", 0) <= len(item) <= field.get("maxLength", 20000):
            raise ValueError("表单文本长度不正确：" + key)
        if type(item) in (int, float) and not field.get("minimum", float("-inf")) <= item <= field.get("maximum", float("inf")):
            raise ValueError("表单数值超出范围：" + key)
