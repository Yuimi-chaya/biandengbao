import gzip
import http.client
import json
import unittest
from unittest.mock import Mock

from bridge.model import normalize_state
from bridge.store import SessionStore
import test_bridge as fixture
from test_bridge import THREAD, state


class EffortTests(unittest.TestCase):
    def test_current_settings_override_old_turn_effort_and_model(self):
        value = state()
        value.update(latestModel="old", latestReasoningEffort="minimal",
                     latestThreadSettings={"model": "new", "effort": "high"})
        self.assertEqual(normalize_state(value)["effort"], "high")
        self.assertEqual(normalize_state(value)["model"], "new")

    def test_collaboration_and_legacy_fallbacks_keep_exact_effort(self):
        value = state()
        value["latestCollaborationMode"] = {"settings": {"model": "model", "reasoning_effort": "xhigh"}}
        self.assertEqual(normalize_state(value)["effort"], "xhigh")
        value["latestThreadSettings"] = {"effort": None}
        self.assertIsNone(normalize_state(value)["effort"])
        del value["latestThreadSettings"], value["latestCollaborationMode"]
        value["latestReasoningEffort"] = "high"
        self.assertEqual(normalize_state(value)["effort"], "high")

    def test_saved_history_reads_latest_model_and_effort(self):
        import tempfile
        from pathlib import Path
        from test_bridge import ROOT
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as folder:
            root = Path(folder)
            path = root / "sessions" / "saved.jsonl"
            path.parent.mkdir()
            records = [{"type": "turn_context", "payload": {"model": "old", "effort": "low"}},
                       {"type": "turn_context", "payload": {"model": "new", "effort": "high"}}]
            path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            store = SessionStore(root)
            store.get = lambda _: {"rollout_path": str(path), "cwd": str(root)}
            view = normalize_state(store.history(THREAD), connected=False)
            self.assertEqual((view["model"], view["effort"]), ("new", "high"))


class FastListTests(unittest.TestCase):
    setUp = fixture.IntegrationTests.setUp
    tearDown = fixture.IntegrationTests.tearDown

    def test_list_does_not_scan_history_or_usage(self):
        self.bridge.store.usage = Mock(side_effect=AssertionError("List must not scan rollouts"))
        self.bridge.store.history = Mock(side_effect=AssertionError("List must not read history"))
        rows = self.bridge.list()
        self.assertTrue(rows)
        self.assertFalse(rows[0]["contextUsage"]["available"])

    def test_list_keeps_cached_saved_usage(self):
        self.bridge.store.usage_cache[THREAD] = (None, {
            "last_token_usage": {"total_tokens": 100}, "model_context_window": 1000})
        row = next(row for row in self.bridge.list() if row["id"] == THREAD)
        self.assertEqual(row["contextUsage"]["percent"], 10)
        self.assertFalse(row["contextUsage"]["live"])


class AssetTests(unittest.TestCase):
    setUpClass = classmethod(fixture.HttpTests.setUpClass.__func__)
    setUp = fixture.HttpTests.setUp
    tearDown = fixture.HttpTests.tearDown

    def raw(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_static_compression_and_conditional_revalidation(self):
        status, headers, body = self.raw("/vendor/lucide.js", {"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertEqual(headers["Cache-Control"], "private, no-cache")
        status, plain_headers, plain = self.raw("/vendor/lucide.js")
        self.assertEqual(gzip.decompress(body), plain)
        self.assertLess(len(body), len(plain) / 2)
        status, _, body = self.raw("/vendor/lucide.js", {"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(plain_headers["ETag"], headers["ETag"])

    def test_gzip_opt_out_and_index_never_cached(self):
        status, headers, _ = self.raw("/settings.js", {"Accept-Encoding": "gzip;q=0"})
        self.assertNotIn("Content-Encoding", headers)
        status, headers, _ = self.raw("/")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.raw("/", {"If-None-Match": headers["ETag"]})[0], 200)

    def test_api_data_stays_no_store_and_compresses(self):
        self.server.bridge.list = lambda **_: [{"title": "long" * 1000}]
        self.server.auth.config = {"mode": "none"}
        token, _ = self.server.auth.login("", "", "127.0.0.1")
        from bridge.auth import Auth
        status, headers, body = self.raw("/api/sessions", {
            "Cookie": Auth.COOKIE + "=" + token, "Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertTrue(json.loads(gzip.decompress(body))["sessions"])


if __name__ == "__main__":
    unittest.main()
