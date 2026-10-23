"""Data-quality gate for completed END-labelled 15m candles (paper scanner).

A signal is only evaluated on a frame that passes every BLOCKING check. Strategy B looks only at the newest
completed bar, so evaluating a stale or damaged frame would either miss the real retest candle or fire on an
older bar. Blocking issues are retried inside the same cycle by the caller; persistent ones are logged and the
symbol is skipped for that cycle (never traded on bad data).
"""
from datetime import time as dtime

import numpy as np
import pandas as pd

SESSION_FIRST_END = dtime(9, 30)   # first end-labelled bar of the NSE session
SESSION_LAST_END = dtime(15, 30)
PRICE_CHECK_BARS = 30              # recent bars checked for price/OHLC sanity
GAP_CHECK_BARS = 16                # setup window (sweep->retest spans up to ~15 bars) must have no holes
JUMP_WARN_PCT = 20.0
MIN_BARS = 80


def expected_last_bar(now):
    """End label of the bar that must already exist at `now`, or None outside the session."""
    now = pd.Timestamp(now)
    if now.weekday() >= 5:
        return None
    exp = now.floor("15min")
    if exp.time() < SESSION_FIRST_END:
        return None
    if exp.time() > SESSION_LAST_END:
        exp = exp.normalize() + pd.Timedelta(hours=15, minutes=30)
    return exp


def check_frame(df, now, require_volume=True):
    """Return (blocking_issues, warnings) for one symbol's completed-bar frame.

    require_volume=False for the NIFTY index, whose candles legitimately carry zero volume.
    """
    block, warn = [], []
    if df is None or len(df) == 0:
        return ["NO_BARS"], warn
    if len(df) < MIN_BARS:
        block.append(f"TOO_FEW_BARS({len(df)})")
    x = df.sort_values("timestamp")
    ts = x["timestamp"]
    if ts.duplicated().any():
        block.append("DUPLICATE_TIMESTAMPS")
    exp = expected_last_bar(now)
    if exp is not None and ts.iloc[-1] < exp:
        block.append(f"STALE_LAST_BAR(last={ts.iloc[-1].strftime('%H:%M')},expected={exp.strftime('%H:%M')})")

    tail = x.tail(PRICE_CHECK_BARS)
    cols = tail[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if not np.isfinite(cols).all() or (cols <= 0).any():
        block.append("NON_POSITIVE_OR_NAN_PRICE")
    else:
        o, h, l, c = (tail[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
        tol = 1e-6 * np.maximum(c, 1.0)
        if (h < np.maximum(o, c) - tol).any() or (l > np.minimum(o, c) + tol).any():
            block.append("OHLC_INCONSISTENT")
        prev = c[:-1]
        same_day = (tail["timestamp"].dt.date.to_numpy()[1:] == tail["timestamp"].dt.date.to_numpy()[:-1])
        with np.errstate(divide="ignore", invalid="ignore"):
            jump = np.abs(c[1:] / prev - 1.0) * 100.0
        if (same_day & (jump > JUMP_WARN_PCT)).any():
            warn.append(f"PRICE_JUMP>{JUMP_WARN_PCT:.0f}%")

    vol = pd.to_numeric(x["volume"], errors="coerce")
    if require_volume and (pd.isna(vol.iloc[-1]) or vol.iloc[-1] <= 0):
        block.append("LATEST_VOLUME_ZERO_OR_NAN")

    gtail = x.tail(GAP_CHECK_BARS)
    day = gtail["timestamp"].dt.date.to_numpy()
    diffs = gtail["timestamp"].diff().to_numpy()[1:]
    intraday_gap = (day[1:] == day[:-1]) & (diffs != np.timedelta64(15, "m"))
    if intraday_gap.any():
        block.append(f"MISSING_BARS_IN_LAST_{GAP_CHECK_BARS}")
    return block, warn


def summarize(results):
    """results: {symbol: (block, warn)} -> (blocked_dict, warn_count)."""
    blocked = {s: b for s, (b, _) in results.items() if b}
    return blocked, sum(1 for _, (_, w) in results.items() if w)
