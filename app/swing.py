"""Daily-chart swing research: (A) Donchian trend-following, (B) monthly relative-strength momentum.
Research only - no orders. Pre-registered rules, signals at close t, execution at open t+1, costs per side.

PASS BAR (fixed before running): at 10 bps/side, in BOTH halves of the history, CAGR >= Nifty CAGR + 3pp AND
Sharpe >= Nifty Sharpe. Parameter neighbours are reported to show whether the edge survives small changes.
Universe = the repo's CURRENT symbol list (survivorship bias flatters stock-picking results) - the equal-weight
universe line is the like-for-like reference for that bias.
"""
import itertools
import os
import pickle
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

OUT = os.getenv("SWING_OUT", "swing_results")
CACHE = os.getenv("SWING_CACHE", ".cache_swing/daily.pkl")
START_TS = int(pd.Timestamp("2014-01-01", tz="UTC").timestamp())
COST_BPS = 10.0
WARMUP = 252
PRIMARY_TREND = dict(brk=55, exit=20, maxpos=10)
PRIMARY_MOM = dict(look=126, skip=21, topn=15)
BAR_EXCESS = 0.03


def fetch_daily(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(ticker, safe='^')}"
    last = None
    for a in range(3):
        try:
            r = requests.get(url, params={"period1": START_TS, "period2": int(time.time()), "interval": "1d", "events": "div,splits"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose")
            idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None).normalize()
            df = pd.DataFrame({"o": q["open"], "h": q["high"], "l": q["low"], "c": q["close"]}, index=idx)
            if adj is not None:
                f = pd.Series(adj, index=idx) / df["c"]
                for k in ("o", "h", "l", "c"):
                    df[k] = df[k] * f
            df = df[~df.index.duplicated(keep="last")].dropna()
            return df[(df > 0).all(axis=1)]
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (a + 1))
    raise RuntimeError(f"{ticker}: {last}")


def load_panels(symbols):
    if os.path.exists(CACHE) and time.time() - os.path.getmtime(CACHE) < 3 * 86400:
        with open(CACHE, "rb") as fh:
            raw = pickle.load(fh)
    else:
        def one(s):
            try:
                return s, fetch_daily("^NSEI" if s == "NIFTY" else f"{s}.NS"), None
            except Exception as exc:
                return s, None, str(exc)
        with ThreadPoolExecutor(max_workers=8) as pool:
            raw = {s: (d, e) for s, d, e in pool.map(one, ["NIFTY", *symbols])}
        os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)
        with open(CACHE, "wb") as fh:
            pickle.dump(raw, fh)
    nifty = raw["NIFTY"][0]
    if nifty is None:
        raise SystemExit(f"NIFTY daily data unavailable: {raw['NIFTY'][1]}")
    dates = nifty.index
    good = {s: d for s, (d, e) in raw.items() if s != "NIFTY" and d is not None and len(d) > 300}
    panel = {k: pd.DataFrame({s: d[k] for s, d in good.items()}).reindex(dates) for k in ("o", "h", "l", "c")}
    failed = [s for s in symbols if s not in good]
    return nifty, panel, failed


def _val_prices(P):
    C = P["c"]
    Cff = C.ffill()
    Off = P["o"].where(P["o"].notna(), Cff)
    return Cff.fillna(0.0), Off.fillna(0.0)        # not-yet-listed stocks: price 0 and zero shares, never NaN in sums


def sim_trend(P, mkt_ok, brk, exit, maxpos, bps=COST_BPS, start_cash=1_000_000.0):
    C, H, L = P["c"], P["h"], P["l"]
    Cff, Off = _val_prices(P)
    sma = C.rolling(200).mean()
    hi = H.rolling(brk).max().shift(1)
    lo = L.rolling(exit).min().shift(1)
    mom = C.shift(21) / C.shift(126) - 1
    sig_in = ((C > hi) & (C > sma)).to_numpy() & mkt_ok.to_numpy()[:, None]
    sig_out = (C < lo).to_numpy()
    momv, Cn, Co, Cffn, Offn = mom.to_numpy(), C.to_numpy(), C.notna().to_numpy(), Cff.to_numpy(), Off.to_numpy()
    n, m = Cn.shape
    cash, sh = start_cash, np.zeros(m)
    entry_px = np.zeros(m)
    pend_out, pend_in = [], []
    eq = np.full(n, np.nan)
    trades = []
    c = bps / 1e4
    for t in range(WARMUP, n):
        # --- execute at today's open
        for j in pend_out:
            if sh[j] > 0 and Co[t, j]:
                px = Offn[t, j]
                cash += sh[j] * px * (1 - c)
                trades.append((entry_px[j], px))
                sh[j] = 0
        held = sh > 0
        v_open = cash + float((sh[held] * Offn[t, held]).sum())
        free = maxpos - int(held.sum())
        for j in pend_in[:max(free, 0)]:
            if sh[j] == 0 and Co[t, j]:
                alloc = min(v_open / maxpos, cash / (1 + c))
                px = Offn[t, j]
                if alloc > 0 and px > 0:
                    q = alloc / px
                    sh[j], entry_px[j] = q, px
                    cash -= q * px * (1 + c)
        eq[t] = cash + float((sh * Cffn[t]).sum())
        # --- signals at today's close for tomorrow
        held = sh > 0
        pend_out = list(np.where(held & sig_out[t])[0])
        cand = np.where(~held & sig_in[t] & Co[t])[0]
        order = sorted(cand, key=lambda j: -(momv[t, j] if np.isfinite(momv[t, j]) else -9))
        pend_in = order
    return pd.Series(eq, index=C.index), trades


