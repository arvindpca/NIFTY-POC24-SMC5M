"""NIFTY weekly-option strategy comparison (research only, never places orders).

Data
  * NIFTY 50 index 5-minute candles from Angel SmartAPI (token 99926000), ~3 years, in 90-day chunks.
    Fallback: Yahoo ^NSEI 5m (only ~60 days available).
  * India VIX daily closes from Yahoo (^INDIAVIX). The PREVIOUS day's close is used as the IV for the
    whole day, so there is no look-ahead.

Option prices
  Free historical option-chain data is not available, so every option premium is a Black-Scholes price
  with IV = previous-day India VIX. This ignores volatility smile/skew and intraday IV changes (IV usually
  spikes on big moves, so real sellers lose MORE on trend days than this model shows). Treat results as a
  relative comparison between strategies, not as exact rupee P&L.

Weekly expiry: Thursday until 2025-08-28, Tuesday from 2025-09-01 (NSE change). Holidays are ignored.

Costs: COST_PTS per leg round-trip (slippage + brokerage + STT + exchange charges), default 2 points.
"""
import math
import os
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

IST_TZ = "Asia/Kolkata"
OUT = Path("option_study_results")
NIFTY_TOKEN = "99926000"
DAYS = int(os.getenv("OPT_STUDY_DAYS", "1100"))
COST_PTS = float(os.getenv("OPT_COST_PTS", "2.0"))
LOT = int(os.getenv("OPT_LOT_SIZE", "65"))
RATE = 0.065
STEP = 50
EXPIRY_SWITCH = date(2025, 9, 1)  # weekly expiry moved Thursday -> Tuesday


# --------------------------------------------------------------------------- pricing
def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# Pricing model knobs (set per run in main):
#   intraday_share: fraction of a day's variance (= theta) that is earned during market hours. 1.0 = naive
#                   model (all decay happens 09:15-15:30). Real markets price part of each day's decay for
#                   the overnight gap, so an intraday seller only collects this share. Measured from data.
#   iv_shock:       IV rises when NIFTY moves away from the 09:20 price: iv * (1 + iv_shock * |move|).
#                   e.g. 6 -> a 1% move lifts IV by 6% (14 -> 14.84). 0 = constant IV (naive).
MODEL = {"intraday_share": 1.0, "iv_shock": 0.0}
DAY = {"spot0": None}


def bs(spot, strike, t_years, iv, kind):
    if DAY["spot0"] and MODEL["iv_shock"]:
        iv = iv * (1.0 + MODEL["iv_shock"] * abs(spot / DAY["spot0"] - 1.0))
    if t_years <= 1e-6 or iv <= 0:
        return max(0.0, spot - strike) if kind == "C" else max(0.0, strike - spot)
    sq = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (RATE + 0.5 * iv * iv) * t_years) / sq
    d2 = d1 - sq
    if kind == "C":
        return spot * _ncdf(d1) - strike * math.exp(-RATE * t_years) * _ncdf(d2)
    return strike * math.exp(-RATE * t_years) * _ncdf(-d2) - spot * _ncdf(-d1)


def expiry_for(d):
    wd = 3 if d < EXPIRY_SWITCH else 1  # Thu / Tue
    return d + timedelta(days=(wd - d.weekday()) % 7)


def weekdays_between(a, b):
    """Weekdays strictly after a up to and including b."""
    n, cur = 0, a
    while cur < b:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def t_years(d, clock):
    """Trading-time to expiry close (15:30) from intraday clock on day d."""
    mins_left = max(0.0, (15 * 60 + 30) - (clock.hour * 60 + clock.minute))
    return (weekdays_between(d, expiry_for(d)) + MODEL["intraday_share"] * mins_left / 375.0) / 252.0


def atm(spot):
    return int(round(spot / STEP) * STEP)


# --------------------------------------------------------------------------- data
def yahoo_chart(ticker, interval, rng):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(ticker, safe='^')}"
    r = requests.get(url, params={"interval": interval, "range": rng}, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"timestamp": pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(IST_TZ),
                       "open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"]})
    return df.dropna().reset_index(drop=True)


def fetch_vix():
    df = yahoo_chart("^INDIAVIX", "1d", "10y")
    df["date"] = df["timestamp"].dt.date
    s = df.groupby("date")["close"].last()
    return s


