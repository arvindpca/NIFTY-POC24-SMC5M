"""Filter ablation + signal-quality study for Strategy B (research only, no orders).

* Candidate setups are produced by an exact copy of the LIVE detector (sweep -> displacement>=1ATR -> MSS ->
  FVG/OB -> retest+confirmation) WITHOUT the NIFTY / POC filters; those filters are then applied afterwards, in
  the same "first passing setup wins" order as app.strategy_adapter. A parity test pins this to the live code.
* Success bar is fixed BEFORE looking at results: net of 5 bps/side, >= BAR_MIN_TRADES trades in each half,
  avg net R >= BAR_MIN_R in BOTH halves and overall t-stat >= BAR_MIN_T (multiple-testing guard: ~20 configs tried).
* Feature thresholds are chosen on the first half only and scored on the unseen second half.
"""
import os
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

import strategy_b_original as original
import app.study as st
from app.backtest import fetch_angel_history, prepare, WINDOW
from app.main import load_tokens
from app.strategy_adapter import nifty_trend_frame

IST = original.IST
OUT = os.getenv("ABL_OUT", "ablation_results")
CACHE = os.getenv("ABL_DATA_CACHE", ".cache_angel/angel_15m.pkl")
TEST_DAYS = int(os.getenv("STUDY_DAYS", "280"))
BAR_MIN_TRADES, BAR_MIN_R, BAR_MIN_T = 100, 0.10, 3.0
CUTOFF_MIN = original.ENTRY_CUTOFF.hour * 60 + original.ENTRY_CUTOFF.minute
LIVE_EXIT = dict(rr=1.5, min_sl=0.0, exit_min=900, be=False, skip_open=False)
TUNED_EXIT = dict(rr=1.0, min_sl=0.8, exit_min=915, be=False, skip_open=True)   # best H1 variant of the previous study


def full_arrays(frame):
    a = st.arrays(frame)
    a.update({
        "atr": frame["atr"].to_numpy(float), "lsh": frame["last_swing_high"].to_numpy(float),
        "lsl": frame["last_swing_low"].to_numpy(float), "poc": frame["poc"].to_numpy(float),
        "ntrend": frame["nifty_trend"].to_numpy(object), "shtf": frame["stock_htf_trend"].to_numpy(object),
        "ema_up": frame["ema_rising"].fillna(False).to_numpy(bool), "ema_dn": frame["ema_falling"].fillna(False).to_numpy(bool),
        "vol_ok": frame["volume_1_5x"].fillna(False).to_numpy(bool), "vol": frame["volume"].to_numpy(float),
        "vsma": frame["volume_sma20_prev"].to_numpy(float),
    })
    return a


