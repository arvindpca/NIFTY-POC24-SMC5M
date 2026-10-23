"""End-labelled candles must not be shifted twice; shared NIFTY trend must not change signals."""
import unittest
import numpy as np
import pandas as pd

import strategy_b_original as original
from app import strategy_adapter as sa
from app.main import completed_15m

IST = original.IST


def synth(seed, days=10):
    rng = np.random.default_rng(seed)
    rows, price = [], 1000.0
    start = pd.Timestamp("2026-10-12", tz=IST)
    for d in range(days):
        day = start + pd.Timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for k in range(25):  # END labels 09:30 .. 15:30
            ts = day + pd.Timedelta(hours=9, minutes=30 + 15 * k)
            o, c = price, price + rng.normal(0, 3)
            rows.append((ts, o, max(o, c) + abs(rng.normal(0, 2)), min(o, c) - abs(rng.normal(0, 2)),
                         c, abs(rng.normal(1e5, 3e4)) + 1))
            price = c
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


class LabelAndReuseTests(unittest.TestCase):
    def test_end_labelled_input_is_not_shifted_again(self):
        raw = synth(1)
        out = original.prepare_symbol_from_15m(raw, end_labelled=True, wick_zones=False)
        self.assertEqual(list(out["timestamp"]), list(raw["timestamp"]))
        shifted = original.prepare_symbol_from_15m(raw)  # legacy start-label behaviour
        self.assertEqual(shifted["timestamp"].iloc[0], raw["timestamp"].iloc[0] + pd.Timedelta(minutes=15))

    def test_wick_zone_skip_does_not_change_indicators(self):
        raw = synth(3)
        a = original.prepare_symbol_from_15m(raw, end_labelled=True, wick_zones=True)
        b = original.prepare_symbol_from_15m(raw, end_labelled=True, wick_zones=False)
        for col in ["atr", "poc", "last_swing_high", "last_swing_low", "stock_htf_trend"]:
            pd.testing.assert_series_equal(a[col], b[col], check_names=False)

    def test_completed_15m_keeps_end_labels(self):
        raw = synth(1)
        frame = completed_15m(raw)
        self.assertEqual(frame["timestamp"].iloc[0], raw["timestamp"].iloc[0])
        self.assertLessEqual(frame["timestamp"].iloc[-1], pd.Timestamp.now(tz=IST).floor("15min"))

    def test_shared_nifty_trend_gives_identical_signal(self):
        stock, nifty = synth(1), synth(2)
        shared = sa.nifty_trend_frame(nifty)
        self.assertEqual(sa.get_latest_signal("X", stock, nifty),
                         sa.get_latest_signal("X", stock, nifty, nifty_trend=shared))


if __name__ == "__main__":
    unittest.main()
