# Anon-tradebot — Phase 1 Smart Money

This phase is observational only.

## What it does

1. Keeps the existing Pump.fun launch detector unchanged.
2. Only after the existing hard filters + qualification score pass, queries
   Solana Tracker's `/trades/{tokenAddress}` endpoint.
3. Matches recent Pump.fun BUY trades against `SMART_MONEY_WALLETS`.
4. Ignores tracked-wallet buys below `SMART_MONEY_MIN_BUY_USD`.
5. Calculates a descriptive smart-money score from buy size, recency and the
   number of distinct tracked wallets.
6. Fetches/caches wallet PnL/quality information from Solana Tracker PnL V2.
7. Adds the information to the existing qualified-token Telegram alert.
8. Does NOT make smart money a requirement and does NOT alter `_maybe_trade()`.

## Railway variables

Set:

SOLANA_TRACKER_API_KEY=your-key
SMART_MONEY_WALLETS=6cNjLym8bDZ5JFGFSDom2us27iF7EBHYUXdFCdC5zWhX
SMART_MONEY_ENABLED=true
SMART_MONEY_MIN_BUY_USD=50
SMART_MONEY_MAX_TRADES_PER_TOKEN=100
SMART_MONEY_TRADE_LOOKBACK_SECONDS=180

The API key must stay in Railway/environment variables and must not be
committed to the repository.

## Apply

From the repository root:

    git apply phase1_smart_money.patch

Then copy:

    backend/app/connectors/solana_tracker.py

into the matching repository path.

No new Python dependency is required; the existing project already uses
`httpx`.

## Safety

Phase 1 is telemetry only. Even if Solana Tracker is unavailable, the bot
continues through its existing qualification/trading path.
