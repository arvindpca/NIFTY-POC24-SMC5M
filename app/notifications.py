"""Telegram notifications for paper trades only."""
import os
import requests


def send_telegram(message: str) -> None:
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        # Telegram is optional for local syntax/config checks; the event is still logged.
        return
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message},
        timeout=15,
    )
    response.raise_for_status()


def entry_alert(trade: dict) -> str:
    return (
        "EDUCATION PURPOSE ONLY\n"
        "NIFTY_SMC_POC — VIRTUAL PAPER ENTRY\n"
        f"Trade ID: {trade['trade_id']}\nSymbol: {trade['symbol']} | Side: {trade['side']}\n"
        f"Signal time (IST): {trade['signal_time']}\nEntry (virtual): {trade['entry_price']:.2f}\n"
        f"SL: {trade['stop_loss']:.2f} | TP (1.5R): {trade['target']:.2f}\n"
        f"Qty: {trade['quantity']} | Notional: ₹{trade['notional']:.2f}\n"
        f"NIFTY 1H: {trade.get('nifty_trend')} | Stock POC: {trade.get('stock_poc')}\n"
        f"Setup: {trade.get('setup')}\nOpen virtual positions limit: 5\nNO REAL ORDERS"
    )


def close_alert(trade: dict, stats: dict) -> str:
    return (
        f"NIFTY_SMC_POC — PAPER TRADE {trade.get('result', '')}\n"
        f"Trade ID: {trade.get('trade_id')} | {trade.get('symbol')} {trade.get('side')}\n"
        f"Exit reason: {trade.get('exit_reason')} | Exit: {trade.get('exit_price')}\n"
        f"Net P&L: ₹{trade.get('net_pnl', 0):.2f}\n"
        f"Closed trades: {stats.get('closed', 0)} | Win rate: {stats.get('win_rate_pct', 0):.2f}%\n"
        f"Gross profit: ₹{stats.get('gross_profit', 0):.2f} | Gross loss: ₹{stats.get('gross_loss', 0):.2f}\n"
        f"Profit factor: {stats.get('profit_factor', 0):.2f}\nVIRTUAL PAPER TRADE ONLY"
    )
