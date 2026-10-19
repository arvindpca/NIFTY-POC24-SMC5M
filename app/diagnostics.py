"""Read-only off-session SmartAPI diagnostic. Never creates or closes paper trades.

Checks every configured symbol/token by requesting a recent completed 5-minute
candle. This verifies API resolution and recent candle availability, not that a
token has been compared against Angel One's official instrument master.
"""
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
    failures = []
    passed = 0

    try:
        for index, (symbol, item) in enumerate(tokens.items(), start=1):
            try:
                candles = fetch_5m_candles(api, item["exchange"], item["token"])
                last = candles.iloc[-1]
                passed += 1
                print(
                    f"DATA OK | {index}/{len(tokens)} | {symbol} | "
                    f"rows={len(candles)} | last_completed_5m={last['timestamp']} | "
                    f"close={float(last['close']):.2f}"
                )
            except Exception as exc:
                failures.append(symbol)
                print(f"DATA FAIL | {index}/{len(tokens)} | {symbol} | {type(exc).__name__}: {exc}")
    finally:
        try:
            api.terminateSession(os.getenv("ANGEL_CLIENT_CODE", ""))
        except Exception:
            pass

    print(f"TOKEN DIAGNOSTIC SUMMARY | configured={len(tokens)} | passed={passed} | failed={len(failures)}")
    if failures:
        print("FAILED SYMBOLS | " + ", ".join(failures))
        raise SystemExit(f"Diagnostic failed for {len(failures)} symbol(s).")
    print("READ-ONLY DIAGNOSTIC PASSED. No strategy signals, virtual trades, or state writes were performed.")


if __name__ == "__main__":
    main()
