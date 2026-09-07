import unittest
from datetime import datetime, timedelta

from event_policy import normal_observation_ids_to_delete, should_persist_observation


class EventPolicyTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, 15, 0, 0)

    def test_dashboard_is_read_only(self):
        persist, reason = should_persist_observation(
            "dashboard", "NORMAL", "NORMAL", self.now - timedelta(hours=2), self.now, 60
        )
        self.assertFalse(persist)
        self.assertEqual(reason, "transient")

    def test_normal_state_change_is_kept(self):
        persist, reason = should_persist_observation(
            "schedule", "NORMAL", "BUSY", self.now - timedelta(minutes=2), self.now, 60
        )
        self.assertTrue(persist)
        self.assertEqual(reason, "state_change")

    def test_normal_is_suppressed_until_heartbeat(self):
        persist, reason = should_persist_observation(
            "schedule", "NORMAL", "NORMAL", self.now - timedelta(minutes=20), self.now, 60
        )
        self.assertFalse(persist)
        self.assertEqual(reason, "suppressed")
        persist, reason = should_persist_observation(
            "schedule", "NORMAL", "NORMAL", self.now - timedelta(minutes=61), self.now, 60
        )
        self.assertTrue(persist)
        self.assertEqual(reason, "heartbeat")

    def test_alert_state_is_kept_every_cycle(self):
        persist, reason = should_persist_observation(
            "schedule", "PLEXMATE_DB_LOCKED", "PLEXMATE_DB_LOCKED",
            self.now - timedelta(minutes=2), self.now, 60
        )
        self.assertTrue(persist)
        self.assertEqual(reason, "alert")

    def test_compaction_preserves_normal_recovery_transition(self):
        rows = [
            {"id": 1, "created_at": self.now - timedelta(days=40), "trigger": "schedule", "state": "NORMAL", "action": "observe"},
            {"id": 2, "created_at": self.now - timedelta(days=39), "trigger": "schedule", "state": "PLEX_UNAVAILABLE", "action": "observe"},
            {"id": 3, "created_at": self.now - timedelta(days=39) + timedelta(minutes=2), "trigger": "schedule", "state": "NORMAL", "action": "observe"},
            {"id": 4, "created_at": self.now - timedelta(minutes=50), "trigger": "schedule", "state": "NORMAL", "action": "observe"},
            {"id": 5, "created_at": self.now - timedelta(minutes=20), "trigger": "schedule", "state": "NORMAL", "action": "observe"},
            {"id": 6, "created_at": self.now - timedelta(minutes=10), "trigger": "dashboard", "state": "NORMAL", "action": "observe"},
        ]
        delete_ids = normal_observation_ids_to_delete(rows, self.now - timedelta(days=30))
        self.assertIn(1, delete_ids)
        self.assertNotIn(3, delete_ids)  # recovery transition is permanent
        self.assertIn(4, delete_ids)     # same hour, newest representative wins
        self.assertNotIn(5, delete_ids)
        self.assertIn(6, delete_ids)


if __name__ == "__main__":
    unittest.main()