def candidates(a, k):
    """Every setup whose retest/confirmation fires on bar k (unfiltered), in the live detector's discovery order."""
    W, lo = WINDOW, k - WINDOW + 1
    kl = W - 1
    out = []
    if kl < max(original.MIN_WARMUP_BARS, 15):
        return out
    atr_now = a["atr"][k]
    if not np.isfinite(atr_now) or atr_now <= 0:
        return out
    H, L, O, C, ATR = a["high"], a["low"], a["open"], a["close"], a["atr"]
    for il in range(max(original.MIN_WARMUP_BARS, kl - original.SETUP_EXPIRY - 3), kl):
        i = lo + il
        atr_s = ATR[i]
        if not np.isfinite(atr_s) or atr_s <= 0:
            continue
        prev_hi = H[max(lo, i - 10):i].max()
        prev_lo = L[max(lo, i - 10):i].min()
        long_sweep = L[i] < prev_lo and C[i] > prev_lo
        short_sweep = H[i] > prev_hi and C[i] < prev_hi
        if not (long_sweep or short_sweep):
            continue
        side = "LONG" if long_sweep else "SHORT"
        for jl in range(il + 1, min(il + 4, kl)):
            j = lo + jl
            atr_j = ATR[j]
            body = abs(C[j] - O[j])
            if not np.isfinite(atr_j) or atr_j <= 0 or body < original.DISPLACEMENT_ATR * atr_j:
                continue
            if side == "LONG":
                mss = a["lsh"][j - 1]
                if not np.isfinite(mss) or C[j] <= mss or C[j] <= O[j]:
                    continue
                fvg = jl >= 2 and L[j] > H[j - 2]
                ob = next((z for z in range(j - 1, max(lo - 1, j - 6), -1) if C[z] < O[z]), None)
                if fvg:
                    zl, zh, zone = float(H[j - 2]), float(L[j]), "FVG"
                elif ob is not None:
                    zl, zh, zone = float(L[ob]), float(O[ob]), "OB"
                else:
                    continue
                sl = float(L[i] - original.SL_ATR_BUFFER * atr_s)
                retest = (kl > jl and kl < jl + 1 + original.SETUP_EXPIRY and L[k] <= zh and H[k] >= zl
                          and C[k] > O[k] and C[k] > zh)
                depth = (zh - L[k]) / max(zh - zl, 1e-9)
                sweep_atr = (prev_lo - L[i]) / atr_s
            else:
                mss = a["lsl"][j - 1]
                if not np.isfinite(mss) or C[j] >= mss or C[j] >= O[j]:
                    continue
                fvg = jl >= 2 and H[j] < L[j - 2]
                ob = next((z for z in range(j - 1, max(lo - 1, j - 6), -1) if C[z] > O[z]), None)
                if fvg:
                    zl, zh, zone = float(H[j]), float(L[j - 2]), "FVG"
                elif ob is not None:
                    zl, zh, zone = float(O[ob]), float(H[ob]), "OB"
                else:
                    continue
                sl = float(H[i] + original.SL_ATR_BUFFER * atr_s)
                retest = (kl > jl and kl < jl + 1 + original.SETUP_EXPIRY and H[k] >= zl and L[k] <= zh
                          and C[k] < O[k] and C[k] < zl)
                depth = (H[k] - zl) / max(zh - zl, 1e-9)
                sweep_atr = (H[i] - prev_hi) / atr_s
            if not retest:
                continue
            nt, poc = a["ntrend"][k], a["poc"][k]
            want = "POSITIVE" if side == "LONG" else "NEGATIVE"
            opp = "NEGATIVE" if side == "LONG" else "POSITIVE"
            sgn = 1.0 if side == "LONG" else -1.0
            vs = a["vsma"][k]
            out.append({
                "k": k, "side": side, "sl": sl, "zone": zone, "sweep_atr": float(sweep_atr), "disp_atr": float(body / atr_j),
                "bars_sweep": kl - il, "bars_disp": kl - jl, "retest_depth": float(min(max(depth, 0.0), 2.0)),
                "zone_h_atr": float((zh - zl) / atr_now), "risk_pct": float(abs(C[k] - sl) / C[k] * 100),
                "atr_pct": float(atr_now / C[k] * 100), "tod": int(a["min"][k]),
                "poc_dist_atr": float((C[k] - poc) / atr_now * sgn) if np.isfinite(poc) else np.nan,
                "vol_ratio": float(a["vol"][k] / vs) if np.isfinite(vs) and vs > 0 else np.nan,
                "nifty_aligned": nt == want, "nifty_opposite": nt == opp,
                "poc_ok": bool(np.isfinite(poc) and ((side == "LONG" and C[k] > poc) or (side == "SHORT" and C[k] < poc))),
                "stock_htf_aligned": a["shtf"][k] == want,
                "ema_aligned": bool(a["ema_up"][k] if side == "LONG" else a["ema_dn"][k]),
                "vol_ok": bool(a["vol_ok"][k]),
            })
    return out


def scan_symbol(args):
    symbol, raw, nifty_trend, test_days = args
    frame = prepare(raw, nifty_trend)
    a = full_arrays(frame)
    cands = []
    for k in range(max(original.MIN_WARMUP_BARS, WINDOW), len(frame) - 1):
        if int(a["day"][k]) not in test_days or a["min"][k] > CUTOFF_MIN:
            continue
        for c in candidates(a, k):
            c["symbol"] = symbol
            cands.append(c)
    return symbol, cands, st.arrays(frame)


def pick(cands, pred):
    """First passing candidate per (symbol, bar) - mirrors the live 'continue to the next setup' behaviour."""
    chosen, seen = [], set()
    for c in cands:                       # cands are in (symbol, k, discovery) order
        key = (c["symbol"], c["k"])
        if key in seen or not pred(c):
            continue
        seen.add(key)
        chosen.append(c)
    return chosen


def run_cfg(chosen, arrs, cfg, cost_bps):
    st.COST_BPS = cost_bps
    cands = []
    for c in chosen:
        arr = arrs[c["symbol"]]
        if cfg["skip_open"] and arr["min"][c["k"]] < 600:
            continue
        res = st.simulate(arr, c["k"], c["side"], c["sl"], cfg["rr"], cfg["exit_min"], cfg["be"])
        if res is None:
            continue
        entry, exit_px, j, reason = res
        if abs(entry - c["sl"]) / entry * 100 < cfg["min_sl"]:
            continue
        import math
        qty = math.floor(st.NOTIONAL / entry)
        if qty < 1:
            continue
        gross = (exit_px - entry) * qty if c["side"] == "LONG" else (entry - exit_px) * qty
        cost = (entry + exit_px) * qty * cost_bps / 10000.0
        t = dict(c)
        t.update({"entry_ns": int(arr["ns"][c["k"] + 1]), "exit_ns": int(arr["ns"][j]), "day": int(arr["day"][c["k"]]),
                  "net": gross - cost, "R": (gross - cost) / (abs(entry - c["sl"]) * qty), "reason": reason})
        cands.append(t)
    return st.portfolio(cands)


