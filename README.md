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

Run the **Validate Strategy B** workflow to compile Python. After the verified token CSV and secrets are present, use **Strategy B Paper Trading → Run workflow**. Check the Actions log and `data/system_log.csv`.

## Important limitations

- Strategy B's POC is an OHLCV-based proxy, not true tick-level volume-at-price.
- This is a paper simulation, not evidence of profitability or a recommendation to trade.
- GitHub Actions is not an always-on trading server; alerts can arrive late.
- Paper state is committed to this private repository. Keep repository access restricted.
- No real orders are sent; `PAPER_ONLY` must remain `true`.
