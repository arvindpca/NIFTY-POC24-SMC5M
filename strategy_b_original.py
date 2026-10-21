"""
NSE SMC V2 | 15-minute backtest | FAST ALL-TESTS edition | local CSV cache only
======================================================
Research/backtest only. Does not place orders or download data.
V10 audit fixes: logs hybrid partial-exit components and explicitly documents 15m force-exit timing. Zero costs remain intentional for comparison only; not realistic net-performance estimates.

Core setup:
Liquidity sweep -> displacement (body >= 1 ATR) -> MSS -> FVG OR OB
-> retest -> confirmation candle close -> filters -> next candle open.

Filters:
- NIFTY 1H trend: close vs EMA20, EMA20 direction, confirmed HH/HL or LH/LL
- Optional stock 1H trend comparison
- Approximate 24h rolling POC using actual available intraday OHLCV only
- POC LONG above / SHORT below (configurable; comparison variants included)

Portfolio controls:
- no overlapping position in the same symbol
- max 5 new entries per NSE trading date
- entry cutoff 14:20 IST; force exit at/after 15:10 IST
- SL = sweep structure +/- 0.10 ATR buffer
- fixed targets 1.5R, 2R, 3R; trailing variants included
- conservative same-bar ambiguity: SL first
- fixed notional about INR 50,000 per position (whole shares), cash-only (no leverage); max concurrent positions tested at 5 and 10. Daily loss limit is disabled in this research run. These are backtest assumptions, not recommendations to trade live.

Inputs:
1) Preferred cache:
   D:\\UTILITIES\\Software Making\\5 15 Minuts-Indian Market\\ANGEL_5M_90D\\cache
2) Fallback: a cache folder named ANGEL_5M_90D\\cache near this script.
3) Optional NIFTY 50 5m/15m CSV must exist in that cache; if missing, the script
   stops rather than silently running without the agreed market filter.

Expected CSV columns are detected flexibly: timestamp/date/time, open/high/low/close,
volume. Filenames should contain the stock symbol; NIFTY file name should contain
NIFTY. 5m data is resampled to 15m and 1h using completed bars only.

Outputs are written to:
  <script folder>\\SMC_V2_15M_RESULTS\\
including manifest, trade logs, summary, daily equity, charts and run log.
"""
from __future__ import annotations
import re, sys, math, traceback, time, os, pickle
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from datetime import time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception:
    plt = None

IST = ZoneInfo("Asia/Kolkata")
SCRIPT_DIR = Path(__file__).resolve().parent
RESULT_DIR = SCRIPT_DIR / "SMC_V2_15M_RESULTS_FIXED50K_5V10_ABC_RECHECK"
RESULT_DIR.mkdir(parents=True, exist_ok=True)

# FINAL PROJECT PATHS: existing raw 5-minute CSVs only; never download.
PROJECT_ROOT = Path(r"D:\UTILITIES\Software Making\INDIAN MARKET\01 — Permanent Historical Data")
RAW_DATA_DIR = PROJECT_ROOT / "01_RAW_5M 90 DAYS"
WATCHLIST_FILE = PROJECT_ROOT / "Intraday Stock Selection Nifty 500.csv"
CACHE_CANDIDATES = [RAW_DATA_DIR]
WATCHLIST_CANDIDATES = [WATCHLIST_FILE]
STARTING_EQUITY = 1_000_000.0
RISK_PER_TRADE = 0.005
DAILY_LOSS_LIMIT = None  # DISABLED for strategy-condition accuracy testing
MAX_NEW_ENTRIES_PER_DAY = 5
MAX_CONCURRENT_POSITIONS = 5
ENTRY_CUTOFF = dtime(14, 20)
FORCE_EXIT_TIME = dtime(15, 0)  # User-selected force exit: first completed end-labelled 15m bar timestamp >= 15:00 (15:00 close).
ATR_LEN = 14
EMA_LEN = 20
PIVOT_L = 2
PIVOT_R = 2
SETUP_EXPIRY = 12            # 12 x 15m = 3 hours
DISPLACEMENT_ATR = 1.0
SL_ATR_BUFFER = 0.10
POC_LOOKBACK_BARS = 96       # informational: 24h is timestamp-based, not a fixed bar count
POC_PRICE_BIN_ATR_FRACTION = 0.10
MIN_WARMUP_BARS = 80
COST_BPS_PER_SIDE = 0.0      # configurable approximate all-in per-side cost; replace with broker-specific costs
TRAIL_ATR_MULTIPLIER = 1.5
BREAKEVEN_AT_R = 1.0
# Dedicated fixed-notional comparison: only the three requested frozen candidates.
# Tuple: (name, use_nifty_trend, use_stock_htf, use_stock_poc, use_volume, use_wick)
EXIT_VARIANTS = [
    ("FIXED_1R5", "fixed", 1.5, False, False),
]
FILTER_VARIANTS = [
    ("A_WITHOUT_NIFTY__WICK_OFF__VOLUME_AND_POC", False, False, True, True, False),
    ("B_WITH_NIFTY__WICK_OFF__STOCK_POC_ONLY", True, False, True, False, False),
    ("C_WITHOUT_NIFTY__WICK_OFF__VOLUME_ONLY", False, False, False, True, False),
]
FIXED_NOTIONAL_PER_POSITION = 50_000.0
CONCURRENT_POSITION_TESTS = (5, 10)


def clean_symbol(s):
    s = str(s).upper().strip().replace("\ufeff", "")
    s = re.sub(r"\.(CSV|TXT)$", "", s, flags=re.I)
    s = re.sub(r"(_5M.*|_15M.*|_5MIN.*|_15MIN.*|_90D.*|_1M.*|_1H.*)$", "", s, flags=re.I)
    s = s.replace("-EQ", "").replace(".NS", "")
    s = s.strip(" _-.")
    # Explicit aliases for existing raw filenames vs selected-stock symbols.
    aliases = {"ARE_M": "ARE&M", "M_MFIN": "M&MFIN"}
    return aliases.get(s, s)

def find_cache():
    # Strict policy: use only the agreed permanent raw-5M folder.
    p = RAW_DATA_DIR
    if not p.is_dir():
        raise FileNotFoundError(f"Required existing Raw 5M folder not found: {p}. No download attempted.")
    if not any(p.glob("*.csv")):
        raise FileNotFoundError(f"No CSV files in existing Raw 5M folder: {p}. No download attempted.")
    return p

