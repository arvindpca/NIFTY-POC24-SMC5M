import unittest
import numpy as np
import pandas as pd

import strategy_b_original as original
import app.ablation as ab
from app.backtest import WINDOW
from app.strategy_adapter import _latest_setup_signal
from tests.test_backtest_angel import start_labelled


def frame_for(seed):
    raw = start_labelled(seed, 90)
    f = original.prepare_symbol_from_15m(raw.assign(timestamp=raw["timestamp"] + pd.Timedelta(minutes=15)),
                                         end_labelled=True, wick_zones=False).reset_index(drop=True)
    rng = np.random.default_rng(seed)
    day = f["timestamp"].dt.date
    pick = {d: rng.choice(["POSITIVE", "NEGATIVE", "NEUTRAL"], p=[.4, .4, .2]) for d in day.unique()}
    f["nifty_trend"] = day.map(pick)
    return f


class ParityTests(unittest.TestCase):
    def test_candidates_plus_live_filters_equal_live_detector(self):
        total = 0
        for seed in range(1, 9):
            f = frame_for(seed)
            a = ab.full_arrays(f)
            cands = []
            for k in range(max(original.MIN_WARMUP_BARS, WINDOW), len(f) - 1):
                for c in ab.candidates(a, k):
                    c["symbol"] = "X"
                    cands.append(c)
            chosen = {(c["k"], c["side"], round(c["sl"], 6)) for c in ab.pick(
                [c for c in cands if a["min"][c["k"]] <= ab.CUTOFF_MIN], lambda c: c["nifty_aligned"] and c["poc_ok"])}
            live = set()
            for k in range(max(original.MIN_WARMUP_BARS, WINDOW), len(f) - 1):
                s = _latest_setup_signal(f.iloc[k - WINDOW + 1: k + 1], f.at[k, "nifty_trend"])
                if s:
                    live.add((k, s["side"], round(float(s["stop_loss"]), 6)))
            self.assertEqual(chosen, live, f"seed {seed}")
            total += len(live)
        self.assertGreater(total, 0, "parity test must not be vacuous")


if __name__ == "__main__":
    unittest.main()
