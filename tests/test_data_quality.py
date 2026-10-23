import unittest
import pandas as pd
from app import data_quality as dq
from tests.test_scan_speed_and_labels import synth

IST = dq.pd.Timestamp("2026-10-21", tz="Asia/Kolkata").tz


class DataQualityTests(unittest.TestCase):
    def setUp(self):
        self.df = synth(1)
        self.last = self.df["timestamp"].iloc[-1]          # 2026-10-21 15:30 IST end label

    def test_clean_frame_passes_when_fresh(self):
        block, _ = dq.check_frame(self.df, self.last + pd.Timedelta(seconds=20))
        self.assertEqual(block, [])

    def test_stale_last_bar_blocks_during_session(self):
        now = pd.Timestamp("2026-10-21 11:15:20", tz=IST)
        block, _ = dq.check_frame(self.df[self.df["timestamp"] < pd.Timestamp("2026-10-21 11:15", tz=IST)], now)
        self.assertTrue(any(b.startswith("STALE_LAST_BAR") for b in block))

    def test_fresh_bar_passes_during_session(self):
        now = pd.Timestamp("2026-10-21 11:15:20", tz=IST)
        sub = self.df[self.df["timestamp"] <= pd.Timestamp("2026-10-21 11:15", tz=IST)]
        self.assertEqual(dq.check_frame(sub, now)[0], [])

    def test_missing_bar_in_recent_window_blocks(self):
        df = self.df.drop(self.df.index[-6]).reset_index(drop=True)
        block, _ = dq.check_frame(df, self.last + pd.Timedelta(seconds=20))
        self.assertTrue(any(b.startswith("MISSING_BARS") for b in block))

    def test_old_gap_outside_window_is_ignored(self):
        df = self.df.drop(self.df.index[-60]).reset_index(drop=True)
        block, _ = dq.check_frame(df, self.last + pd.Timedelta(seconds=20))
        self.assertEqual(block, [])

    def test_bad_ohlc_blocks(self):
        df = self.df.copy()
        df.loc[df.index[-3], "high"] = df.loc[df.index[-3], "low"] - 1
        self.assertIn("OHLC_INCONSISTENT", dq.check_frame(df, self.last)[0])

    def test_zero_latest_volume_blocks_stock_but_not_index(self):
        df = self.df.copy()
        df.loc[df.index[-1], "volume"] = 0
        self.assertIn("LATEST_VOLUME_ZERO_OR_NAN", dq.check_frame(df, self.last)[0])
        self.assertEqual(dq.check_frame(df, self.last, require_volume=False)[0], [])

    def test_duplicates_and_short_history_block(self):
        dup = pd.concat([self.df, self.df.tail(1)], ignore_index=True)
        self.assertIn("DUPLICATE_TIMESTAMPS", dq.check_frame(dup, self.last)[0])
        self.assertTrue(any(b.startswith("TOO_FEW") for b in dq.check_frame(self.df.head(30), self.last)[0]))

    def test_off_session_has_no_freshness_requirement(self):
        sat = pd.Timestamp("2026-10-24 11:00", tz=IST)
        self.assertEqual(dq.check_frame(self.df, sat)[0], [])


if __name__ == "__main__":
    unittest.main()
