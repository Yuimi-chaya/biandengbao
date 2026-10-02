import http.client
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import test_bridge as fixture
from bridge.model import normalize_item, normalize_state
from bridge.store import SessionStore
from bridge.uploads import Uploads, MAX_FILE
from test_bridge import THREAD, ROOT, state


class ReasoningTimingTests(unittest.TestCase):
    def test_file_creation_retains_kind_and_content_once(self):
        item = {"type": "fileChange", "changes": [
            {"path": "new.py", "kind": {"type": "add"}, "diff": "print('new')\n"},
            {"path": "old.py", "kind": {"type": "update"}, "diff": "-old\n+new"}]}
        row = normalize_item(item)
        self.assertEqual(row["changes"], item["changes"])
        self.assertEqual(row["text"], "new.py\nold.py")
        self.assertNotIn("print", row["text"])
        item["changes"][0]["kind"]["type"] = "update"
        self.assertEqual(row["changes"][0]["kind"]["type"], "add")

    def test_unknown_file_kind_is_not_invented(self):
        row = normalize_item({"type": "fileChange", "changes": [{"path": "unknown.py", "diff": "body"}]})
        self.assertIsNone(row["changes"][0]["kind"])

    def test_summary_detail_are_separate_and_encrypted_content_not_exposed(self):
        row = normalize_item({"type": "reasoning", "summary": ["**Headline**", {"text": "Summary"}],
                              "content": [{"type": "reasoning_text", "text": "Available detail"}],
                              "encrypted_content": "not-readable"})
        self.assertEqual(row["summary"], "**Headline**\n\nSummary")
        self.assertEqual(row["detail"], "Available detail")
        self.assertNotIn("encrypted_content", row)
        self.assertNotIn("attachments", row)

    def test_no_detail_is_not_invented(self):
        self.assertEqual(normalize_item({"type": "reasoning", "summary": ["Summary"]})["detail"], "")

    def test_native_duration_and_completion_use_recorded_values(self):
        value = state()
        value["turns"] = [{"turnId": "t", "status": "completed", "turnStartedAtMs": 1000,
                           "durationMs": 4321, "items": []}]
        turn = normalize_state(value)["turns"][0]
        self.assertEqual((turn["durationMs"], turn["completedAt"]), (4321, 5321))
        del value["turns"][0]["durationMs"]
        turn = normalize_state(value)["turns"][0]
        self.assertIsNone(turn["durationMs"])
        self.assertIsNone(turn["completedAt"])

    def test_saved_timestamp_and_reasoning_are_preserved(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as folder:
            root = Path(folder)
            path = root / "sessions" / "saved.jsonl"
            path.parent.mkdir()
            records = [
                {"timestamp": "2026-10-01T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t"}},
                {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": "Summary"}], "content": [{"text": "Detail"}]}},
                {"timestamp": "2026-10-01T00:00:12Z", "type": "event_msg", "payload": {"type": "task_complete"}},
            ]
            path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            store = SessionStore(root)
            store.get = lambda _: {"rollout_path": str(path), "cwd": str(root)}
            turn = normalize_state(store.history(THREAD), False)["turns"][0]
            self.assertEqual(turn["durationMs"], 12000)
            self.assertEqual(turn["messages"][0]["detail"], "Detail")


class AttachmentTests(unittest.TestCase):
    setUp = fixture.IntegrationTests.setUp
    tearDown = fixture.IntegrationTests.tearDown

    def test_attachment_only_uses_original_owner_and_deduplicates(self):
        upload = self.bridge.upload(THREAD, "owner", "notes.txt", b"sample")
        message_id = str(uuid.uuid4())
        self.bridge.send(THREAD, "", message_id, attachments=[upload["id"]], upload_owner="owner")
        self.bridge.send(THREAD, "", message_id, attachments=[upload["id"]], upload_owner="owner")
        calls = [r for r in self.fixture.requests if r["method"] == "thread-follower-start-turn"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["targetClientId"], "owner")
        start = calls[0]["params"]["turnStart"]
        self.assertEqual(start["context"]["attachments"][0]["label"], "notes.txt")
        self.assertIn("# Files mentioned by the user:", start["request"]["input"][-1]["text"])
        self.assertTrue(start["context"]["inheritThreadSettings"])

    def test_image_uses_local_image_and_steer_descriptors(self):
        upload = self.bridge.upload(THREAD, "owner", "image.png", b"\x89PNG\r\n\x1a\nsample")
        self.fixture.state["threadRuntimeStatus"] = {"type": "active"}
        self.bridge.send(THREAD, "see image", str(uuid.uuid4()), "steer", attachments=[upload["id"]], upload_owner="owner")
        call = next(r for r in self.fixture.requests if r["method"] == "thread-follower-steer-turn")
        image = next(item for item in call["params"]["input"] if item["type"] == "localImage")
        self.assertEqual(image["path"], call["params"]["attachments"][0]["path"])
        self.assertTrue(call["params"]["attachments"][0]["isImageAttachment"])

    def test_attachment_ids_are_scoped_and_not_paths(self):
        upload = self.bridge.upload(THREAD, "owner", "notes.txt", b"sample")
        for thread, owner in ((THREAD, "other"), (str(uuid.uuid4()), "owner")):
            with self.assertRaises(PermissionError):
                self.bridge.uploads.resolve(thread, owner, [upload["id"]])
        for ids in (["../secret"], [upload["id"]] * 2, [str(uuid.uuid4())] * 9):
            with self.assertRaises(ValueError):
                self.bridge.uploads.resolve(THREAD, "owner", ids)

    def test_validation_rejects_unsafe_names_types_size_and_false_images(self):
        for name, data in (("../bad.txt", b"a"), ("bad.exe", b"a"), ("bad.png", b"not png"), ("notes.txt", b""),
                           ("notes.txt", b"x" * (MAX_FILE + 1)), ("bad\nname.txt", b"a")):
            with self.assertRaises(ValueError):
                self.bridge.upload(THREAD, "owner", name, data)
        self.bridge.host = "remote"
        with self.assertRaises(ValueError):
            self.bridge.upload(THREAD, "owner", "notes.txt", b"a")

    def test_queued_attachments_survive_ledger_reload_and_modified_retry_refused(self):
        upload = self.bridge.upload(THREAD, "owner", "notes.txt", b"sample")
        message_id = str(uuid.uuid4())
        self.bridge.send(THREAD, "read", message_id, "queue", attachments=[upload["id"]], upload_owner="owner")
        ledger = json.loads(self.bridge.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(ledger[THREAD + ":" + message_id]["attachments"][0]["name"], "notes.txt")
        with self.assertRaises(ValueError):
            self.bridge.send(THREAD, "read", message_id, "queue", attachments=[], upload_owner="owner")

    def test_stop_rejects_changed_turn(self):
        self.fixture.state.update(threadRuntimeStatus={"type": "active"}, turns=[{"turnId": "new", "status": "inProgress", "items": []}])
        with self.assertRaises(ValueError):
            self.bridge.interrupt(THREAD, "old")
        self.bridge.interrupt(THREAD, "new")
        calls = [r for r in self.fixture.requests if r["method"] == "thread-follower-interrupt-turn"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["params"]["expectedTurnId"], "new")

    def test_upload_store_has_byte_and_count_limits(self):
        with patch("bridge.uploads.MAX_TOTAL", 2):
            with self.assertRaises(ValueError):
                self.bridge.upload(THREAD, "owner", "notes.txt", b"123")
        with patch("bridge.uploads.Path.iterdir", return_value=[Mock(is_dir=lambda: True)] * 512):
            with self.assertRaises(ValueError):
                self.bridge.upload(THREAD, "owner", "notes.txt", b"1")


class UploadEndpoints(unittest.TestCase):
    setUpClass = classmethod(fixture.HttpTests.setUpClass.__func__)
    setUp = fixture.HttpTests.setUp
    tearDown = fixture.HttpTests.tearDown
    request = fixture.HttpTests.request
    login = fixture.HttpTests.login

    def raw_upload(self, headers=None, data=b"sample"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("POST", "/api/sessions/" + THREAD + "/upload", data, headers={
                "Content-Type": "application/octet-stream", "X-Filename": "notes.txt",
                "Origin": self.origin, **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_auth_csrf_before_upload_and_bound_attachment(self):
        self.assertEqual(self.raw_upload()[0], 401)
        auth = self.login()
        self.assertEqual(self.raw_upload({"Cookie": auth["Cookie"]})[0], 403)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as folder:
            uploads = Uploads(folder)
            self.server.bridge.upload = uploads.put
            status, result = self.raw_upload(auth)
            self.assertEqual(status, 200)
            self.assertEqual(result["name"], "notes.txt")
            self.assertNotIn("path", result)

    def test_stop_requires_explicit_turn(self):
        auth = self.login()
        status, _, _ = self.request("POST", "/api/sessions/" + THREAD + "/stop", {}, auth)
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
