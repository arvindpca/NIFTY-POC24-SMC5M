"""Scheduled Strategy B paper trading. Real order APIs are intentionally unused."""
import csv
import json
import math
import os
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import validate_config
from app.notifications import send_telegram, entry_alert, close_alert
from app.smartapi_client import create_data_client, fetch_5m_candles
from app.storage import load_state, save_state, save_trade, log_event, export_excel, DATA_DIR, FIELDS
from app.strategy_adapter import get_latest_signal
from app.rules import exit_decision, is_stale_entry, target_for
import strategy_b_original as original

IST = ZoneInfo("Asia/Kolkata")
TOKEN_FILE = Path(os.getenv("SYMBOL_TOKEN_CSV", "config/symbol_tokens.csv"))
MAX_OPEN = 5
MAX_ENTRIES_PER_DAY = 5
NOTIONAL = 50_000.0
RR = 1.5
COST_BPS_PER_SIDE = float(os.getenv("PAPER_COST_BPS_PER_SIDE", "0"))


def load_tokens():
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(
            f"{TOKEN_FILE} missing. Create it from Angel One's official instrument master; "
            "required columns: symbol,token,exchange. Never guess tokens."
        )
    with TOKEN_FILE.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    required = {"symbol", "token", "exchange"}
    if not rows or not required.issubset(rows[0]):
        raise RuntimeError("symbol_tokens.csv must contain symbol,token,exchange columns and verified rows.")
    result = {}
    for row in rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        token = str(row.get("token", "")).strip()
        exchange = str(row.get("exchange", "")).strip().upper()
        if not symbol or not token or not exchange:
            continue
        if symbol in result:
            raise RuntimeError(f"Duplicate symbol in token file: {symbol}")
        result[symbol] = {"token": token, "exchange": exchange}
    if "NIFTY" not in result:
        raise RuntimeError("Verified NIFTY index token is required in symbol_tokens.csv.")
    if len(result) < 2:
        raise RuntimeError("Add verified stock token rows; only NIFTY is present.")
    return result


def stats_from_history():
    path = DATA_DIR / "trade_history.csv"
    if not path.exists():
        return {"closed": 0, "win_rate_pct": 0.0, "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": 0.0}
    df = pd.read_csv(path)
    if df.empty:
        return {"closed": 0, "win_rate_pct": 0.0, "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": 0.0}
    pnl = pd.to_numeric(df["net_pnl"], errors="coerce").fillna(0)
    wins = float(pnl[pnl > 0].sum())
    losses = float(-pnl[pnl < 0].sum())
    return {
        "closed": len(df),
        "win_rate_pct": float((pnl > 0).mean() * 100),
        "gross_profit": wins,
        "gross_loss": losses,
        "profit_factor": wins / losses if losses else (float("inf") if wins else 0.0),
    }


def completed_15m(raw):
    d = original.prepare_symbol(original.normalize_columns(raw))
    now = pd.Timestamp.now(tz=IST)
    # END-labelled 15m bars only; ensure the bar has actually ended.
    return d[d["timestamp"] <= now.floor("15min")].sort_values("timestamp").reset_index(drop=True)


