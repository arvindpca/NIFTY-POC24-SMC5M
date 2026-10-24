"""Walk-forward variant study of Strategy B exits/filters (research only, no orders).

Signals are generated ONCE per symbol with the unchanged Strategy B logic; only exit/filter parameters vary.
Every number is NET of costs (STUDY_COST_BPS per side). The test period is split in half chronologically:
variants are judged on BOTH halves, and the best first-half variant is re-scored on the untouched second half.
"""
import itertools
import math
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

import strategy_b_original as original
from app.backtest import fetch_angel_history, prepare, WINDOW
from app.main import load_tokens
from app.strategy_adapter import _latest_setup_signal, nifty_trend_frame

IST = original.IST
NOTIONAL, MAX_OPEN, MAX_PER_DAY = 50_000.0, 5, 5
COST_BPS = float(os.getenv("STUDY_COST_BPS", "5"))
TEST_DAYS = int(os.getenv("STUDY_DAYS", "280"))
OUT = os.getenv("STUDY_OUT", "study_results")
MIN_TRADES_HALF = int(os.getenv("STUDY_MIN_TRADES_HALF", "30"))

RR = (1.0, 1.25, 1.5, 2.0)
MIN_SL_PCT = (0.0, 0.5, 0.8)
EXIT_MIN = (900, 915)            # end-label minute of day: 15:00 / 15:15 (close of that bar)
BREAKEVEN = (False, True)
SKIP_OPEN = (False, True)        # skip signals whose bar ends before 10:00
BASELINE = dict(rr=1.5, min_sl=0.0, exit_min=900, be=False, skip_open=False)


def arrays(frame):
    ts = frame["timestamp"]
    return {"ns": ts.astype("int64").to_numpy() if ts.dt.tz is None else ts.dt.tz_convert("UTC").astype("int64").to_numpy(),
            "day": (ts.dt.year * 10000 + ts.dt.month * 100 + ts.dt.day).to_numpy(),
            "min": (ts.dt.hour * 60 + ts.dt.minute).to_numpy(),
            "open": frame["open"].to_numpy(float), "high": frame["high"].to_numpy(float),
            "low": frame["low"].to_numpy(float), "close": frame["close"].to_numpy(float)}


def scan_symbol(args):
    """Worker: indicators + every signal for one symbol (unchanged Strategy B logic)."""
    symbol, raw, nifty_trend, test_days = args
    frame = prepare(raw, nifty_trend)
    arr = arrays(frame)
    sigs = []
    for k in range(max(original.MIN_WARMUP_BARS, WINDOW), len(frame) - 1):
        if int(arr["day"][k]) not in test_days:
            continue
        sig = _latest_setup_signal(frame.iloc[k - WINDOW + 1: k + 1], frame.at[k, "nifty_trend"])
        if sig:
            sigs.append({"symbol": symbol, "k": k, "side": sig["side"], "sl": float(sig["stop_loss"])})
    return symbol, sigs, arr


def simulate(arr, k, side, sl, rr, exit_min, be):
    """Same bar rules as app.rules.exit_decision, plus optional breakeven-at-1R. Returns (entry, exit, j, reason) or None."""
    n, e = len(arr["close"]), k + 1
    if arr["day"][e] != arr["day"][k]:
        return None
    entry = float(arr["open"][e])
    if (side == "LONG" and sl >= entry) or (side == "SHORT" and sl <= entry):
        return None
    risk = abs(entry - sl)
    long_ = side == "LONG"
    target = entry + rr * risk if long_ else entry - rr * risk
    stop, armed = sl, False
    for j in range(e, n):
        if arr["day"][j] != arr["day"][k]:
            return None
        if arr["min"][j] >= exit_min:
            return entry, float(arr["close"][j]), j, "TIME"
        hi, lo = arr["high"][j], arr["low"][j]
        stop_hit = lo <= stop if long_ else hi >= stop
        tgt_hit = hi >= target if long_ else lo <= target
        if stop_hit:
            return entry, float(stop), j, "BE" if armed else "SL"
        if tgt_hit:
            return entry, float(target), j, "TP"
        if be and not armed and ((hi >= entry + risk) if long_ else (lo <= entry - risk)):
            armed, stop = True, entry
    return None