def load_watchlist():
    if not WATCHLIST_FILE.is_file():
        raise FileNotFoundError(f"Required selected-stock list not found: {WATCHLIST_FILE}")
    w = pd.read_csv(WATCHLIST_FILE)
    if w.empty:
        raise RuntimeError(f"Selected-stock list is empty: {WATCHLIST_FILE}")
    candidates = []
    for col in w.columns:
        vals = w[col].dropna().astype(str).str.strip()
        if len(vals) and vals.str.contains(r"^[A-Za-z0-9&_-]+(?:-EQ)?$", regex=True).mean() > 0.5:
            candidates.append((vals, col))
    if not candidates:
        raise RuntimeError("Could not identify symbol column in selected-stock CSV. No other stock list will be substituted.")
    vals, col = max(candidates, key=lambda item: len(item[0]))
    symbols = {clean_symbol(v) for v in vals if clean_symbol(v)}
    symbols = {s for s in symbols if s not in {"SYMBOL", "SYMBOLS", "STOCK", "STOCKS", "NAME", "NSE"}}
    if not symbols:
        raise RuntimeError(f"No symbols parsed from {WATCHLIST_FILE} column {col!r}")
    return symbols

def normalize_columns(df):
    df = df.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    aliases = {
        "timestamp": ["timestamp", "datetime", "date_time", "time", "date", "ts", "candle_time", "start_time"],
        "open": ["open", "o"],
        "high": ["high", "h"],
        "low": ["low", "l"],
        "close": ["close", "c", "ltp"],
        "volume": ["volume", "vol", "v", "total_traded_volume"],
    }
    rename = {}
    for target, choices in aliases.items():
        for c in choices:
            if c in df.columns:
                rename[c] = target
                break
    df = df.rename(columns=rename)
    if "timestamp" not in df.columns:
        # Angel/cache formats sometimes store timestamp in the first column.
        first = df.columns[0]
        maybe = pd.to_datetime(df[first], errors="coerce")
        if maybe.notna().mean() > 0.8:
            df = df.rename(columns={first: "timestamp"})
    required = {"timestamp", "open", "high", "low", "close"}
    if not required.issubset(df.columns):
        return None
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(IST, ambiguous="NaT", nonexistent="shift_forward")
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(IST)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[(df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)]
    df = df[df["high"] >= df[["open", "close", "low"]].max(axis=1)]
    df = df[df["low"] <= df[["open", "close", "high"]].min(axis=1)]
    df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    return df.reset_index(drop=True)

def load_csv(path):
    try:
        raw = pd.read_csv(path)
        df = normalize_columns(raw)
        if df is not None and len(df) >= 30:
            return df
    except Exception:
        pass
    return None

def resample_ohlcv(df, rule):
    """Resample OHLCV using explicit candle-label conventions.

    Raw Angel 5-minute timestamps are treated as candle START times. Therefore
    5m -> 15m uses left-closed bins and right-edge labels (e.g. 09:15, 09:20,
    09:25 form the completed candle labelled 09:30).

    The 15-minute frame produced above is END-labelled. Therefore 15m -> 1h
    uses right-closed bins, aligned to the NSE 09:15 session anchor (first hour
    ends at 10:15). Do not use this function for other input label conventions.
    """
    x = df.set_index("timestamp").sort_index()
    if rule == "15min":
        out = x.resample("15min", origin="start_day", offset="15min",
                         label="right", closed="left").agg(
            open=("open", "first"), high=("high", "max"), low=("low", "min"),
            close=("close", "last"), volume=("volume", "sum"))
        # A 15m candle labelled 09:30 contains 09:15, 09:20, 09:25.
        min_label, max_label = 9*60+30, 15*60+30
    elif rule == "1h":
        out = x.resample("1h", origin="start_day", offset="15min",
                         label="right", closed="right").agg(
            open=("open", "first"), high=("high", "max"), low=("low", "min"),
            close=("close", "last"), volume=("volume", "sum"))
        # END-labelled 15m bars aggregate into session-anchored hourly bars.
        min_label, max_label = 10*60+15, 15*60+15
    else:
        raise ValueError(f"Unsupported resample rule: {rule!r}")
    out = out.dropna(subset=["open", "high", "low", "close"])
    mins = out.index.hour * 60 + out.index.minute
    out = out[(mins >= min_label) & (mins <= max_label)]
    out = out.reset_index()
    if out["timestamp"].dt.tz is None:
        out["timestamp"] = out["timestamp"].dt.tz_localize(IST)
    return out

def true_range(df):
    prev = df["close"].shift(1)
    return pd.concat([df["high"]-df["low"], (df["high"]-prev).abs(), (df["low"]-prev).abs()], axis=1).max(axis=1)

def add_indicators(df):
    x = df.copy()
    x["atr"] = true_range(x).ewm(alpha=1/ATR_LEN, min_periods=ATR_LEN, adjust=False).mean()
    x["ema20"] = x["close"].ewm(span=EMA_LEN, adjust=False, min_periods=EMA_LEN).mean()
    x["ema_rising"] = x["ema20"] > x["ema20"].shift(1)
    x["ema_falling"] = x["ema20"] < x["ema20"].shift(1)
    # Volume filter: confirmation candle volume must exceed 1.5x the mean of
    # the PREVIOUS 20 bars (shift prevents current-bar volume leaking into baseline).
    x["volume_sma20_prev"] = x["volume"].rolling(20, min_periods=20).mean().shift(1)
    x["volume_1_5x"] = x["volume_sma20_prev"].notna() & (x["volume"] > 1.5 * x["volume_sma20_prev"])
    # Confirmed pivots only become available after PIVOT_R bars; no look-ahead.
    highs = x["high"].to_numpy()
    lows = x["low"].to_numpy()
    x["pivot_high"] = np.nan
    x["pivot_low"] = np.nan
    ph = np.full(len(x), np.nan); pl = np.full(len(x), np.nan)
    for i in range(PIVOT_L + PIVOT_R, len(x)):
        j = i - PIVOT_R
        if j-PIVOT_L < 0: continue
        if highs[j] >= np.max(highs[j-PIVOT_L:j+PIVOT_R+1]):
            ph[i] = highs[j]
        if lows[j] <= np.min(lows[j-PIVOT_L:j+PIVOT_R+1]):
            pl[i] = lows[j]
    x["pivot_high"] = ph; x["pivot_low"] = pl
    x["last_swing_high"] = x["pivot_high"].ffill()
    x["last_swing_low"] = x["pivot_low"].ffill()
    # Rolling POC proxy: allocate each bar's actual volume to its typical price bin.
    # True volume-at-price needs tick/trade data; OHLCV bars only permit an approximation.
    tick = (x["atr"] * POC_PRICE_BIN_ATR_FRACTION).clip(lower=0.01)
    typical = (x["high"] + x["low"] + x["close"]) / 3
    x["poc"] = np.nan
    # Efficient timestamp-based 24h rolling window. Bin every price in a given
    # window using the current bar's bin width so bins are comparable.
    ts_ns = pd.DatetimeIndex(x["timestamp"]).asi8
    typ = typical.to_numpy(dtype=float)
    vol = np.maximum(x["volume"].to_numpy(dtype=float), 0.0)
    tick_values = tick.to_numpy(dtype=float)
    poc_values = np.full(len(x), np.nan, dtype=float)
    window_ns = int(pd.Timedelta(hours=24).value)
    for i in range(len(x)):
        width = tick_values[i]
        if i < 1 or not np.isfinite(width) or width <= 0:
            continue
        left = int(np.searchsorted(ts_ns, ts_ns[i] - window_ns, side="right"))
        prices = typ[left:i+1]
        volumes = vol[left:i+1]
        if len(prices) == 0 or volumes.sum() <= 0:
            continue
        bins = np.rint(prices / width).astype(np.int64)
        unique_bins, inverse = np.unique(bins, return_inverse=True)
        volume_by_bin = np.bincount(inverse, weights=volumes)
        poc_values[i] = float(unique_bins[int(np.argmax(volume_by_bin))]) * width
    x["poc"] = poc_values
    return x