def close_positions(state, latest_by_symbol):
    open_positions = state["open_positions"]
    for trade_id, pos in list(open_positions.items()):
        bars = latest_by_symbol.get(pos["symbol"])
        if bars is None or bars.empty:
            continue
        entry_ts = pd.Timestamp(pos["entry_time"])
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.tz_localize(IST)
        for _, bar in bars[bars["timestamp"] > entry_ts].iterrows():
            ts = bar["timestamp"]
            if ts.isoformat() <= pos.get("last_checked_candle", ""):
                continue
            side = pos["side"]
            sl, target = float(pos["stop_loss"]), float(pos["target"])
            decision = exit_decision(ts, bar, side, sl, target)
            if decision is None:
                pos["last_checked_candle"] = ts.isoformat()
                continue
            exit_px, reason = decision
            qty = int(pos["quantity"])
            gross = (exit_px - float(pos["entry_price"])) * qty if side == "LONG" else (float(pos["entry_price"]) - exit_px) * qty
            costs = (float(pos["entry_price"]) * qty + exit_px * qty) * COST_BPS_PER_SIDE / 10000.0
            net = gross - costs
            result = "WIN" if net > 0 else ("LOSS" if net < 0 else "BREAKEVEN")
            closed = {**pos, "exit_time": ts.isoformat(), "exit_price": exit_px, "exit_reason": reason,
                      "gross_pnl": round(gross, 2), "estimated_cost": round(costs, 2),
                      "net_pnl": round(net, 2), "result": result}
            save_trade(closed)
            del open_positions[trade_id]
            summary = stats_from_history()
            try:
                send_telegram(close_alert(closed, summary))
            except Exception as exc:
                log_event("ERROR", "TELEGRAM_EXIT_FAILED", f"{trade_id}: {exc}")
            log_event("INFO", "PAPER_EXIT", f"{trade_id} {reason} net={net:.2f}")
            break


def process_pending(state, latest_by_symbol):
    pending = state.setdefault("pending_entries", {})
    for key, signal in list(pending.items()):
        symbol = signal["symbol"]
        bars = latest_by_symbol.get(symbol)
        if bars is None or bars.empty:
            continue
        signal_ts = pd.Timestamp(signal["signal_time"])
        if signal_ts.tzinfo is None:
            signal_ts = signal_ts.tz_localize(IST)
        next_bars = bars[bars["timestamp"] > signal_ts]
        if next_bars.empty:
            continue
        entry_bar = next_bars.iloc[0]
        # Resampled candles are end-labelled; the next candle opens 15 minutes before its label.
        entry_ts = entry_bar["timestamp"] - pd.Timedelta(minutes=15)
        # Never simulate a new entry retrospectively from a stale candle.
        # GitHub-hosted schedules can be delayed; skip stale signals instead of
        # pretending the historical next-candle open was actionable in real time.
        now = pd.Timestamp.now(tz=IST)
        if is_stale_entry(entry_ts, now):
            log_event(
                "WARNING", "STALE_SIGNAL_SKIPPED",
                f"{symbol} {signal_ts.isoformat()} intended_entry={entry_ts.isoformat()} now={now.isoformat()}",
            )
            del pending[key]
            continue
        entry = float(entry_bar["open"])
        sl = float(signal["stop_loss"])
        side = signal["side"]
        if (side == "LONG" and sl >= entry) or (side == "SHORT" and sl <= entry):
            del pending[key]
            continue
        try:
            target = target_for(entry, sl, side, RR)
        except ValueError as exc:
            log_event("WARNING", "INVALID_RISK_SKIPPED", f"{symbol}: {exc}")
            del pending[key]
            continue
        qty = math.floor(NOTIONAL / entry) if entry > 0 else 0
        if qty < 1:
            del pending[key]
            continue
        open_positions = state["open_positions"]
        day = entry_ts.date().isoformat()
        daily_entries = state.setdefault("daily_entries", {})
        entries_today = int(daily_entries.get(day, 0))
        if len(open_positions) >= MAX_OPEN or entries_today >= MAX_ENTRIES_PER_DAY:
            del pending[key]
            continue
        # Do not overlap positions in the same symbol.
        if any(p["symbol"] == symbol for p in open_positions.values()):
            del pending[key]
            continue
        trade_id = f"SB-{entry_ts.strftime('%Y%m%d-%H%M')}-{symbol}-{uuid.uuid4().hex[:6].upper()}"
        trade = {
            "trade_id": trade_id, "symbol": symbol, "side": side,
            "signal_time": signal["signal_time"], "entry_time": entry_ts.isoformat(),
            "entry_price": round(entry, 4), "quantity": qty, "notional": round(entry*qty, 2),
            "stop_loss": round(sl, 4), "target": round(target, 4), "exit_time": "",
            "exit_price": "", "exit_reason": "", "gross_pnl": "", "result": "",
            "nifty_trend": signal["nifty_trend"], "stock_poc": signal["stock_poc"],
            "setup": signal["setup"], "estimated_cost": "", "net_pnl": "",
            "last_checked_candle": entry_ts.isoformat(),
        }
        open_positions[trade_id] = trade
        daily_entries[day] = entries_today + 1
        try:
            send_telegram(entry_alert(trade))
        except Exception as exc:
            log_event("ERROR", "TELEGRAM_ENTRY_FAILED", f"{trade_id}: {exc}")
        log_event("INFO", "PAPER_ENTRY", f"{trade_id} qty={qty} entry={entry}")
        del pending[key]