def run_variant(sigs, arrs, rr, min_sl, exit_min, be, skip_open):
    cands = []
    for s in sigs:
        arr = arrs[s["symbol"]]
        if skip_open and arr["min"][s["k"]] < 600:
            continue
        res = simulate(arr, s["k"], s["side"], s["sl"], rr, exit_min, be)
        if res is None:
            continue
        entry, exit_px, j, reason = res
        if abs(entry - s["sl"]) / entry * 100 < min_sl:
            continue
        qty = math.floor(NOTIONAL / entry)
        if qty < 1:
            continue
        gross = (exit_px - entry) * qty if s["side"] == "LONG" else (entry - exit_px) * qty
        cost = (entry + exit_px) * qty * COST_BPS / 10000.0
        k = s["k"]
        cands.append({"symbol": s["symbol"], "side": s["side"], "entry_ns": int(arr["ns"][k + 1]), "exit_ns": int(arr["ns"][j]),
                      "day": int(arr["day"][k]), "net": gross - cost, "gross": gross, "R": (gross - cost) / (abs(entry - s["sl"]) * qty),
                      "reason": reason})
    return portfolio(cands)


def portfolio(cands):
    acc, per_day = [], {}
    for t in sorted(cands, key=lambda x: (x["entry_ns"], x["symbol"])):
        open_now = [a for a in acc if a["exit_ns"] > t["entry_ns"]]
        if len(open_now) >= MAX_OPEN or per_day.get(t["day"], 0) >= MAX_PER_DAY:
            continue
        if any(a["symbol"] == t["symbol"] for a in open_now):
            continue
        acc.append(t)
        per_day[t["day"]] = per_day.get(t["day"], 0) + 1
    return acc


def metrics(trades):
    if not trades:
        return {"n": 0, "net": 0.0, "avgR": 0.0, "win": 0.0, "pf": 0.0, "dd": 0.0}
    net = np.array([t["net"] for t in trades])
    wins, losses = net[net > 0].sum(), -net[net < 0].sum()
    eq = net.cumsum()
    return {"n": len(net), "net": float(net.sum()), "avgR": float(np.mean([t["R"] for t in trades])),
            "win": float(100 * (net > 0).mean()), "pf": float(wins / losses) if losses else float("inf"),
            "dd": float((eq - np.maximum.accumulate(eq)).min())}


def fmt(m):
    return f"n={m['n']} net=Rs{m['net']:,.0f} avgR={m['avgR']:+.3f} win={m['win']:.0f}% pf={m['pf']:.2f} dd=Rs{m['dd']:,.0f}"


