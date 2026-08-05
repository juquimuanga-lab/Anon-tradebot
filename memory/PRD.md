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

Creator watchlist signal wallet: `7AbRGzM3NBvvUXi7j1Mga2SraTfjpPBMzGpyHcXSzV3v`.

## User choices captured
- Admin Telegram ID: `6284967019` (stored in `TELEGRAM_ADMIN_IDS`).
- Non-admins may use read-only commands; only admin IDs can trade/change config.
- Anoncoin API key provided directly by user, stored via env + `/connect` (encrypted).
- Database: SQLite (per explicit choice).
- Default mode: paper, with live gated behind explicit confirmation - confirmed.

## Key research findings (as of build date)
- Anoncoin's public docs (`docs.anoncoin.it`) mark `Coins`, `Coin Details`,
  `My Profile`, `Create Coin` endpoints as "Coming Soon" - not live yet.
  Only `Top Holders` is live. **No documented buy/sell trade endpoint exists
  at all.**
- Auth: header `x-api-key: <ANONCOIN_API_KEY>`, host `https://api.anoncoin.it`.
- Solscan: Pro API v2 (`https://pro-api.solscan.io/v2.0`), header
  `token: <SOLSCAN_API_KEY>`. The provided key authenticates but its plan
  returns 401 "please upgrade your api key level" on `/token/meta` and
  `/token/holders` - enrichment degrades gracefully, no crash.

## Architecture implemented
`/app/backend/app/{bot,config,connectors,scanners,scoring,execution,positions,
storage,security,utils}` + `server.py` (FastAPI, embeds PTB polling bot,
`/api/health`, `/api/metrics`). SQLite via SQLAlchemy async + aiosqlite.
Minimal read-only React status dashboard at `/app/frontend` (dark terminal
aesthetic, IBM Plex Mono) polling health/metrics every 5s - Telegram remains
the primary control surface.

## What's been implemented (2026-08-05)
- Full Telegram command set: /start /status /connect /rules /setrule
  /listrules /enable /disable /paper /live /balance /positions /history /help.
- /setrule: 18-step guided wizard covering every rule parameter in the spec,
  with /skip and /cancel, ending in an inline-button save/activate confirmation.
- Admin allowlist decorator + read-only vs admin command split.
- Confirmation-token flow (2 min TTL) for /disable, /paper, /live, rule
  activation, and manual position close.
- Encrypted local secret storage (Fernet) for the Anoncoin key, settable via
  /connect (message auto-deleted after storage); env var fallback.
- Logging redaction filter (telegram token / JWT / long-token regex) verified
  to redact secrets without nuking normal structured log messages.
- Anoncoin + Solscan connectors with retry/backoff (tenacity) on transient
  errors; Anoncoin 404/501 -> `AnoncoinUnavailable` triggers mock feed fallback.
- Scanner: dedupes by mint, enriches via Solscan, evaluates hard filters,
  computes weighted score (liquidity/holders/freshness/volume/market-cap-fit +
  creator-watchlist bonus), persists screening results, executes paper buys
  when qualified, respects max-trades/hour and cooldown.
- Position manager: SL/trailing-stop/time-exit/take-profit-levels, manual
  close via Telegram, paper balance credited back on sell.
- Persistence: tokens, screening_results, rules, trade_decisions, orders,
  positions, bot_state, audit_log, secrets tables.
- Metrics + /api/health + /api/metrics; daily summary loop to Telegram admins.
- 25 unit/integration tests passing (rules, scoring, both connectors mocked
  with respx, paper execution adapter).
- Docker support (`backend/Dockerfile`, root `docker-compose.yml`), `.env.example`.

## Known limitations / backlog
- Wallet-based live execution against Meteora DBC has NOT been exercised
  against a real live pool end-to-end (no real Anoncoin token was available
  to test against, and doing so would require spending real SOL) - the
  Node.js builder was validated to run cleanly and return clean errors for a
  non-existent pool; a real fire-drill with a small amount on a genuine
  Anoncoin launch is recommended before trusting it with meaningful size.
- Solscan enrichment is degraded until the user's Solscan plan is upgraded.
- Telegram interaction flows could not be end-to-end tested by an automated
  agent (no Telegram user session available to simulate incoming commands) -
  code-level tests + manual verification of bot liveness (getMe) done instead.
- Multi-user / multi-account rule sets not implemented (one active rule set
  at a time; wallets ARE per-admin already).

## Next tasks
- User to message @anoncoinsniper_bot on Telegram and run through /start,
  /setrule, /connectwallet, /paper, /status themselves to confirm the live
  Telegram UX.
- Do a small real-money fire-drill (tiny SOL amount) on /live once a genuine
  Anoncoin pre-graduation token is available, to validate the Meteora DBC
  path end-to-end on-chain.
- Consider upgrading Solscan plan for full token/meta + holders enrichment.
hes a trade
  endpoint (isolated adapter ready to receive it via `ANONCOIN_TRADE_ENDPOINT`).
- Solscan enrichment is degraded until the user's Solscan plan is upgraded.
- Telegram interaction flows could not be end-to-end tested by an automated
  agent (no Telegram user session available to simulate incoming commands) -
  code-level tests + manual verification of bot liveness (getMe) done instead.
- Multi-user / multi-account rule sets not implemented (single active rule
  set at a time, matches MVP scope).

## Next tasks
- User to message @anoncoinsniper_bot on Telegram and run through /start,
  /setrule, /paper, /status themselves to confirm the live Telegram UX.
- Revisit `app/execution/anoncoin_live.py` once Anoncoin ships a trade endpoint.
- Consider upgrading Solscan plan for full token/meta + holders enrichment.