def stats(tr):
    if not tr:
        return {"n": 0, "avgR": 0.0, "se": 0.0, "t": 0.0, "net": 0.0, "win": 0.0, "pf": 0.0}
    r = np.array([t["R"] for t in tr]); net = np.array([t["net"] for t in tr])
    se = r.std(ddof=1) / np.sqrt(len(r)) if len(r) > 1 else 0.0
    w, l = net[net > 0].sum(), -net[net < 0].sum()
    return {"n": len(r), "avgR": float(r.mean()), "se": float(se), "t": float(r.mean() / se) if se > 0 else 0.0,
            "net": float(net.sum()), "win": float(100 * (net > 0).mean()), "pf": float(w / l) if l else float("inf")}


def halves(tr, mid):
    return [t for t in tr if t["day"] < mid], [t for t in tr if t["day"] >= mid]


def passes(a, h1, h2):
    return (h1["n"] >= BAR_MIN_TRADES and h2["n"] >= BAR_MIN_TRADES and h1["avgR"] >= BAR_MIN_R
            and h2["avgR"] >= BAR_MIN_R and a["t"] >= BAR_MIN_T)


PREDICATES = {
    "LIVE (NIFTY trend + POC)": lambda c: c["nifty_aligned"] and c["poc_ok"],
    "no NIFTY filter (POC only)": lambda c: c["poc_ok"],
    "no POC filter (NIFTY only)": lambda c: c["nifty_aligned"],
    "no filters at all": lambda c: True,
    "NIFTY counter-trend + POC (sanity)": lambda c: c["nifty_opposite"] and c["poc_ok"],
    "LIVE + volume>1.5x": lambda c: c["nifty_aligned"] and c["poc_ok"] and c["vol_ok"],
    "LIVE + stock own 1H trend": lambda c: c["nifty_aligned"] and c["poc_ok"] and c["stock_htf_aligned"],
    "LIVE + EMA20 direction": lambda c: c["nifty_aligned"] and c["poc_ok"] and c["ema_aligned"],
    "orig variant A (volume + POC)": lambda c: c["vol_ok"] and c["poc_ok"],
    "orig variant C (volume only)": lambda c: c["vol_ok"],
}
CONT_FEATURES = ["sweep_atr", "disp_atr", "bars_sweep", "bars_disp", "retest_depth", "zone_h_atr", "risk_pct",
                 "atr_pct", "tod", "poc_dist_atr", "vol_ratio"]
CAT_FEATURES = ["side", "zone", "stock_htf_aligned", "ema_aligned", "vol_ok"]


def load_data(tokens, symbols, total_days):
    if os.path.exists(CACHE) and time.time() - os.path.getmtime(CACHE) < 3 * 86400:
        print(f"Using cached Angel history {CACHE}")
        with open(CACHE, "rb") as fh:
            return pickle.load(fh)
    data = fetch_angel_history(tokens, ["NIFTY", *symbols], total_days)
    os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)
    with open(CACHE, "wb") as fh:
        pickle.dump(data, fh)
    return data


