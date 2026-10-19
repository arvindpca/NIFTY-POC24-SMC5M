"""Restart-safe storage for paper-only positions and audit history."""
import csv
import json
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "paper_state.json"
TRADE_FILE = DATA_DIR / "trade_history.csv"
LOG_FILE = DATA_DIR / "system_log.csv"
FIELDS = [
    "trade_id", "symbol", "side", "signal_time", "entry_time", "entry_price",
    "quantity", "notional", "stop_loss", "target", "exit_time", "exit_price",
    "exit_reason", "gross_pnl", "result", "nifty_trend", "stock_poc",
    "setup", "estimated_cost", "net_pnl"
]


def load_state():
    if not STATE_FILE.exists():
        return {"open_positions": {}, "seen_signals": [], "last_candle": {}}
    with STATE_FILE.open(encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("open_positions", {})
    state.setdefault("seen_signals", [])
    state.setdefault("last_candle", {})
    return state


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def append_csv(path, fields, record):
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(record)


def save_trade(trade):
    """Append each closed virtual trade once, even after an interrupted workflow run."""
    trade_id = str(trade.get("trade_id", "")).strip()
    if trade_id and TRADE_FILE.exists():
        with TRADE_FILE.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("trade_id") == trade_id:
                    return False
    append_csv(TRADE_FILE, FIELDS, trade)
    return True


def log_event(level, event, detail=""):
    append_csv(LOG_FILE, ["timestamp", "level", "event", "detail"], {
        "timestamp": __import__("datetime").datetime.now().astimezone().isoformat(),
        "level": level, "event": event, "detail": str(detail)[:1000]
    })
