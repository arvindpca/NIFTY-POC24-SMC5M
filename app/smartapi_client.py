"""Angel One SmartAPI historical-data client. Never places orders."""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import pyotp
from SmartApi import SmartConnect

IST = ZoneInfo("Asia/Kolkata")


def create_data_client():
    required = ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError("Missing GitHub Actions secrets: " + ", ".join(missing))
    api = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
    otp = pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now()
    result = api.generateSession(os.environ["ANGEL_CLIENT_CODE"], os.environ["ANGEL_PIN"], otp)
    if not result or not result.get("status"):
        raise RuntimeError("Angel One login failed; inspect SmartAPI status without logging secrets.")
    return api


def fetch_5m_candles(api, exchange, token, lookback_days=5):
    now = datetime.now(IST)
    start = now - timedelta(days=lookback_days)
    params = {
        "exchange": str(exchange),
        "symboltoken": str(token),
        "interval": "FIVE_MINUTE",
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": now.strftime("%Y-%m-%d %H:%M"),
    }
    response = api.getCandleData(params)
    if not response or not response.get("status") or not response.get("data"):
        raise RuntimeError(f"SmartAPI returned no candle data for token {token}: {response.get('message') if response else 'empty response'}")
    df = pd.DataFrame(response["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    # SmartAPI candle timestamps are treated as start-labelled 5-minute bars.
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(IST)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(IST)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
    df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    # Drop the currently-forming 5-minute bar. A 5m bar timestamp denotes its start.
    df = df[df["timestamp"] + pd.Timedelta(minutes=5) <= pd.Timestamp.now(tz=IST)]
    if len(df) < 120:
        raise RuntimeError(f"Insufficient 5-minute history ({len(df)} bars) for warm-up and 24h POC.")
    return df.reset_index(drop=True)
