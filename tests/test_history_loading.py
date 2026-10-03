import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

from bridge.history import PAGE_SIZE, keyed_items, merged_turns, page, present_turn, state_delta
from bridge.service import LiveSession
import test_bridge as fixture
from test_bridge import THREAD, state


def turn(number, status="completed"):
    return {"turnId": str(number), "status": status, "items": [
        {"id": "u" + str(number), "type": "userMessage", "content": [{"type": "input_text", "text": "prompt-" + str(number)}]},
        {"id": "tool" + str(number), "type": "commandExecution", "command": "echo synthetic",
         "aggregatedOutput": "x" * 200000, "status": "completed"},
        {"id": "a" + str(number), "type": "agentMessage", "phase": "final_answer", "text": "answer-" + str(number)}]}


class PresentationTests(unittest.TestCase):
    def test_300_turn_history_pages_are_complete_small_and_duplicate_free(self):
        saved = {**state(), "turns": [turn(index) for index in range(300)]}
        native = {**state(), "turns": [turn(299)]}
        merged = merged_turns(saved, native)
        seen, cursor = [], None
        while True:
            rows, cursor = page(merged, cursor)
            seen = [row["turnId"] for row in rows] + seen
            self.assertLess(len(json.dumps([present_turn(row) for row in rows])), 15000)
            if not cursor:
                break
        self.assertEqual(seen, [str(index) for index in range(300)])
        self.assertLessEqual(len(page(merged)[0]), PAGE_SIZE)

    def test_native_partial_snapshot_does_not_delete_saved_turns_or_patch_base(self):
        live = LiveSession(THREAD)
        live.saved_state = {**state(), "turns": [turn(0), turn(1), turn(2)]}
        live.state = {**state(), "turns": [turn(2)]}
        live.connected = True
        with live.condition:
            view = live.view()
            self.assertEqual([row["id"] for row in view["turns"]], ["0", "1", "2"])
            self.assertTrue(view["historyComplete"])
            self.assertEqual(len(live.state["turns"]), 1)
            self.assertIs(live.view_cache["turns"], live.view()["turns"])

    def test_partial_items_preserve_earlier_saved_content(self):
        saved_turn = turn(1)
        item = {**saved_turn["items"][-1], "text": "native answer"}
        partial = {**saved_turn, "items": {"isComplete": False, "entitiesByKey": {"a": item},
                   "islands": [{"entries": [{"value": "a"}]}]}}
        merged = merged_turns({"turns": [saved_turn]}, {"turns": [partial]})
        self.assertEqual(len(merged[0]["items"]), 3)
        self.assertEqual(merged[0]["items"][-1]["text"], "native answer")

    def test_offline_saved_priority_keeps_chronological_order(self):
        live = LiveSession(THREAD)
        live.saved_state = {**state(), "turns": [turn(index) for index in range(30)]}
        live.state = {**state(), "turns": [turn(29, "inProgress")]}
        self.assertEqual([row["turnId"] for row in live.display_turns()], [str(index) for index in range(30)])
        self.assertEqual(live.view()["turns"][-1]["status"], "completed")

    def test_old_import_matches_unique_opening_not_array_index(self):
        imported = {**turn(1), "turnId": "saved-18"}
        self.assertEqual(len(merged_turns({"turns": [imported]}, {"turns": [turn(1)]})), 1)

    def test_call_and_output_same_id_have_distinct_detail_keys(self):
        raw = {"turnId": "t", "status": "inProgress", "items": [
            {"id": "call", "type": "function_call", "arguments": "arg"},
            {"id": "call", "type": "function_call_output", "output": "result"}]}
        rows = present_turn(raw)["messages"]
        self.assertNotEqual(rows[0]["detailKey"], rows[1]["detailKey"])
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        self.assertEqual(list(keyed_items(raw))[1][1]["output"], "result")

    def test_reasoning_summary_not_truncated_and_detail_available(self):
        row = present_turn({"turnId": "t", "status": "inProgress", "items": [
            {"id": "r", "type": "reasoning", "summary": ["s" * 5000], "content": ["detail" * 5000]}]})["messages"][0]
        self.assertEqual(len(row["summary"]), 5000)
        self.assertTrue(row["detailAvailable"])
        self.assertEqual(row["detail"], "")

    def test_errors_remain_visible_when_completed(self):
        raw = turn(1)
        raw["items"].insert(1, {"id": "err", "type": "error", "message": "failure"})
        self.assertTrue(any(row["role"] == "error" for row in present_turn(raw)["messages"]))

    def test_exact_detail_version_changes_for_same_length_middle_edit(self):
        raw = turn(1, "inProgress")
        original = present_turn(raw)["messages"][1]["detailVersion"]
        raw["items"][1]["aggregatedOutput"] = "x" * 100000 + "y" + "x" * 99999
        self.assertNotEqual(present_turn(raw)["messages"][1]["detailVersion"], original)

    def test_delta_omits_unchanged_turns_and_messages_with_explicit_base(self):
        first = {"sequence": 4, "turns": [present_turn(turn(1)), present_turn(turn(2, "inProgress"))]}
        second = copy.deepcopy(first)
        second["sequence"] = 5
        second["turns"][1]["messages"][-1]["text"] += " more"
        delta = state_delta(first, second)
        self.assertEqual(delta["baseSequence"], 4)
        self.assertEqual(len(delta["turns"]), 1)
        self.assertEqual(len(delta["turns"][0]["messages"]), 1)
        self.assertTrue(delta["turns"][0]["messagesDelta"])


