"""Backtest speed-up must not change signals; chunked Angel download must reassemble the full history."""
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd

import strategy_b_original as original
import app.backtest as bt
from app.strategy_adapter import _latest_setup_signal, nifty_trend_frame

IST = original.IST


def start_labelled(seed, days, end=None):
    """Angel-style START-labelled 15m bars, mean-reverting + spiky so sweeps/displacements occur."""
    rng = np.random.default_rng(seed)
    end = end or pd.Timestamp.now(tz=IST).normalize()
    rows, price = [], 1000.0
    for d in range(days, 0, -1):
        day = end - pd.Timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for k in range(25):
            ts = day + pd.Timedelta(hours=9, minutes=15 + 15 * k)
            o = price
            c = o + rng.normal(0, 2.5) + (1000 - o) * 0.02 + (rng.normal(0, 14) if rng.random() < 0.08 else 0)
            h = max(o, c) + abs(rng.normal(0, 2.5)); l = min(o, c) - abs(rng.normal(0, 2.5))
            rows.append((ts, o, h, l, c, abs(rng.normal(1e5, 4e4)) + 1)); price = c
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


class FakeAngel:
    def __init__(self, df):
        self.df, self.calls = df, 0

    def getCandleData(self, params):
        self.calls += 1
        a = pd.Timestamp(params["fromdate"], tz=IST); b = pd.Timestamp(params["todate"], tz=IST)
        sub = self.df[(self.df["timestamp"] >= a) & (self.df["timestamp"] <= b)]
        return {"status": True, "data": [[t.isoformat(), *r] for t, r in zip(sub["timestamp"], sub[["open", "high", "low", "close", "volume"]].to_numpy().tolist())]}

    def terminateSession(self, *_):
        pass


class BacktestTests(unittest.TestCase):
    def test_window_prefix_gives_identical_signals(self):
        total = 0
        for seed in (7,):
            raw = start_labelled(seed, 100)
            end_lab = raw.assign(timestamp=raw["timestamp"] + pd.Timedelta(minutes=15))
            frame = original.prepare_symbol_from_15m(end_lab, end_labelled=True, wick_zones=False)
            n = len(frame)
            frame["nifty_trend"] = np.where((np.arange(n) // 60) % 2 == 0, "POSITIVE", "NEGATIVE")
            for k in range(100, n - 1):
                full = _latest_setup_signal(frame.iloc[: k + 1], frame.at[k, "nifty_trend"])
                win = _latest_setup_signal(frame.iloc[k - bt.WINDOW + 1: k + 1], frame.at[k, "nifty_trend"])
                self.assertEqual(full is None, win is None, f"seed {seed} bar {k}")
                if full:
                    total += 1
                    for key in ("side", "stop_loss", "signal_time", "zone_low", "zone_high", "stock_poc"):
                        self.assertEqual(full[key], win[key])
        self.assertGreater(total, 0, "synthetic data produced no signals - test would be vacuous")

    def test_chunked_download_reassembles_full_history(self):
        raw = start_labelled(3, 200)
        fake = FakeAngel(raw)
        tokens = {"AAA": {"exchange": "NSE", "token": "1"}}
        with patch.object(bt, "create_data_client", return_value=fake), patch("app.smartapi_client._sleep"):
            out = bt.fetch_angel_history(tokens, ["AAA"], 190)
        df, err = out["AAA"]
        self.assertIsNone(err)
        self.assertGreater(fake.calls, 2)                       # really chunked
        self.assertTrue(df["timestamp"].is_unique)
        self.assertTrue((df["timestamp"].diff().dropna() > pd.Timedelta(0)).all())
        expected = raw[raw["timestamp"] >= pd.Timestamp.now(tz=IST) - pd.Timedelta(days=190)]
        want = set(expected["timestamp"] + pd.Timedelta(minutes=15))   # end-labelled
        self.assertTrue(want.issubset(set(df["timestamp"])))           # no bar lost at any chunk edge
        self.assertLessEqual(len(df) - len(want), 4)                   # only the 30-min edge overlap is extra


if __name__ == "__main__":
    unittest.main()