def main():
    tokens = load_tokens()
    symbols = [s for s in tokens if s != "NIFTY"]
    total_days = int(TEST_DAYS * 1.5) + 14
    print(f"Downloading from Angel: {len(symbols)+1} symbols, {total_days} calendar days")
    data = fetch_angel_history(tokens, ["NIFTY", *symbols], total_days)
    nifty_df, err = data["NIFTY"]
    if nifty_df is None:
        raise SystemExit(f"NIFTY data unavailable: {err}")
    nifty_trend = nifty_trend_frame(nifty_df)
    today = pd.Timestamp.now(tz=IST).date()
    all_days = sorted({t.date() for t in nifty_df["timestamp"] if t.date() < today})
    all_days = all_days[10:]  # NIFTY 1H-trend warm-up
    test = all_days[-TEST_DAYS:]
    key = lambda d: d.year * 10000 + d.month * 100 + d.day
    test_days = {key(d) for d in test}
    print(f"Test window {test[0]} -> {test[-1]} ({len(test)} trading days; NIFTY history starts {nifty_df['timestamp'].min().date()})")

    jobs = [(s, data[s][0], nifty_trend, test_days) for s in symbols if data[s][0] is not None]
    failed = [s for s in symbols if data[s][0] is None]
    sigs, arrs = [], {}
    workers = max(1, os.cpu_count() or 1)
    print(f"Scanning signals: {len(jobs)} symbols on {workers} workers")
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for symbol, s_list, arr in pool.map(scan_symbol, jobs, chunksize=4):
            sigs += s_list
            arrs[symbol] = arr
    print(f"Raw signals: {len(sigs)}")

    mid = key(test[len(test) // 2])
    rows = []
    for rr, min_sl, exit_min, be, skip_open in itertools.product(RR, MIN_SL_PCT, EXIT_MIN, BREAKEVEN, SKIP_OPEN):
        acc = run_variant(sigs, arrs, rr, min_sl, exit_min, be, skip_open)
        h1, h2 = [t for t in acc if t["day"] < mid], [t for t in acc if t["day"] >= mid]
        row = dict(rr=rr, min_sl=min_sl, exit_min=exit_min, be=be, skip_open=skip_open)
        for tag, tr in (("all", acc), ("h1", h1), ("h2", h2)):
            for k, v in metrics(tr).items():
                row[f"{tag}_{k}"] = v
        rows.append(row)
    df = pd.DataFrame(rows)
    os.makedirs(OUT, exist_ok=True)
    df.to_csv(os.path.join(OUT, "variants.csv"), index=False)

    desc = lambda r: f"RR={r.rr} minSL={r.min_sl}% exit={'15:00' if r.exit_min == 900 else '15:15'} BE={'on' if r.be else 'off'} skip<10:00={'on' if r.skip_open else 'off'}"
    base = df[(df.rr == 1.5) & (df.min_sl == 0) & (df.exit_min == 900) & (~df.be) & (~df.skip_open)].iloc[0]
    L = [f"VARIANT STUDY (Angel 15m) | window {test[0]} -> {test[-1]} ({len(test)} days) | split at {mid} | cost {COST_BPS} bps/side | symbols={len(jobs)} failed={len(failed)} | raw signals={len(sigs)} | variants={len(df)}",
         "All metrics NET of cost. avgR = mean net R per trade (sizing-neutral). H1 = first half, H2 = second half.",
         f"BASELINE (current live rules) {desc(base)}",
         f"  ALL: {fmt({k: base[f'all_{k}'] for k in ('n','net','avgR','win','pf','dd')})}",
         f"  H1 : {fmt({k: base[f'h1_{k}'] for k in ('n','net','avgR','win','pf','dd')})}",
         f"  H2 : {fmt({k: base[f'h2_{k}'] for k in ('n','net','avgR','win','pf','dd')})}"]
    ok = df[(df.h1_n >= MIN_TRADES_HALF) & (df.h2_n >= MIN_TRADES_HALF)].copy()
    both = ok[(ok.h1_avgR > 0) & (ok.h2_avgR > 0)].copy()
    both["worst"] = both[["h1_avgR", "h2_avgR"]].min(axis=1)
    L.append(f"Variants with >= {MIN_TRADES_HALF} trades in each half: {len(ok)}/{len(df)}; positive net avgR in BOTH halves: {len(both)}  (some of these are luck: {len(df)} variants were tried)")
    for _, r in both.sort_values("worst", ascending=False).head(10).iterrows():
        L.append(f"  * {desc(r)} | H1 avgR={r.h1_avgR:+.3f} (n={int(r.h1_n)}) | H2 avgR={r.h2_avgR:+.3f} (n={int(r.h2_n)}) | ALL net=Rs{r.all_net:,.0f} pf={r.all_pf:.2f} dd=Rs{r.all_dd:,.0f}")
    if len(ok):
        best = ok.sort_values("h1_avgR", ascending=False).iloc[0]
        L.append(f"HONEST OUT-OF-SAMPLE: best variant chosen on H1 only = {desc(best)}")
        L.append(f"  H1 (selection): avgR={best.h1_avgR:+.3f} n={int(best.h1_n)} | H2 (unseen): avgR={best.h2_avgR:+.3f} n={int(best.h2_n)} net=Rs{best.h2_net:,.0f} pf={best.h2_pf:.2f}")
        L.append(f"  Baseline H2 (unseen): avgR={base.h2_avgR:+.3f} n={int(base.h2_n)} net=Rs{base.h2_net:,.0f}")
        # sensitivity: average effect of each knob across all variants (H2 only, avgR)
        for col, name in (("rr", "RR"), ("min_sl", "minSL%"), ("exit_min", "exit"), ("be", "breakeven"), ("skip_open", "skip<10:00")):
            g = ok.groupby(col)[["h1_avgR", "h2_avgR"]].mean().round(3)
            L.append(f"  knob {name}: " + " | ".join(f"{idx}: H1 {r.h1_avgR:+.3f} H2 {r.h2_avgR:+.3f}" for idx, r in g.iterrows()))
    if failed:
        L.append("Data failures: " + ", ".join(failed[:20]))
    L.append("Universe = current watchlist (survivorship bias). Entry at next bar open, no slippage. Paper research, not a forecast.")
    text = "\n".join(L)
    with open(os.path.join(OUT, "summary_study.txt"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
