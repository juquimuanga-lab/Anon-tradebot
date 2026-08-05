# Anoncoin Sniper Bot - PRD / Memory

## Original problem statement
Build a secure Telegram trading bot for the Anoncoin.it ecosystem: watches new
token launches, enriches with Solscan, applies user-defined rules, and places
automated trades via the user's Anoncoin profile API key only when conditions
are met. Must be Telegram-configurable, store secrets securely, never expose
keys in logs/chat/commits, support paper and live modes, and include
confirmation flows for destructive actions. Full spec covered: rules engine,
launch detection/screening, trade execution (paper + live, isolated adapter),
position management (TP/SL/trailing/time exit), notifications, persistence,
observability (health, metrics, daily summary), tests, Docker, README.

Creator watchlist signal wallet: `7AbRGzM3NBvvUXi7j1Mga2SraTfjpPBMzGpyHcXSzV3v`
(confirmed to be Anoncoin's own pool-creation relayer wallet - it fires
`InitializeVirtualPoolWithToken2022` on Meteora's DBC program for every new
Anoncoin token, so watching it on-chain = real launch detection).

## User choices captured
- Admin Telegram ID: `6284967019` (stored in `TELEGRAM_ADMIN_IDS`).
- Non-admins may use read-only commands; only admin IDs can trade/change config.
- Anoncoin API key provided directly by user, stored via env + `/connect` (encrypted).
- Database: SQLite (per explicit choice).
- Default mode: paper, with live gated behind explicit confirmation - confirmed.
- User asked for direct wallet-based execution (burner wallet private key,
  base58 or JSON array format) since Anoncoin has no trade API - confirmed:
  burner wallet only, both Meteora DBC + Jupiter venues, per-admin wallets,
  free public Solana RPC.
- User asked to use Solscan + the creator address to find new tokens -
  investigation showed Solscan's plan blocks all endpoints, so built a free
  on-chain watcher instead (see below).

## Key research findings
- Anoncoin's public docs (`docs.anoncoin.it`) mark `Coins`, `Coin Details`,
  `My Profile`, `Create Coin` endpoints as "Coming Soon" - not live yet.
  Only `Top Holders` is live. **No documented buy/sell trade endpoint exists
  at all.** Auth: header `x-api-key: <ANONCOIN_API_KEY>`, host `https://api.anoncoin.it`.
- Solscan Pro API v2 (`https://pro-api.solscan.io/v2.0`, header
  `token: <SOLSCAN_API_KEY>`): the provided key authenticates but its plan
  returns 401 "please upgrade your api key level" on **every** endpoint
  tested (token/meta, token/holders, account/transactions,
  account/defi/activities, account/token-accounts, token/list,
  token/trending, market/list) - not just meta/holders as first assumed.
- Anoncoin's own `create-coin-tx` response includes a `meteoraConfigKey`,
  confirming they build on Meteora's public, documented **Dynamic Bonding
  Curve (DBC)** program (`dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN`).
  Confirmed via real on-chain inspection of the watched wallet's
  transactions. No official Python SDK exists, so a Node.js sidecar using
  Meteora's official TS SDK builds/reads transactions; Python signs locally.
- Jupiter's older `quote-api.jup.ag/v6` and `api.jup.ag/price/v2` are dead;
  current free/keyless endpoints are `lite-api.jup.ag/swap/v1` (quote+swap)
  and `lite-api.jup.ag/price/v3` (price).

## Architecture implemented
`/app/backend/app/{bot,config,connectors,scanners,scoring,execution,positions,
storage,security,utils}` + `server.py` (FastAPI, embeds PTB polling bot,
`/api/health`, `/api/metrics`). SQLite via SQLAlchemy async + aiosqlite.
Minimal read-only React status dashboard at `/app/frontend` (dark terminal
aesthetic, IBM Plex Mono) polling health/metrics every 5s - Telegram remains
the primary control surface. `app/execution/onchain/dbc_builder/` is a small
Node.js sidecar (own package.json/node_modules) invoked via subprocess from
Python for anything requiring Meteora's official SDK (building swap txs,
reading pool state); it never receives private keys.

## What's been implemented (2026-08-05)
- Full Telegram command set: /start /status /connect /connectwallet
  /disconnectwallet /rules /setrule /listrules /enable /disable /paper /live
  /balance /positions /history /help.
- **Real on-chain launch detection** (no paid API needed): `app/scanners/
  onchain_watcher.py` polls the free public Solana RPC's
  `getSignaturesForAddress` for each `CREATOR_WATCHLIST` wallet and diffs
  pre/post token balances to spot brand-new SPL mints in real time.
  `app/execution/onchain/dbc_builder/pool_info.js` reads the live Meteora
  DBC pool (price/reserves/supply/migration status) for each new mint;
  `app/scanners/price_feed.py` converts SOL to USD via Jupiter's free price
  API. Verified end-to-end against real mainnet data (detected 3 real
  tokens from the watched wallet in one test run, ~$2,200 realistic
  starting market cap). Freshly-detected tokens are re-screened every scan
  cycle for up to `max_age_seconds` since a fresh curve starts at ~0
  liquidity. `source=anoncoin_onchain` tokens are real and can trigger real
  paper/live trades. Only alerts on launches after the bot starts watching
  (no history backfill). Gentle 150ms pacing between per-tx RPC calls to
  reduce free-RPC rate-limit risk.
