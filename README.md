# Anoncoin Sniper Bot

A Telegram-controlled trading assistant for the Anoncoin.it ecosystem. It watches
newly created tokens, enriches them with Solscan on-chain data, applies your
rules, and (once Anoncoin ships a public trade-execution endpoint) places
automated buys/sells through your Anoncoin profile API key. **Paper trading
works today end-to-end; live trading is intentionally isolated and blocked
until Anoncoin publishes a real trade endpoint (see "Known upstream
limitation" below).**

## Why some things are simulated right now

At the time this was built, Anoncoin's public docs (`docs.anoncoin.it`) marked
`Coins`, `Coin Details`, `My Profile`, and `Create Coin` as **"Coming Soon"**,
and there is **no documented buy/sell trade endpoint at all**. So the bot:

- Tries the real Anoncoin discovery/detail endpoints first.
- Falls back to a clearly-labelled simulated token feed (`source=mock_simulated`,
  every Telegram alert prefixed `[SIMULATED]`) so scanning, scoring, paper
  trading, and position management all work end to end today.
- Isolates trade execution behind `app/execution/` so a real
  `ANONCOIN_TRADE_ENDPOINT` can be plugged in later without touching
  scanning, scoring, or Telegram code at all.
- Never fakes a live fill: if you switch to `/live` and Anoncoin still hasn't
  published a trade endpoint, buys/sells fail loudly with a clear Telegram
  message instead of pretending to succeed.

## Real launch detection today (no Anoncoin API, no Solscan upgrade needed)

Anoncoin's discovery API isn't live, and the Solscan key's plan returns 401
"please upgrade your api key level" on **every** `pro-api.solscan.io/v2.0`
endpoint (confirmed - not just token/meta/holders). So the bot watches
`CREATOR_WATCHLIST` wallets directly on-chain, for free, via the public
Solana RPC:

- `app/scanners/onchain_watcher.py` polls `getSignaturesForAddress` for each
  watched wallet and diffs `preTokenBalances`/`postTokenBalances` on each new
  transaction to spot freshly-created SPL mints - this is exactly how a new
  Anoncoin pool creation shows up on-chain (confirmed by inspecting real
  transactions from `7AbRGz...V3v`, which fires
  `InitializeVirtualPoolWithToken2022` on Meteora's DBC program for every new
  Anoncoin token).
- For each newly detected mint, `app/execution/onchain/dbc_builder/pool_info.js`
  reads the live Meteora DBC pool account (price, reserves, supply,
  migration status) directly from chain - no Anoncoin/Solscan call needed.
- SOL amounts are converted to USD via Jupiter's free price API so existing
  USD-denominated rules (min liquidity, market cap, etc.) work unmodified.
- Freshly-detected tokens are re-screened every scan cycle (not just once)
  for up to `max_age_seconds`, since a bonding-curve pool starts at ~0
  liquidity and only qualifies once real buys start flowing in.
- Tokens sourced this way get `source=anoncoin_onchain` (real, not
  `[SIMULATED]`) and can trigger real paper or live trades.
- The bot only alerts on launches that happen *after* it starts watching -
  it does not backfill each wallet's full history on first boot.
- The free public RPC can rate-limit under bursty activity; if you see
  frequent `get_transaction_failed` warnings, set `SOLANA_RPC_URL` to a
  dedicated provider (Helius/QuickNode/Triton) for more reliable detection.

Anoncoin's own discovery API (once live) and the simulated feed remain as
fallbacks/demo data for everything outside the watched wallets.

## Live trading: two ways to execute

1. **Anoncoin trade API (future)** - isolated in `app/execution/`, ready the
   moment Anoncoin publishes one.
2. **Direct wallet execution (available now)** - each admin can run
   `/connectwallet` with a **dedicated/burner wallet's private key** (base58
   secret key or JSON byte array). The bot signs and broadcasts trades
   on-chain directly:
   - **Pre-graduation tokens** trade against **Meteora's public Dynamic
     Bonding Curve program** - Anoncoin's own API reveals a `meteoraConfigKey`
     on coin creation, confirming they build on this open, documented
     program. There's no official Python SDK, so a small Node.js sidecar
     (`app/execution/onchain/dbc_builder/`, using Meteora's official
     TypeScript SDK) builds the *unsigned* transaction; Python then signs it
     locally with the wallet's key, which never leaves the Python process.
   - **Post-graduation (migrated) tokens** trade through the **Jupiter**
     aggregator (`app/execution/onchain/jupiter.py`), pure Python/HTTP.
   - Each rule executes through its creator's connected wallet
     (`Rule.created_by` / `Position.owner_user_id`) - per-admin wallets, not
     one shared wallet.
   - `/disconnectwallet` deletes the stored key (confirmation required).

**Use a burner wallet funded only with what you're willing to risk - never
your main wallet.** The key is encrypted at rest (Fernet) the same way as the
Anoncoin key, and the message containing it is deleted from the chat
immediately after storage.

## Architecture