def make_htf_trend(df15):
    h = resample_ohlcv(df15[["timestamp","open","high","low","close","volume"]], "1h")
    h = add_indicators(h)
    n = len(h)
    pos = np.arange(n)
    ph = h["pivot_high"].to_numpy(dtype=float)
    pl = h["pivot_low"].to_numpy(dtype=float)
    hi_idx = np.flatnonzero(np.isfinite(ph))
    lo_idx = np.flatnonzero(np.isfinite(pl))
    hi_count = np.searchsorted(hi_idx, pos, side="right")
    lo_count = np.searchsorted(lo_idx, pos, side="right")
    hi_latest = np.full(n, np.nan); hi_prev = np.full(n, np.nan)
    lo_latest = np.full(n, np.nan); lo_prev = np.full(n, np.nan)
    m = hi_count >= 1
    hi_latest[m] = ph[hi_idx[hi_count[m]-1]]
    m = hi_count >= 2
    hi_prev[m] = ph[hi_idx[hi_count[m]-2]]
    m = lo_count >= 1
    lo_latest[m] = pl[lo_idx[lo_count[m]-1]]
    m = lo_count >= 2
    lo_prev[m] = pl[lo_idx[lo_count[m]-2]]
    close = h["close"].to_numpy(dtype=float)
    ema = h["ema20"].to_numpy(dtype=float)
    rising = h["ema_rising"].fillna(False).to_numpy(dtype=bool)
    falling = h["ema_falling"].fillna(False).to_numpy(dtype=bool)
    pos_trend = (close > ema) & rising & (hi_latest > hi_prev) & (lo_latest > lo_prev)
    neg_trend = (close < ema) & falling & (hi_latest < hi_prev) & (lo_latest < lo_prev)
    trend = np.full(n, "NEUTRAL", dtype=object)
    trend[pos_trend] = "POSITIVE"
    trend[neg_trend] = "NEGATIVE"
    h["trend"] = trend
    return h[["timestamp","trend","close","ema20"]].rename(columns={"timestamp":"htf_timestamp","trend":"stock_htf_trend","close":"htf_close","ema20":"htf_ema20"})

def align_htf(df15, htf):
    left = df15.sort_values("timestamp").copy()
    right = htf.sort_values("htf_timestamp").copy()
    return pd.merge_asof(left, right, left_on="timestamp", right_on="htf_timestamp",
                         direction="backward", allow_exact_matches=True)

def add_4h_wick_zones(df15):
    """Create session-aligned 4H wick zones from completed NSE-session bars only.

    NSE regular session is 09:15-15:30 IST. A full 4-hour candle consists of
    the first 16 end-labelled 15m bars (09:30..13:15). The remaining session
    segment is partial and is deliberately excluded from 4H zone creation.
    A zone created at 13:15 is available only to later 15m bars (strictly after
    13:15), preventing the zone-forming candle from trading against itself.
    Wick zone is [wick extreme, body edge]. Long wick threshold: wick >= 1.5x
    body AND >= 30% of total candle range. These are explicit research defaults.
    """
    x = df15.copy().sort_values("timestamp").reset_index(drop=True)
    x["wick_demand_low"] = np.nan
    x["wick_demand_high"] = np.nan
    x["wick_supply_low"] = np.nan
    x["wick_supply_high"] = np.nan
    x["wick_zone_match"] = False
    if x.empty:
        return x
    x["session_date"] = x["timestamp"].dt.date
    records = []
    for day, g in x.groupby("session_date", sort=True):
        # END-labelled 15m bars in first four hours: 09:30 through 13:15.
        mins = g["timestamp"].dt.hour * 60 + g["timestamp"].dt.minute
        full = g[(mins >= 9*60+30) & (mins <= 13*60+15)]
        if len(full) != 16:
            continue  # incomplete/missing session bars: do not fabricate 4H candle
        bar = {
            "timestamp": full["timestamp"].iloc[-1],
            "open": float(full["open"].iloc[0]),
            "high": float(full["high"].max()),
            "low": float(full["low"].min()),
            "close": float(full["close"].iloc[-1]),
        }
        body = abs(bar["close"] - bar["open"])
        rng = max(bar["high"] - bar["low"], 0.0)
        upper = max(0.0, bar["high"] - max(bar["open"], bar["close"]))
        lower = max(0.0, min(bar["open"], bar["close"]) - bar["low"])
        demand = (bar["low"], min(bar["open"], bar["close"])) if (rng > 0 and lower >= 1.5 * max(body, 0.01) and lower >= 0.30 * rng) else (np.nan, np.nan)
        supply = (max(bar["open"], bar["close"]), bar["high"]) if (rng > 0 and upper >= 1.5 * max(body, 0.01) and upper >= 0.30 * rng) else (np.nan, np.nan)
        records.append((bar["timestamp"], demand[0], demand[1], supply[0], supply[1]))
    # Sequential causal mapping. Zones become active only after their 4H bar closes;
    # a close beyond the wick extreme invalidates the respective zone.
    records.sort(key=lambda r: r[0])
    ridx = 0
    demand = (np.nan, np.nan)
    supply = (np.nan, np.nan)
    for i, row in x.iterrows():
        ts = row["timestamp"]
        while ridx < len(records) and records[ridx][0] < ts:
            rec = records[ridx]
            if np.isfinite(rec[1]) and np.isfinite(rec[2]): demand = (rec[1], rec[2])
            if np.isfinite(rec[3]) and np.isfinite(rec[4]): supply = (rec[3], rec[4])
            ridx += 1
        close = float(row["close"])
        if np.isfinite(demand[0]) and close < demand[0]: demand = (np.nan, np.nan)
        if np.isfinite(supply[1]) and close > supply[1]: supply = (np.nan, np.nan)
        x.at[i, "wick_demand_low"], x.at[i, "wick_demand_high"] = demand
        x.at[i, "wick_supply_low"], x.at[i, "wick_supply_high"] = supply
        long_match = np.isfinite(demand[0]) and row["low"] <= demand[1] and row["high"] >= demand[0]
        short_match = np.isfinite(supply[0]) and row["high"] >= supply[0] and row["low"] <= supply[1]
        x.at[i, "wick_zone_match"] = bool(long_match or short_match)
    x.drop(columns=["session_date"], inplace=True)
    return x

