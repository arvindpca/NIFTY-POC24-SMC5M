
import csv
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

TRADE_FILE = DATA_DIR / "trade_history.csv"

FIELDS = [
    "trade_id", "symbol", "side", "entry_time", "entry_price",
    "stop_loss", "target", "exit_time", "exit_price",
    "exit_reason", "gross_pnl", "result"
]

def save_trade(trade: dict) -> None:
    new_file = not TRADE_FILE.exists()

    with TRADE_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(trade)
