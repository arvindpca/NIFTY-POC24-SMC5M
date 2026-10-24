import os
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd

import strategy_b_original as original
import app.study as st
from app.rules import exit_decision, target_for
from tests.test_backtest_angel import start_labelled


def make_arr(rows):
    """rows: list of (min_of_day, open, high, low, close); one session."""
    return {"ns": np.arange(len(rows)) * 900, "day": np.full(len(rows), 20261001),
            "min": np.array([r[0] for r in rows]), "open": np.array([r[1] for r in rows], float),
            "high": np.array([r[2] for r in rows], float), "low": np.array([r[3] for r in rows], float),
            "close": np.array([r[4] for r in rows], float)}


class SimulateTests(unittest.TestCase):
    def test_tp_sl_time_and_ambiguous(self):
        # k=0 signal bar; entry = open of bar 1 = 100, SL 99 -> risk 1, RR1.5 target 101.5
        up = make_arr([(570, 100, 100, 100, 100), (585, 100, 102, 99.5, 101), (600, 101, 101, 101, 101)])
        self.assertEqual(st.simulate(up, 0, "LONG", 99, 1.5, 900, False)[3], "TP")
        amb = make_arr([(570, 100, 100, 100, 100), (585, 100, 102, 98.5, 101)])
        self.assertEqual(st.simulate(amb, 0, "LONG", 99, 1.5, 900, False)[3], "SL")   # SL-first when both hit
        late = make_arr([(570, 100, 100, 100, 100), (885, 100, 100.5, 99.5, 100.2), (900, 100.2, 105, 90, 100.4)])
        r = st.simulate(late, 0, "LONG", 99, 1.5, 900, False)
        self.assertEqual((r[3], r[1]), ("TIME", 100.4))                                # 15:00 bar: time exit beats SL/TP
        self.assertEqual(st.simulate(late, 0, "LONG", 99, 1.5, 915, False)[3], "SL")   # 15:15 exit lets that bar hit SL first

    def test_breakeven_after_1R(self):
        arr = make_arr([(570, 100, 100, 100, 100), (585, 100, 101.2, 99.5, 101), (600, 101, 101, 99.9, 100.5)])
        res = st.simulate(arr, 0, "LONG", 99, 1.5, 900, True)
        self.assertEqual((res[3], res[1]), ("BE", 100.0))
        self.assertEqual(st.simulate(arr, 0, "LONG", 99, 1.5, 900, False)[3] if st.simulate(arr, 0, "LONG", 99, 1.5, 900, False) else None, None)

    def test_short_side_mirrors_long(self):
        arr = make_arr([(570, 100, 100, 100, 100), (585, 100, 100.5, 98, 99)])
        self.assertEqual(st.simulate(arr, 0, "SHORT", 101, 1.5, 900, False)[3], "TP")

    def test_matches_live_exit_rules_on_random_trades(self):
        raw = start_labelled(7, 60)
        frame = original.prepare_symbol_from_15m(raw.assign(timestamp=raw["timestamp"] + pd.Timedelta(minutes=15)),
                                                  end_labelled=True, wick_zones=False)
        arr = st.arrays(frame)
        rng = np.random.default_rng(1)
        checked = 0
        for k in rng.integers(100, len(frame) - 30, 400):
            side = "LONG" if rng.random() < 0.5 else "SHORT"
            entry = float(frame.at[k + 1, "open"])
            sl = entry - rng.uniform(0.3, 6) if side == "LONG" else entry + rng.uniform(0.3, 6)
            mine = st.simulate(arr, int(k), side, sl, 1.5, 900, False)
            target = target_for(entry, sl, side, 1.5)
            live = None
            if frame.at[k + 1, "timestamp"].date() == frame.at[k, "timestamp"].date():
                for j in range(k + 1, len(frame)):
                    bar = frame.iloc[j]
                    if bar["timestamp"].date() != frame.at[k, "timestamp"].date():
                        break
                    dec = exit_decision(bar["timestamp"], bar, side, sl, target)
                    if dec:
                        live = (dec[0], {"TIME_EXIT": "TIME", "SL_FIRST_AMBIGUOUS": "SL"}.get(dec[1], dec[1]))
                        break
            if mine is None or live is None:
                self.assertEqual(mine is None, live is None)
                continue
            self.assertAlmostEqual(mine[1], live[0], places=9)
            self.assertEqual(mine[3], live[1])
            checked += 1
        self.assertGreater(checked, 300)


class PortfolioTests(unittest.TestCase):
    def test_limits(self):
        mk = lambda s, e, x, d=1: {"symbol": s, "entry_ns": e, "exit_ns": x, "day": d}
        c = [mk(f"S{i}", 10, 100) for i in range(7)] + [mk("S0", 50, 60)]
        self.assertEqual(len(st.portfolio(c)), 5)                                   # max 5 concurrent, no same-symbol overlap
        d = [mk(f"S{i}", i, i + 1) for i in range(8)]
        self.assertEqual(len(st.portfolio(d)), 5)                                   # max 5 entries per day


@unittest.skipUnless(os.getenv("RUN_SLOW_TESTS") == "1", "end-to-end study run is slow; set RUN_SLOW_TESTS=1")
class StudyEndToEnd(unittest.TestCase):
    def test_runs_and_writes_outputs(self):
        import tempfile
        out = tempfile.mkdtemp()
        syn = lambda seed: (lambda r: r.assign(timestamp=r["timestamp"] + pd.Timedelta(minutes=15)))(start_labelled(seed, 140))
        fake = {"NIFTY": (syn(1), None), "AAA": (syn(7), None), "BBB": (syn(7), None)}
        nf = fake["NIFTY"][0][["timestamp"]].copy()   # force a tradeable NIFTY regime so signals exist
        nf["nifty_trend"] = np.where((np.arange(len(nf)) // 60) % 2 == 0, "POSITIVE", "NEGATIVE")
        with patch.object(st, "nifty_trend_frame", return_value=nf), patch.object(st, "fetch_angel_history", return_value=fake), patch.object(st, "load_tokens",
             return_value={"NIFTY": {}, "AAA": {}, "BBB": {}}), patch.object(st, "OUT", out), patch.object(st, "TEST_DAYS", 85), \
             patch.object(st, "MIN_TRADES_HALF", 0):
            st.main()
        self.assertTrue(os.path.exists(os.path.join(out, "summary_study.txt")))
        df = pd.read_csv(os.path.join(out, "variants.csv"))
        self.assertEqual(len(df), 96)


if __name__ == "__main__":
    unittest.main()
