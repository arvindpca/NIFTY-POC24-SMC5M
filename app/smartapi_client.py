"""Angel One SmartAPI historical-data client. Never places orders."""
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import pyotp
from SmartApi import SmartConnect

IST = ZoneInfo("Asia/Kolkata")

# Conservative account-level pacing: some SmartAPI accounts/IPs are throttled
# well below the published endpoint ceiling. 1.25s means at most ~48 calls/min,
# reducing bursts while still allowing a 175-instrument pass in about 4 minutes.
# Keep this shared throttle for every historical candle request in this process.
MIN_CALL_GAP_SEC = float(os.getenv("SMARTAPI_MIN_CALL_GAP_SEC", "2.0"))
MAX_RETRIES = int(os.getenv("SMARTAPI_MAX_RETRIES", "0"))
BACKOFF_BASE_SEC = float(os.getenv("SMARTAPI_BACKOFF_BASE_SEC", "60.0"))
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



def fetch_yahoo_15m_candles(symbol, lookback_days=10):
    """Fallback 15m OHLCV from Yahoo chart API; never submits orders.

    Yahoo timestamps are interval-start labels. Returned timestamps are shifted
    to interval-end labels to match Strategy B's existing completed-bar contract.
    """
    clean = str(symbol).strip().upper().replace("-EQ", "")
    ticker = "^NSEI" if clean == "NIFTY" else f"{clean}.NS"
    now = int(datetime.now(IST).timestamp())
    start = now - int(min(max(lookback_days, 1), 60) * 86400)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(ticker, safe='^')}"
    response = requests.get(
        url,
        params={"period1": start, "period2": now, "interval": "15m", "events": "history"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json().get("chart", {})
    if payload.get("error"):
        raise RuntimeError(f"Yahoo chart error for {ticker}: {payload['error']}")
    results = payload.get("result") or []
    if not results:
        raise RuntimeError(f"Yahoo returned no 15m history for {ticker}")
    result = results[0]
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    if not stamps or not quote:
        raise RuntimeError(f"Yahoo returned incomplete 15m OHLCV for {ticker}")
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(stamps, unit="s", utc=True).tz_convert(IST),
        "open": quote.get("open", []),
        "high": quote.get("high", []),
        "low": quote.get("low", []),
        "close": quote.get("close", []),
        "volume": quote.get("volume", []),
    })
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
    df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    # Drop the in-progress interval, then convert start labels to end labels.
    now_ist = pd.Timestamp.now(tz=IST)
    df = df[df["timestamp"] + pd.Timedelta(minutes=15) <= now_ist].copy()
    df["timestamp"] = df["timestamp"] + pd.Timedelta(minutes=15)
    df = df[df["timestamp"] >= now_ist - pd.Timedelta(days=lookback_days)]
    if len(df) < 80:
        raise RuntimeError(f"Yahoo 15m history too short for {ticker}: {len(df)} completed bars")
    return df.reset_index(drop=True)


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
    # SmartAPI returns candle START timestamps. Remove the still-forming candle,
    # then normalize to END-labelled 15m bars because Strategy B's source logic,
    # NIFTY/stock alignment, and next-candle paper-entry model use end labels.
    df = df[df["timestamp"] + pd.Timedelta(minutes=15) <= pd.Timestamp.now(tz=IST)].copy()
    df["timestamp"] = df["timestamp"] + pd.Timedelta(minutes=15)
    if from_timestamp is None and len(df) < 80:
        raise RuntimeError(f"Insufficient completed 15-minute history ({len(df)} bars) for strategy warm-up.")
    return df.reset_index(drop=True)
