import unittest
from datetime import datetime, timedelta

from recovery_policy import assess_auto_recovery, expected_limit_for_stage, within_minutes


class RecoveryPolicyTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 6, 12, 0, 0)
        self.base = dict(
            active=True,
            owner="guard",
            stage="stabilizing",
            recovery_ready=True,
            manual_required=False,
            auto_resume_enabled=True,
            actual_limit=0,
            previous_limit=2,
            state="NORMAL",
            identity_ok=True,
            library_ok=True,
            db_lock_count=0,
            scanning_count=0,
            d_state_count=1,
            max_d_state=3,
            recovery_ready_at=(self.now - timedelta(minutes=11)).isoformat(),
            probe_started_at="",
            stable_minutes=10,
            probe_minutes=15,
            now=self.now,
        )

    def test_stable_recovery_starts_one_scan_probe(self):
        result = assess_auto_recovery(**self.base)
        self.assertEqual(result["action"], "start_probe")
        self.assertEqual(result["target_limit"], 1)

    def test_host_io_pressure_delays_resume(self):
        values = dict(self.base, d_state_count=5)
        result = assess_auto_recovery(**values)
        self.assertEqual(result["action"], "wait_stable")
        self.assertIn("D-state", result["reason"])

    def test_manual_or_external_limit_change_releases_control(self):
        values = dict(self.base, actual_limit=1)
        result = assess_auto_recovery(**values)
        self.assertEqual(result["action"], "external_override")

    def test_probe_allows_busy_and_completes_to_previous_limit(self):
        values = dict(
            self.base,
            stage="probe",
            actual_limit=1,
            state="BUSY",
            probe_started_at=(self.now - timedelta(minutes=16)).isoformat(),
        )
        result = assess_auto_recovery(**values)
        self.assertEqual(result["action"], "complete_resume")
        self.assertEqual(result["target_limit"], 2)

    def test_probe_failure_requests_immediate_pause(self):
        values = dict(
            self.base,
            stage="probe",
            actual_limit=1,
            state="PLEX_UNAVAILABLE",
            identity_ok=False,
            probe_started_at=(self.now - timedelta(minutes=2)).isoformat(),
        )
        result = assess_auto_recovery(**values)
        self.assertEqual(result["action"], "probe_failed")
        self.assertEqual(result["target_limit"], 0)

    def test_manual_required_never_auto_resumes(self):
        values = dict(self.base, manual_required=True)
        result = assess_auto_recovery(**values)
        self.assertEqual(result["action"], "none")

    def test_auto_resume_disabled_never_starts_probe(self):
        values = dict(self.base, auto_resume_enabled=False)
        result = assess_auto_recovery(**values)
        self.assertEqual(result["action"], "none")

    def test_time_window_helpers(self):
        self.assertEqual(expected_limit_for_stage("probe"), 1)
        self.assertEqual(expected_limit_for_stage("stabilizing"), 0)
        self.assertTrue(within_minutes((self.now - timedelta(minutes=20)).isoformat(), 30, self.now))
        self.assertFalse(within_minutes((self.now - timedelta(minutes=31)).isoformat(), 30, self.now))


if __name__ == "__main__":
    unittest.main()