```
app/
  bot/          Telegram command handlers, /setrule wizard, confirmations, notifications
  config/       Pydantic settings (env-driven)
  connectors/   Anoncoin + Solscan HTTP clients
  scanners/     Launch detection loop, mock fallback feed, normalization
  scoring/      Rule schema, hard filters, weighted scoring model
  execution/    Execution adapter interface + paper/live implementations
  positions/    Open-position monitoring, TP/SL/trailing/time exits
  storage/      SQLAlchemy models + repository helpers (SQLite)
  security/     Secret encryption, redaction, admin allowlist
server.py       FastAPI app: embeds the Telegram bot (polling) + /api/health, /api/metrics
```

## Security model

- All secrets (Telegram token, Anoncoin key, Solscan key, encryption key) come
  from environment variables only - never hardcoded.
- The Anoncoin API key can also be set at runtime via `/connect` in Telegram;
  it is encrypted with Fernet (`SECRET_ENCRYPTION_KEY`) and stored in SQLite.
  The message containing your raw key is deleted immediately after storage.
- A logging filter redacts anything that looks like a bot token, JWT, or long
  API key from every log line before it's written.
- Telegram messages never include raw keys - only a masked `****last4` on
  confirmation.
- Admin-only commands (`/connect`, `/setrule`, `/enable`, `/disable`,
  `/paper`, `/live`, manual position close) are restricted to the numeric
  Telegram user IDs in `TELEGRAM_ADMIN_IDS`. Everyone else gets read-only
  commands (`/status`, `/rules`, `/balance`, `/positions`, `/history`).
- Destructive actions (`/disable`, `/paper`, `/live`, manual close, rule
  activation) require an inline-button confirmation that expires in 2 minutes.

## Setup

1. `cp .env.example backend/.env` and fill in:
   - `TELEGRAM_BOT_TOKEN` - from @BotFather.
   - `TELEGRAM_ADMIN_IDS` - your numeric Telegram user ID(s) from @userinfobot.
   - `SOLSCAN_API_KEY` - from solscan.io -> Account -> API Management.
   - `ANONCOIN_API_KEY` - optional here; you can also set it later from
     Telegram with `/connect`.
   - `SECRET_ENCRYPTION_KEY` - generate with:
     `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Install dependencies: `pip install -r backend/requirements.txt`
3. Run: `cd backend && uvicorn server:app --host 0.0.0.0 --port 8001`

The bot starts in **paper trading mode** by default. Talk to your bot on
Telegram and send `/start`, then `/setrule` to create your first rule set,
then watch `/status` and `/positions`.

## Running with Docker

```bash
cp .env.example .env   # fill in the same values as above
docker compose up --build
```

## Telegram commands

Read-only (anyone can use): `/status`, `/rules`, `/listrules`, `/balance`,
`/positions`, `/history`, `/help`.

Admin-only (`TELEGRAM_ADMIN_IDS`): `/connect`, `/connectwallet`,
`/disconnectwallet`, `/setrule`, `/enable`, `/disable`, `/paper`, `/live`,
`/positions close <id>`.

`/setrule` walks you through all 18 parameters step by step (max buy size,
min liquidity, min holders, max age, creator allow/denylist, bonding curve
phase, market cap range, max slippage, max trades/hour, cooldown, take-profit
levels, stop loss, trailing stop, sell-on-volume-drop, time-based exit) with
`/skip` for optional fields and `/cancel` anytime.

## Paper mode first

**Always validate your rules in paper mode before going live.** `/paper` is
the default. `/live` requires an explicit confirmation and - since Anoncoin
has not published a trade endpoint yet - will currently fail safely on any
real buy/sell attempt rather than risk funds.

## Creator watchlist

`CREATOR_WATCHLIST` (comma-separated wallet addresses) gets a scoring bonus
when a new token's creator wallet matches. It defaults to:
`7AbRGzM3NBvvUXi7j1Mga2SraTfjpPBMzGpyHcXSzV3v`.

## Tests

```bash
cd backend
pytest -q
```

Covers rule hard-filter evaluation, the weighted scoring model, the Anoncoin
and Solscan connectors against mocked HTTP responses (respx), and the paper
execution adapter.

## Observability

- `GET /api/health` - mode, trading_enabled.
- `GET /api/metrics` - tokens scanned/qualified, trades placed, win rate,
  total PnL, error count.
- A minimal read-only status dashboard (React) polls both endpoints every 5s.
- A daily summary is posted to Telegram admins at `DAILY_SUMMARY_HOUR_UTC`.

## Known upstream limitations (as of writing)

- Anoncoin's `coins`, `coin-details`, `my-profile`, and `create-coin`
  endpoints are marked "Coming Soon" in their public docs -> the bot falls
  back to a simulated feed, clearly labelled, until Anoncoin ships them.
- Anoncoin has no public buy/sell trade endpoint -> live execution defaults to
  **direct on-chain wallet trading** instead (see "Live trading" above):
  Meteora's Dynamic Bonding Curve for pre-graduation tokens, Jupiter for
  post-graduation. `app/execution/anoncoin_live.py`-style API execution can
  still be added later behind the same adapter interface with no changes
  elsewhere.
- The provided Solscan key authenticates against `pro-api.solscan.io` but
  its plan blocks every v2.0 endpoint tested, including `token/meta`,
  `token/holders`, `account/transactions`, and `account/defi/activities`
  (all return 401 "please upgrade your api key level"). Real launch
  detection therefore comes from watching wallets on-chain (see above)
  instead of Solscan; enrichment (holders/meta) stays degraded until the
  plan is upgraded, with no code changes needed once it is.
