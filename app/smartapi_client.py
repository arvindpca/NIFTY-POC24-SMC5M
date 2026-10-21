"""Angel One SmartAPI historical-data client. Never places orders."""
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import pyotp
from SmartApi import SmartConnect

IST = ZoneInfo("Asia/Kolkata")

# Historical candle endpoint is documented at up to 3 requests/second and
# 150 requests/minute. A 0.45s minimum gap stays below both limits (~133/min)
# while avoiding the old 12s delay that made a full-universe scan impractical.
# If this account/IP still receives a rate-limit response, the circuit breaker
# below stops the cycle rather than continuing to hammer the API.
MIN_CALL_GAP_SEC = float(os.getenv("SMARTAPI_MIN_CALL_GAP_SEC", "0.45"))
MAX_RETRIES = int(os.getenv("SMARTAPI_MAX_RETRIES", "0"))
BACKOFF_BASE_SEC = float(os.getenv("SMARTAPI_BACKOFF_BASE_SEC", "30.0"))
RATE_LIMIT_COOLDOWN_SEC = float(os.getenv("SMARTAPI_RATE_LIMIT_COOLDOWN_SEC", "300.0"))


class SmartAPIRateLimitError(RuntimeError):
    """Raised when Angel One rejects historical-data requests due to rate limits."""

_last_call_ts = 0.0


def _throttle():
    global _last_call_ts
    wait = MIN_CALL_GAP_SEC - (time.monotonic() - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.monotonic()


def _is_rate_limited(text):
    t = str(text).lower()
    return "exceeding access rate" in t or "too many requests" in t or "access denied" in t or "ab1021" in t


def _get_candle_data_with_retry(api, params, token):
    last_error = "unknown error"
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        try:
            response = api.getCandleData(params)
        except Exception as exc:  # SmartApi can raise while parsing a rate-limit HTML/text body
            last_error = str(exc)
            if _is_rate_limited(last_error):
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_BASE_SEC * (2 ** attempt))
                    continue
                # Stop this scan immediately. The caller must not continue hammering
                # the remaining symbols after an account/IP-level rate-limit response.
                raise SmartAPIRateLimitError(f"SmartAPI rate limit for token {token}: {last_error}") from exc
            raise
        if response and response.get("status") and response.get("data"):
            return response
        last_error = response.get("message") if response else "empty response"
        if _is_rate_limited(last_error):
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE_SEC * (2 ** attempt))
                continue
            raise SmartAPIRateLimitError(f"SmartAPI rate limit for token {token}: {last_error}")
        break
    raise RuntimeError(f"SmartAPI returned no candle data for token {token}: {last_error}")


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
    response = _get_candle_data_with_retry(api, params, token)
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


def fetch_15m_candles(api, exchange, token, lookback_days=10, from_timestamp=None):
    """Fetch direct 15-minute candles. from_timestamp enables incremental cache updates."""
    now = datetime.now(IST)
    if from_timestamp is None:
        start = now - timedelta(days=lookback_days)
    else:
        start = pd.Timestamp(from_timestamp)
        if start.tzinfo is None:
            start = start.tz_localize(IST)
        else:
            start = start.tz_convert(IST)
        # Re-fetch one candle of overlap to repair boundary/gap issues.
        start = start - pd.Timedelta(minutes=30)
    params = {
        "exchange": str(exchange),
        "symboltoken": str(token),
        "interval": "FIFTEEN_MINUTE",
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": now.strftime("%Y-%m-%d %H:%M"),
    }
    response = _get_candle_data_with_retry(api, params, token)
    df = pd.DataFrame(response["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(IST)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(IST)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
    df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    # Angel One timestamps are treated as candle-start labels; discard forming bars.
    df = df[df["timestamp"] + pd.Timedelta(minutes=15) <= pd.Timestamp.now(tz=IST)]
    if from_timestamp is None and len(df) < 80:
        raise RuntimeError(f"Insufficient completed 15-minute history ({len(df)} bars) for strategy warm-up.")
    return df.reset_index(drop=True)
