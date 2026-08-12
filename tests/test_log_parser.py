import unittest

from log_parser import parse_lines


class LogParserTest(unittest.TestCase):
    def test_collects_queue_wait_timeout_and_search(self):
        lines = [
            "Aug 10, 2026 14:00:00.000 [1] DEBUG - Push: Waiting for refresh queue of 193 items to quiesce.",
            "Aug 10, 2026 14:00:01.000 [1] DEBUG - [Req#abc] [com.plexapp.agents.sjva_agent] Plug-in is starting, waiting 120 seconds for it to complete.",
            "Aug 10, 2026 14:00:02.000 [1] DEBUG - [HttpClient] HTTP simulating 408 after curl timeout",
            "Aug 10, 2026 14:00:03.000 [1] DEBUG - /system/agents/search?mediaType=1&id=1936195&identifier=com.plexapp.agents.sjva_agent_movie&filename=%2Fmedia%2Fmovie.mkv",
        ]
        report = parse_lines(lines)
        self.assertEqual(report["latest_queue"]["count"], 193)
        self.assertEqual(report["agent_waits"][0]["seconds"], 120)
        self.assertEqual(len(report["timeout_events"]), 1)
        self.assertEqual(report["searches"][0]["rating_key"], "1936195")


    def test_collects_plexmate_db_locks_without_paths(self):
        from log_parser import parse_plexmate_lines

        lines = [
            "[2026-08-12 03:13:34,122|INFO|plex_mate|plex_db.py:90] {'log': 'Runtime error near line 1: database is locked (5)'}",
            "[2026-08-12 03:13:39,996|INFO|plex_mate|plex_db.py:90] {'log': 'Runtime error near line 1: database is locked (5)'}",
            "[2026-08-12 03:13:40,000|INFO|plex_mate|plex_db.py:90] normal",
        ]
        report = parse_plexmate_lines(lines)
        self.assertEqual(len(report["db_locks"]), 2)
        self.assertEqual(report["db_locks"][0]["code"], 5)
        self.assertNotIn("line", report["db_locks"][0])

if __name__ == "__main__":
    unittest.main()
