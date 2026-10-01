import json
import http.client
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_bridge as fixture
from bridge.service import latest_prompt
from bridge.store import SessionStore, prompt_preview
from test_bridge import ROOT, THREAD


class PromptPreviewTests(unittest.TestCase):
    def test_native_latest_input_and_accepted_steering(self):
        state = {"turns": [{"items": [{"type": "userMessage", "content": [{"type": "text", "text": "old"}]},
                                    {"type": "steeringUserMessage", "status": "accepted", "input": [{"type": "text", "text": "latest\n input"}]},
                                    {"type": "steeringUserMessage", "status": "rejected", "input": [{"type": "text", "text": "rejected"}]}]}]}
        self.assertEqual(latest_prompt(state), "latest input")
        self.assertEqual(latest_prompt({"turns": [{"params": {"input": [{"type": "text", "text": "opening"}]}}]}), "opening")
        self.assertIsNone(latest_prompt(None))
        self.assertEqual(prompt_preview("x" * 1000), "x" * 240 + "...")

    def test_saved_latest_input_cache_invalidation_and_path_safety(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as folder:
            home = Path(folder)
            path = home / "sessions" / "preview.jsonl"
            path.parent.mkdir()
            records = [{"type": "response_item", "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "first"}]}},
                       {"type": "event_msg", "payload": {"type": "user_message", "message": "latest"}},
                       {"type": "event_msg", "payload": {"type": "token_count", "info": {"last": {"totalTokens": 10}, "modelContextWindow": 100}}}]
            path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            store = SessionStore(home)
            store.get = lambda _: {"rollout_path": str(path)}
            value = store.summary(THREAD)
            self.assertEqual(value["latestPrompt"], "latest")
            with patch("pathlib.Path.open", side_effect=AssertionError("cached file must not reopen")):
                self.assertEqual(store.summary(THREAD), value)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "<b>new</b>"}}) + "\n")
            self.assertEqual(store.summary(THREAD)["latestPrompt"], "<b>new</b>")
            outside = home / "outside.jsonl"
            store.get = lambda _: {"rollout_path": str(outside)}
            self.assertIsNone(store.summary(THREAD)["latestPrompt"])

    def test_bounded_tail_does_not_scan_entire_history(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as folder:
            home = Path(folder)
            path = home / "sessions" / "big.jsonl"
            path.parent.mkdir()
            path.write_text('{"type":"ignored"}\n' * 150000
                            + json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "tail"}}),
                            encoding="utf-8")
            store = SessionStore(home)
            store.get = lambda _: {"rollout_path": str(path)}
            self.assertEqual(store.summary(THREAD)["latestPrompt"], "tail")


class PreviewReadBoundaryTests(unittest.TestCase):
    setUp = fixture.IntegrationTests.setUp
    tearDown = fixture.IntegrationTests.tearDown

    def test_list_does_not_scan_saved_history(self):
        self.bridge.store.summary = Mock(side_effect=AssertionError("no disk history scans in list"))
        self.assertIn("latestPrompt", self.bridge.list()[0])
        self.bridge.store.summary.assert_not_called()

    def test_visible_context_preview_does_not_activate_or_start_work(self):
        self.bridge.desktop_tools = Mock()
        self.fixture.state["turns"] = [{"turnId": "input", "status": "completed",
                                      "items": [{"type": "userMessage", "content": [{"type": "text", "text": "hello"}]}]}]
        self.bridge.view(THREAD)
        value = self.bridge.contexts([THREAD])[0]
        self.assertEqual(value["latestPrompt"], "hello")
        self.bridge.desktop_tools.call.assert_not_called()
        self.assertFalse(any("start-turn" in request["method"] for request in self.fixture.requests))


class ReadingAssetTests(unittest.TestCase):
    setUpClass = classmethod(fixture.HttpTests.setUpClass.__func__)
    setUp = fixture.HttpTests.setUp
    tearDown = fixture.HttpTests.tearDown
    request = fixture.HttpTests.request

    def test_reading_script_is_served_as_javascript(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("GET", "/reading.js")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("text/javascript", response.getheader("Content-Type"))
            self.assertIn(b"ReadingUI", response.read())
        finally:
            connection.close()
