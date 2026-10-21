"""Refresh the paper scanner's NSE equity tokens from Angel One's official instrument master.

This is for paper-only market-data scanning. It never places orders and never guesses tokens.
"""
import csv
import json
import os
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ROOT / "config" / "scan_universe.csv"
TOKEN_FILE = ROOT / "config" / "symbol_tokens.csv"
MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
NIFTY_TOKEN = "99926000"


def main():
    with UNIVERSE.open(newline="", encoding="utf-8-sig") as f:
        symbols = [r["symbol"].strip().upper() for r in csv.DictReader(f) if r.get("symbol", "").strip()]
    if not symbols or len(symbols) != len(set(symbols)):
        raise SystemExit("Universe is empty or contains duplicate symbols.")

    req = urllib.request.Request(MASTER_URL, headers={"User-Agent": "StrategyB-PaperScanner/1.0"})
    with urllib.request.urlopen(req, timeout=45) as response:
        master = json.load(response)

    by_symbol = {}
    for item in master:
        exchange_segment = str(item.get("exch_seg", "")).lower()
        tradingsymbol = str(item.get("symbol", "")).strip().upper()
        token = str(item.get("token", "")).strip()
        if exchange_segment != "nse_cm" or not token:
            continue
        if tradingsymbol.endswith("-EQ"):
            base = tradingsymbol[:-3]
            if base:
                by_symbol.setdefault(base, token)

    missing = [s for s in symbols if s not in by_symbol]
    if missing:
        raise SystemExit("No verified NSE-EQ token found for: " + ", ".join(missing))

    rows = [{"symbol": "NIFTY", "token": NIFTY_TOKEN, "exchange": "NSE"}]
    rows.extend({"symbol": s, "token": by_symbol[s], "exchange": "NSE"} for s in symbols)
    fd, tmp = tempfile.mkstemp(prefix="symbol_tokens_", suffix=".csv", dir=str(TOKEN_FILE.parent))
    os.close(fd)
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["symbol", "token", "exchange"])
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, TOKEN_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"VERIFIED_TOKEN_REFRESH_OK | index=NIFTY | stocks={len(symbols)} | total={len(rows)}")
    print("SCAN_UNIVERSE_SOURCE=config/scan_universe.csv")


if __name__ == "__main__":
    main()
