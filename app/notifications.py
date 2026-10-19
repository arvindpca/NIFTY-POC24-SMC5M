"""Polished Telegram notifications for virtual paper trades only."""
import html
import os

import requests


def _h(value) -> str:
    """Escape dynamic values for Telegram HTML parse mode."""
    return html.escape(str(value if value is not None else "—"), quote=False)


def _fixed(value) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _money(value) -> str:
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return "₹0.00"


def send_telegram(message: str) -> None:
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        # Telegram is optional for local syntax/config checks; the event is still logged.
        return
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=15,
    )
    response.raise_for_status()


def entry_alert(trade: dict) -> str:
    side = str(trade.get("side", "")).upper()
    side_icon = "🟢" if side == "LONG" else "🔴" if side == "SHORT" else "⚪"
    return (
        "━━━━━━━━━━━━━━━━━━\n"
        "📡 <b>NIFTY_SMC_POC</b>\n"
        "🧪 <b>VIRTUAL ENTRY ALERT</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{side_icon} <b>{_h(trade.get('symbol'))} · {_h(side)}</b>\n"
        f"🕒 Signal: <code>{_h(trade.get('signal_time'))}</code> IST\n"
        "──────── <b>TRADE PLAN</b> ────────\n"
        f"🎯 Virtual entry: <b>{_money(trade.get('entry_price'))}</b>\n"
        f"🛡️ Stop-loss: <b>{_money(trade.get('stop_loss'))}</b>\n"
        f"🏁 Target (1.5R): <b>{_money(trade.get('target'))}</b>\n"
        f"📦 Quantity: <b>{_h(trade.get('quantity'))}</b> · Notional: <b>{_money(trade.get('notional'))}</b>\n"
        "──────── <b>SETUP CHECK</b> ────────\n"
        f"📈 NIFTY 1H trend: {_h(trade.get('nifty_trend'))}\n"
        f"📍 Stock POC: {_h(trade.get('stock_poc'))}\n"
        f"🧩 Setup: {_h(trade.get('setup'))}\n"
        f"🆔 ID: <code>{_h(trade.get('trade_id'))}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <b>PAPER TRADING ONLY · NO REAL ORDERS</b>"
    )


def close_alert(trade: dict, stats: dict) -> str:
    result = str(trade.get("result", "")).upper()
    icon = "🟢" if result == "WIN" else "🔴" if result == "LOSS" else "⚪"
    pnl = float(trade.get("net_pnl", 0) or 0)
    return (
        "━━━━━━━━━━━━━━━━━━\n"
        "📡 <b>NIFTY_SMC_POC</b>\n"
        f"{icon} <b>VIRTUAL TRADE CLOSED · {_h(result or 'RESULT')}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 {_h(trade.get('symbol'))} · {_h(trade.get('side'))}\n"
        f"🆔 ID: <code>{_h(trade.get('trade_id'))}</code>\n"
        "──────── <b>EXIT DETAILS</b> ────────\n"
        f"🚪 Reason: {_h(trade.get('exit_reason'))}\n"
        f"💰 Exit price: <b>{_money(trade.get('exit_price'))}</b>\n"
        f"📊 Net P&amp;L: <b>{_money(pnl)}</b>\n"
        "──────── <b>SESSION STATS</b> ────────\n"
        f"🔢 Closed trades: {_h(stats.get('closed', 0))}\n"
        f"🎯 Win rate: {_fixed(stats.get('win_rate_pct', 0))}%\n"
        f"📈 Gross profit: {_money(stats.get('gross_profit'))}\n"
        f"📉 Gross loss: {_money(stats.get('gross_loss'))}\n"
        f"⚖️ Profit factor: {_fixed(stats.get('profit_factor', 0))}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🧪 <b>VIRTUAL PAPER TRADE ONLY · NO REAL ORDERS</b>"
    )