class IndependentHistoryTests(unittest.TestCase):
    setUp = fixture.IntegrationTests.setUp
    tearDown = fixture.IntegrationTests.tearDown

    def wait_history(self):
        session = self.bridge.live[THREAD]
        with session.condition:
            self.assertTrue(session.condition.wait_for(lambda: not session.history_loading, 3))
        return session

    def test_context_preconnect_does_not_skip_saved_history(self):
        self.fixture.state["turns"] = [turn(299)]
        self.bridge.store.history = lambda _: {**state(), "turns": [turn(index) for index in range(300)]}
        self.bridge.session(THREAD)  # Native connection is already warm, like homepage contexts.
        self.bridge.view(THREAD, background=True)
        session = self.wait_history()
        self.assertTrue(session.connected)
        self.assertEqual(len(session.saved_state["turns"]), 300)
        result = self.bridge.view(THREAD, background=True)
        self.assertEqual(len(result["turns"]), PAGE_SIZE)
        self.assertTrue(result["historyCursor"])
        self.assertEqual(len(session.state["turns"]), 1)

    def test_slow_saved_history_does_not_gate_native_connection(self):
        entered, release = threading.Event(), threading.Event()
        def read(_):
            entered.set()
            release.wait(3)
            return {**state(), "turns": [turn(1)]}
        self.bridge.store.history = read
        try:
            self.bridge.view(THREAD, background=True)
            self.assertTrue(entered.wait(1))
            session = self.bridge.live[THREAD]
            with session.condition:
                self.assertTrue(session.condition.wait_for(lambda: session.connected, 1))
            self.assertTrue(session.history_loading)
        finally:
            release.set()
            self.wait_history()

    def test_saved_history_refreshes_after_ttl_and_preserves_native_state(self):
        records = [turn(1)]
        self.bridge.store.history = lambda _: {**state(), "turns": list(records)}
        self.bridge.view(THREAD, background=True)
        session = self.wait_history()
        records.append(turn(2))
        session.history_read_at = time.monotonic() - 31
        session.history_retry_at = 0
        with session.condition:
            session.condition.wait_for(lambda: session.connected, 1)
        native = session.state
        self.bridge.view(THREAD, background=True)
        self.wait_history()
        self.assertEqual(len(session.saved_state["turns"]), 2)
        self.assertIs(session.state, native)

    def test_offline_saved_refresh_wins_old_fallback_and_native_turn(self):
        session = LiveSession(THREAD)
        old = turn(1, "inProgress")
        updated = turn(1)
        updated["items"][-1]["text"] = "saved completed"
        session.state = {**state(), "turns": [old]}
        session.saved_state = {**state(), "turns": [updated]}
        self.assertEqual(session.view()["turns"][0]["status"], "completed")
        self.assertEqual(session.view()["turns"][0]["messages"][-1]["text"], "saved completed")
        self.assertEqual(session.state["turns"][0]["status"], "inProgress")
        with session.condition:
            session.connected = True
            session.changed()
        self.assertEqual(session.view()["turns"][0]["status"], "inProgress")

    def test_first_offline_read_never_becomes_native_patch_base(self):
        self.bridge.ipc.owner = lambda *_: (_ for _ in ()).throw(fixture.IPCError("no-client-found"))
        self.bridge.store.history = lambda _: {**state(), "turns": [turn(1)]}
        self.bridge.view(THREAD, background=True)
        session = self.wait_history()
        self.assertIsNone(session.state)
        self.assertEqual(session.view()["turns"][0]["id"], "1")

    def test_rollout_append_truncate_and_partial_line_recovery(self):
        from bridge.store import SessionStore
        path = self.root / "sessions" / "history.jsonl"
        path.parent.mkdir()
        store = SessionStore(self.root)
        store.get = lambda _: {"rollout_path": str(path), "cwd": str(self.root)}
        first = {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t"}}
        message = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": "complete text"}]}}
        encoded = json.dumps(message)
        path.write_text(json.dumps(first) + "\n" + encoded[:10], encoding="utf-8")
        self.assertEqual(len(store.history(THREAD)["turns"][0]["items"]), 0)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(encoded[10:] + "\n")
        self.assertEqual(len(store.history(THREAD)["turns"][0]["items"]), 1)
        path.write_text(json.dumps(message) + "\n", encoding="utf-8")
        self.assertTrue(store.history(THREAD)["turns"][0]["turnId"].startswith("saved-"))

    def test_slow_artifact_filesystem_does_not_hold_session_lock(self):
        entered, release = threading.Event(), threading.Event()
        self.bridge.view(THREAD)
        session = self.bridge.live[THREAD]
        deadline = time.monotonic() + 2
        while session.artifact_loading and time.monotonic() < deadline:
            time.sleep(.01)
        session.artifact_read_at = 0
        with patch("bridge.service.artifact_paths", side_effect=lambda *_: (entered.set(), release.wait(2), {})[-1]):
            try:
                started = time.monotonic()
                self.bridge.view(THREAD, background=True)
                self.assertLess(time.monotonic() - started, .5)
                self.assertTrue(entered.wait(1))
                self.assertTrue(session.condition.acquire(timeout=.2))
                session.condition.release()
            finally:
                release.set()
                deadline = time.monotonic() + 2
                while session.artifact_loading and time.monotonic() < deadline:
                    time.sleep(.01)

    def test_approval_related_item_enrichment_survives_small_view(self):
        self.fixture.state.update(turns=[{"turnId": "t", "status": "inProgress", "items": [
            {"id": "cmd", "type": "commandExecution", "command": "echo synthetic"}]}],
            requests=[{"id": "r", "method": "item/commandExecution/requestApproval",
                       "params": {"itemId": "cmd"}}])
        view = self.bridge.view(THREAD)
        self.assertEqual(view["requests"][0]["params"]["command"], "echo synthetic")


if __name__ == "__main__":
    unittest.main()
