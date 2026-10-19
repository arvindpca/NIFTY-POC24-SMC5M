import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.daily_summary import format_summary, is_today

IST = ZoneInfo("Asia/Kolkata")


class DailySummaryTests(unittest.TestCase):
    def test_today_uses_ist_for_utc_timestamp(self):
        day = datetime(2026, 10, 20).date()
        self.assertTrue(is_today("2026-10-19T20:00:00+00:00", day))

    def test_other_day_is_false(self):
        day = datetime(2026, 10, 20).date()
        self.assertFalse(is_today("2026-10-19T10:00:00+05:30", day))

    def test_invalid_timestamp_is_false(self):
        self.assertFalse(is_today("not-a-date", datetime(2026, 10, 20).date()))

    def test_summary_includes_paper_only_and_pnl(self):
        message = format_summary({
            "date": "2026-10-20", "entries": 2, "closed": 1,
            "wins": 1, "losses": 0, "breakeven": 0, "net_pnl": 125.5,
            "open_positions": 1, "errors": [],
        })
        self.assertIn("VIRTUAL TRADING ONLY", message)
        self.assertIn("Net P&L today: ₹125.50", message)
        self.assertIn("Errors today: 0", message)


if __name__ == "__main__":
    unittest.main()
