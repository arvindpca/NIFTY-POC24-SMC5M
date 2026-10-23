"""10-day replay of the LIVE paper-trading logic (no orders, research only).

Replays app.strategy_adapter._latest_setup_signal bar by bar on end-labelled 15m candles with the same
paper rules as app.main: next-candle-open entry, SL at sweep extreme +/- 0.10 ATR, 1.5R target, SL-first when
both hit, 15:00 time exit, max 5 concurrent, max 5 entries/day, no same-symbol overlap, Rs 50,000 notional.
Indicators are causal (EWM / backward windows / lagged pivots), so computing them once per symbol and
evaluating each bar on its prefix equals what the live scanner would have seen.
"""
import math
import os
import sys
from pathlib import Path

import pandas as pd

import strategy_b_original as original
from app.main import load_tokens, bulk_yahoo_fetch
from app.rules import exit_decision, target_for
from app.strategy_adapter import _latest_setup_signal, nifty_trend_frame

IST = original.IST
NOTIONAL = 50_000.0
MAX_OPEN = 5
MAX_PER_DAY = 5
RR = 1.5
TEST_DAYS = int(os.getenv("BACKTEST_DAYS", "10"))
OUT_DIR = Path(os.getenv("BACKTEST_OUT", "backtest_results"))


def prepare(raw, nifty_trend):
    d = original.prepare_symbol_from_15m(original.normalize_columns(raw), end_labelled=True, wick_zones=False)
    d = pd.merge_asof(d.sort_values("timestamp"), nifty_trend.sort_values("timestamp"),
                      on="timestamp", direction="backward", allow_exact_matches=True)
    return d.reset_index(drop=True)


def symbol_trades(symbol, frame, test_dates):
    """All signals (one per bar at most) with their stand-alone exit, before portfolio limits."""
    out = []
    n = len(frame)
    for k in range(max(original.MIN_WARMUP_BARS, 15), n - 1):
        row_ts = frame.at[k, "timestamp"]
        if row_ts.date() not in test_dates:
            continue
        sig = _latest_setup_signal(frame.iloc[: k + 1], frame.at[k, "nifty_trend"])
        if not sig:
            continue
        entry_bar = frame.iloc[k + 1]
        if entry_bar["timestamp"].date() != row_ts.date():
            continue  # next bar is a new session: live scanner would never enter overnight
        entry = float(entry_bar["open"])
        sl, side = float(sig["stop_loss"]), sig["side"]
        if (side == "LONG" and sl >= entry) or (side == "SHORT" and sl <= entry):
            continue
        target = target_for(entry, sl, side, RR)
        qty = math.floor(NOTIONAL / entry) if entry > 0 else 0
        if qty < 1:
            continue
        entry_ts = entry_bar["timestamp"] - pd.Timedelta(minutes=15)
        exit_px = exit_ts = reason = None
        for j in range(k + 1, n):
            bar = frame.iloc[j]
            if bar["timestamp"].date() != row_ts.date():
                break
            dec = exit_decision(bar["timestamp"], bar, side, sl, target)
            if dec:
                exit_px, reason = dec
                exit_ts = bar["timestamp"]
                break
        if exit_px is None:  # data ended before an exit (should not happen for completed days)
            continue
        gross = (exit_px - entry) * qty if side == "LONG" else (entry - exit_px) * qty
        out.append({"symbol": symbol, "side": side, "signal_time": row_ts, "entry_time": entry_ts,
                    "entry": entry, "sl": sl, "target": target, "qty": qty, "exit_time": exit_ts,
                    "exit": exit_px, "reason": reason, "gross_pnl": gross,
                    "r_multiple": gross / (abs(entry - sl) * qty), "nifty_trend": sig["nifty_trend"]})
    return out


def apply_portfolio(candidates):
    accepted, per_day = [], {}
    for t in sorted(candidates, key=lambda x: (x["entry_time"], x["symbol"])):
        day = t["entry_time"].date()
        open_now = [a for a in accepted if a["exit_time"] > t["entry_time"]]
        if len(open_now) >= MAX_OPEN or per_day.get(day, 0) >= MAX_PER_DAY:
            continue
        if any(a["symbol"] == t["symbol"] for a in open_now):
            continue
        accepted.append(t)
        per_day[day] = per_day.get(day, 0) + 1
    return accepted