def main():
    tokens = load_tokens()
    symbols = [s for s in tokens if s != "NIFTY"]
    data = load_data(tokens, symbols, int(TEST_DAYS * 1.5) + 14)
    nifty_df, err = data["NIFTY"]
    if nifty_df is None:
        raise SystemExit(f"NIFTY data unavailable: {err}")
    nifty_trend = nifty_trend_frame(nifty_df)
    today = pd.Timestamp.now(tz=IST).date()
    days = sorted({t.date() for t in nifty_df["timestamp"] if t.date() < today})[10:][-TEST_DAYS:]
    key = lambda d: d.year * 10000 + d.month * 100 + d.day
    test_days = {key(d) for d in days}
    mid = key(days[len(days) // 2])
    jobs = [(s, data[s][0], nifty_trend, test_days) for s in symbols if data[s][0] is not None]
    failed = [s for s in symbols if data[s][0] is None]
    cands, arrs = [], {}
    with ProcessPoolExecutor(max_workers=max(1, os.cpu_count() or 1)) as pool:
        for symbol, cl, arr in pool.map(scan_symbol, jobs, chunksize=4):
            cands += cl
            arrs[symbol] = arr
    cands.sort(key=lambda c: (c["symbol"], c["k"]))     # stable: keeps discovery order within a bar
    print(f"Unfiltered candidate setups: {len(cands)}")

    L = [f"ABLATION + SIGNAL QUALITY | Angel 15m | {days[0]} -> {days[-1]} ({len(days)} days) | split at {mid} | symbols={len(jobs)} failed={len(failed)} | unfiltered setups={len(cands)}",
         f"PRE-SET BAR (net of 5 bps/side): >= {BAR_MIN_TRADES} trades in EACH half, avg net R >= {BAR_MIN_R:+.2f} in BOTH halves, overall t >= {BAR_MIN_T} ({len(PREDICATES)*2} configs tried -> multiple-testing guard).",
         "avgR = mean net R per trade; se = std error; t = avgR/se. H1 = first half, H2 = unseen second half.", ""]
    rows, live_trades = [], None
    for name, pred in PREDICATES.items():
        chosen = pick(cands, pred)
        for ename, ecfg in (("LIVE-exit", LIVE_EXIT), ("TUNED-exit", TUNED_EXIT)):
            t5 = run_cfg(chosen, arrs, ecfg, 5.0)
            t10 = run_cfg(chosen, arrs, ecfg, 10.0)
            a5, (h1, h2) = stats(t5), [stats(x) for x in halves(t5, mid)]
            a10 = stats(t10)
            ok = passes(a5, h1, h2)
            rows.append({"filters": name, "exit": ename, "n": a5["n"], "avgR5": a5["avgR"], "t5": a5["t"], "net5": a5["net"],
                         "h1_n": h1["n"], "h1_avgR": h1["avgR"], "h2_n": h2["n"], "h2_avgR": h2["avgR"], "avgR10": a10["avgR"], "PASS": ok})
            L.append(f"{'PASS' if ok else 'fail'} | {name:36s} | {ename:10s} | n={a5['n']:4d} avgR={a5['avgR']:+.3f} (se {a5['se']:.3f}, t={a5['t']:+.1f}) net=Rs{a5['net']:,.0f} | "
                     f"H1 n={h1['n']} {h1['avgR']:+.3f} | H2 n={h2['n']} {h2['avgR']:+.3f} | @10bps avgR={a10['avgR']:+.3f}")
            if name.startswith("LIVE (") and ename == "LIVE-exit":
                live_trades = t5
    npass = sum(r["PASS"] for r in rows)
    L += ["", f"CONFIGS PASSING THE PRE-SET BAR: {npass}/{len(rows)}"]

    L += ["", "SIGNAL QUALITY on the live signal set (LIVE filters, LIVE exits, 5 bps). Threshold = H1 median; side chosen on H1; scored on unseen H2."]
    ft = []
    if live_trades:
        tr1, tr2 = halves(live_trades, mid)
        base2 = stats(tr2)
        L.append(f"  H2 all live trades: n={base2['n']} avgR={base2['avgR']:+.3f}")
        for f in CONT_FEATURES:
            v1 = np.array([t[f] for t in tr1], float)
            if np.isfinite(v1).sum() < 20:
                continue
            thr = np.nanmedian(v1)
            hi1 = [t for t in tr1 if np.isfinite(t[f]) and t[f] > thr]; lo1 = [t for t in tr1 if np.isfinite(t[f]) and t[f] <= thr]
            take_hi = stats(hi1)["avgR"] >= stats(lo1)["avgR"]
            sel2 = [t for t in tr2 if np.isfinite(t[f]) and ((t[f] > thr) if take_hi else (t[f] <= thr))]
            s1 = stats(hi1 if take_hi else lo1); s2 = stats(sel2)
            ft.append({"feature": f, "rule": f"{'>' if take_hi else '<='} {thr:.3f}", "h1_n": s1["n"], "h1_avgR": s1["avgR"], "h2_n": s2["n"], "h2_avgR": s2["avgR"]})
            L.append(f"  {f:14s} keep {'>' if take_hi else '<='} {thr:8.3f} | H1 n={s1['n']:3d} {s1['avgR']:+.3f} | H2 (unseen) n={s2['n']:3d} {s2['avgR']:+.3f} (se {s2['se']:.3f})")
        for f in CAT_FEATURES:
            parts = []
            for val in sorted({t[f] for t in live_trades}, key=str):
                a1 = stats([t for t in tr1 if t[f] == val]); a2 = stats([t for t in tr2 if t[f] == val])
                parts.append(f"{val}: H1 n={a1['n']} {a1['avgR']:+.3f} / H2 n={a2['n']} {a2['avgR']:+.3f}")
            L.append(f"  {f:14s} " + " | ".join(parts))
    L += ["", "Universe = current watchlist (survivorship bias). Entry at next bar open, no slippage. 4H wick-zone filter not tested. Research only, not a forecast."]
    os.makedirs(OUT, exist_ok=True)
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "configs.csv"), index=False)
    pd.DataFrame(ft).to_csv(os.path.join(OUT, "features.csv"), index=False)
    text = "\n".join(L)
    with open(os.path.join(OUT, "summary_ablation.txt"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