- Fixed a bug the user caught: a `[SIMULATED]` mock-feed token attempted a
  live buy and failed with "Non-base58 character" (confusing, though no
  funds/network call were actually at risk - it failed in-process before
  touching the chain). Scanner now skips mock-simulated tokens entirely in
  live mode.
- Direct wallet-based live execution: each admin runs `/connectwallet` with
  a burner wallet's private key (base58 or JSON byte array), encrypted at
  rest per-user. Pre-graduation buys/sells go through Meteora's DBC program
  (Node sidecar builds unsigned tx, Python signs locally with solders -
  private key never touches the Node process). Post-graduation (migrated)
  tokens route through Jupiter aggregator. `ExecutionRouter` resolves paper
  vs. per-admin-wallet live adapters based on `Rule.created_by` /
  `Position.owner_user_id`. `/disconnectwallet` removes the stored key with
  confirmation.
- /setrule: 18-step guided wizard covering every rule parameter in the spec,
  with /skip and /cancel, ending in an inline-button save/activate confirmation.
- Admin allowlist decorator + read-only vs admin command split.
- Confirmation-token flow (2 min TTL) for /disable, /paper, /live, rule
  activation, disconnect wallet, and manual position close.
- Encrypted local secret storage (Fernet) for the Anoncoin key and each
  admin's wallet private key, settable via /connect and /connectwallet
  (messages auto-deleted after storage); env var fallback for Anoncoin key.
- Logging redaction filter (telegram token / JWT / long-token regex) verified
  to redact secrets without nuking normal structured log messages.
- Anoncoin + Solscan connectors with retry/backoff (tenacity) on transient
  errors; Anoncoin 404/501 -> `AnoncoinUnavailable` triggers mock feed fallback.
- Position manager: SL/trailing-stop/time-exit/take-profit-levels, manual
  close via Telegram, paper balance credited back on sell, live sells route
  through the position owner's connected wallet, real-time price for
  `anoncoin_onchain` positions reads the live DBC pool instead of simulating.
- Persistence: tokens, screening_results, rules, trade_decisions, orders,
  positions (with owner_user_id), bot_state, audit_log, secrets tables.
- Metrics + /api/health + /api/metrics; daily summary loop to Telegram admins.
- 41 unit/integration tests passing (rules, scoring, connectors, paper
  execution, wallet key parsing, execution router, on-chain mint extraction,
  price feed caching).
- Docker support updated to install Node.js + the dbc_builder sidecar's
  npm deps alongside Python deps.

## Known limitations / backlog
- Wallet-based live execution against Meteora DBC has NOT been exercised
  end-to-end with a real signed+broadcast transaction (would require
  spending real SOL) - the build/read paths were validated against real
  mainnet pools; a small real fire-drill is recommended before trusting it
  with meaningful size.
- Free public Solana RPC can rate-limit under bursty wallet activity
  (mitigated with pacing, but a dedicated RPC provider is more reliable for
  production use).
- Solscan enrichment (holders/meta) stays degraded until the user's plan is
  upgraded - detection no longer depends on it, but richer holder data would.
- Telegram interaction flows could not be end-to-end tested by an automated
  agent (no Telegram user session available to simulate incoming commands) -
  code-level tests + manual verification of bot liveness (getMe) done instead.
- Multi-user / multi-account rule sets not implemented (one active rule set
  at a time; wallets ARE per-admin already).
- On-chain-detected tokens have no name/symbol (Metaplex metadata not parsed
  yet) - shown as a mint-prefix placeholder instead.

## Ops note
- Wallet private keys and the Anoncoin API key share the same
  `SECRET_ENCRYPTION_KEY` (Fernet). Rotating that key means every connected
  admin wallet and the Anoncoin key must be re-registered via
  `/connectwallet` / `/connect` afterwards.

## Next tasks
- User to message @anoncoinsniper_bot on Telegram and run through /start,
  /setrule, /connectwallet, /paper, /status themselves to confirm the live
  Telegram UX.
- Do a small real-money fire-drill (tiny SOL amount) on /live to validate
  the Meteora DBC signed-broadcast path end-to-end on-chain.
- Consider fetching Metaplex token metadata for real name/symbol on
  on-chain-detected tokens.
- Consider upgrading Solscan plan or a dedicated RPC provider for richer/
  more reliable enrichment and detection under load.
