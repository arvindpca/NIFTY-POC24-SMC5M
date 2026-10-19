"""Unit tests for Final B paper-trading timing and exit rules."""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.rules import exit_decision, is_stale_entry, target_for

IST = ZoneInfo("Asia/Kolkata")


class FinalBRulesTests(unittest.TestCase):
    def test_long_target_is_1_5r(self):
        self.assertEqual(target_for(100, 98, "LONG"), 103.0)

    def test_short_target_is_1_5r(self):
        self.assertEqual(target_for(100, 102, "SHORT"), 97.0)

    def test_invalid_zero_risk_rejected(self):
        with self.assertRaises(ValueError):
            target_for(100, 100, "LONG")

    def test_force_exit_at_1500_close_precedes_intrabar_sl(self):
        ts = datetime(2026, 10, 19, 15, 0, tzinfo=IST)
        result = exit_decision(ts, {"open": 100, "high": 105, "low": 95, "close": 101},
                               "LONG", 98, 103)
        self.assertEqual(result, (101.0, "TIME_EXIT"))

    def test_before_force_exit_stop_wins_if_both_levels_touched(self):
        ts = datetime(2026, 10, 19, 14, 45, tzinfo=IST)
        result = exit_decision(ts, {"open": 100, "high": 104, "low": 97, "close": 101},
                               "LONG", 98, 103)
        self.assertEqual(result, (98.0, "SL_FIRST_AMBIGUOUS"))

    def test_target_exit_before_force_exit(self):
        ts = datetime(2026, 10, 19, 14, 45, tzinfo=IST)
        result = exit_decision(ts, {"open": 100, "high": 104, "low": 99, "close": 102},
                               "LONG", 98, 103)
        self.assertEqual(result, (103.0, "TP"))

    def test_stale_entry_skipped_after_grace(self):
        now = datetime(2026, 10, 19, 10, 30, tzinfo=IST)
        self.assertTrue(is_stale_entry(now - timedelta(minutes=31), now))

    def test_recent_entry_not_stale_at_grace_boundary(self):
        now = datetime(2026, 10, 19, 10, 30, tzinfo=IST)
        self.assertFalse(is_stale_entry(now - timedelta(minutes=20), now))


if __name__ == "__main__":
    unittest.main()
