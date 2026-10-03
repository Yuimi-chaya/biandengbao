import threading
import unittest
import uuid
from unittest.mock import Mock

from bridge.service import Bridge, LiveSession, MAX_IDLE_SESSIONS


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.lock = threading.RLock()
        self.bridge.submit_lock = threading.RLock()
        self.bridge.live = {}
        self.bridge.submissions = {}
        self.bridge.host = "local"
        self.bridge.ipc = Mock()

    def session(self, touched=0, connected=True):
        session = LiveSession(str(uuid.uuid4()))
        session.touched = touched
        session.connected = connected
        session.owner = "synthetic"
        session.state = {"threadRuntimeStatus": {"type": "idle"}}
        self.bridge.live[session.id] = session
        return session

    def test_recent_connected_session_survives_five_minute_gap(self):
        session = self.session()
        self.bridge._trim_sessions(now=600)
        self.assertIs(self.bridge.live[session.id], session)
        self.bridge.ipc.follow.assert_not_called()

    def test_returning_thread_uses_existing_connection_without_discovery(self):
        session = self.session()
        self.bridge._trim_sessions(now=600)
        self.assertIs(self.bridge.session(session.id), session)
        self.bridge.ipc.owner.assert_not_called()
        self.bridge.ipc.follow.assert_not_called()

    def test_expired_connected_session_unsubscribes(self):
        session = self.session()
        self.bridge._trim_sessions(now=1801)
        self.assertNotIn(session.id, self.bridge.live)
        self.bridge.ipc.follow.assert_called_once_with(
            session.id, session.owner, False, host=self.bridge.host)

    def test_cold_history_still_expires_after_five_minutes(self):
        session = self.session(connected=False)
        self.bridge._trim_sessions(now=301)
        self.assertNotIn(session.id, self.bridge.live)
        self.bridge.ipc.follow.assert_not_called()

    def test_idle_count_is_bounded_and_oldest_retired_first(self):
        sessions = [self.session(touched=index) for index in range(MAX_IDLE_SESSIONS + 3)]
        self.bridge._trim_sessions(now=100)
        self.assertEqual(len(self.bridge.live), MAX_IDLE_SESSIONS)
        self.assertTrue(all(session.id not in self.bridge.live for session in sessions[:3]))
        self.assertTrue(all(session.id in self.bridge.live for session in sessions[3:]))

    def test_viewed_active_queued_and_inflight_sessions_not_evicted(self):
        viewed = self.session()
        viewed.viewers = 1
        active = self.session()
        active.state["threadRuntimeStatus"]["type"] = "active"
        queued = self.session()
        self.bridge.submissions[queued.id + ":request"] = {"status": "queued"}
        connecting = self.session()
        connecting.connecting = True
        activating = self.session()
        activating.activating = True
        compacting = self.session()
        compacting.compaction_pending = True
        self.bridge._trim_sessions(now=3600)
        self.assertEqual(len(self.bridge.live), 6)
        self.bridge.ipc.follow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
