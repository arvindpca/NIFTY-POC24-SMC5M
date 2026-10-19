"""Causal, paper-only Strategy B signal adapter.

Uses the original source's candle preparation/indicator functions. The live
signal is evaluated only on the latest completed 15-minute candle; it does not
use future candles to decide whether a retest happened.
"""
import math
import numpy as np
import pandas as pd
import strategy_b_original as original


def _latest_setup_signal(df, nifty_trend):
    """Evaluate the latest completed bar as a retest/confirmation candle."""
    x = df.reset_index(drop=True)
    k = len(x) - 1
    if k < max(original.MIN_WARMUP_BARS, 15):
        return None
    row = x.iloc[k]
    atr_now = row.get("atr", np.nan)
    if not np.isfinite(atr_now) or atr_now <= 0:
        return None

    # Search only setups whose retest could still be valid at this bar.
    first_sweep = max(original.MIN_WARMUP_BARS, k - original.SETUP_EXPIRY - 3)
    # Preserve Final B's original chronological sweep priority when multiple setups overlap.
    for i in range(first_sweep, k):
        atr_sweep = x.at[i, "atr"]
        if not np.isfinite(atr_sweep) or atr_sweep <= 0:
            continue
        prev_hi = x["high"].iloc[max(0, i-10):i].max()
        prev_lo = x["low"].iloc[max(0, i-10):i].min()
        long_sweep = x.at[i, "low"] < prev_lo and x.at[i, "close"] > prev_lo
        short_sweep = x.at[i, "high"] > prev_hi and x.at[i, "close"] < prev_hi
        if not (long_sweep or short_sweep):
            continue
        side = "LONG" if long_sweep else "SHORT"

        for j in range(i+1, min(i+4, k)):
            atr_j = x.at[j, "atr"]
            body = abs(x.at[j, "close"] - x.at[j, "open"])
            if not np.isfinite(atr_j) or atr_j <= 0 or body < original.DISPLACEMENT_ATR * atr_j:
                continue
            if side == "LONG":
                mss = x.at[j-1, "last_swing_high"]
                if not np.isfinite(mss) or x.at[j, "close"] <= mss or x.at[j, "close"] <= x.at[j, "open"]:
                    continue
                fvg = j >= 2 and x.at[j, "low"] > x.at[j-2, "high"]
                ob_idx = next((z for z in range(j-1, max(-1, j-6), -1)
                               if x.at[z, "close"] < x.at[z, "open"]), None)
                if fvg:
                    zl, zh = float(x.at[j-2, "high"]), float(x.at[j, "low"])
                elif ob_idx is not None:
                    zl, zh = float(x.at[ob_idx, "low"]), float(x.at[ob_idx, "open"])
                else:
                    continue
                sl = float(x.at[i, "low"] - original.SL_ATR_BUFFER * atr_sweep)
                retest = (k > j and k < j + 1 + original.SETUP_EXPIRY
                          and row["low"] <= zh and row["high"] >= zl
                          and row["close"] > row["open"] and row["close"] > zh)
            else:
                mss = x.at[j-1, "last_swing_low"]
                if not np.isfinite(mss) or x.at[j, "close"] >= mss or x.at[j, "close"] >= x.at[j, "open"]:
                    continue
                fvg = j >= 2 and x.at[j, "high"] < x.at[j-2, "low"]
                ob_idx = next((z for z in range(j-1, max(-1, j-6), -1)
                               if x.at[z, "close"] > x.at[z, "open"]), None)
                if fvg:
                    zl, zh = float(x.at[j, "high"]), float(x.at[j-2, "low"])
                elif ob_idx is not None:
                    zl, zh = float(x.at[ob_idx, "open"]), float(x.at[ob_idx, "high"])
                else:
                    continue
                sl = float(x.at[i, "high"] + original.SL_ATR_BUFFER * atr_sweep)
                retest = (k > j and k < j + 1 + original.SETUP_EXPIRY
                          and row["high"] >= zl and row["low"] <= zh
                          and row["close"] < row["open"] and row["close"] < zl)
            if not retest:
                continue

            # Frozen Strategy B filters: NIFTY trend ON; stock POC ON.
            nt = str(nifty_trend or "NEUTRAL")
            if side == "LONG" and nt != "POSITIVE":
                continue
            if side == "SHORT" and nt != "NEGATIVE":
                continue
            poc = row.get("poc", np.nan)
            if not np.isfinite(poc):
                continue
            if side == "LONG" and row["close"] <= poc:
                continue
            if side == "SHORT" and row["close"] >= poc:
                continue
            if row["timestamp"].time() > original.ENTRY_CUTOFF:
                continue
            return {
                "signal_time": row["timestamp"].isoformat(),
                "symbol": "",
                "side": side,
                "signal_close": float(row["close"]),
                "stop_loss": sl,
                "target": None,  # filled from actual next-candle paper entry at 1.5R
                "zone_low": zl,
                "zone_high": zh,
                "nifty_trend": nt,
                "stock_poc": float(poc),
                "sweep_time": x.at[i, "timestamp"].isoformat(),
                "displacement_time": x.at[j, "timestamp"].isoformat(),
                "setup": "Sweep>Displacement>=1ATR>MSS>FVG_OR_OB>Retest>Confirmation",
            }
    return None


def get_latest_signal(symbol, raw_5m, nifty_5m):
    """Return a signal only for the newest completed 15m candle, or None."""
    stock = original.normalize_columns(raw_5m)
    nifty = original.normalize_columns(nifty_5m)
    if stock is None or nifty is None:
        return None
    stock15 = original.prepare_symbol(stock)
    nifty15 = original.prepare_symbol(nifty)
    nifty_trend = nifty15[["timestamp", "stock_htf_trend"]].rename(
        columns={"stock_htf_trend": "nifty_trend"}
    )
    aligned = pd.merge_asof(
        stock15.sort_values("timestamp"),
        nifty_trend.sort_values("timestamp"),
        on="timestamp", direction="backward", allow_exact_matches=True
    )
    if aligned.empty:
        return None
    # Only fully completed candles whose end timestamp is safely in the past.
    now = pd.Timestamp.now(tz=original.IST)
    aligned = aligned[aligned["timestamp"] <= now.floor("15min")]
    if aligned.empty:
        return None
    aligned = aligned.reset_index(drop=True)
    signal = _latest_setup_signal(aligned, aligned.iloc[-1].get("nifty_trend", "NEUTRAL"))
    if signal:
        signal["symbol"] = symbol
    return signal