def summarize(trades, label, cost_bps=0.0):
    if not trades:
        return f"{label}: 0 trades"
    df = pd.DataFrame(trades)
    cost = (df["entry"] * df["qty"] + df["exit"] * df["qty"]) * cost_bps / 10000.0
    pnl = df["gross_pnl"] - cost
    wins, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    eq = pnl.cumsum()
    dd = (eq - eq.cummax()).min()
    return (f"{label}: trades={len(df)} win_rate={100*(pnl>0).mean():.1f}% net_pnl=Rs{pnl.sum():,.0f} "
            f"profit_factor={(wins/losses if losses else float('inf')):.2f} avg_R={df['r_multiple'].mean():.2f} "
            f"max_drawdown=Rs{dd:,.0f} (notional Rs{NOTIONAL:,.0f}/trade, cost {cost_bps} bps/side)")


def main():
    tokens = load_tokens()
    symbols = [s for s in tokens if s != "NIFTY"]
    print(f"Downloading 15m history: {len(symbols)+1} symbols (Yahoo, parallel)")
    data = bulk_yahoo_fetch(["NIFTY", *symbols])
    nifty_df, err = data["NIFTY"]
    if nifty_df is None:
        raise SystemExit(f"NIFTY data unavailable: {err}")
    nifty_trend = nifty_trend_frame(nifty_df)
    today = pd.Timestamp.now(tz=IST).date()
    include_today = os.getenv("BACKTEST_INCLUDE_TODAY", "false").lower() == "true"
    days = sorted({t.date() for t in nifty_df["timestamp"] if (t.date() <= today if include_today else t.date() < today)})[-TEST_DAYS:]
    test_dates = set(days)
    print(f"Test window: {days[0]} to {days[-1]} ({len(days)} trading days)")
    candidates, ok, failed = [], 0, []
    for sym in symbols:
        df, e = data[sym]
        if df is None:
            failed.append(sym)
            continue
        try:
            candidates += symbol_trades(sym, prepare(df, nifty_trend), test_dates)
            ok += 1
        except Exception as exc:  # one bad symbol must not kill the study
            failed.append(f"{sym}({exc})")
    accepted = apply_portfolio(candidates)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(accepted).to_csv(OUT_DIR / "trades_10d.csv", index=False)
    lines = [f"BACKTEST {days[0]} -> {days[-1]} | symbols ok={ok} failed={len(failed)} | raw signals={len(candidates)}",
             summarize(accepted, "ALL (portfolio-limited)"), summarize(accepted, "ALL, 5 bps/side costs", 5.0)]
    for side in ("LONG", "SHORT"):
        lines.append(summarize([t for t in accepted if t["side"] == side], side))
    if accepted:
        d = pd.DataFrame(accepted)
        d["day"] = d["entry_time"].dt.date
        lines.append("Per day net P&L (Rs): " + ", ".join(f"{k}:{v:,.0f}" for k, v in d.groupby("day")["gross_pnl"].sum().items()))
        lines.append("Exit reasons: " + ", ".join(f"{k}={v}" for k, v in d["reason"].value_counts().items()))
    nt = nifty_trend[nifty_trend["timestamp"].dt.date.isin(test_dates)].copy()
    nt["day"] = nt["timestamp"].dt.date
    for day, g in nt.groupby("day"):
        c = g["nifty_trend"].value_counts().to_dict()
        lines.append(f"NIFTY 1H trend {day}: POSITIVE={c.get('POSITIVE',0)} NEGATIVE={c.get('NEGATIVE',0)} NEUTRAL={c.get('NEUTRAL',0)} (15m bars)")
    if candidates:
        lines.append("Raw signals by day: " + ", ".join(f"{k}:{v}" for k, v in pd.Series([t["signal_time"].date() for t in candidates]).value_counts().sort_index().items()))
    if failed:
        lines.append("Data failures: " + ", ".join(failed[:20]))
    lines.append("Note: Yahoo 15m OHLCV (volume/prices differ slightly from Angel). Paper simulation, not a forecast.")
    text = "\n".join(lines)
    (OUT_DIR / "summary_10d.txt").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
