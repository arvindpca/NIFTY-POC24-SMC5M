"""Robustness check of the proposed v2 config (NIFTY trend + POC + stock 1H trend, tuned exits).
v2 was CHOSEN using this same data, so this is an in-sample stability test (thirds, months, costs, neighbours,
bootstrap CI) - it can reject v2 but it cannot prove it. Only forward paper trades can."""
import itertools
import os

import numpy as np
import pandas as pd

import app.ablation as ab
import strategy_b_original as original
from app.backtest import WINDOW
from app.main import load_tokens
from app.strategy_adapter import nifty_trend_frame
from concurrent.futures import ProcessPoolExecutor

V2 = dict(rr=1.0, min_sl=0.8, exit_min=915, be=False, skip_open=True)
PRED = lambda c: c["nifty_aligned"] and c["poc_ok"] and c["stock_htf_aligned"]


def boot(r, n=5000, seed=1):
    rng = np.random.default_rng(seed)
    m = rng.choice(r, (n, len(r))).mean(axis=1)
    return np.percentile(m, [2.5, 97.5])


def main():
    tokens = load_tokens()
    symbols = [s for s in tokens if s != "NIFTY"]
    data = ab.load_data(tokens, symbols, int(ab.TEST_DAYS * 1.5) + 14)
    nifty_trend = nifty_trend_frame(data["NIFTY"][0])
    today = pd.Timestamp.now(tz=ab.IST).date()
    days = sorted({t.date() for t in data["NIFTY"][0]["timestamp"] if t.date() < today})[10:][-ab.TEST_DAYS:]
    key = lambda d: d.year * 10000 + d.month * 100 + d.day
    test_days = {key(d) for d in days}
    jobs = [(s, data[s][0], nifty_trend, test_days) for s in symbols if data[s][0] is not None]
    cands, arrs = [], {}
    with ProcessPoolExecutor(max_workers=max(1, os.cpu_count() or 1)) as pool:
        for symbol, cl, arr in pool.map(ab.scan_symbol, jobs, chunksize=4):
            cands += cl
            arrs[symbol] = arr
    cands.sort(key=lambda c: (c["symbol"], c["k"]))
    chosen = ab.pick(cands, PRED)
    L = [f"V2 ROBUSTNESS | {days[0]} -> {days[-1]} ({len(days)} days) | v2 = NIFTY trend + POC + stock 1H trend, RR1.0 minSL0.8% exit15:15 skip<10:00",
         "CAUTION: v2 was selected on this same data -> in-sample. Treat any positive as unproven.", ""]
    for bps in (0.0, 5.0, 10.0):
        tr = ab.run_cfg(chosen, arrs, V2, bps)
        s = ab.stats(tr)
        r = np.array([t["R"] for t in tr])
        lo, hi = boot(r) if len(r) > 5 else (0, 0)
        L.append(f"cost {bps:>4.1f} bps/side: n={s['n']} avgR={s['avgR']:+.3f} 95%CI[{lo:+.3f},{hi:+.3f}] t={s['t']:+.1f} win={s['win']:.0f}% pf={s['pf']:.2f} net=Rs{s['net']:,.0f}")
    tr = ab.run_cfg(chosen, arrs, V2, 5.0)
    kd = sorted(days)
    third = [key(kd[len(kd) // 3]), key(kd[2 * len(kd) // 3])]
    parts = [[t for t in tr if t["day"] < third[0]], [t for t in tr if third[0] <= t["day"] < third[1]], [t for t in tr if t["day"] >= third[1]]]
    L += ["", "Thirds (5 bps): " + " | ".join(f"T{i+1} n={ab.stats(p)['n']} avgR={ab.stats(p)['avgR']:+.3f}" for i, p in enumerate(parts))]
    months = {}
    for t in tr:
        months.setdefault(t["day"] // 100, []).append(t)
    pos = sum(1 for v in months.values() if sum(x["net"] for x in v) > 0)
    L.append(f"Months positive (5 bps): {pos}/{len(months)} | " + " ".join(f"{m}:{sum(x['net'] for x in v):+,.0f}" for m, v in sorted(months.items())))
    L += ["", "Neighbouring settings (5 bps) - a real edge should not collapse when parameters move slightly:"]
    pos_n = 0
    for rr, msl, em, so in itertools.product((1.0, 1.25), (0.5, 0.8), (900, 915), (False, True)):
        cfg = dict(rr=rr, min_sl=msl, exit_min=em, be=False, skip_open=so)
        s = ab.stats(ab.run_cfg(chosen, arrs, cfg, 5.0))
        pos_n += s["avgR"] > 0
        L.append(f"  RR={rr} minSL={msl} exit={'15:00' if em == 900 else '15:15'} skip<10={so}: n={s['n']} avgR={s['avgR']:+.3f} t={s['t']:+.1f}")
    L.append(f"Neighbours with avgR>0: {pos_n}/16")
    os.makedirs(ab.OUT, exist_ok=True)
    open(os.path.join(ab.OUT, "summary_v2check.txt"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
