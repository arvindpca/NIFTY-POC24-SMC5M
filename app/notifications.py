
import os
import requests

def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError("Telegram credentials are not configured.")

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message},
        timeout=15,
    )
    response.raise_for_status()

def entry_alert(message: str) -> str:
    return "EDUCATION PURPOSE ONLY\n" + message

def close_alert(message: str) -> str:
    return message

