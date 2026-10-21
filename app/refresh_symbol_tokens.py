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
    exchange_counts = {}
    for item in master:
        exchange_segment = str(item.get("exch_seg", "")).strip().upper()
        exchange_counts[exchange_segment] = exchange_counts.get(exchange_segment, 0) + 1
        tradingsymbol = str(item.get("symbol", "")).strip().upper()
        token = str(item.get("token", "")).strip()
        # Angel One master variants have used NSE and nse_cm for the cash segment.
        if exchange_segment not in {"NSE", "NSE_CM"} or not token:
            continue
        if tradingsymbol.endswith("-EQ"):
            base = tradingsymbol[:-3]
            if base:
                by_symbol.setdefault(base, token)

    missing = [s for s in symbols if s not in by_symbol]
    if missing:
        print("INSTRUMENT_MASTER_EXCHANGE_SEGMENTS | " + ", ".join(
            f"{key or '<blank>'}:{value}" for key, value in sorted(exchange_counts.items())
        ))
        # Show only official-master candidates for diagnosis; never infer a token.
        for symbol in missing:
            candidates = []
            for item in master:
                seg = str(item.get("exch_seg", "")).strip().upper()
                sym = str(item.get("symbol", "")).strip().upper()
                name = str(item.get("name", "")).strip().upper()
                if seg in {"NSE", "NSE_CM"} and (
                    sym.startswith(symbol) or symbol in sym or name == symbol or symbol in name
                ):
                    candidates.append(
                        f"{sym or '<blank>'}(token={str(item.get('token', '')).strip()},name={name or '<blank>'})"
                    )
            print(f"MASTER_CANDIDATES | {symbol} | " + ("; ".join(candidates[:12]) if candidates else "none"))
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
