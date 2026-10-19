"""Send styled sample Telegram messages without reading/writing paper-trading state."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.notifications import send_telegram, entry_alert, close_alert
from app.daily_summary import format_summary

IST = ZoneInfo("Asia/Kolkata")


def main():
    now = datetime.now(IST).isoformat(timespec="minutes")
    sample = {
        "trade_id": "DESIGN-PREVIEW-001",
        "symbol": "NIFTY",
        "side": "LONG",
        "signal_time": now,
        "entry_price": 25000.00,
        "stop_loss": 24900.00,
        "target": 25150.00,
        "quantity": 2,
        "notional": 50000.00,
        "nifty_trend": "BULLISH",
        "stock_poc": "ABOVE POC",
        "setup": "SWEEP → DISPLACEMENT → MSS → RETEST",
        "exit_time": now,
        "exit_price": 25150.00,
        "exit_reason": "TP",
        "net_pnl": 300.00,
        "result": "WIN",
    }
    stats = {
        "closed": 4,
        "win_rate_pct": 50.0,
        "gross_profit": 900.0,
        "gross_loss": 300.0,
        "profit_factor": 3.0,
    }
    summary = {
        "date": datetime.now(IST).date().isoformat(),
        "entries": 5,
        "closed": 4,
        "wins": 2,
        "losses": 2,
        "breakeven": 0,
        "net_pnl": 600.0,
        "open_positions": 1,
        "errors": [],
    }

    messages = [
        entry_alert(sample),
        close_alert(sample, stats),
        format_summary(summary),
    ]
    for index, message in enumerate(messages, start=1):
        send_telegram(message)
        print(f"Preview message {index}/3 submitted to Telegram API.")
    print("DESIGN PREVIEW COMPLETE. Sample values only; no strategy state read/written and no real orders.")


if __name__ == "__main__":
    main()