def fetch_nifty_angel(days):
    from app.smartapi_client import _get_candle_data_with_retry, create_data_client

    api = create_data_client()
    end = pd.Timestamp.now(tz=IST_TZ).floor("min")
    start_all = end - pd.Timedelta(days=days)
    parts, cur = [], start_all
    try:
        while cur < end:
            nxt = min(end, cur + pd.Timedelta(days=90))
            params = {"exchange": "NSE", "symboltoken": NIFTY_TOKEN, "interval": "FIVE_MINUTE",
                      "fromdate": cur.strftime("%Y-%m-%d %H:%M"), "todate": nxt.strftime("%Y-%m-%d %H:%M")}
            try:
                resp = _get_candle_data_with_retry(api, params, NIFTY_TOKEN)
                d = pd.DataFrame(resp["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
                parts.append(d)
                print(f"ANGEL {cur.date()} -> {nxt.date()}: {len(d)} bars", flush=True)
            except RuntimeError as exc:
                print(f"ANGEL {cur.date()} -> {nxt.date()}: skipped ({exc})", flush=True)
            cur = nxt
    finally:
        try:
            api.terminateSession(os.getenv("ANGEL_CLIENT_CODE", ""))
        except Exception:
            pass
    if not parts:
        raise RuntimeError("Angel returned no NIFTY data")
    df = pd.concat(parts, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["timestamp"] = (df["timestamp"].dt.tz_localize(IST_TZ) if df["timestamp"].dt.tz is None
                       else df["timestamp"].dt.tz_convert(IST_TZ))
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def load_nifty():
    try:
        df, src = fetch_nifty_angel(DAYS), "Angel SmartAPI 5m"
    except Exception as exc:
        print(f"Angel failed ({exc}); falling back to Yahoo 5m (60 days only)", flush=True)
        df, src = yahoo_chart("^NSEI", "5m", "60d"), "Yahoo 5m (60d fallback)"
    df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    df = df[(df["timestamp"].dt.time >= dtime(9, 15)) & (df["timestamp"].dt.time <= dtime(15, 25))]
    df["date"] = df["timestamp"].dt.date
    return df.reset_index(drop=True), src


# --------------------------------------------------------------------------- simulation
class Leg:
    def __init__(self, kind, strike, side, price, sl_pct=None):
        self.kind, self.strike, self.side, self.entry, self.sl_pct = kind, strike, side, price, sl_pct
        self.exit = None

    def pnl(self):
        return self.side * (self.exit - self.entry) - COST_PTS


def worst_spot(leg, bar):
    """Spot extreme that hurts this leg most within the bar."""
    adverse_up = (leg.kind == "C") == (leg.side == -1)  # short call / long put lose when spot rises
    return bar.high if adverse_up else bar.low


def best_spot(leg, bar):
    return bar.low if worst_spot(leg, bar) == bar.high else bar.high


def run_short(day, bars, iv, legs_spec, combined_sl=None):
    """Sell/buy legs at 09:20, per-leg premium SL on short legs, exit 15:15."""
    entry_bar = bars.iloc[0]
    spot0 = entry_bar.close
    t0 = t_years(day, dtime(9, 20))
    k0 = atm(spot0)
    legs = []
    for kind, offset_pct, side, sl in legs_spec:
        strike = k0 if offset_pct == 0 else atm(spot0 * (1 + offset_pct))
        legs.append(Leg(kind, strike, side, bs(spot0, strike, t0, iv, kind), sl))
    credit0 = sum(-l.side * l.entry for l in legs)
    for _, bar in bars.iloc[1:].iterrows():
        clock = (bar.timestamp + pd.Timedelta(minutes=5)).time()
        if clock > dtime(15, 15):
            break
        t = t_years(day, clock)
        if combined_sl is not None:
            open_legs = [l for l in legs if l.exit is None]
            # mark open legs at the bar's worse extreme for the combined book (approximation: use the side
            # that makes the overall position worse)
            for sp in (bar.high, bar.low):
                mtm = sum(l.side * (bs(sp, l.strike, t, iv, l.kind) - l.entry) for l in open_legs)
                mtm += sum(l.side * (l.exit - l.entry) for l in legs if l.exit is not None)
                if mtm <= -combined_sl * credit0:
                    for l in open_legs:
                        l.exit = bs(sp, l.strike, t, iv, l.kind)
                    break
        for l in legs:
            if l.exit is not None or l.sl_pct is None or l.side != -1:
                continue
            stop = l.entry * (1 + l.sl_pct)
            px_open = bs(bar.open, l.strike, t, iv, l.kind)
            px_worst = bs(worst_spot(l, bar), l.strike, t, iv, l.kind)
            if px_open >= stop:
                l.exit = px_open  # gapped through
            elif px_worst >= stop:
                l.exit = stop
        if all(l.exit is not None for l in legs):
            break
    last = bars[bars["timestamp"].dt.time <= dtime(15, 10)].iloc[-1]
    t_end = t_years(day, dtime(15, 15))
    for l in legs:
        if l.exit is None:
            l.exit = bs(last.close, l.strike, t_end, iv, l.kind)
    return sum(l.pnl() for l in legs), credit0


def run_orb_buy(day, bars, iv, sl_pct=0.30, tgt_pct=0.60):
    """15-min opening range breakout: buy ATM CE/PE on 5m close outside range; one trade/day."""
    rng = bars[bars["timestamp"].dt.time < dtime(9, 30)]
    if len(rng) < 3:
        return None, 0.0
    hi, lo = rng.high.max(), rng.low.min()
    rest = bars[bars["timestamp"].dt.time >= dtime(9, 30)]
    leg = None
    for _, bar in rest.iterrows():
        clock = (bar.timestamp + pd.Timedelta(minutes=5)).time()
        if clock > dtime(15, 15):
            break
        t = t_years(day, clock)
        if leg is None:
            if clock > dtime(14, 30):
                return None, 0.0
            kind = "C" if bar.close > hi else "P" if bar.close < lo else None
            if kind:
                k = atm(bar.close)
                leg = Leg(kind, k, +1, bs(bar.close, k, t, iv, kind))
            continue
        px_worst = bs(worst_spot(leg, bar), leg.strike, t, iv, leg.kind)
        px_best = bs(best_spot(leg, bar), leg.strike, t, iv, leg.kind)
        if px_worst <= leg.entry * (1 - sl_pct):
            leg.exit = min(leg.entry * (1 - sl_pct), bs(bar.open, leg.strike, t, iv, leg.kind))
            break
        if px_best >= leg.entry * (1 + tgt_pct):
            leg.exit = leg.entry * (1 + tgt_pct)
            break
    if leg is None:
        return None, 0.0
    if leg.exit is None:
        last = bars[bars["timestamp"].dt.time <= dtime(15, 10)].iloc[-1]
        leg.exit = bs(last.close, leg.strike, t_years(day, dtime(15, 15)), iv, leg.kind)
    return leg.pnl(), leg.entry


STRATEGIES = {
    "A_straddle_920_SL25": lambda d, b, iv: run_short(d, b, iv, [("C", 0, -1, 0.25), ("P", 0, -1, 0.25)]),
    "B_straddle_920_SL40": lambda d, b, iv: run_short(d, b, iv, [("C", 0, -1, 0.40), ("P", 0, -1, 0.40)]),
    "C_strangle_1pct_SL50": lambda d, b, iv: run_short(d, b, iv, [("C", 0.01, -1, 0.50), ("P", -0.01, -1, 0.50)]),
    "D_ironfly_wings1.25pct": lambda d, b, iv: run_short(
        d, b, iv, [("C", 0, -1, None), ("P", 0, -1, None), ("C", 0.0125, +1, None), ("P", -0.0125, +1, None)]),
    "E_ironfly_combinedSL30": lambda d, b, iv: run_short(
        d, b, iv, [("C", 0, -1, None), ("P", 0, -1, None), ("C", 0.0125, +1, None), ("P", -0.0125, +1, None)],
        combined_sl=0.30),
    "F_ORB15_buy_SL30_T60": run_orb_buy,
}


# --------------------------------------------------------------------------- reporting
def stats(s):
    s = s.dropna()
    if s.empty:
        return {}
    eq = s.cumsum()
    wins, losses = s[s > 0].sum(), -s[s < 0].sum()
    return {"days": len(s), "win%": round(100 * (s > 0).mean(), 1), "total_pts": round(s.sum(), 1),
            "avg_pts": round(s.mean(), 2), "PF": round(wins / losses, 2) if losses else float("inf"),
            "maxDD_pts": round((eq - eq.cummax()).min(), 1), "worst_day": round(s.min(), 1),
            "best_day": round(s.max(), 1)}


def intraday_share(nifty):
    """Share of daily variance that happens during market hours (vs the overnight gap), from the data."""
    d = nifty.groupby("date").agg(o=("open", "first"), c=("close", "last"))
    intra = np.log(d.c / d.o)
    gap = np.log(d.o / d.c.shift(1))
    both = pd.concat([intra, gap], axis=1).dropna()
    vi, vg = (both.iloc[:, 0] ** 2).mean(), (both.iloc[:, 1] ** 2).mean()
    return float(vi / (vi + vg))


def run_mode(label, nifty, vix_prev, src):
    rows = []
    for day, bars in nifty.groupby("date"):
        bars = bars.reset_index(drop=True)
        if len(bars) < 70 or bars.timestamp.iloc[0].time() != dtime(9, 15):
            continue
        prev = vix_prev[vix_prev.index <= day]
        if prev.empty or pd.isna(prev.iloc[-1]):
            continue
        iv = float(prev.iloc[-1]) / 100.0
        DAY["spot0"] = float(bars.close.iloc[0])
        row = {"date": day, "weekday": day.strftime("%a"), "dte": weekdays_between(day, expiry_for(day)),
               "iv": round(iv * 100, 2), "open": bars.open.iloc[0], "close": bars.close.iloc[-1],
               "range_pct": round(100 * (bars.high.max() - bars.low.min()) / bars.open.iloc[0], 2)}
        for name, fn in STRATEGIES.items():
            pnl, prem = fn(day, bars, iv)
            row[name] = None if pnl is None else round(pnl, 2)
            row[name + "_prem"] = round(prem, 1)
        rows.append(row)
    DAY["spot0"] = None
    df = pd.DataFrame(rows)
    df.to_csv(OUT / f"daily_pnl_{label}.csv", index=False)

    lines = [f"NIFTY weekly option strategy study [{label}]  (generated {datetime.now():%Y-%m-%d %H:%M} UTC)",
             f"Data: {src}; {df.date.min()} -> {df.date.max()}; {len(df)} trading days",
             f"Option prices: Black-Scholes, IV = previous-day India VIX; intraday_share={MODEL['intraday_share']:.2f}, "
             f"iv_shock={MODEL['iv_shock']}. Cost {COST_PTS} pts per leg round-trip.",
             f"Rs figures = points x lot size {LOT}. Research only, not advice.", ""]
    names = list(STRATEGIES)
    table = pd.DataFrame({n: stats(df[n]) for n in names}).T
    table["total_Rs_per_lot"] = (table["total_pts"] * LOT).round(0)
    table["maxDD_Rs_per_lot"] = (table["maxDD_pts"] * LOT).round(0)
    lines += ["=== Overall ===", table.to_string(), ""]

    half = df.date.iloc[len(df) // 2]
    lines.append(f"=== Consistency: first half (< {half}) vs second half ===")
    for n in names:
        a, b = stats(df[df.date < half][n]), stats(df[df.date >= half][n])
        lines.append(f"{n:26s} H1 total={a.get('total_pts')} PF={a.get('PF')} | H2 total={b.get('total_pts')} PF={b.get('PF')}")
    lines.append("")

    df["year"] = pd.to_datetime(df.date).dt.year
    lines += ["=== Total points by year ===", df.groupby("year")[names].sum().round(0).to_string(), ""]
    df["expiry_day"] = df.dte == 0
    lines += ["=== Avg points/day: expiry day vs other days ===",
              df.groupby("expiry_day")[names].mean().round(2).to_string(), ""]
    df["vix_bucket"] = pd.cut(df.iv, [0, 12, 15, 20, 100], labels=["<12", "12-15", "15-20", ">20"])
    lines += ["=== Avg points/day by previous-day VIX ===",
              df.groupby("vix_bucket", observed=True)[names].mean().round(2).to_string(), ""]
    lines += ["=== Sanity: average 09:20 ATM straddle premium (model) by days-to-expiry ===",
              df.groupby("dte")["A_straddle_920_SL25_prem"].agg(["count", "mean"]).round(1).to_string(), ""]
    text = "\n".join(lines)
    (OUT / f"summary_{label}.txt").write_text(text)
    print(text, flush=True)
    return table


def main():
    OUT.mkdir(exist_ok=True)
    vix = fetch_vix()
    nifty, src = load_nifty()
    vix_prev = vix.shift(1)
    share = intraday_share(nifty)
    print(f"Measured intraday share of daily variance: {share:.2f}", flush=True)
    modes = {
        "naive": {"intraday_share": 1.0, "iv_shock": 0.0},
        "realistic": {"intraday_share": share, "iv_shock": float(os.getenv("OPT_IV_SHOCK", "6"))},
        "harsh": {"intraday_share": share, "iv_shock": 12.0},
    }
    tables = {}
    for label, cfg in modes.items():
        MODEL.update(cfg)
        tables[label] = run_mode(label, nifty, vix_prev, src)
    cmp_lines = [f"Measured intraday share of variance = {share:.2f}", "",
                 "Total points (3 yrs) / profit factor / max drawdown points, by pricing model:"]
    cmp = pd.concat({k: t[["total_pts", "PF", "win%", "maxDD_pts"]] for k, t in tables.items()}, axis=1)
    cmp_lines.append(cmp.to_string())
    text = "\n".join(cmp_lines)
    (OUT / "compare_models.txt").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
