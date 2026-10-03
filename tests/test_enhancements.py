import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from bridge.model import context_usage, normalize_state
from bridge.remote import AppHosts
import test_bridge as fixture
from test_bridge import THREAD, state, ROOT


class ContextTests(unittest.TestCase):
    def test_native_last_usage_not_lifetime_total(self):
        info = {"total": {"totalTokens": 9000000}, "last": {"totalTokens": 125000}, "modelContextWindow": 250000}
        self.assertEqual(context_usage(info, True)["percent"], 50)
        self.assertTrue(context_usage(info, True)["live"])
        value = state()
        value["latestTokenUsageInfo"] = info
        self.assertEqual(normalize_state(value)["contextUsage"]["usedTokens"], 125000)

    def test_invalid_or_unavailable_never_fabricates_zero(self):
        for info in (None, {}, {"last": {"totalTokens": 1}}, {"last": {"totalTokens": -1}, "modelContextWindow": 10},
                     {"last": {"totalTokens": float("nan")}, "modelContextWindow": 10}):
            result = context_usage(info, True)
            self.assertFalse(result["available"])
            self.assertIsNone(result["percent"])
            self.assertFalse(result["live"])

    def test_saved_and_overflow_usage(self):
        result = context_usage({"last_token_usage": {"total_tokens": 300}, "model_context_window": 200})
        self.assertEqual(result["percent"], 100)
        self.assertFalse(result["live"])

    def test_explicit_unassignment_overrides_cwd_inference(self):
        hosts = AppHosts(ROOT)
        hosts.state = lambda: {"local-projects": {"p": {"id": "p", "name": "Work", "rootPaths": ["/work"]}},
                               "thread-project-assignments": {"one": {"projectId": None}}}
        rows = hosts.decorate([{"id": "one", "cwd": "/work/sub"}, {"id": "two", "cwd": "/elsewhere"}], "local", "PC")
        self.assertEqual({row["projectKey"] for row in rows}, {"local|unassigned"})
        self.assertTrue(all(row["projectId"] is None for row in rows))