def sim_mom(P, mkt_ok, look, skip, topn, bps=COST_BPS, start_cash=1_000_000.0):
    C = P["c"]
    Cff, Off = _val_prices(P)
    mom = (C.shift(skip) / C.shift(look) - 1).to_numpy()
    Cn, Co, Cffn, Offn = C.to_numpy(), C.notna().to_numpy(), Cff.to_numpy(), Off.to_numpy()
    okv = mkt_ok.to_numpy()
    idx = C.index
    n, m = Cn.shape
    cash, sh = start_cash, np.zeros(m)
    eq = np.full(n, np.nan)
    c = bps / 1e4
    turnover = 0.0
    for t in range(WARMUP, n):
        if idx[t].month != idx[t - 1].month:                 # first trading day of the month; signal from t-1 close
            sel = []
            if okv[t - 1]:
                score = np.where(Co[t - 1] & np.isfinite(mom[t - 1]), mom[t - 1], -np.inf)
                sel = [j for j in np.argsort(-score)[:topn] if np.isfinite(score[j]) and Co[t, j]]
            held = sh > 0
            v = cash + float((sh[held] * Offn[t, held]).sum())
            target = np.zeros(m)
            for j in sel:
                target[j] = v / topn
            for j in range(m):
                cur = sh[j] * Offn[t, j]
                if cur > 0 and (target[j] == 0) and not Co[t, j]:
                    target[j] = cur                          # cannot trade today: keep
            delta = target - sh * Offn[t]
            sells = np.where(delta < 0)[0]
            buys = np.where(delta > 0)[0]
            for j in sells:
                sold = -delta[j]
                cash += sold * (1 - c); sh[j] -= sold / Offn[t, j]; turnover += sold
            for j in buys:
                if Offn[t, j] <= 0:
                    continue
                spend = min(delta[j], cash / (1 + c))
                if spend > 0:
                    sh[j] += spend / Offn[t, j]; cash -= spend * (1 + c); turnover += spend
        eq[t] = cash + float((sh * Cffn[t]).sum())
    return pd.Series(eq, index=C.index), turnover


