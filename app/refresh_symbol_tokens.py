"""Refresh verified NSE cash-equity tokens for the paper scanner.

Only symbols with an exact official NSE -EQ instrument are scanned. Unresolved
symbols are written to a report instead of aborting the whole paper-only scan.
"""
import csv
import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ROOT / "config" / "scan_universe.csv"
TOKEN_FILE = ROOT / "config" / "symbol_tokens.csv"
REPORT_FILE = ROOT / "data" / "unresolved_symbols.csv"
MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
NIFTY_TOKEN = "99926000"
MASTER_CACHE = ROOT / "data" / "angel_instrument_master.json"
MASTER_MAX_AGE_SEC = int(os.getenv("ANGEL_MASTER_CACHE_MAX_AGE_SEC", "21600"))
NSE_SEGMENTS = {"NSE", "NSE_CM"}


def atomic_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.stem + "_", suffix=".csv", dir=str(path.parent))
    os.close(fd)
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    with UNIVERSE.open(newline="", encoding="utf-8-sig") as f:
        symbols = [r["symbol"].strip().upper() for r in csv.DictReader(f) if r.get("symbol", "").strip()]
    if not symbols or len(symbols) != len(set(symbols)):
        raise SystemExit("Universe is empty or contains duplicate symbols.")

    # Reuse cached official instrument master for up to six hours.
    master = None
    if MASTER_CACHE.exists():
        try:
            age = time.time() - MASTER_CACHE.stat().st_mtime
            if age <= MASTER_MAX_AGE_SEC:
                with MASTER_CACHE.open("r", encoding="utf-8") as f:
                    cached_master = json.load(f)
                if isinstance(cached_master, list) and cached_master:
                    master = cached_master
                    print(f"INSTRUMENT_MASTER_CACHE_HIT | age_sec={int(age)} | instruments={len(master)}")
        except (OSError, ValueError) as exc:
            print(f"INSTRUMENT_MASTER_CACHE_INVALID | {type(exc).__name__}: {exc}")
    if master is None:
        req = urllib.request.Request(MASTER_URL, headers={"User-Agent": "StrategyB-PaperScanner/1.0"})
        with urllib.request.urlopen(req, timeout=45) as response:
            master = json.load(response)
        if not isinstance(master, list) or not master:
            raise SystemExit("Official Angel One instrument master was empty/invalid.")
        MASTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="angel_master_", suffix=".json", dir=str(MASTER_CACHE.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(master, f, separators=(",", ":"))
            os.replace(tmp, MASTER_CACHE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        print(f"INSTRUMENT_MASTER_REFRESHED | instruments={len(master)}")

    by_symbol = {}
    candidates = {}
    exchange_counts = {}
    for item in master:
        segment = str(item.get("exch_seg", "")).strip().upper()
        exchange_counts[segment] = exchange_counts.get(segment, 0) + 1
        symbol = str(item.get("symbol", "")).strip().upper()
        token = str(item.get("token", "")).strip()
        name = str(item.get("name", "")).strip().upper()
        if segment not in NSE_SEGMENTS or not token:
            continue
        if symbol.endswith("-EQ"):
            by_symbol.setdefault(symbol[:-3], token)
        if symbol:
            candidates.setdefault(symbol.split("-")[0], []).append((symbol, token, name))

    valid = [s for s in symbols if s in by_symbol]
    missing = [s for s in symbols if s not in by_symbol]
    report_rows = []
    for s in missing:
        matches = candidates.get(s, [])
        exact_non_eq = [x for x in matches if x[0].startswith(s + "-")]
        if exact_non_eq:
            detail = "; ".join(f"{sym} token={tok}" for sym, tok, _ in exact_non_eq[:10])
            status = "FOUND_NON_EQ_SERIES"
        else:
            detail = "; ".join(f"{sym} token={tok}" for sym, tok, _ in matches[:10])
            status = "NO_EXACT_NSE_EQ"
        report_rows.append({"symbol": s, "status": status, "official_master_candidates": detail})
    atomic_csv(REPORT_FILE, ["symbol", "status", "official_master_candidates"], report_rows)

    rows = [{"symbol": "NIFTY", "token": NIFTY_TOKEN, "exchange": "NSE"}]
    rows.extend({"symbol": s, "token": by_symbol[s], "exchange": "NSE"} for s in valid)
    if not valid:
        raise SystemExit("No verified NSE-EQ symbols found; existing token file was not replaced.")
    atomic_csv(TOKEN_FILE, ["symbol", "token", "exchange"], rows)

    print(f"VERIFIED_TOKEN_REFRESH_OK | verified_stocks={len(valid)} | unresolved={len(missing)} | total_tokens={len(rows)}")
    print(f"UNRESOLVED_REPORT={REPORT_FILE.relative_to(ROOT).as_posix()}")
    if missing:
        print("UNRESOLVED_SYMBOLS | " + ", ".join(missing))
    print("SCAN_UNIVERSE_SOURCE=config/scan_universe.csv")


if __name__ == "__main__":
    main()