class NativeOperationTests(unittest.TestCase):
    setUp = fixture.IntegrationTests.setUp
    tearDown = fixture.IntegrationTests.tearDown
    def test_compact_routes_to_same_owner_and_is_idempotent(self):
        self.bridge.view(THREAD)
        operation_id = str(uuid.uuid4())
        self.bridge.compact(THREAD, operation_id)
        self.bridge.compact(THREAD, operation_id)
        calls = [call for call in self.fixture.requests if call["method"] == "thread-follower-compact-thread"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["version"], 1)
        self.assertEqual(calls[0]["targetClientId"], "owner")
        self.assertEqual(calls[0]["params"], {"conversationId": THREAD})

    def test_compact_refuses_active_or_invalid_id(self):
        self.fixture.state["threadRuntimeStatus"] = {"type": "active"}
        with self.assertRaises(ValueError):
            self.bridge.compact(THREAD, str(uuid.uuid4()))
        with self.assertRaises(ValueError):
            self.bridge.compact(THREAD, "bad")
        self.assertFalse(any(call["method"] == "thread-follower-compact-thread" for call in self.fixture.requests))

    def test_compaction_state_clears_on_native_usage_update(self):
        operation_id = str(uuid.uuid4())
        self.bridge.compact(THREAD, operation_id)
        self.assertTrue(self.bridge.view(THREAD)["compactionPending"])
        self.fixture.state["latestTokenUsageInfo"] = {"last": {"totalTokens": 100}, "modelContextWindow": 1000}
        self.fixture.revision += 1
        self.fixture.snapshot()
        session = self.bridge.live[THREAD]
        with session.condition:
            session.condition.wait_for(lambda: not session.compaction_pending, timeout=2)
        self.assertFalse(session.compaction_pending)
        self.bridge.compact(THREAD, operation_id)
        self.assertFalse(session.compaction_pending)
        self.assertEqual(len([call for call in self.fixture.requests
                              if call["method"] == "thread-follower-compact-thread"]), 1)

    def test_compaction_terminal_marker_clears_pending_without_usage_change(self):
        self.bridge.compact(THREAD, str(uuid.uuid4()))
        session = self.bridge.live[THREAD]
        self.fixture.state.setdefault("turns", []).append({
            "turnId": "compact-new", "status": "completed",
            "items": [{"type": "contextCompaction", "id": "compact-item"}]})
        self.fixture.revision += 1
        self.fixture.snapshot()
        with session.condition:
            session.condition.wait_for(lambda: not session.compaction_pending, timeout=2)
        self.assertFalse(session.compaction_pending)

    def test_old_terminal_marker_does_not_finish_new_compaction(self):
        self.fixture.state.setdefault("turns", []).append({
            "turnId": "compact-old", "status": "completed",
            "items": [{"type": "contextCompaction", "id": "old"}]})
        self.bridge.compact(THREAD, str(uuid.uuid4()))
        self.fixture.revision += 1
        self.fixture.snapshot()
        session = self.bridge.live[THREAD]
        with session.condition:
            session.condition.wait_for(lambda: session.revision == self.fixture.revision, timeout=2)
        self.assertTrue(session.compaction_pending)

    def test_same_compaction_marker_finishes_only_at_terminal_status(self):
        self.bridge.compact(THREAD, str(uuid.uuid4()))
        item = {"type": "contextCompaction", "id": "live-compact", "status": "inProgress"}
        self.fixture.state["turns"] = [{"turnId": "compact", "status": "inProgress", "items": [item]}]
        self.fixture.revision += 1
        self.fixture.snapshot()
        session = self.bridge.live[THREAD]
        with session.condition:
            session.condition.wait_for(lambda: session.revision == self.fixture.revision, timeout=2)
        self.assertTrue(session.compaction_pending)
        item["status"] = "completed"
        self.fixture.revision += 1
        self.fixture.snapshot()
        with session.condition:
            session.condition.wait_for(lambda: not session.compaction_pending, timeout=2)
        self.assertFalse(session.compaction_pending)

    def test_historical_page_does_not_finish_current_compaction(self):
        self.fixture.state["turns"] = [{"turnId": "latest", "status": "completed", "items": []}]
        self.bridge.compact(THREAD, str(uuid.uuid4()))
        self.fixture.state["turns"].insert(0, {
            "turnId": "older", "status": "completed",
            "items": [{"type": "contextCompaction", "id": "older-compact"}]})
        self.fixture.revision += 1
        self.fixture.snapshot()
        session = self.bridge.live[THREAD]
        with session.condition:
            session.condition.wait_for(lambda: session.revision == self.fixture.revision, timeout=2)
        self.assertTrue(session.compaction_pending)

    def test_create_validated_and_deduplicated_native_app_call(self):
        self.bridge.desktop_tools = Mock()
        self.bridge.creation_options = lambda: {
            "projects": [{"projectId": "p", "hostId": "local"}],
            "models": [{"id": "same-model", "efforts": ["high"]}], "canCreate": True}
        self.bridge.desktop_tools.call.return_value = {"threadId": THREAD, "hostId": "local"}
        body = {"id": str(uuid.uuid4()), "prompt": "Hello", "model": "same-model", "effort": "high", "projectId": "p"}
        first = self.bridge.create(body)
        self.assertEqual(self.bridge.create(body), first)
        self.bridge.desktop_tools.call.assert_called_once_with("create_thread", {
            "prompt": "Hello", "model": "same-model", "thinking": "high",
            "target": {"type": "project", "projectId": "p", "environment": {"type": "local"}}})
        with self.assertRaises(ValueError):
            self.bridge.create({**body, "prompt": "different"})
        with self.assertRaises(ValueError):
            self.bridge.create({**body, "id": str(uuid.uuid4()), "projectId": "arbitrary-path"})

    def test_unknown_creation_not_replayed(self):
        from bridge.ipc import IPCError
        self.bridge.creation_options = lambda: {"projects": [], "models": [], "canCreate": True}
        self.bridge.desktop_tools = Mock()
        self.bridge.desktop_tools.call.side_effect = IPCError("Unknown")
        body = {"id": str(uuid.uuid4()), "prompt": "Hello", "model": "custom", "effort": "high", "projectId": None}
        with self.assertRaises(IPCError):
            self.bridge.create(body)
        self.assertEqual(self.bridge.create(body), {"status": "unknown"})
        self.assertEqual(self.bridge.desktop_tools.call.call_count, 1)

    def test_passive_cold_reads_do_not_navigate_desktop(self):
        from bridge.ipc import IPCError
        self.bridge.ipc.owner = Mock(side_effect=IPCError("no-client-found"))
        self.bridge.store.history = lambda thread_id: state()
        self.bridge.desktop_tools = Mock()
        view = self.bridge.view(THREAD)
        self.assertFalse(view["connected"])
        self.assertTrue(view["activationRequired"])
        self.assertTrue(view["canActivate"])
        self.bridge.contexts([THREAD])
        self.bridge.desktop_tools.call.assert_not_called()

    def test_cold_activation_loads_original_thread_once_without_turn(self):
        from bridge.ipc import IPCError
        owner = self.bridge.ipc.owner
        loaded = threading.Event()
        def discover(*args):
            if not loaded.is_set():
                raise IPCError("no-client-found")
            return owner(*args)
        self.bridge.ipc.owner = discover
        self.bridge.desktop_tools = Mock()
        def navigate(name, arguments):
            self.assertEqual(name, "navigate_to_codex_page")
            self.assertEqual(arguments, {"threadId": THREAD})
            loaded.set()
            return {"navigated": True}
        self.bridge.desktop_tools.call.side_effect = navigate
        view = self.bridge.activate(THREAD)
        self.assertTrue(view["connected"])
        self.assertFalse(view["activating"])
        self.assertFalse(view["activationRequired"])
        self.assertTrue(self.bridge.activate(THREAD)["connected"])
        self.bridge.desktop_tools.call.assert_called_once()
        self.assertFalse(any(call["method"] == "thread-follower-start-turn" for call in self.fixture.requests))

    def test_activation_refuses_archived_and_remote_threads(self):
        self.bridge.desktop_tools = Mock()
        original_get = self.bridge.store.get
        self.bridge.store.get = lambda thread_id: {**original_get(thread_id), "archived": True}
        with self.assertRaises(ValueError):
            self.bridge.activate(THREAD)
        self.bridge.host = "remote"
        with self.assertRaises(ValueError):
            self.bridge.activate(THREAD)
        self.bridge.desktop_tools.call.assert_not_called()

    def test_unavailable_app_does_not_attempt_navigation(self):
        from bridge.ipc import IPCError
        self.bridge.ipc.owner = Mock(side_effect=IPCError("Desktop unavailable"))
        self.bridge.desktop_tools = Mock()
        with self.assertRaises(IPCError):
            self.bridge.activate(THREAD)
        self.assertFalse(self.bridge.live[THREAD].activating)
        self.bridge.desktop_tools.call.assert_not_called()


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from bridge.auth import password_record
        cls.record = password_record("correct password test")

    setUp = fixture.HttpTests.setUp
    tearDown = fixture.HttpTests.tearDown
    request = fixture.HttpTests.request
    login = fixture.HttpTests.login

    def test_new_metadata_and_contexts_require_login(self):
        for path in ("/api/create-options", "/api/contexts?ids=" + THREAD):
            self.assertEqual(self.request("GET", path)[0], 401)
        self.server.bridge.creation_options = lambda: {"projects": [], "models": [], "canCreate": True}
        self.server.bridge.contexts = lambda ids: [{"id": ids[0], "contextUsage": context_usage(None)}]
        auth = self.login()
        self.assertEqual(self.request("GET", "/api/create-options", headers=auth)[0], 200)
        status, _, value = self.request("GET", "/api/contexts?ids=" + THREAD, headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(value["contexts"][0]["id"], THREAD)

    def test_creation_and_compaction_csrf_gate_precedes_mutation(self):
        calls = []
        self.server.bridge.create = lambda body: calls.append(body) or {"threadId": THREAD}
        self.server.bridge.compact = lambda thread_id, operation_id: calls.append(operation_id) or {"ok": True}
        auth = self.login()
        bad = {**auth, "X-CSRF-Token": "wrong"}
        self.assertEqual(self.request("POST", "/api/sessions", {"id": str(uuid.uuid4())}, bad)[0], 403)
        self.assertEqual(self.request("POST", "/api/sessions/" + THREAD + "/compact", {"id": str(uuid.uuid4())}, bad)[0], 403)
        self.assertEqual(calls, [])
        self.assertEqual(self.request("POST", "/api/sessions", {"id": str(uuid.uuid4())}, auth)[0], 200)
        self.assertEqual(len(calls), 1)

    def test_activation_requires_login_and_csrf(self):
        calls = []
        self.server.bridge.activate = lambda thread_id: calls.append(thread_id) or {"connected": True}
        path = "/api/sessions/" + THREAD + "/activate"
        self.assertEqual(self.request("POST", path, {})[0], 401)
        auth = self.login()
        self.assertEqual(self.request("POST", path, {}, {**auth, "X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(calls, [])
        self.assertEqual(self.request("POST", path, {}, auth)[0], 200)
        self.assertEqual(calls, [THREAD])

    def test_desktop_adapter_refuses_arbitrary_tools(self):
        from bridge.desktop_tools import DesktopTools
        tools = DesktopTools()
        tools.tools = Mock(side_effect=AssertionError("Must reject before desktop I/O"))
        with self.assertRaises(ValueError):
            tools.call("set_thread_archived", {})

    def test_desktop_adapter_only_discovers_unambiguous_pipe(self):
        from bridge.desktop_tools import DesktopTools
        from bridge.ipc import IPCError
        tools = DesktopTools()
        tools.pipe = None
        with patch("bridge.desktop_tools.os.name", "nt"):
            with patch("bridge.desktop_tools.os.listdir", return_value=["codex-browser-use-one", "other"]):
                self.assertEqual(tools._path(), "\\\\.\\pipe\\codex-browser-use-one")
            with patch("bridge.desktop_tools.os.listdir",
                       return_value=["codex-browser-use-one", "codex-browser-use-two"]):
                with self.assertRaises(IPCError):
                    tools._path()
