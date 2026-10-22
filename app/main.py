"""Scheduled Strategy B paper trading. Real order APIs are intentionally unused."""
import csv
import json
import math
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import validate_config
from app.notifications import send_telegram, entry_alert, close_alert
from app.smartapi_client import create_data_client, fetch_15m_candles, SmartAPIRateLimitError
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


def record_scan_diagnostic(cycle_id, symbol, stage, reason, detail=""):
    """Append an auditable per-symbol status row for the latest scan cycle."""
    path = DATA_DIR / "scan_diagnostics.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp_ist", "cycle_id", "symbol", "stage", "reason", "detail"
        ])
        if not exists:
            writer.writeheader()
        writer.writerow({
            "timestamp_ist": datetime.now(IST).isoformat(),
            "cycle_id": cycle_id,
            "symbol": symbol,
            "stage": stage,
            "reason": reason,
            "detail": str(detail).replace("\\n", " ")[:500],
        })


def completed_15m(raw):
    d = original.prepare_symbol_from_15m(original.normalize_columns(raw))
    now = pd.Timestamp.now(tz=IST)
    # END-labelled 15m bars only; ensure the bar has actually ended.
    return d[d["timestamp"] <= now.floor("15min")].sort_values("timestamp").reset_index(drop=True)


def load_or_update_candle_cache(api, symbol, item):
    """Keep direct 15m candles locally and request only data newer than the cache."""
    cache_dir = DATA_DIR / "candle_cache_15m"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{symbol}.csv"
    cached = None
    if cache_path.exists():
        try:
            cached = original.normalize_columns(pd.read_csv(cache_path))
            if cached is None or len(cached) < 80:
                cached = None
        except Exception as exc:
            log_event("WARNING", "CANDLE_CACHE_READ_FAILED", f"{symbol}: {exc}")
            cached = None

    if cached is None:
        fetched = fetch_15m_candles(api, item["exchange"], item["token"], lookback_days=10)
        mode = "INITIAL_HISTORY"
    else:
        last_ts = cached["timestamp"].max()
        fetched = fetch_15m_candles(
            api, item["exchange"], item["token"], from_timestamp=last_ts
        )
        mode = "INCREMENTAL"

    if cached is not None:
        combined = pd.concat([cached, fetched], ignore_index=True)
    else:
        combined = fetched.copy()
    combined = original.normalize_columns(combined)
    if combined is None or combined.empty:
        raise RuntimeError(f"{symbol}: merged 15M cache is empty or invalid")
    combined = combined.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    # Bound cache size while preserving ample indicator/POC warm-up history.
    cutoff = pd.Timestamp.now(tz=IST) - pd.Timedelta(days=10)
    combined = combined[combined["timestamp"] >= cutoff].reset_index(drop=True)
    if len(combined) < 80:
        raise RuntimeError(f"{symbol}: only {len(combined)} completed 15M bars after cache merge")
    tmp_path = cache_path.with_suffix(".tmp")
    combined.to_csv(tmp_path, index=False)
    tmp_path.replace(cache_path)
    return combined, mode, len(fetched)


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
    outside_session = now.weekday() >= 5 or now.time() < datetime.strptime("09:15", "%H:%M").time() or now.time() > datetime.strptime("15:35", "%H:%M").time()
    allow_offsession_scan = os.getenv("PAPER_ALLOW_OFFSESSION_SCAN", "false").strip().lower() == "true"
    if outside_session and not allow_offsession_scan:
        print(f"Outside NSE session: {now.isoformat()}; no live polling.")
        # Still permit an explicitly manual off-session run to create the
        # workbook from the current persisted CSVs. Do not authenticate or poll data.
        try:
            workbook = export_excel()
            print(f"Excel history exported (off-session, no data polling): {workbook}")
        except Exception as exc:
            log_event("ERROR", "EXCEL_EXPORT_FAILED", str(exc))
            print(f"Excel export failed: {exc}")
            raise
        return
    if outside_session and allow_offsession_scan:
        print(f"OFF-SESSION DIAGNOSTIC SCAN ENABLED | {now.isoformat()} | historical candles only; no real orders.")
    tokens = load_tokens()
    api = create_data_client()
    state = load_state()
    raw_by_symbol = {}
    frames = {}
    cycle_id = now.strftime("%Y%m%d-%H%M%S")
    diagnostic_counts = {"fetch_ok": 0, "fetch_failed": 0, "no_new_candle": 0, "no_signal": 0, "duplicate_signal": 0, "signal_queued": 0, "evaluation_failed": 0, "invalid_frame": 0}

    # Rate-limit-safe rotating scan: fetch NIFTY and positions requiring attention
    # every cycle, then a small stock batch. Fetching the whole universe each cycle
    # risks exceeding API limits and overlapping scheduled runs. The cursor persists
    # in paper_state.json so verified stocks rotate fairly across cycles.
    universe = [s for s in tokens if s != "NIFTY"]
    # Default 35 stocks per cycle: at 2s pacing this is about 70s plus response
    # latency. A 174-stock universe rotates in about 25 minutes. NIFTY and any
    # open/pending-position symbols are fetched every cycle as priorities.
    batch_size = max(1, min(int(os.getenv("PAPER_SCAN_BATCH_SIZE", "35")), 50))
    cursor = int(state.get("scan_cursor", 0) or 0) % max(1, len(universe))
    if universe:
        rotating = [universe[(cursor + i) % len(universe)] for i in range(min(batch_size, len(universe)))]
        state["scan_cursor"] = (cursor + len(rotating)) % len(universe)
    else:
        rotating = []
        state["scan_cursor"] = 0
    priority = []
    for pos in state.get("open_positions", {}).values():
        priority.append(str(pos.get("symbol", "")).upper())
    for signal in state.get("pending_entries", {}).values():
        priority.append(str(signal.get("symbol", "")).upper())
    selected = []
    for symbol in ["NIFTY", *priority, *rotating]:
        if symbol in tokens and symbol not in selected:
            selected.append(symbol)
    print(
        f"SCAN PLAN | timeframe=15M_DIRECT | universe={len(universe)} | "
        f"batch_size={batch_size} | cursor={cursor} | selected={len(selected)} | "
        f"symbols={','.join(selected)}"
    )
    pending_symbols = [(symbol, tokens[symbol]) for symbol in selected]

    rate_limit_hit = False
    scan_started = time.monotonic()
    fetched_count = 0
    cache_modes = {"INITIAL_HISTORY": 0, "INCREMENTAL": 0}
    for symbol, item in pending_symbols:
        symbol_started = time.monotonic()
        try:
            raw, cache_mode, fetched_rows = load_or_update_candle_cache(api, symbol, item)
            raw_by_symbol[symbol] = raw
            frames[symbol] = completed_15m(raw)
            fetched_count += 1
            diagnostic_counts["fetch_ok"] += 1
            record_scan_diagnostic(cycle_id, symbol, "FETCH", "OK", f"cache={cache_mode}; rows={len(raw)}; new_rows={fetched_rows}; completed_bars={len(frames[symbol])}")
            cache_modes[cache_mode] = cache_modes.get(cache_mode, 0) + 1
            print(
                f"CANDLE_FETCH_OK | timeframe=15M_DIRECT | symbol={symbol} | "
                f"cache={cache_mode} | cached_rows={len(raw)} | new_rows={fetched_rows} | "
                f"seconds={time.monotonic()-symbol_started:.1f}"
            )
        except SmartAPIRateLimitError as exc:
            diagnostic_counts["fetch_failed"] += 1
            record_scan_diagnostic(cycle_id, symbol, "FETCH", "RATE_LIMIT", exc)
            log_event("ERROR", "SMARTAPI_RATE_LIMIT_CIRCUIT_BREAKER", f"{symbol}: {exc}")
            print(f"RATE LIMIT CIRCUIT BREAKER | symbol={symbol} | stopping this scan to protect the API: {exc}")
            rate_limit_hit = True
            break
        except Exception as exc:
            diagnostic_counts["fetch_failed"] += 1
            record_scan_diagnostic(cycle_id, symbol, "FETCH", "FAILED", exc)
            log_event("ERROR", "CANDLE_FETCH_FAILED", f"{symbol}: {exc}")
            print(f"Data unavailable for {symbol}; no new signal from this symbol: {exc}")
    if "NIFTY" not in raw_by_symbol:
        save_state(state)
        try:
            export_excel()
        except Exception as exc:
            log_event("ERROR", "EXCEL_EXPORT_FAILED", str(exc))
        try:
            api.terminateSession(os.getenv("ANGEL_CLIENT_CODE", ""))
        except Exception:
            pass
        record_scan_diagnostic(
            cycle_id, "NIFTY", "CYCLE",
            "ABORTED_RATE_LIMIT" if rate_limit_hit else "ABORTED_NIFTY_DATA_UNAVAILABLE",
        )
        if rate_limit_hit:
            message = (
                "PAPER SCAN FAILED: SmartAPI rate limit blocked the required NIFTY "
                "candle fetch. No signals were evaluated. Check Angel One SmartAPI "
                "account/IP throttling; do not repeatedly rerun the workflow."
            )
            log_event("ERROR", "PAPER_SCAN_ABORTED_RATE_LIMIT", message)
            print(message)
            raise RuntimeError(message)
        print("PAPER SCAN FAILED: NIFTY data unavailable; no signals evaluated.")
        raise RuntimeError("NIFTY data unavailable; refusing to report a successful scan.")
    nifty_raw = raw_by_symbol["NIFTY"]
    process_pending(state, frames)
    close_positions(state, frames)

    for symbol, raw in raw_by_symbol.items():
        if symbol == "NIFTY":
            continue
        if symbol not in frames or frames[symbol].empty:
            diagnostic_counts["invalid_frame"] += 1
            record_scan_diagnostic(cycle_id, symbol, "STRATEGY", "NO_COMPLETED_CANDLES")
            print(f"STOCK SKIPPED | {symbol} | reason=NO_COMPLETED_CANDLES")
            continue
        candle_key = frames[symbol].iloc[-1]["timestamp"].isoformat()
        if state["last_candle"].get(symbol) == candle_key:
            diagnostic_counts["no_new_candle"] += 1
            record_scan_diagnostic(cycle_id, symbol, "STRATEGY", "SKIP_NO_NEW_CANDLE", f"latest_completed_candle={candle_key}")
            print(f"STOCK SKIPPED | {symbol} | reason=NO_NEW_CANDLE | candle={candle_key}")
            continue
        try:
            signal = get_latest_signal(symbol, raw, nifty_raw)
            # Mark the candle only after successful evaluation. If evaluation raises,
            # the next scheduled run must be allowed to retry the same candle.
            state["last_candle"][symbol] = candle_key
            if not signal:
                diagnostic_counts["no_signal"] += 1
                record_scan_diagnostic(cycle_id, symbol, "STRATEGY", "NO_SIGNAL", f"evaluated_candle={candle_key}; no setup")
                print(f"STOCK CHECKED | {symbol} | result=NO_SIGNAL | candle={candle_key}")
                continue
            signal_key = f"{symbol}|{signal['side']}|{signal['signal_time']}"
            if signal_key in state["seen_signals"] or signal_key in state.get("pending_entries", {}):
                diagnostic_counts["duplicate_signal"] += 1
                record_scan_diagnostic(cycle_id, symbol, "STRATEGY", "DUPLICATE_SIGNAL", signal_key)
                print(f"STOCK SKIPPED | {symbol} | reason=DUPLICATE_SIGNAL | key={signal_key}")
                continue
            state.setdefault("seen_signals", []).append(signal_key)
            state["seen_signals"] = state["seen_signals"][-5000:]
            state.setdefault("pending_entries", {})[signal_key] = signal
            diagnostic_counts["signal_queued"] += 1
            record_scan_diagnostic(cycle_id, symbol, "STRATEGY", "SIGNAL_QUEUED", signal_key)
            print(f"SIGNAL QUEUED | {symbol} | side={signal['side']} | signal_time={signal['signal_time']}")
            log_event("INFO", "PAPER_SIGNAL_QUEUED", signal_key)
        except Exception as exc:
            diagnostic_counts["evaluation_failed"] += 1
            record_scan_diagnostic(cycle_id, symbol, "STRATEGY", "EVALUATION_FAILED", exc)
            print(f"STRATEGY ERROR | {symbol} | reason=EVALUATION_FAILED | {exc}")
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
    print(f"SCAN DIAGNOSTICS | cycle_id={cycle_id} | " + " | ".join(f"{k}={v}" for k, v in diagnostic_counts.items()))
    print(
        f"Paper cycle complete {now.isoformat()} | selected={len(pending_symbols)} | "
        f"fetched_ok={fetched_count} | cache_initial={cache_modes.get('INITIAL_HISTORY', 0)} | "
        f"cache_incremental={cache_modes.get('INCREMENTAL', 0)} | "
        f"elapsed_sec={time.monotonic()-scan_started:.1f} | "
        f"open={len(state['open_positions'])} | pending={len(state.get('pending_entries', {}))}"
    )


if __name__ == "__main__":
    main()
