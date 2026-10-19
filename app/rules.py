"""Small deterministic rules shared by paper-trading code and unit tests."""
from datetime import time, timedelta

FORCE_EXIT_TIME = time(15, 0)
ENTRY_STALE_GRACE = timedelta(minutes=20)
RISK_REWARD = 1.5


def target_for(entry: float, stop_loss: float, side: str, rr: float = RISK_REWARD) -> float:
    side = str(side).upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError(f"Unsupported side: {side}")
    risk = abs(float(entry) - float(stop_loss))
    if risk <= 0:
        raise ValueError("Entry and stop-loss must differ")
    return float(entry) + rr * risk if side == "LONG" else float(entry) - rr * risk


def is_stale_entry(entry_time, now, grace: timedelta = ENTRY_STALE_GRACE) -> bool:
    """True when the intended next-candle open is too old to simulate honestly."""
    return entry_time < now - grace


def exit_decision(timestamp, bar, side: str, stop_loss: float, target: float):
    """Return (price, reason) for a completed bar, or None if still open.

    Strategy B rule: at/after the 15:00 end-labelled 15m candle, time exit at
    close takes priority over that candle's intrabar SL/TP checks. Before then,
    conservative SL-first handling applies if both SL and TP occur in one bar.
    """
    side = str(side).upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError(f"Unsupported side: {side}")
    if timestamp.time() >= FORCE_EXIT_TIME:
        return float(bar["close"]), "TIME_EXIT"
    if side == "LONG":
        stop_hit = float(bar["low"]) <= float(stop_loss)
        target_hit = float(bar["high"]) >= float(target)
    else:
        stop_hit = float(bar["high"]) >= float(stop_loss)
        target_hit = float(bar["low"]) <= float(target)
    if stop_hit:
        return float(stop_loss), "SL_FIRST_AMBIGUOUS" if target_hit else "SL"
    if target_hit:
        return float(target), "TP"
    return None