def prepare_symbol(df5):
    d15 = resample_ohlcv(df5, "15min")
    d15 = add_indicators(d15)
    d15 = add_4h_wick_zones(d15)
    htf = make_htf_trend(d15)
    d15 = align_htf(d15, htf)
    return d15

def prepare_symbol_from_15m(df15):
    """Prepare direct Angel One 15-minute candles without resampling 5-minute bars.

    Input timestamps are treated as candle-start labels. Convert them to end labels
    so existing completed-candle checks and signal timestamps retain their meaning.
    Strategy conditions and indicator parameters are intentionally unchanged.
    """
    if df15 is None or df15.empty:
        return pd.DataFrame()
    d15 = df15.copy()
    d15["timestamp"] = pd.to_datetime(d15["timestamp"], errors="coerce")
    d15 = d15.dropna(subset=["timestamp"])
    if d15["timestamp"].dt.tz is None:
        d15["timestamp"] = d15["timestamp"].dt.tz_localize(IST, ambiguous="NaT", nonexistent="shift_forward")
    else:
        d15["timestamp"] = d15["timestamp"].dt.tz_convert(IST)
    d15["timestamp"] = d15["timestamp"] + pd.Timedelta(minutes=15)
    d15 = d15.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    d15 = add_indicators(d15)
    d15 = add_4h_wick_zones(d15)
    htf = make_htf_trend(d15)
    d15 = align_htf(d15, htf)
    return d15

def detect_signals(df):
    x = df.copy().reset_index(drop=True)
    n = len(x)
    x["signal"] = ""
    x["setup_sl"] = np.nan
    x["signal_zone_low"] = np.nan
    x["signal_zone_high"] = np.nan
    # Simple deterministic state machine: sweep bar; displacement/MSS within expiry;
    # then a valid FVG or approximate OB; retest; close-confirmation.
    for i in range(max(MIN_WARMUP_BARS, 10), n-2):
        atr = x.at[i, "atr"]
        if not np.isfinite(atr) or atr <= 0: continue
        prev_hi = x["high"].iloc[max(0,i-10):i].max()
        prev_lo = x["low"].iloc[max(0,i-10):i].min()
        sweep_long = x.at[i, "low"] < prev_lo and x.at[i, "close"] > prev_lo
        sweep_short = x.at[i, "high"] > prev_hi and x.at[i, "close"] < prev_hi
        if not (sweep_long or sweep_short): continue
        side = "LONG" if sweep_long else "SHORT"
        sweep_idx = i
        # Look for displacement + MSS in next 1..3 candles, using only data known then.
        for j in range(i+1, min(i+4, n-2)):
            body = abs(x.at[j,"close"] - x.at[j,"open"])
            if body < DISPLACEMENT_ATR * x.at[j,"atr"]: continue
            if side == "LONG":
                mss_level = x["last_swing_high"].iloc[j-1]
                if not np.isfinite(mss_level) or x.at[j,"close"] <= mss_level or x.at[j,"close"] <= x.at[j,"open"]: continue
                # FVG if low[j] > high[j-2]; OB fallback: last bearish candle in last 5 bars
                fvg = j >= 2 and x.at[j,"low"] > x.at[j-2,"high"]
                ob_idx = next((k for k in range(j-1,max(-1,j-6),-1) if x.at[k,"close"] < x.at[k,"open"]), None)
                if fvg:
                    zl, zh = x.at[j-2,"high"], x.at[j,"low"]
                elif ob_idx is not None:
                    zl, zh = x.at[ob_idx,"low"], x.at[ob_idx,"open"]
                else: continue
                sl = x.at[sweep_idx,"low"] - SL_ATR_BUFFER*x.at[sweep_idx,"atr"]
                # Retest and bullish confirmation within this setup only.
                found_this_setup = False
                for k in range(j+1, min(j+1+SETUP_EXPIRY, n-1)):
                    if x.at[k,"low"] <= zh and x.at[k,"high"] >= zl and x.at[k,"close"] > x.at[k,"open"] and x.at[k,"close"] > zh:
                        if x.at[k, "signal"] == "":
                            x.at[k,"signal"]="LONG"; x.at[k,"setup_sl"]=sl
                            x.at[k,"signal_zone_low"]=zl; x.at[k,"signal_zone_high"]=zh
                        found_this_setup = True
                        break
                if found_this_setup:
                    break
                # No valid retest: try the next displacement/MSS candidate.
                continue
            else:
                mss_level = x["last_swing_low"].iloc[j-1]
                if not np.isfinite(mss_level) or x.at[j,"close"] >= mss_level or x.at[j,"close"] >= x.at[j,"open"]: continue
                fvg = j >= 2 and x.at[j,"high"] < x.at[j-2,"low"]
                ob_idx = next((k for k in range(j-1,max(-1,j-6),-1) if x.at[k,"close"] > x.at[k,"open"]), None)
                if fvg:
                    zl, zh = x.at[j,"high"], x.at[j-2,"low"]
                elif ob_idx is not None:
                    zl, zh = x.at[ob_idx,"open"], x.at[ob_idx,"high"]
                else: continue
                sl = x.at[sweep_idx,"high"] + SL_ATR_BUFFER*x.at[sweep_idx,"atr"]
                found_this_setup = False
                for k in range(j+1, min(j+1+SETUP_EXPIRY, n-1)):
                    if x.at[k,"high"] >= zl and x.at[k,"low"] <= zh and x.at[k,"close"] < x.at[k,"open"] and x.at[k,"close"] < zl:
                        if x.at[k, "signal"] == "":
                            x.at[k,"signal"]="SHORT"; x.at[k,"setup_sl"]=sl
                            x.at[k,"signal_zone_low"]=zl; x.at[k,"signal_zone_high"]=zh
                        found_this_setup = True
                        break
                if found_this_setup:
                    break
                # No valid retest: try the next displacement/MSS candidate.
                continue
    return x

