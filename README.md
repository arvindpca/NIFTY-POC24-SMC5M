# Strategy B Paper Trading — Angel One SmartAPI

This repository is **paper-trading only**. It does not place orders. `strategy_b_original.py` remains the Final B source of truth, with the requested force-exit time changed to 15:00 IST.

## Current implementation

- Reads 5-minute historical candles from Angel One SmartAPI.
- Resamples using the original source's 15-minute and 1-hour candle logic.
- Uses the Strategy B core setup: liquidity sweep → displacement body >= 1 ATR → MSS → FVG OR OB → retest and confirmation.
- Active filters: NIFTY 1H trend and approximate rolling 24-hour stock POC.
- Paper entry model: next 15-minute candle open, fixed target 1.5R, SL at sweep extreme +/- 0.10 ATR.
- Entry cutoff 14:20 IST; user-selected force exit is the completed end-labelled 15-minute candle at 15:00 IST (exit at that candle's close, before same-bar SL/TP checks).
- Maximum 5 concurrent positions, no same-symbol overlap, maximum 5 entries per day.
- One login per day: `app.loop` scans after every 15-minute candle (Actions job runs the whole NSE session), re-logging in only if Angel reports an invalid session.
- Fast scan: the whole universe is scanned EVERY cycle (parallel Yahoo 15m download, `PAPER_UNIVERSE_SOURCE=yahoo`); SmartAPI is used only for NIFTY and open/pending symbols. Set `PAPER_UNIVERSE_SOURCE=angel` for the old rotating SmartAPI batches. NIFTY trend is computed once per cycle. If Angel login fails, the scan continues on Yahoo.
- Data-quality gate (`app/data_quality.py`): before any signal, every frame must be fresh (latest completed bar present), have no missing bars in the setup window, consistent OHLC, positive prices, no duplicate timestamps and non-zero latest volume (NIFTY index exempt). Failing symbols are re-downloaded up to twice in the same cycle; if still bad they are skipped (never traded on bad data) and logged as `DATA_QUALITY_BLOCKED`. If NIFTY fails, no signals are evaluated that cycle.
- Restart-safe JSON state, CSV trade history and system log; optional Telegram alerts.
- No Angel One order-placement method is called.

## Required setup before running

### 1. Verify instrument tokens

Edit `config/symbol_tokens.csv` using the **official Angel One instrument master**. Required columns: `symbol,token,exchange`.

Include one row for the NIFTY 50 index with symbol exactly `NIFTY` and verified stock rows to monitor. Do not guess token IDs. The CSV is deliberately not populated with invented tokens.

Example format (replace the placeholder with the actual verified token before running):

    symbol,token,exchange
    NIFTY,REPLACE_WITH_VERIFIED_TOKEN,NSE

### 2. Add GitHub Actions secrets

Repository → Settings → Secrets and variables → Actions → New repository secret:

- `ANGEL_API_KEY`
- `ANGEL_CLIENT_CODE`
- `ANGEL_PIN`
- `ANGEL_TOTP_SECRET`
- `TELEGRAM_BOT_TOKEN` (optional)
- `TELEGRAM_CHAT_ID` (optional)

Never put credentials in code, CSV files, commits or chat. This uses TOTP-based login; if Angel One login policy or SmartAPI response changes, the run should fail safely.

### 3. Enable Actions and configure branch

The workflow is `.github/workflows/paper-trading.yml`. It is scheduled every 15 minutes in a broad UTC window and the Python program gates execution to NSE hours in IST. GitHub-hosted schedules can be delayed, so **exact candle-open timing is not guaranteed**. Stale pending signals are skipped rather than entered retrospectively.

The workflow persists paper state and CSV records by committing to `paper-trading-implementation`. Review and merge this implementation branch/PR before expecting the scheduled workflow to run from the default branch.

### 4. Validate first

Run the **Validate Strategy B** workflow to compile Python and execute unit tests for 1.5R targets, stale-entry handling, SL/TP precedence, and the 15:00 force exit. After the verified token CSV and secrets are present, use **Strategy B Paper Trading → Run workflow**. Check the Actions log and `data/system_log.csv`.

## Important limitations

- Strategy B's POC is an OHLCV-based proxy, not true tick-level volume-at-price.
- This is a paper simulation, not evidence of profitability or a recommendation to trade.
- GitHub Actions is not an always-on trading server; alerts can arrive late. Entries are simulated at the next 15-minute candle open after that candle is available in completed history, so the alert may arrive after the simulated entry. Signals older than the 20-minute grace are skipped rather than backfilled.
- Paper state is committed to this private repository. Keep repository access restricted.
- No real orders are sent; `PAPER_ONLY` must remain `true`.

## Reliable start (GitHub cron can be hours late)

The workflow has several off-peak cron attempts, but GitHub may still delay them. For a guaranteed 09:10 IST start, create a free job at cron-job.org (Mon-Fri 09:10 IST) that POSTs to
`https://api.github.com/repos/arvindpca/NIFTY-POC24-SMC5M/dispatches` with header `Authorization: Bearer <fine-grained token, Contents: write>` and body `{"event_type":"start-paper-loop"}`.

## Angel-only mode (opt-in, accuracy over coverage)

Set `PAPER_UNIVERSE_SOURCE=angel` and `PAPER_ALLOW_YAHOO_FALLBACK=false` to scan NIFTY + the whole universe from Angel SmartAPI only (separate cache `data/candle_cache_angel_15m_v1`). A symbol that Angel refuses after retries is skipped for that cycle (never replaced by Yahoo data); the cycle stops early after `PAPER_MAX_CONSEC_RATE_LIMITS` (4) consecutive refusals or `PAPER_CYCLE_BUDGET_SEC` (780). Recommended pacing: `SMARTAPI_MIN_CALL_GAP_SEC=0.45`, `SMARTAPI_MAX_RETRIES=4`, `SMARTAPI_BACKOFF_BASE_SEC=8`. Not enabled in the workflow yet; test during market hours before switching.