def main():
    validate_config()
    if os.getenv("PAPER_ONLY", "true").strip().lower() != "true":
        raise RuntimeError("Safety stop: PAPER_ONLY must be true.")
    now = datetime.now(IST)
    if now.weekday() >= 5 or now.time() < datetime.strptime("09:15", "%H:%M").time() or now.time() > datetime.strptime("15:35", "%H:%M").time():
        print(f"Outside NSE session: {now.isoformat()}; no live polling.")
        return
    tokens = load_tokens()
    api = create_data_client()
    state = load_state()
    raw_by_symbol = {}
    frames = {}
    for symbol, item in tokens.items():
        try:
            raw = fetch_5m_candles(api, item["exchange"], item["token"])
            raw_by_symbol[symbol] = raw
            frames[symbol] = completed_15m(raw)
        except Exception as exc:
            log_event("ERROR", "CANDLE_FETCH_FAILED", f"{symbol}: {exc}")
            print(f"Data unavailable for {symbol}; no new signal from this symbol.")
    if "NIFTY" not in raw_by_symbol:
        raise RuntimeError("NIFTY candles unavailable; stop safely rather than bypassing market filter.")
    nifty_raw = raw_by_symbol["NIFTY"]
    process_pending(state, frames)
    close_positions(state, frames)

    for symbol, raw in raw_by_symbol.items():
        if symbol == "NIFTY" or symbol not in frames or frames[symbol].empty:
            continue
        candle_key = frames[symbol].iloc[-1]["timestamp"].isoformat()
        if state["last_candle"].get(symbol) == candle_key:
            continue
        try:
            signal = get_latest_signal(symbol, raw, nifty_raw)
            # Mark the candle only after successful evaluation. If evaluation raises,
            # the next scheduled run must be allowed to retry the same candle.
            state["last_candle"][symbol] = candle_key
            if not signal:
                continue
            signal_key = f"{symbol}|{signal['side']}|{signal['signal_time']}"
            if signal_key in state["seen_signals"] or signal_key in state.get("pending_entries", {}):
                continue
            state.setdefault("seen_signals", []).append(signal_key)
            state["seen_signals"] = state["seen_signals"][-5000:]
            state.setdefault("pending_entries", {})[signal_key] = signal
            log_event("INFO", "PAPER_SIGNAL_QUEUED", signal_key)
        except Exception as exc:
            log_event("ERROR", "SIGNAL_EVALUATION_FAILED", f"{symbol}: {exc}")
    # Process again so a signal with an available immediate next candle is handled.
    process_pending(state, frames)
    save_state(state)
    try:
        workbook = export_excel()
        print(f"Excel history exported: {workbook}")
    except Exception as exc:
        log_event("ERROR", "EXCEL_EXPORT_FAILED", str(exc))
        print(f"Excel export failed: {exc}")
    try:
        api.terminateSession(os.getenv("ANGEL_CLIENT_CODE", ""))
    except Exception:
        pass
    print(f"Paper cycle complete {now.isoformat()} | open={len(state['open_positions'])} | pending={len(state.get('pending_entries', {}))}")


if __name__ == "__main__":
    main()