def calc_exit(df, entry_i, side, entry, sl, target, mode, breakeven, hybrid):
    """Return (exit_i, final_exit_px, reason, pnl_per_share, weighted_exit_px).

    Trailing levels are calculated after the current bar and can only trigger on a
    subsequent bar, avoiding same-bar path assumptions. For hybrid exits,
    weighted_exit_px represents the volume-weighted exit price for cost estimation.
    """
    risk = abs(entry - sl)
    if risk <= 0:
        return entry_i, entry, "INVALID_RISK", 0.0, entry, np.nan, 0.0, 1.0, 0.0
    active_sl = float(sl)
    # Hybrid's first scale-out is fixed at 1R even though its variant target is None.
    if hybrid and target is None:
        target = entry + BREAKEVEN_AT_R * risk if side == "LONG" else entry - BREAKEVEN_AT_R * risk
    remaining = 1.0
    realized = 0.0
    weighted_exit_px = 0.0
    partial_done = False
    partial_exit_px = np.nan
    partial_fraction = 0.0
    partial_realized_pnl_per_share = 0.0
    for j in range(entry_i, len(df)):
        row = df.iloc[j]
        ts = row["timestamp"]
        if ts.time() >= FORCE_EXIT_TIME:
            px = float(row["close"])
            weighted_exit_px += remaining * px
            pnl = realized + remaining * ((px-entry) if side == "LONG" else (entry-px))
            return j, px, "TIME_EXIT", pnl, weighted_exit_px, partial_exit_px, partial_fraction, remaining, partial_realized_pnl_per_share
        hi, lo = float(row["high"]), float(row["low"])
        if side == "LONG":
            stop_hit = lo <= active_sl
            target_hit = target is not None and hi >= target
        else:
            stop_hit = hi >= active_sl
            target_hit = target is not None and lo <= target
        if stop_hit and target_hit:
            px = active_sl  # conservative SL-first assumption
            weighted_exit_px += remaining * px
            pnl = realized + remaining * ((px-entry) if side == "LONG" else (entry-px))
            return j, px, "SL_FIRST_AMBIGUOUS", pnl, weighted_exit_px, partial_exit_px, partial_fraction, remaining, partial_realized_pnl_per_share
        if stop_hit:
            px = active_sl
            weighted_exit_px += remaining * px
            pnl = realized + remaining * ((px-entry) if side == "LONG" else (entry-px))
            return j, px, "SL", pnl, weighted_exit_px, partial_exit_px, partial_fraction, remaining, partial_realized_pnl_per_share
        if target_hit:
            if hybrid and not partial_done:
                px = float(target)
                partial_exit_px = px
                partial_fraction = 0.5
                partial_realized_pnl_per_share = partial_fraction * ((px-entry) if side == "LONG" else (entry-px))
                realized += partial_realized_pnl_per_share
                weighted_exit_px += partial_fraction * px
                remaining = 0.5
                partial_done = True
                target = None
                active_sl = max(active_sl, entry) if side == "LONG" else min(active_sl, entry)
            else:
                px = float(target)
                weighted_exit_px += remaining * px
                pnl = realized + remaining * ((px-entry) if side == "LONG" else (entry-px))
                return j, px, "TP", pnl, weighted_exit_px, partial_exit_px, partial_fraction, remaining, partial_realized_pnl_per_share
        # Update trailing stop after evaluating this candle; new stop applies next candle.
        favorable = hi-entry if side == "LONG" else entry-lo
        if mode in ("trail", "hybrid") and favorable >= BREAKEVEN_AT_R*risk:
            atr = row["atr"] if np.isfinite(row["atr"]) and row["atr"] > 0 else risk
            if side == "LONG":
                candidate = hi - TRAIL_ATR_MULTIPLIER*atr
                active_sl = max(active_sl, entry if breakeven else active_sl, candidate)
            else:
                candidate = lo + TRAIL_ATR_MULTIPLIER*atr
                active_sl = min(active_sl, entry if breakeven else active_sl, candidate)
    row = df.iloc[-1]
    px = float(row["close"])
    weighted_exit_px += remaining * px
    pnl = realized + remaining * ((px-entry) if side == "LONG" else (entry-px))
    return len(df)-1, px, "DATA_END_EXIT", pnl, weighted_exit_px, partial_exit_px, partial_fraction, remaining, partial_realized_pnl_per_share

