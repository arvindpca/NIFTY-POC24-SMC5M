# Strategy B — Angel One paper-trading starter

**Education/research only. No real orders are placed.** `strategy_b_original.py` is the unmodified uploaded Strategy B backtest. Live-data integration is kept separate so the original is not overwritten.

## Preserved Strategy B settings
- 15-minute NSE setup: liquidity sweep → displacement → MSS → FVG/OB → retest/confirmation
- NIFTY 1H trend filter ON; stock rolling POC proxy ON; other optional filters OFF
- fixed target 1.5R; SL from sweep extreme plus/minus 0.10 ATR buffer
- approx ₹50,000 notional per position; max 5 simultaneous positions
- POC is an OHLCV approximation, not true volume-at-price

## Important: adapter is deliberately fail-closed
This starter does **not yet send signals**. The live runner stops until `app/strategy_adapter.py` is integrated and parity-tested against the original historical backtest. This prevents fabricated or altered strategy signals. It is not yet a completed, validated live paper-trading bot.

## Setup
1. Create a **private** GitHub repository and upload this folder.
2. Deploy as an always-on worker/container with persistent disk. Do not use scheduled GitHub Actions for continuous market monitoring.
3. Add environment secrets in the hosting dashboard: `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PIN`, `ANGEL_TOTP_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `PAPER_ONLY=true`.
4. Create `config/symbol_tokens.csv` with verified official Angel One instrument-master values and columns `symbol,token,exchange`. Include a verified `NIFTY` row. Never guess tokens.
5. Run `python -m app.main`. It will fail closed until the strategy adapter is validated.

Never commit API credentials, PIN, TOTP secret, bot token, `.env`, or account details. Attach persistent storage for `data/` so state and trade logs survive restarts.
