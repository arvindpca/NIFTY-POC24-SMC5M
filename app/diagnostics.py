"""Read-only off-session SmartAPI diagnostic. Never creates or closes paper trades."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from app.smartapi_client import create_data_client, fetch_5m_candles
from app.main import load_tokens

IST = ZoneInfo("Asia/Kolkata")


def main():
    if os.getenv("PAPER_ONLY", "true").strip().lower() != "true":
        raise RuntimeError("Safety stop: PAPER_ONLY must be true.")
    print(f"Read-only diagnostic started: {datetime.now(IST).isoformat()}")
    tokens = load_tokens()
    api = create_data_client()
    print("Angel One login: SUCCESS")
    # Only NIFTY and two stock symbols; diagnostic does not modify trade state.
    chosen = ["NIFTY"] + [s for s in tokens if s != "NIFTY"][:2]
    failures = []
    for symbol in chosen:
        item = tokens[symbol]
        try:
            candles = fetch_5m_candles(api, item["exchange"], item["token"])
            last = candles.iloc[-1]
            print(
                f"DATA OK | {symbol} | rows={len(candles)} | "
                f"last_completed_5m={last['timestamp']} | close={float(last['close']):.2f}"
            )
        except Exception as exc:
            failures.append(symbol)
            print(f"DATA FAIL | {symbol} | {type(exc).__name__}: {exc}")
    if failures:
        raise SystemExit(f"Diagnostic failed for: {', '.join(failures)}")
    print("READ-ONLY DIAGNOSTIC PASSED. No strategy signals, virtual trades, or state writes were performed.")


if __name__ == "__main__":
    main()