def backtest_variant(symbol_frames, variant, filter_variant):
    exit_name, mode, rr, be, hybrid = variant
    filter_name, use_nifty, use_stock_htf, use_poc, use_volume, use_wick = filter_variant
    name = f"{filter_name}__{exit_name}__MAXPOS_{MAX_CONCURRENT_POSITIONS}"
    all_trades = []
    equity = STARTING_EQUITY
    daily_count = {}
    daily_start_equity = {}
    open_symbols = set()
    # Global chronological signal queue; at most 5 new positions per date across all symbols.
    signals=[]
    # FAST PATH: scan only actual signal rows, not every 15m candle on every variant.
    # This preserves the same signal timestamps/order while avoiding millions of pandas iloc checks.
    for sym, df in symbol_frames.items():
        sig_col = df["signal"].to_numpy(dtype=object)
        ts_col = df["timestamp"].to_numpy()
        idxs = np.flatnonzero((sig_col == "LONG") | (sig_col == "SHORT"))
        if len(idxs) == 0:
            continue
        for i in idxs:
            if i >= len(df)-2:
                continue
            ts = df.iloc[i]["timestamp"]
            if ts.time() > ENTRY_CUTOFF:
                continue
            signals.append((ts, sym, int(i), str(sig_col[i])))
    signals.sort(key=lambda z:z[0])
    # Trades are simulated serially in timestamp order. If a symbol has an open
    # trade at signal time it is skipped; daily limit counts actual new entries.
    open_until = {}
    pending = []  # (exit_timestamp, net_pnl, exit_date, entry_notional)
    capital_in_use = 0.0  # no leverage: open position notionals cannot exceed equity
    realized_daily_pnl = {}
    for ts, sym, i, side in signals:
        # Release only trades that would already have closed by this signal timestamp.
        still_pending = []
        for exit_ts_pending, pnl_pending, exit_day_pending, notional_pending in pending:
            if exit_ts_pending <= ts:
                equity += pnl_pending
                capital_in_use = max(0.0, capital_in_use - notional_pending)
                realized_daily_pnl[exit_day_pending] = realized_daily_pnl.get(exit_day_pending, 0.0) + pnl_pending
            else:
                still_pending.append((exit_ts_pending, pnl_pending, exit_day_pending, notional_pending))
        pending = still_pending
        if len(pending) >= MAX_CONCURRENT_POSITIONS:
            continue
        df=symbol_frames[sym]
        day=ts.date()
        daily_start_equity.setdefault(day,equity)
        if daily_count.get(day,0) >= MAX_NEW_ENTRIES_PER_DAY: continue
        # Daily loss-limit entry block intentionally disabled for this research run.
        if sym in open_until and ts <= open_until[sym]: continue
        row=df.iloc[i]
        # Optional 4H wick-zone gate; ON/OFF pairs differ only by this gate.
        if use_wick and not bool(row.get("wick_zone_match", False)):
            continue
        # Match direction to its relevant wick zone, not merely either zone.
        if use_wick and side == "LONG":
            if not (np.isfinite(row.get("wick_demand_low", np.nan)) and
                    row["low"] <= row.get("wick_demand_high", np.nan) and
                    row["high"] >= row.get("wick_demand_low", np.nan)):
                continue
        elif use_wick:
            if not (np.isfinite(row.get("wick_supply_low", np.nan)) and
                    row["high"] >= row.get("wick_supply_low", np.nan) and
                    row["low"] <= row.get("wick_supply_high", np.nan)):
                continue
        nt=row.get("nifty_trend","NEUTRAL")
        st=row.get("stock_htf_trend","NEUTRAL")
        poc=row.get("poc",np.nan)
        if use_nifty and ((side=="LONG" and nt!="POSITIVE") or (side=="SHORT" and nt!="NEGATIVE")): continue
        if use_stock_htf and ((side=="LONG" and st!="POSITIVE") or (side=="SHORT" and st!="NEGATIVE")): continue
        if use_poc and (not np.isfinite(poc) or (side=="LONG" and row["close"] <= poc) or (side=="SHORT" and row["close"] >= poc)): continue
        if use_volume and not bool(row.get("volume_1_5x", False)): continue
        entry_i=i+1
        if entry_i>=len(df): continue
        entry=float(df.iloc[entry_i]["open"])
        sl=float(row["setup_sl"])
        # Prevent stop on wrong side of actual fill; skip invalid risk.
        if (side=="LONG" and sl>=entry) or (side=="SHORT" and sl<=entry): continue
        risk=abs(entry-sl)
        target=entry + rr*risk if (rr is not None and side=="LONG") else (
            entry-rr*risk if rr is not None else None)
        # Fixed-notional comparison: target about INR 50,000 per position,
        # whole shares only, cash-only, and skip if the full position cannot be funded.
        available_capital=max(0.0, equity-capital_in_use)
        qty=math.floor(FIXED_NOTIONAL_PER_POSITION/entry) if entry > 0 else 0
        requested_notional=entry*qty
        if qty < 1 or requested_notional <= 0 or available_capital < requested_notional:
            continue
        (exit_i, exit_px, reason, pnl_per_share, weighted_exit_px,
         partial_exit_px, partial_fraction, remaining_fraction,
         partial_realized_pnl_per_share) = calc_exit(df,entry_i,side,entry,sl,target,mode,be,hybrid)
        partial_qty = qty * partial_fraction
        remaining_qty = qty * remaining_fraction
        partial_realized_pnl = qty * partial_realized_pnl_per_share
        qty_pnl=qty*pnl_per_share
        cost=(entry*qty + weighted_exit_px*qty)*COST_BPS_PER_SIDE/10000
        net=qty_pnl-cost
        daily_count[day]=daily_count.get(day,0)+1
        exit_ts=df.iloc[exit_i]["timestamp"]
        open_until[sym]=exit_ts
        entry_notional=entry*qty
        capital_in_use += entry_notional
        pending.append((exit_ts, net, exit_ts.date(), entry_notional))
        all_trades.append({
            "variant":name,"symbol":sym,"side":side,"signal_time":ts,"entry_time":df.iloc[entry_i]["timestamp"],
            "entry":entry,"initial_sl":sl,"target":target if target is not None else np.nan,
            "exit_time":exit_ts,"exit":exit_px,"exit_reason":reason,"quantity":qty,
            "risk_per_share":risk,"entry_notional":entry_notional,"available_capital_before_entry":available_capital,
            "gross_pnl":qty_pnl,"estimated_cost":cost,"net_pnl":net,
            "partial_exit_price":partial_exit_px,
            "partial_exit_fraction":partial_fraction,
            "partial_exit_quantity":partial_qty,
            "partial_realized_gross_pnl":partial_realized_pnl,
            "remaining_exit_fraction":remaining_fraction,
            "remaining_exit_quantity":remaining_qty,
            "weighted_exit_price_for_cost":weighted_exit_px,
            "force_exit_policy":"first END-labelled 15m bar timestamp >= 15:00; 15:00 close before intrabar SL/TP checks",
            "cost_model":"ZERO_COST_BASELINE_NOT_REALISTIC",
            "equity_at_entry":equity,"nifty_trend":nt,"stock_htf_trend":st,"poc":poc,
            "wick_demand_low":row.get("wick_demand_low", np.nan),"wick_demand_high":row.get("wick_demand_high", np.nan),
            "wick_supply_low":row.get("wick_supply_low", np.nan),"wick_supply_high":row.get("wick_supply_high", np.nan),
            "daily_entry_number":daily_count[day]
        })
    # Realize remaining trades after the final signal; this does not affect earlier decisions.
    for exit_ts_pending, pnl_pending, exit_day_pending, notional_pending in sorted(pending, key=lambda z: z[0]):
        equity += pnl_pending
        realized_daily_pnl[exit_day_pending] = realized_daily_pnl.get(exit_day_pending, 0.0) + pnl_pending
    trades=pd.DataFrame(all_trades)
    return trades

def summarize(trades, variant_name):
    if trades.empty:
        return {"variant":variant_name,"trades":0,"win_rate_pct":np.nan,"gross_pnl":0.0,"net_pnl":0.0,
                "profit_factor":np.nan,"max_drawdown_pct":np.nan,"avg_net_per_trade":np.nan}
    trades = trades.sort_values("exit_time").reset_index(drop=True)
    wins=trades.loc[trades.net_pnl>0,"net_pnl"].sum()
    losses=-trades.loc[trades.net_pnl<0,"net_pnl"].sum()
    eq = pd.Series([STARTING_EQUITY] + (STARTING_EQUITY + trades["net_pnl"].cumsum()).tolist(), dtype=float)
    peak=eq.cummax()
    dd=((eq-peak)/peak*100).min() if len(eq) else 0
    return {"variant":variant_name,"trades":len(trades),"win_rate_pct":(trades.net_pnl>0).mean()*100,
            "gross_pnl":trades.gross_pnl.sum(),"estimated_cost":trades.estimated_cost.sum(),
            "net_pnl":trades.net_pnl.sum(),"profit_factor":wins/losses if losses>0 else np.inf,
            "max_drawdown_pct":dd,"avg_net_per_trade":trades.net_pnl.mean(),
            "long_trades":int((trades.side=="LONG").sum()),"short_trades":int((trades.side=="SHORT").sum())}

FAST_CACHE_DIR = SCRIPT_DIR / "SMC_V2_15M_RESULTS_4H_WICK_SMC_V7_NIFTY_TREND_AB" / "_FAST_PREP_CACHE"  # reuse compatible V7 prep cache to avoid repeating indicators/signals
FAST_CACHE_VERSION = "SMC_V2_4H_WICK_SMC_V10_AUDIT_FIXED_SESSION_ALIGNED_NO_LOOKAHEAD"
# Corrected candle alignment changes the backtest inputs. Do not resume old result CSVs.
# This version writes new outputs in a separate result folder (see RESULT_DIR override below).
RESUME_AFTER_DEFAULT = "2099-01-01 00:00:00"  # force fresh comparison outputs; never reuse old variants

def _prepare_worker(payload):
    sym, df5, cache_file, raw_mtime, need_signals = payload
    try:
        if cache_file.exists():
            try:
                with cache_file.open("rb") as f:
                    item = pickle.load(f)
                if (item.get("version") == FAST_CACHE_VERSION and
                    item.get("raw_mtime") == raw_mtime and
                    item.get("need_signals") == need_signals):
                    return sym, item["data"], "CACHE", ""
            except Exception:
                pass
        d = prepare_symbol(df5)
        if need_signals:
            d = detect_signals(d)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        with tmp.open("wb") as f:
            pickle.dump({"version": FAST_CACHE_VERSION, "raw_mtime": raw_mtime,
                         "need_signals": need_signals, "data": d}, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, cache_file)
        return sym, d, "PREPARED", ""
    except Exception as e:
        return sym, None, "ERROR", repr(e)

