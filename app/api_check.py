"""Read-only SmartAPI health check (no orders, no market-hours gate). Writes data/api_check.txt - no secrets."""
import time
from datetime import datetime

from app import smartapi_client as sc
from app.main import load_tokens
from app.storage import DATA_DIR

IST = sc.IST


def main(count=8):
    lines = [f"API CHECK {datetime.now(IST).isoformat()}"]
    tokens = load_tokens()
    symbols = ["NIFTY"] + [s for s in tokens if s != "NIFTY"][:count - 1]
    t0 = time.monotonic()
    try:
        session = sc.create_data_client()
        lines.append(f"LOGIN OK ({time.monotonic()-t0:.1f}s)")
    except Exception as exc:
        lines.append(f"LOGIN FAILED: {str(exc)[:200]}")
        return _write(lines)
    ok = 0
    for sym in symbols:
        item = tokens.get(sym)
        if not item:
            lines.append(f"{sym}: not in token list")
            continue
        t1 = time.monotonic()
        try:
            df = sc.fetch_15m_candles(session, item["exchange"], item["token"], lookback_days=10)
            ok += 1
            lines.append(f"{sym}: OK rows={len(df)} last_bar={df['timestamp'].max()} ({time.monotonic()-t1:.1f}s)")
        except sc.SmartAPIRateLimitError as exc:
            lines.append(f"{sym}: RATE LIMITED -> {str(exc)[:160]}")
            break
        except Exception as exc:
            lines.append(f"{sym}: FAILED -> {str(exc)[:160]}")
    lines.append(f"RESULT {ok}/{len(symbols)} fetched; logins={session.login_count}; gap={sc.current_gap():.2f}s")
    try:
        session.terminateSession(__import__("os").getenv("ANGEL_CLIENT_CODE", ""))
    except Exception:
        pass
    _write(lines)


def _write(lines):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    (DATA_DIR / "api_check.txt").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