def perf(eq):
    eq = eq.dropna()
    if len(eq) < 30:
        return dict(cagr=np.nan, sharpe=np.nan, dd=np.nan, mult=np.nan)
    r = eq.pct_change().dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    return dict(cagr=(eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1, sharpe=float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0,
                dd=float((eq / eq.cummax() - 1).min()), mult=float(eq.iloc[-1] / eq.iloc[0]))


def fmt(p):
    return f"CAGR {p['cagr']*100:+6.1f}%  Sharpe {p['sharpe']:+.2f}  maxDD {p['dd']*100:6.1f}%  x{p['mult']:.2f}"


def halves(eq, mid):
    return perf(eq[eq.index < mid]), perf(eq[eq.index >= mid])


def verdict(h1, h2, b1, b2):
    ok = (h1["cagr"] >= b1["cagr"] + BAR_EXCESS and h2["cagr"] >= b2["cagr"] + BAR_EXCESS
          and h1["sharpe"] >= b1["sharpe"] and h2["sharpe"] >= b2["sharpe"])
    return ok


def main():
    import csv
    with open("config/symbol_tokens.csv", newline="", encoding="utf-8-sig") as fh:
        symbols = [r["symbol"].strip().upper() for r in csv.DictReader(fh) if r["symbol"].strip().upper() != "NIFTY"]
    nifty, P, failed = load_panels(symbols)
    mkt_ok = pd.Series((nifty["c"] > nifty["c"].rolling(200).mean()).to_numpy(), index=nifty.index)
    dates = nifty.index[WARMUP:]
    mid = dates[len(dates) // 2]
    bench = nifty["c"].loc[dates[0]:]
    ew = (1 + P["c"].pct_change().mean(axis=1).fillna(0)).cumprod().loc[dates[0]:]
    b1, b2 = halves(bench, mid)
    L = [f"SWING STUDY | {dates[0].date()} -> {dates[-1].date()} | split {mid.date()} | symbols loaded={P['c'].shape[1]} failed={len(failed)} | cost {COST_BPS:g} bps/side",
         "Rules fixed in advance; signal at close, trade at next open; prices split/dividend adjusted. Survivorship bias: current watchlist.",
         f"PASS BAR: both halves CAGR >= Nifty +{BAR_EXCESS*100:.0f}pp AND Sharpe >= Nifty.", "",
         f"NIFTY buy&hold   ALL {fmt(perf(bench))}", f"                  H1 {fmt(b1)}", f"                  H2 {fmt(b2)}",
         f"EqualWt universe ALL {fmt(perf(ew))}   <- survivorship-biased reference (no costs)", ""]
    results = {}
    for bps in (10.0, 20.0):
        eqA, trades = sim_trend(P, mkt_ok, **PRIMARY_TREND, bps=bps)
        eqB, turn = sim_mom(P, mkt_ok, **PRIMARY_MOM, bps=bps)
        for name, eq in (("A TREND (55d breakout, 20d exit, 10 pos, Nifty>200DMA)", eqA), ("B MOMENTUM (6-1m, top15, monthly, Nifty>200DMA)", eqB)):
            e = eq.loc[dates[0]:]
            h1, h2 = halves(e, mid)
            tag = "PASS" if verdict(h1, h2, b1, b2) else "fail"
            extra = f" | trades={len(trades)} win={100*np.mean([x[1] > x[0] for x in trades]):.0f}%" if name.startswith("A") and trades else ""
            L += [f"[{tag}] @{bps:g}bps {name}{extra}", f"      ALL {fmt(perf(e))}", f"      H1  {fmt(h1)}", f"      H2  {fmt(h2)}"]
            results[(name[0], bps)] = (tag, e)
    L += ["", "YEARLY RETURNS @10bps (%):", "year   NIFTY   A-trend   B-mom"]
    yr = lambda s: s.groupby(s.index.year).apply(lambda x: x.iloc[-1] / x.iloc[0] - 1)
    ya, yb, yn = yr(results[("A", 10.0)][1]), yr(results[("B", 10.0)][1]), yr(bench)
    for y in yn.index:
        L.append(f"{y}  {yn.get(y, np.nan)*100:7.1f} {ya.get(y, np.nan)*100:9.1f} {yb.get(y, np.nan)*100:8.1f}")
    L += ["", "NEIGHBOURS @10bps (primary marked *): does it survive small parameter changes?"]
    npa = 0
    for brk, ex in itertools.product((40, 55, 80), (10, 20, 30)):
        e = sim_trend(P, mkt_ok, brk=brk, exit=ex, maxpos=10)[0].loc[dates[0]:]
        h1, h2 = halves(e, mid); ok = verdict(h1, h2, b1, b2); npa += ok
        L.append(f"  A brk={brk} exit={ex}{'*' if (brk, ex) == (55, 20) else ' '}: {'PASS' if ok else 'fail'} | H1 {h1['cagr']*100:+.1f}% H2 {h2['cagr']*100:+.1f}% | all {fmt(perf(e))}")
    npb = 0
    for look, topn in itertools.product((90, 126, 189), (10, 15, 20)):
        e = sim_mom(P, mkt_ok, look=look, skip=21, topn=topn)[0].loc[dates[0]:]
        h1, h2 = halves(e, mid); ok = verdict(h1, h2, b1, b2); npb += ok
        L.append(f"  B look={look} top{topn}{'*' if (look, topn) == (126, 15) else ' '}: {'PASS' if ok else 'fail'} | H1 {h1['cagr']*100:+.1f}% H2 {h2['cagr']*100:+.1f}% | all {fmt(perf(e))}")
    L += ["", f"Neighbours passing: A {npa}/9, B {npb}/9 (18 configs tried -> a lone pass is weak evidence)."]
    os.makedirs(OUT, exist_ok=True)
    text = "\n".join(L)
    open(os.path.join(OUT, "summary_swing.txt"), "w", encoding="utf-8").write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