def _resume_cutoff_timestamp():
    value = os.environ.get("SMC_RESUME_AFTER", RESUME_AFTER_DEFAULT)
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        raise ValueError("SMC_RESUME_AFTER format must be YYYY-MM-DD HH:MM:SS")

def _is_current_completed_variant(name, cutoff):
    trade_file = RESULT_DIR / f"TRADES_{name}.csv"
    daily_file = RESULT_DIR / f"DAILY_EQUITY_{name}.csv"
    return (trade_file.is_file() and daily_file.is_file() and
            trade_file.stat().st_mtime >= cutoff and daily_file.stat().st_mtime >= cutoff)

def main():
    global MAX_CONCURRENT_POSITIONS, MAX_NEW_ENTRIES_PER_DAY
    started = time.time()
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    FAST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log = []
    cache = find_cache()
    csvs = sorted(cache.glob("*.csv"))
    selected_symbols = load_watchlist()
    manifest = []
    raw_frames = {}
    nifty_candidates = []
    print(f"FAST RESUME: existing CSVs only; found {len(csvs)} files.", flush=True)
    print(f"Watchlist symbols: {len(selected_symbols)}. No downloads will be attempted.", flush=True)
    for file_i, p in enumerate(csvs, 1):
        if file_i == 1 or file_i % 25 == 0:
            print(f"CSV scan {file_i}/{len(csvs)}: {p.name}", flush=True)
        df = load_csv(p)
        if df is None:
            manifest.append({"file":p.name,"symbol":clean_symbol(p.stem),"status":"INVALID_SCHEMA_OR_TOO_FEW_ROWS","rows":0})
            continue
        sym = clean_symbol(p.stem)
        is_nifty = (sym in {"NIFTY50", "NIFTY_50", "NIFTY50INDEX", "NIFTY_50_INDEX"} or
                    ("NIFTY" in sym and "50" in sym and "BANK" not in sym and "NEXT" not in sym))
        manifest.append({"file":p.name,"symbol":sym,"status":"OK","rows":len(df),
                         "start":df.timestamp.min(),"end":df.timestamp.max(),
                         "unique_timestamps":df.timestamp.nunique(),
                         "selected_watchlist_symbol":sym in selected_symbols})
        if is_nifty:
            nifty_candidates.append((p, df, sym))
        elif sym in selected_symbols:
            if sym in raw_frames:
                raise RuntimeError(f"Multiple raw CSVs resolve to selected symbol {sym}: refusing ambiguous mapping.")
            raw_frames[sym] = (df, p)
    pd.DataFrame(manifest).to_csv(RESULT_DIR/"00_DATA_MANIFEST.csv", index=False)
    if not nifty_candidates:
        raise FileNotFoundError(f"NIFTY 50 Raw 5M CSV missing in {cache}. No download attempted.")
    if len(nifty_candidates) > 1:
        exact = [x for x in nifty_candidates if x[2] in {"NIFTY50", "NIFTY_50", "NIFTY50INDEX", "NIFTY_50_INDEX"}]
        if len(exact) != 1:
            raise RuntimeError("Multiple possible NIFTY 50 files found; cannot safely identify index CSV: " + ", ".join(x[0].name for x in nifty_candidates))
        nifty_candidates = exact
    if not raw_frames:
        raise RuntimeError("No selected watchlist stock CSVs matched the existing raw folder.")
    print(f"Matched stocks: {len(raw_frames)} / {len(selected_symbols)}; NIFTY: {nifty_candidates[0][0].name}", flush=True)

    nifty_path, nifty_raw, nifty_sym = nifty_candidates[0]
    nifty_cache = FAST_CACHE_DIR / "NIFTY50_prepared.pkl"
    nifty_payload = [("NIFTY50", nifty_raw, nifty_cache, nifty_path.stat().st_mtime, False)]
    workers = max(1, min(4, (os.cpu_count() or 2) - 1))  # FAST: up to 4 workers; reduce to 2 if RAM is limited
    print(f"V10 AUDIT-FIXED MODE: up to {workers} prep workers; reusing compatible V7 prep cache; only signal rows scanned per variant.", flush=True)
    # NIFTY preparation is a single job; run in the parent process to avoid unnecessary Windows spawn overhead.
    nf = _prepare_worker(nifty_payload[0])
    if nf[2] == "ERROR":
        raise RuntimeError(f"NIFTY preparation failed: {nf[3]}")
    nifty15 = nf[1]
    nifty_htf = nifty15[["timestamp","stock_htf_trend"]].rename(columns={"stock_htf_trend":"nifty_trend"})

    prepared = {}
    payloads = []
    for sym, (raw, path) in raw_frames.items():
        cf = FAST_CACHE_DIR / f"{re.sub(r'[^A-Z0-9&_-]', '_', sym)}_prepared.pkl"
        payloads.append((sym, raw, cf, path.stat().st_mtime, True))
    errors = []
    def accept_result(result, done):
        sym, d, status, detail = result
        if status == "ERROR" or d is None:
            errors.append(f"{sym}: PREPARE ERROR: {detail}")
        else:
            d = pd.merge_asof(d.sort_values("timestamp"), nifty_htf.sort_values("timestamp"),
                              on="timestamp", direction="backward", allow_exact_matches=True)
            d["nifty_trend"] = d["nifty_trend"].fillna("NEUTRAL")
            if len(d) >= MIN_WARMUP_BARS:
                prepared[sym] = d
        if done == 1 or done % 10 == 0 or done == len(payloads):
            print(f"Stock prep/cache: {done}/{len(payloads)} | prepared={len(prepared)} | errors={len(errors)}", flush=True)
    try:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_prepare_worker, item): item[0] for item in payloads}
            done = 0
            for fut in as_completed(futures):
                done += 1
                accept_result(fut.result(), done)
    except Exception as pool_error:
        print(f"WARNING: multiprocessing failed ({type(pool_error).__name__}: {pool_error}). Falling back to sequential processing; compatible cache will be reused.", flush=True)
        prepared.clear()
        errors.clear()
        for done, item in enumerate(payloads, 1):
            accept_result(_prepare_worker(item), done)
    log.extend(errors)
    log.append(f"Selected watchlist symbols: {len(selected_symbols)}; matched raw CSVs: {len(raw_frames)}; prepared: {len(prepared)}")
    log.append("V7 4H audit: session-anchored full candle 09:15-13:15 IST from 16 completed 15m bars; 13:15-15:30 partial segment excluded; zone activates strictly after 13:15; incomplete 16-bar windows skipped.")
    log.append("V7 wick thresholds: wick >= 1.5x body and >= 30% of 4H range. Zone is invalidated on close beyond wick extreme.")
    log.append("V10 exit audit: hybrid partial exit price/fraction/quantity/realized gross PnL and remaining position are recorded per trade. User-selected force exit uses first END-labelled 15m bar timestamp >= 15:00 (15:00 close) before checking that bar intrabar SL/TP. Costs are 0 bps/side by design and results are not realistic net-return estimates.")
    log.append("NIFTY trend A/B audit: every stock-filter combination is run WITH and WITHOUT the NIFTY 1H trend gate; disabling the gate does not remove NIFTY data preparation, only the directional entry filter.")
    unmatched = sorted(selected_symbols - set(raw_frames))
    if unmatched:
        log.append("WATCHLIST SYMBOLS WITHOUT MATCHING RAW CSV: " + ", ".join(unmatched))
    if not prepared:
        raise RuntimeError("No selected stocks prepared. Check 00_DATA_MANIFEST.csv and RUN_LOG.txt.")

    # Report 4H zone availability before trading variants.
    zone_audit = []
    for sym, d in prepared.items():
        zone_audit.append({"symbol": sym, "bars_15m": len(d), "bars_with_wick_zone": int(d["wick_zone_match"].fillna(False).sum()),
                           "demand_zone_bars": int(d["wick_demand_low"].notna().sum()), "supply_zone_bars": int(d["wick_supply_low"].notna().sum()),
                           "first_timestamp": d["timestamp"].min(), "last_timestamp": d["timestamp"].max()})
    pd.DataFrame(zone_audit).to_csv(RESULT_DIR / "00_4H_WICK_ZONE_AUDIT.csv", index=False)
    cutoff = _resume_cutoff_timestamp()
    summaries = []
    total = len(FILTER_VARIANTS) * len(EXIT_VARIANTS) * len(CONCURRENT_POSITION_TESTS)
    idx = 0
    print(f"FIXED NOTIONAL TEST: {total} runs; fixed position notional approximately INR {FIXED_NOTIONAL_PER_POSITION:,.0f}; max concurrent positions AND daily new-entry limit both set to 5 vs 10; zero-cost comparison only.", flush=True)
    for max_positions in CONCURRENT_POSITION_TESTS:
        MAX_CONCURRENT_POSITIONS = max_positions
        MAX_NEW_ENTRIES_PER_DAY = max_positions
        print(f"Portfolio limits for this batch: max concurrent={MAX_CONCURRENT_POSITIONS}; max new entries/day={MAX_NEW_ENTRIES_PER_DAY}", flush=True)
        for fv in FILTER_VARIANTS:
            for variant in EXIT_VARIANTS:
                idx += 1
                filter_name = fv[0]
                exit_name = variant[0]
                name = f"{filter_name}__{exit_name}__MAXPOS_{MAX_CONCURRENT_POSITIONS}"
                trade_file = RESULT_DIR / f"TRADES_{name}.csv"
                daily_file = RESULT_DIR / f"DAILY_EQUITY_{name}.csv"
                if _is_current_completed_variant(name, cutoff):
                    try:
                        trades = pd.read_csv(trade_file)
                    except pd.errors.EmptyDataError:
                        trades = pd.DataFrame()
                    if not trades.empty:
                        for col in ("signal_time", "entry_time", "exit_time"):
                            if col in trades.columns:
                                trades[col] = pd.to_datetime(trades[col], errors="coerce")
                    print(f"Variant {idx}/{total}: REUSING completed {name} ({len(trades)} trades)", flush=True)
                else:
                    print(f"Variant {idx}/{total}: RUNNING {name}", flush=True)
                    trades = backtest_variant(prepared, variant, fv)
                    trades.to_csv(trade_file, index=False)
                    if not trades.empty:
                        temp = trades.copy()
                        temp["exit_date"] = pd.to_datetime(temp["exit_time"]).dt.date
                        daily = temp.groupby("exit_date", as_index=False)["net_pnl"].sum()
                        daily["equity"] = STARTING_EQUITY + daily["net_pnl"].cumsum()
                        daily.to_csv(daily_file, index=False)
                    else:
                        pd.DataFrame(columns=["exit_date","net_pnl","equity"]).to_csv(daily_file,index=False)
                summary_row = summarize(trades, name)
                summary_row["filter_variant"] = filter_name
                summary_row["exit_variant"] = exit_name
                summaries.append(summary_row)
    summary = pd.DataFrame(summaries)
    summary.to_csv(RESULT_DIR/"01_VARIANT_SUMMARY.csv", index=False)
    if plt is not None:
        try:
            fig, ax = plt.subplots(figsize=(12,6)); ax.bar(summary["variant"], summary["net_pnl"])
            ax.set_title("NSE SMC V2 15M V10 — Net P&L by Variant (0 bps/side; research only)"); ax.set_ylabel("Net P&L (₹)")
            ax.tick_params(axis="x",rotation=75,labelsize=7); fig.tight_layout()
            fig.savefig(RESULT_DIR/"CHART_01_NET_PNL_BY_VARIANT.png",dpi=150); plt.close(fig)
            fig, ax = plt.subplots(figsize=(12,6)); ax.bar(summary["variant"], summary["win_rate_pct"])
            ax.set_title("Win Rate by Variant"); ax.set_ylabel("Win rate (%)")
            ax.tick_params(axis="x",rotation=75,labelsize=7); fig.tight_layout()
            fig.savefig(RESULT_DIR/"CHART_02_WIN_RATE_BY_VARIANT.png",dpi=150); plt.close(fig)
            fig, ax = plt.subplots(figsize=(12,6)); ax.bar(summary["variant"], summary["max_drawdown_pct"])
            ax.set_title("Maximum Drawdown by Variant"); ax.set_ylabel("Drawdown (%)")
            ax.tick_params(axis="x",rotation=75,labelsize=7); fig.tight_layout()
            fig.savefig(RESULT_DIR/"CHART_03_MAX_DRAWDOWN.png",dpi=150); plt.close(fig)
        except Exception as e:
            log.append(f"CHART ERROR: {e}")
    (RESULT_DIR/"RUN_LOG.txt").write_text(
        f"FIXED-50K 5-vs-10 A/B/C comparison completed.\nCache: {cache}\nSymbols prepared: {len(prepared)}\nWorkers: {workers}\nElapsed seconds: {time.time()-started:.1f}\n"
        + "\n".join(log) + "\n", encoding="utf-8")
    print("="*72)
    print("NSE SMC FIXED-NOTIONAL 5-vs-10 COMPARISON FINISHED")
    print(f"Prepared stocks: {len(prepared)}; variants: {len(summary)}; results: {RESULT_DIR}")
    print(summary.to_string(index=False))
    print("NOTE: Fixed notional about INR 50,000 per position (whole shares); max concurrent positions 5 vs 10; max 5 new entries/day retained; cash-only; zero costs are comparison-only; POC is an OHLCV approximation; force exit is the 15:00 end-labelled 15m bar close, not an exact clock-time fill.")
    print("="*72)

if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        error = f"FAILED: {exc}\n\n{traceback.format_exc()}"
        (RESULT_DIR/"RUN_ERROR.txt").write_text(error,encoding="utf-8")
        print(error)
        sys.exit(1)
