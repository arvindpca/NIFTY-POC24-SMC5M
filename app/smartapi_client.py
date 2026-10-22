"""Angel One SmartAPI historical-data client. Data only - this module never places orders.

Reliability design (why scans used to fail with "Invalid Token" and "exceeding access rate"):

* The old scanner logged in and out on EVERY run.  Overlapping runs invalidated each other's session
  (-> bursts of "Invalid Token") and every login counts against the rate limit.  Now ONE SmartSession
  logs in once and is reused for the whole market day.  A re-login happens only when SmartAPI reports an
  invalid session, at most once per RELOGIN_MIN_INTERVAL_SEC and MAX_RELOGINS_PER_HOUR times an hour.
  An "Invalid Token" right after a fresh login is treated as a bad symbol, never as a session problem,
  so one wrong token can not start a login storm.
* Adaptive pacing: the gap between calls grows when Angel rate-limits us and shrinks back after successes.
"""
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

IST = ZoneInfo("Asia/Kolkata")

# Historical candles: documented limits are 3 requests/second and 180/minute.
MIN_CALL_GAP_SEC = float(os.getenv("SMARTAPI_MIN_CALL_GAP_SEC", "2.0"))
MAX_CALL_GAP_SEC = float(os.getenv("SMARTAPI_MAX_CALL_GAP_SEC", "4.0"))
MAX_RETRIES = int(os.getenv("SMARTAPI_MAX_RETRIES", "1"))
BACKOFF_BASE_SEC = float(os.getenv("SMARTAPI_BACKOFF_BASE_SEC", "60.0"))
RATE_LIMIT_COOLDOWN_SEC = float(os.getenv("SMARTAPI_RATE_LIMIT_COOLDOWN_SEC", "300.0"))
RELOGIN_MIN_INTERVAL_SEC = float(os.getenv("SMARTAPI_RELOGIN_MIN_INTERVAL_SEC", "120"))
MAX_RELOGINS_PER_HOUR = int(os.getenv("SMARTAPI_MAX_RELOGINS_PER_HOUR", "6"))

# Indirection so tests run instantly.
_sleep = time.sleep
_monotonic = time.monotonic

_gap = MIN_CALL_GAP_SEC
_last_call_ts = 0.0


class SmartAPIRateLimitError(RuntimeError):
    """Raised when Angel One rejects historical-data requests due to rate limits."""


def _throttle():
    global _last_call_ts
    wait = _gap - (_monotonic() - _last_call_ts)
    if wait > 0:
        _sleep(wait)
    _last_call_ts = _monotonic()


def _on_rate_limit():
    global _gap
    _gap = min(MAX_CALL_GAP_SEC, _gap * 1.5)


def _on_success():
    global _gap
    _gap = max(MIN_CALL_GAP_SEC, _gap * 0.97)


def current_gap():
    return _gap


def reset_pacing():
    global _gap, _last_call_ts
    _gap, _last_call_ts = MIN_CALL_GAP_SEC, 0.0


def _is_rate_limited(text):
    t = str(text).lower()
    return "exceeding access rate" in t or "too many requests" in t or "access denied" in t or "ab1021" in t


def _is_invalid_session(text):
    t = str(text).lower()
    return any(k in t for k in ("invalid token", "ag8001", "ag8002", "invalid session", "session expired"))


class SmartSession:
    """One logged-in SmartAPI session that re-logs in on demand, but only when that is allowed."""

    def __init__(self, connect_factory=None, otp_factory=None, clock=None):
        self._connect_factory = connect_factory
        self._otp_factory = otp_factory
        self._clock = clock or _monotonic
        self._api = None
        self._last_login = None
        self._login_times = []
        self.login_count = 0

    def _new_connection(self):
        required = ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"]
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            raise RuntimeError("Missing GitHub Actions secrets: " + ", ".join(missing))
        if self._connect_factory is not None:
            api = self._connect_factory()
        else:
            from SmartApi import SmartConnect  # lazy: keeps tests/imports working without the SDK

            api = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
        if self._otp_factory is not None:
            otp = self._otp_factory()
        else:
            import pyotp  # lazy for the same reason

            otp = pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now()
        result = api.generateSession(os.environ["ANGEL_CLIENT_CODE"], os.environ["ANGEL_PIN"], otp)
        if not result or not result.get("status"):
            raise RuntimeError("Angel One login failed; inspect SmartAPI status without logging secrets.")
        return api

    def login(self):
        self._api = self._new_connection()
        now = self._clock()
        self._last_login = now
        self._login_times = [t for t in self._login_times if now - t < 3600] + [now]
        self.login_count += 1
        print(f"SMARTAPI_LOGIN_OK | login_count={self.login_count}")
        return self

    def can_relogin(self):
        if self._last_login is None:
            return True
        now = self._clock()
        if now - self._last_login < RELOGIN_MIN_INTERVAL_SEC:
            return False
        return len([t for t in self._login_times if now - t < 3600]) < MAX_RELOGINS_PER_HOUR

    def relogin(self):
        if not self.can_relogin():
            return False
        print("SMARTAPI_RELOGIN | session was reported invalid; logging in again")
        self.login()
        return True

    def getCandleData(self, params):
        if self._api is None:
            self.login()
        return self._api.getCandleData(params)

    def terminateSession(self, client_code):
        if self._api is not None:
            try:
                self._api.terminateSession(client_code)
            except Exception:
                pass


def _get_candle_data_with_retry(api, params, token):
    last_error = "unknown error"
    attempt = 0
    relogged = False
    while True:
        _throttle()
        response, error = None, None
        try:
            response = api.getCandleData(params)
        except Exception as exc:  # SmartApi can raise while parsing a rate-limit HTML/text body
            error = str(exc)
        if error is None:
            if response and response.get("status") and response.get("data"):
                _on_success()
                return response
            error = (response.get("message") if response else None) or "empty response"
        last_error = error
        if _is_rate_limited(error):
            _on_rate_limit()
            if attempt < MAX_RETRIES:
                _sleep(BACKOFF_BASE_SEC * (2 ** attempt))
                attempt += 1
                continue
            # Stop this scan immediately: the caller must not keep hammering the remaining symbols after
            # an account/IP-level rate-limit response.
            raise SmartAPIRateLimitError(f"SmartAPI rate limit for token {token}: {last_error}")
        if _is_invalid_session(error) and not relogged and hasattr(api, "relogin"):
            relogged = True
            if api.relogin():
                continue
        break
    raise RuntimeError(f"SmartAPI returned no candle data for token {token}: {last_error}")


def create_data_client():
    """Log in ONCE and return a session object (getCandleData / terminateSession / relogin)."""
    return SmartSession().login()


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
