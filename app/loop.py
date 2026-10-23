"""Long-running PAPER-ONLY scan loop: one SmartAPI login, one scan after every 15-minute candle close.

Paper trading only - no order is ever placed. Stops at the NSE close and hands off to the next trading day.
"""
import os
import subprocess
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OPEN, CLOSE = dtime(9, 15), dtime(15, 35)
CANDLE_SETTLE_SEC = float(os.getenv("CANDLE_SETTLE_SEC", "20"))
DATA_FILES = [
    "config/symbol_tokens.csv", "data/unresolved_symbols.csv", "data/paper_state.json",
    "data/trade_history.csv", "data/system_log.csv", "data/scan_diagnostics.csv",
    "data/strategy_b_paper_history.xlsx",
]


def market_open(now):
    return now.weekday() < 5 and OPEN <= now.time() <= CLOSE


def next_cycle_time(now):
    """Next 15-minute boundary (+settle delay) strictly after `now`."""
    base = now.replace(second=0, microsecond=0)
    base += timedelta(minutes=15 - base.minute % 15)
    return base + timedelta(seconds=CANDLE_SETTLE_SEC)


def persist():
    if os.getenv("PERSIST_GIT", "false").lower() != "true":
        return
    try:
        subprocess.run(["git", "add", *DATA_FILES], check=False, capture_output=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-q", "-m", "chore: persist paper scanner outputs"], check=False)
            for _ in range(3):
                subprocess.run(["git", "pull", "-q", "--rebase", "-X", "theirs", "origin", "main"], check=False)
                if subprocess.run(["git", "push", "-q", "origin", "HEAD:main"]).returncode == 0:
                    break
    except Exception as exc:  # persistence must never kill the loop
        print(f"persist failed: {exc}")


def set_output(key, value):
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            fh.write(f"{key}={value}\n")


def run(loop_minutes=None, main_fn=None, session_factory=None, now_fn=None, sleep_fn=time.sleep):
    from app import main as app_main  # imported lazily so tests can inject stand-ins
    if os.getenv("PAPER_ONLY", "true").strip().lower() != "true":
        raise RuntimeError("Safety stop: PAPER_ONLY must be true.")
    main_fn = main_fn or app_main.main
    now_fn = now_fn or (lambda: datetime.now(IST))
    loop_minutes = loop_minutes if loop_minutes is not None else float(os.getenv("LOOP_MINUTES", "340"))
    start = now_fn()
    deadline = start + timedelta(minutes=loop_minutes)
    if not market_open(start) and os.getenv("PAPER_ALLOW_OFFSESSION_SCAN", "false").lower() != "true":
        print(f"Market closed at {start.isoformat()}; loop not started.")
        main_fn(None)  # off-session path only exports the workbook, never logs in
        set_output("continue", "false")
        return 0
    if session_factory is None:
        from app.smartapi_client import create_data_client as session_factory
    try:
        session = session_factory()
    except Exception as exc:  # Angel down/locked: keep scanning on Yahoo; main() retries login per cycle
        print(f"SMARTAPI_LOGIN_FAILED at loop start, continuing without a shared session: {exc}")
        session = None
    cycles = failures = 0
    try:
        while True:
            now = now_fn()
            try:
                main_fn(session)
                failures = 0
            except Exception as exc:  # one bad cycle must not stop the day
                failures += 1
                print(f"CYCLE_FAILED #{failures}: {exc}")
            cycles += 1
            persist()
            now = now_fn()
            nxt = next_cycle_time(now)
            if not market_open(now) or not market_open(nxt):
                print(f"Market closed; loop finished after {cycles} cycles.")
                set_output("continue", "false")
                return 0
            if nxt >= deadline:
                print(f"Loop time budget used after {cycles} cycles; handing off to next run.")
                set_output("continue", "true")
                return 0
            wait = max(0.0, (nxt - now_fn()).total_seconds())
            print(f"Sleeping {wait:.0f}s until {nxt.isoformat()}")
            sleep_fn(wait)
    finally:
        try:
            if session is not None:
                session.terminateSession(os.getenv("ANGEL_CLIENT_CODE", ""))
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(run())
