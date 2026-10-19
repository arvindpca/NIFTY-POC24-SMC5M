"""Send a daily IST summary of Strategy B virtual paper trades."""
import csv
import html
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.notifications import send_telegram
from app.storage import DATA_DIR, STATE_FILE, TRADE_FILE, LOG_FILE

IST = ZoneInfo("Asia/Kolkata")


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def is_today(value, day):
    if not value:
        return False
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        return ts.astimezone(IST).date() == day
    except (TypeError, ValueError):
        return False


def number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def build_summary(now=None):
    now = now or datetime.now(IST)
    day = now.astimezone(IST).date()
    trades = read_csv(TRADE_FILE)
    logs = read_csv(LOG_FILE)
    closed_today = [t for t in trades if is_today(t.get("exit_time"), day)]
    entries_today = [t for t in trades if is_today(t.get("entry_time"), day)]
    # Include today's still-open virtual entries from persistent state.
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    open_positions = state.get("open_positions", {})
    open_today = [
        p for p in open_positions.values()
        if is_today(p.get("entry_time"), day)
    ]
    wins = sum(1 for t in closed_today if number(t.get("net_pnl")) > 0)
    losses = sum(1 for t in closed_today if number(t.get("net_pnl")) < 0)
    breakeven = len(closed_today) - wins - losses
    net_pnl = sum(number(t.get("net_pnl")) for t in closed_today)
    errors_today = [
        row for row in logs
        if is_today(row.get("timestamp"), day)
        and str(row.get("level", "")).strip().upper() in {"ERROR", "CRITICAL"}
    ]
    return {
        "date": day.isoformat(),
        "entries": len(entries_today) + len(open_today),
        "closed": len(closed_today),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "net_pnl": net_pnl,
        "open_positions": len(open_positions),
        "errors": errors_today,
    }


def format_summary(stats):
    error_lines = [
        f"• <b>{html.escape(str(row.get('event', 'ERROR')))}</b>: "
        f"{html.escape(str(row.get('detail', ''))[:180])}"
        for row in stats["errors"][:3]
    ]
    pnl = f"₹{stats['net_pnl']:,.2f}"
    message = (
        "━━━━━━━━━━━━━━━━━━\\n"
        "📊 <b>NIFTY_SMC_POC</b>\\n"
        "🗓️ <b>DAILY PAPER REPORT</b>\\n"
        "━━━━━━━━━━━━━━━━━━\\n"
        f"📅 Date: <code>{html.escape(str(stats['date']))}</code> IST\\n"
        "──────── <b>ACTIVITY</b> ────────\\n"
        f"🟦 Virtual entries: <b>{stats['entries']}</b>\\n"
        f"📂 Closed trades: <b>{stats['closed']}</b>\\n"
        f"🟢 Wins: <b>{stats['wins']}</b>  🔴 Losses: <b>{stats['losses']}</b>  ⚪ BE: <b>{stats['breakeven']}</b>\\n"
        "──────── <b>PERFORMANCE</b> ────────\\n"
        f"💹 Net P&amp;L today: <b>{pnl}</b>\\n"
        f"📌 Open virtual positions: <b>{stats['open_positions']}</b>\\n"
        f"🛠️ Errors: <b>{len(stats['errors'])}</b>"
    )
    if error_lines:
        message += "\\n──────── <b>ERROR DETAILS</b> ────────\\n" + "\\n".join(error_lines)
    if len(stats["errors"]) > 3:
        message += f"\\n…and {len(stats['errors']) - 3} more errors (see GitHub Actions log)."
    message += "\\n━━━━━━━━━━━━━━━━━━\\n🧪 <b>VIRTUAL TRADING ONLY · NO REAL ORDERS</b>"
    return message

def main():
    stats = build_summary()
    message = format_summary(stats)
    print(message)
    # Fail the workflow if Telegram is configured but delivery fails.
    # If not configured, send_telegram intentionally no-ops; the report remains in logs.
    send_telegram(message)


if __name__ == "__main__":
    main()
