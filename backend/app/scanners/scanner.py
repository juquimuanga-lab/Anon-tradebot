"""Launch detection + screening + trade decision loop.

Launch sources:

1. Anoncoin/Meteora
   Detected through the existing CREATOR_WATCHLIST path.

2. Pump.fun
   Detected independently through the Pump.fun mint-authority watcher.

3. Mock/simulated
   Existing development/testing source.

The launch sources remain separate until they reach the common
TokenSnapshot/rule engine.

IMPORTANT:
    Pump.fun tokens are priced through pumpfun.py and must not be routed
    through the Meteora DBC price reader.

The same Telegram/admin screening rules are applied to both sources.
"""

import asyncio
import logging
import json
import time
from datetime import datetime, timedelta, timezone

from app.config.settings import settings
from app.connectors.anoncoin import (
    AnoncoinAPIError,
    AnoncoinClient,
)

from app.connectors.fourmeme import fourmeme_client

from app.connectors.helius import (    HeliusAPIError,
    HeliusClient,
)

from app.execution.base import OrderResult

from app.execution.onchain import (
    meteora_dbc,
    pumpfun,
)

from app.execution.onchain.solana_rpc import (
    extract_wallet_trade_execution,
    get_transaction_details,
)

from app.execution.onchain.meteora_dbc import (
    DbcBuildError,
)

from app.execution.onchain.pumpfun import (
    PumpFunError,
    PumpFunInvalidAccount,
    PumpFunPoolNotFound,
)

from app.execution.router import (
    ExecutionRouter,
)

from app.metrics import metrics
from app.guardian import guardian

from app.scanners import (
    mock_feed,
    onchain_watcher,
    price_feed,
)

from app.scanners.normalize import (
    apply_holder_enrichment,
    from_anoncoin_coin,
)

from app.scoring.rules import (
    TokenSnapshot,
    evaluate_hard_filters,
    evaluate_fast_sniper_filters,
    evaluate_late_entry,
)

from app.scoring.scorer import (
    compute_score,
)

from app.storage import repository as repo


logger = logging.getLogger(
    "app.scanners.scanner"
)


# ---------------------------------------------------------------------------
# Launch sources
# ---------------------------------------------------------------------------

SOURCE_ANONCOIN = "anoncoin_onchain"
SOURCE_PUMPFUN = "pumpfun"
SOURCE_FOURMEME = "fourmeme"
SOURCE_MOCK = "mock_simulated"

# Pump.fun launch-quality gate.
# This is intentionally a score rather than three hard cut-offs so the
# scanner can reject obviously weak launches without filtering out every
# legitimate early launch. The normal rule engine still applies afterward.
PUMPFUN_QUALITY_SCORE_THRESHOLD = 55.0
PUMPFUN_RPC_MAX_CONCURRENCY = 2
PUMPFUN_RPC_RETRY_DELAYS = (0.35, 0.75, 1.5, 3.0)
PUMPFUN_RPC_MIN_REQUEST_INTERVAL = 0.12
PUMPFUN_RPC_RATE_LIMIT_COOLDOWN = 1.5

# ---------------------------------------------------------------------------
# Fast Sniper early-entry tuning
# ---------------------------------------------------------------------------
# The Fast lane must be able to enter before a generic Smart rule's market-cap
# confirmation has already allowed the first vertical move to happen.
# These are deliberately conservative safety floors; the normal Smart lane is
# unchanged.
FAST_EARLY_ENTRY_MAX_AGE_SECONDS = 1.50
FAST_EARLY_ENTRY_MIN_MARKET_CAP_USD = 3_500.0
FAST_EARLY_ENTRY_MAX_RUNUP_FROM_FIRST_PCT = 35.0
FAST_EARLY_ENTRY_MAX_SHORT_RUNUP_PCT = 18.0



def _is_anoncoin_source(source: str) -> bool:
    """True for any Anoncoin-origin source tag.

    Covers both the on-chain watcher tag (anoncoin_onchain) and the
    legacy Anoncoin-API discovery tag (anoncoin), so the
    anoncoin_trading_enabled toggle applies no matter which path
    produced the token.
    """

    return bool(source) and source.startswith("anoncoin")


def _evaluate_fast_early_entry(token, rule_params) -> tuple[bool, list[str]]:
    """Aggressive first-snapshot gate for the Pump.fun Fast Sniper lane.

    This intentionally does NOT wait for the rule's normal minimum market-cap
    confirmation. That confirmation can arrive after the first vertical move,
    which is exactly the late-entry pattern we want the Fast lane to avoid.

    Safety still comes from:
      - very young token age;
      - creator allow/deny rules;
      - configured minimum liquidity;
      - a small absolute MC floor;
      - configured maximum MC;
      - bonding-curve phase;
      - anti-chase limits from the first and previous observations.

    Smart Filter and Smart Money lanes never use this path.
    """
    reasons: list[str] = []

    if token.source != SOURCE_PUMPFUN:
        return False, ["fast early entry is only available for Pump.fun"]

    age = float(getattr(token, "age_seconds", 0.0) or 0.0)
    if age > FAST_EARLY_ENTRY_MAX_AGE_SECONDS:
        reasons.append(
            f"early entry window expired ({age:.2f}s > "
            f"{FAST_EARLY_ENTRY_MAX_AGE_SECONDS:.2f}s)"
        )

    creator = getattr(token, "creator_wallet", "") or ""
    if creator and creator in getattr(rule_params, "creator_denylist", []):
        reasons.append("creator is denylisted")
    allowlist = getattr(rule_params, "creator_allowlist", []) or []
    if allowlist and creator not in allowlist:
        reasons.append("creator not in allowlist")

    liquidity = float(getattr(token, "liquidity_usd", 0.0) or 0.0)
    min_liquidity = float(getattr(rule_params, "min_liquidity_usd", 0.0) or 0.0)
    if liquidity < min_liquidity:
        reasons.append(
            f"liquidity ${liquidity:,.0f} below min ${min_liquidity:,.0f}"
        )

    market_cap = float(getattr(token, "market_cap_usd", 0.0) or 0.0)
    configured_min_mc = getattr(rule_params, "min_market_cap_usd", None)
    configured_min_mc = (
        float(configured_min_mc) if configured_min_mc is not None else 0.0
    )
    # If the normal rule says e.g. $8k, the early lane may enter from $4k,
    # but never below the absolute safety floor.
    early_min_mc = max(
        FAST_EARLY_ENTRY_MIN_MARKET_CAP_USD,
        configured_min_mc * 0.50,
    )
    if market_cap < early_min_mc:
        reasons.append(
            f"early market cap ${market_cap:,.0f} below "
            f"early min ${early_min_mc:,.0f}"
        )

    max_mc = getattr(rule_params, "max_market_cap_usd", None)
    if max_mc is not None and market_cap > float(max_mc):
        reasons.append(
            f"market cap ${market_cap:,.0f} above max ${float(max_mc):,.0f}"
        )

    phase = getattr(rule_params, "bonding_curve_phase", "any")
    if phase != "any" and getattr(token, "bonding_curve_phase", None) != phase:
        reasons.append(
            f"bonding curve phase {getattr(token, 'bonding_curve_phase', None)} "
            f"!= required {phase}"
        )

    history = (getattr(token, "raw_enrichment", {}) or {}).get(
        "late_entry_history"
    ) or {}
    first_price = float(history.get("first_price_usd", 0.0) or 0.0)
    current_price = float(getattr(token, "price_usd", 0.0) or 0.0)
    previous = history.get("previous") or {}
    previous_price = float(previous.get("price_usd", 0.0) or 0.0)

    if first_price > 0 and current_price > 0:
        runup = (current_price - first_price) / first_price * 100.0
        if runup >= FAST_EARLY_ENTRY_MAX_RUNUP_FROM_FIRST_PCT:
            reasons.append(
                f"early anti-chase: +{runup:.0f}% from first observed price"
            )

    if previous_price > 0 and current_price > 0:
        short_runup = (current_price - previous_price) / previous_price * 100.0
        if short_runup >= FAST_EARLY_ENTRY_MAX_SHORT_RUNUP_PCT:
            reasons.append(
                f"early anti-chase: +{short_runup:.0f}% since previous snapshot"
            )

    return len(reasons) == 0, reasons


def _rule_platform_for_source(source: str) -> str:
    """Map a launch source to its isolated rule namespace."""
    return "fourmeme" if source == SOURCE_FOURMEME else "solana"


def _rule_matches_source(rule, source: str) -> bool:
    return (getattr(rule, "platform", "solana") or "solana") == _rule_platform_for_source(source)


def _rule_strategy(rule) -> str:
    return "fast" if getattr(rule, "strategy", "smart") == "fast" else "smart"


# ---------------------------------------------------------------------------
# Scanner service
# ---------------------------------------------------------------------------

class ScannerService:

    def __init__(
        self,
        notifier,
        position_manager,
        anoncoin: AnoncoinClient,
        holders_client: HeliusClient,
        execution_router: ExecutionRouter,
    ):
        self._notifier = notifier
        self._position_manager = position_manager
        self._anoncoin = anoncoin
        self._holders_client = holders_client
        self._execution_router = execution_router

        self._fourmeme = fourmeme_client

        self._watermarks = (            onchain_watcher.WatermarkStore()
        )

        self._pending_watch: dict[
            str,
            dict,
        ] = {}

        # Short-window launch history for momentum scoring. This is tactical
        # signal state and intentionally does not need database persistence.
        self._momentum_history: dict[str, dict] = {}

        # Protect Helius/Pump.fun RPC from launch bursts.
        self._pumpfun_rpc_semaphore = asyncio.Semaphore(
            PUMPFUN_RPC_MAX_CONCURRENCY
        )
        # Helius throttles on request rate as well as concurrency.  The
        # semaphore alone is not enough because get_pool_info() can issue
        # multiple RPC calls per mint.  Space pool-read starts globally.
        self._pumpfun_rpc_rate_lock = asyncio.Lock()
        self._pumpfun_rpc_next_request_at = 0.0
        self._pumpfun_rpc_cooldown_until = 0.0


        self._notified_fail: set[
            tuple[str, int]
        ] = set()

        self._holders_failure_count = 0

        self._holders_backoff_until: (
            datetime | None
        ) = None

    # ------------------------------------------------------------------
    # Anoncoin API discovery
    # ------------------------------------------------------------------

    async def _fetch_new_tokens(
        self,
    ):
        try:
            raw_coins = (
                await self._anoncoin.get_coins(
                    sort_by="new",
                    limit=settings.anoncoin_discovery_limit,
                )
            )

            tokens = [
                from_anoncoin_coin(coin)
                for coin in raw_coins
            ]
            logger.info(
                "anoncoin_discovery_batch",
                extra={
                    "returned": len(tokens),
                    "limit": settings.anoncoin_discovery_limit,
                    "source": SOURCE_ANONCOIN,
                },
            )
            return tokens

        except AnoncoinAPIError as exc:

            logger.info(
                "anoncoin_discovery_unavailable",
                extra={
                    "detail": str(exc),
                },
            )

            if settings.enable_mock_feed:
                return mock_feed.generate()

            return []

    # ------------------------------------------------------------------
    # Anoncoin on-chain watcher
    # ------------------------------------------------------------------

    async def _watch_anoncoin_for_new_mints(
        self,
    ):
        """Watch the existing Anoncoin creator addresses."""

        for wallet in settings.creator_watchlist:

            try:
                discovered = (
                    await onchain_watcher.poll_new_mints(
                        settings.solana_rpc_url,
                        wallet,
                        self._watermarks,
                    )
                )

            except Exception:

                logger.exception(
                    "anoncoin_onchain_watch_poll_failed",
                    extra={
                        "wallet": wallet,
                    },
                )

                continue

            for item in discovered:

                mint = item["mint"]

                if mint in self._pending_watch:
                    continue

                if await repo.token_already_seen(
                    mint
                ):
                    continue

                self._pending_watch[mint] = {
                    "first_seen": (
                        datetime.now(
                            timezone.utc
                        )
                    ),
                    "source": SOURCE_ANONCOIN,
                    "metadata": item,
                }

                metrics.tokens_scanned += 1

                logger.info(
                    "onchain_new_mint_detected",
                    extra={
                        "mint": mint,
                        "watched_wallet": wallet,
                        "source": SOURCE_ANONCOIN,
                        "tx_signature": item.get(
                            "tx_signature"
                        ),
                    },
                )

    # ------------------------------------------------------------------
    # Smart-money wallet watcher
    # ------------------------------------------------------------------
    async def _watch_smart_money_buys(self):
        """Drain Helius WSS smart-money buy events into the Pump.fun queue."""
        if not settings.smart_money_enabled:
            return

        try:
            events = onchain_watcher.drain_smart_money_buys(
                settings.solana_rpc_url,
                settings.smart_money_wallets,
            )
        except Exception as exc:
            logger.exception(
                "smart_money_wallet_stream_poll_failed",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
            return

        for event in events:
            mint = event.get("mint")
            if not mint:
                continue
            if mint in self._pending_watch:
                logger.info(
                    "smart_money_buy_already_pending",
                    extra={"mint": mint, "tx_signature": event.get("tx_signature")},
                )
                continue

            # Do not suppress the event merely because the token was seen by the
            # launch watcher. The smart-money purchase is a new trading signal.
            detected_at = event.get("detected_at")
            try:
                first_seen = datetime.fromtimestamp(float(detected_at), tz=timezone.utc)
            except Exception:
                first_seen = datetime.now(timezone.utc)

            self._pending_watch[mint] = {
                "first_seen": first_seen,
                "source": SOURCE_PUMPFUN,
                "metadata": {
                    "smart_money": True,
                    "smart_money_wallet": event.get("wallet"),
                    "smart_money_tx_signature": event.get("tx_signature"),
                    "tx_signature": event.get("tx_signature"),
                    "creator": "",
                    "discovery": event.get("discovery", "helius_wss_wallet_buy"),
                },
                "smart_money": True,
            }

            metrics.tokens_scanned += 1
            await guardian.record("candidate", source=SOURCE_PUMPFUN, smart_money=True, mint=mint)
            await guardian.record("smart_money_buy", wallet=event.get("wallet"), mint=mint, tx_signature=event.get("tx_signature"))
            logger.info(
                "smart_money_candidate_queued " + json.dumps(
                    {
                        "mint": mint,
                        "wallet": event.get("wallet"),
                        "tx_signature": event.get("tx_signature"),
                        "detected_at": detected_at,
                    },
                    default=str, separators=(",", ":"), sort_keys=True,
                ),
                extra={
                    "mint": mint,
                    "wallet": event.get("wallet"),
                    "tx_signature": event.get("tx_signature"),
                },
            )

    # ------------------------------------------------------------------
    # Pump.fun watcher
    # ------------------------------------------------------------------

    async def _watch_pumpfun_for_new_mints(
        self,
    ):
        """Watch Pump.fun independently from Anoncoin."""

        try:

            discovered = (
                await onchain_watcher
                .poll_new_pumpfun_mints(
                    settings.solana_rpc_url,
                    settings.pumpfun_mint_authority,
                    self._watermarks,
                )
            )

        except Exception:

            logger.exception(
                "pumpfun_onchain_watch_poll_failed",
                extra={
                    "mint_authority": (
                        settings.pumpfun_mint_authority
                    ),
                },
            )

            return

        for item in discovered:

            mint = item["mint"]

            if mint in self._pending_watch:
                continue

            if await repo.token_already_seen(
                mint
            ):
                continue

            self._pending_watch[mint] = {
                "first_seen": (
                    datetime.now(
                        timezone.utc
                    )
                ),
                "source": SOURCE_PUMPFUN,
                "metadata": item,
            }

            metrics.tokens_scanned += 1

            discovery_log = {
                "mint": mint,
                "creator": item.get("creator"),
                "source": SOURCE_PUMPFUN,
                "tx_signature": item.get("tx_signature"),
            }
            logger.info(
                "pumpfun_candidate_discovered " + json.dumps(
                    discovery_log, default=str, separators=(",", ":"), sort_keys=True
                ),
                extra=discovery_log,
            )

    # ------------------------------------------------------------------
    # Four.meme / BSC watcher
    # ------------------------------------------------------------------

    async def _watch_fourmeme_for_new_mints(self):
        """Drain Bitquery's Four.meme mempool TokenCreate stream."""
        if not self._fourmeme.enabled:
            return
        for item in await self._fourmeme.drain():
            mint = item["mint"]
            if mint in self._pending_watch:
                continue
            if await repo.token_already_seen(mint):
                continue
            self._pending_watch[mint] = {
                "first_seen": item.get("created_on") or datetime.now(timezone.utc),
                "source": SOURCE_FOURMEME,
                "metadata": item,
            }
            metrics.tokens_scanned += 1
            queue_log = {
                "mint": mint,
                "creator": item.get("creator"),
                "source": SOURCE_FOURMEME,
                "tx_signature": item.get("tx_signature"),
            }
            logger.info(
                "fourmeme_candidate_queued " + json.dumps(
                    queue_log, default=str, separators=(",", ":"), sort_keys=True
                ),
                extra=queue_log,
            )

    async def _build_fourmeme_snapshot(self, mint, metadata, first_seen):
        try:
            market = await self._fourmeme.market_snapshot(mint)
        except Exception as exc:
            snapshot_log = {
                "mint": mint,
                "source": SOURCE_FOURMEME,
                "error": str(exc),
            }
            logger.warning(
                "fourmeme_snapshot_not_ready " + json.dumps(
                    snapshot_log, default=str, separators=(",", ":"), sort_keys=True
                ),
                extra=snapshot_log,
            )
            return None
        if market["price_usd"] <= 0:
            return None
        return TokenSnapshot(
            mint=mint,
            ticker_name=metadata.get("ticker_name", ""),
            ticker_symbol=metadata.get("ticker_symbol", ""),
            creator_wallet=metadata.get("creator", ""),
            created_on=first_seen,
            price_usd=market["price_usd"],
            market_cap_usd=market["market_cap_usd"],
            liquidity_usd=market.get("liquidity_usd", 0.0),
            holders=market["holders"],
            volume_24h_usd=market["volume_24h_usd"],
            is_migrated=bool(market.get("liquidity_added", False)),
            decimals=18,
            source=SOURCE_FOURMEME,
            raw_enrichment={"fourmeme": market, "tx_signature": metadata.get("tx_signature")},
        )

    # ------------------------------------------------------------------
    # Watch all on-chain sources
    # ------------------------------------------------------------------

    async def _watch_wallets_for_new_mints(
        self,
    ):
        """Poll both supported launch sources."""

        await self._watch_smart_money_buys()
        await self._watch_anoncoin_for_new_mints()

        await self._watch_pumpfun_for_new_mints()

        await self._watch_fourmeme_for_new_mints()

    # ------------------------------------------------------------------
    # Anoncoin / Meteora snapshot
    # ------------------------------------------------------------------

    async def _build_anoncoin_snapshot(
        self,
        mint: str,
        metadata: dict,
        first_seen: datetime,
    ) -> TokenSnapshot | None:

        try:

            info = (
                await meteora_dbc.get_pool_info(
                    mint,
                    settings.solana_rpc_url,
                )
            )

        except DbcBuildError as exc:

            logger.warning(
                "anoncoin_pool_info_failed",
                extra={
                    "mint": mint,
                    "error": str(exc),
                },
            )

            return None

        try:

            sol_price = (
                await price_feed.get_sol_usd_price(
                    settings.jupiter_price_url
                )
            )

        except Exception as exc:

            logger.warning(
                "anoncoin_sol_price_failed",
                extra={
                    "mint": mint,
                    "error": str(exc),
                },
            )

            return None

        return TokenSnapshot(
            mint=mint,
            ticker_name=f"anon-{mint[:6]}",
            ticker_symbol=mint[:6],
            creator_wallet=info.get(
                "creator",
                "",
            ),
            created_on=first_seen,
            price_usd=(
                info[
                    "price_sol_per_token"
                ]
                * sol_price
            ),
            market_cap_usd=(
                info[
                    "market_cap_sol"
                ]
                * sol_price
            ),
            liquidity_usd=(
                info[
                    "quote_reserve_sol"
                ]
                * sol_price
            ),
            holders=0,
            volume_24h_usd=0.0,
            is_migrated=bool(
                info.get(
                    "is_migrated",
                    False,
                )
            ),
            decimals=int(
                info.get(
                    "token_decimals",
                    6,
                )
            ),
            source=SOURCE_ANONCOIN,
        )

    @staticmethod
    def _is_missing_account_error(exc: Exception) -> bool:
        """Return True for transient Solana RPC errors caused by an account
        not being available yet.

        Pump.fun launches can be detected before the mint/token account is
        fully visible through the RPC used by the pool reader. In that case
        pumpfun.get_pool_info() can surface the raw RPC error:
        "Invalid param: could not find account".

        This is a normal transient condition for a newly detected launch, so
        the mint should remain in _pending_watch and be retried on the next
        scan instead of producing a full traceback.
        """
        message = str(exc).lower()
        return (
            "could not find account" in message
            or "account not found" in message
            or "invalid param" in message
            and "account" in message
        )

    # ------------------------------------------------------------------
    async def _get_pumpfun_pool_info(
        self,
        mint: str,
    ):
        """Fetch Pump.fun curve data without stampeding the RPC."""
        last_exc = None
        for attempt, delay in enumerate(
            (0.0,) + PUMPFUN_RPC_RETRY_DELAYS,
            start=1,
        ):
            if delay:
                await asyncio.sleep(delay)

            try:
                async with self._pumpfun_rpc_semaphore:
                    # Rate-limit request *starts* in addition to limiting
                    # concurrent pool reads. This prevents a burst of
                    # asyncio.gather() calls from exceeding Helius RPS.
                    async with self._pumpfun_rpc_rate_lock:
                        now = asyncio.get_running_loop().time()
                        wait_for = max(
                            self._pumpfun_rpc_next_request_at - now,
                            self._pumpfun_rpc_cooldown_until - now,
                            0.0,
                        )
                        if wait_for > 0:
                            await asyncio.sleep(wait_for)
                        self._pumpfun_rpc_next_request_at = (
                            asyncio.get_running_loop().time()
                            + PUMPFUN_RPC_MIN_REQUEST_INTERVAL
                        )

                    return await pumpfun.get_pool_info(
                        mint,
                        settings.solana_rpc_url,
                        commitment="processed",
                    )
            except Exception as exc:
                last_exc = exc
                message = str(exc)
                if (
                    "429" not in message
                    and "Too Many Requests" not in message
                ):
                    raise

                # Slow the entire Pump.fun RPC lane after a 429 rather than
                # allowing all waiting candidates to immediately retry.
                async with self._pumpfun_rpc_rate_lock:
                    self._pumpfun_rpc_cooldown_until = max(
                        self._pumpfun_rpc_cooldown_until,
                        asyncio.get_running_loop().time()
                        + PUMPFUN_RPC_RATE_LIMIT_COOLDOWN
                        * attempt,
                    )

                logger.warning(
                    "pumpfun_rpc_rate_limited",
                    extra={
                        "mint": mint,
                        "attempt": attempt,
                        "cooldown_seconds":
                            PUMPFUN_RPC_RATE_LIMIT_COOLDOWN * attempt,
                    },
                )

        raise last_exc

    # Pump.fun snapshot
    # ------------------------------------------------------------------

    async def _build_pumpfun_snapshot(
        self,
        mint: str,
        metadata: dict,
        first_seen: datetime,
    ) -> TokenSnapshot | None:
        """Build a TokenSnapshot from Pump.fun's bonding curve."""

        try:

            info = await self._get_pumpfun_pool_info(mint)

        except (
            PumpFunPoolNotFound,
            PumpFunInvalidAccount,
            PumpFunError,
        ) as exc:

            logger.warning(
                "pumpfun_pool_info_failed",
                extra={
                    "mint": mint,
                    "error": str(exc),
                },
            )

            return None

        except Exception as exc:

            if self._is_missing_account_error(exc):
                # Newly launched Pump.fun mints can become visible to the
                # mint-authority watcher slightly before the mint/token
                # account is readable through the RPC used by pumpfun.py.
                # Keep the launch pending so the existing retry loop can
                # process it once the account becomes available.
                logger.warning(
                    "pumpfun_snapshot_account_not_ready",
                    extra={
                        "mint": mint,
                        "error": str(exc),
                    },
                )
            else:
                logger.exception(
                    "pumpfun_snapshot_unexpected_error",
                    extra={
                        "mint": mint,
                        "error": str(exc),
                    },
                )

            return None

        creator = (
            info.get("creator")
            or metadata.get("creator")
            or ""
        )

        price_usd = float(
            info.get(
                "price_usd",
                0.0,
            )
        )

        market_cap_usd = float(
            info.get(
                "market_cap_usd",
                0.0,
            )
        )

        liquidity_usd = float(
            info.get(
                "liquidity_usd",
                0.0,
            )
        )

        if price_usd <= 0:

            logger.warning(
                "pumpfun_invalid_price",
                extra={
                    "mint": mint,
                    "price_usd": price_usd,
                },
            )

            return None

        return TokenSnapshot(
            mint=mint,
            ticker_name=f"pump-{mint[:6]}",
            ticker_symbol=mint[:6],
            creator_wallet=creator,
            created_on=first_seen,
            price_usd=price_usd,
            market_cap_usd=market_cap_usd,
            liquidity_usd=liquidity_usd,
            holders=0,
            volume_24h_usd=0.0,
            is_migrated=bool(
                info.get(
                    "is_migrated",
                    False,
                )
            ),
            decimals=int(
                info.get(
                    "token_decimals",
                    6,
                )
            ),
            source=SOURCE_PUMPFUN,
            raw_enrichment={
                "smart_money": bool(metadata.get("smart_money")),
                "smart_money_wallet": metadata.get("smart_money_wallet"),
                "smart_money_tx_signature": metadata.get("smart_money_tx_signature"),
                "tx_signature": metadata.get("tx_signature"),
            },
               )

    # ------------------------------------------------------------------
    # Unified snapshot dispatcher
    # ------------------------------------------------------------------

    async def _build_onchain_snapshot(
        self,
        mint: str,
        source: str,
        metadata: dict,
        first_seen: datetime,
    ) -> TokenSnapshot | None:

        if source == SOURCE_ANONCOIN:

            return await (
                self._build_anoncoin_snapshot(
                    mint,
                    metadata,
                    first_seen,
                )
            )

        if source == SOURCE_PUMPFUN:

            return await (
                self._build_pumpfun_snapshot(
                    mint,
                    metadata,
                    first_seen,
                )
            )

        if source == SOURCE_FOURMEME:
            return await self._build_fourmeme_snapshot(
                mint, metadata, first_seen
            )

        logger.warning(
            "unknown_onchain_source",
            extra={
                "mint": mint,
                "source": source,
            },
        )

        return None

    # ------------------------------------------------------------------
    # Holder enrichment
    # ------------------------------------------------------------------

    async def _enrich_holders(
        self,
        token,
    ):

        # Four.meme is BSC/EVM. Never send its 0x token address to the
        # Solana/Helius holder enrichment path. Bitquery already supplies
        # the BSC holder count in the Four.meme snapshot.
        if token.source == SOURCE_FOURMEME:
            return token

        now = datetime.now(
            timezone.utc
        )

        if (
            self._holders_backoff_until
            and now
            < self._holders_backoff_until
        ):
            return token

        try:

            holder_count = (
                await self._holders_client
                .get_token_holder_count(
                    token.mint
                )
            )

            self._holders_failure_count = 0

        except HeliusAPIError as exc:

            metrics.degraded_count += 1

            self._holders_failure_count += 1

            logger.warning(
                "holders_enrichment_failed",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

            holder_count = None

        if (
            self._holders_failure_count >= 3
            and not self._holders_backoff_until
        ):

            self._holders_backoff_until = (
                now
                + timedelta(
                    minutes=10
                )
            )

            logger.warning(
                "holders_enrichment_disabled_temporarily",
                extra={
                    "minutes": 10,
                },
            )

        elif (
            self._holders_failure_count == 0
        ):

            self._holders_backoff_until = None

        return apply_holder_enrichment(
            token,
            holder_count,
        )


    # ------------------------------------------------------------------
    # Pump.fun launch-quality gate
    # ------------------------------------------------------------------

    @staticmethod
    def _pumpfun_quality_gate(token) -> tuple[str, float, list[str], dict]:
        """Score early Pump.fun launch quality without a brittle hard gate.

        Returns:
            (status, score, reasons, breakdown)

        status is one of:
            - ``pass``: score is high enough to reach the normal rule engine
            - ``reject``: measurable data is available but quality is too low
            - ``defer``: snapshot data is temporarily unavailable; keep the
              launch pending so a transient RPC/account-read failure does not
              permanently discard a potentially valid launch.
        """
        if token.source != SOURCE_PUMPFUN:
            return "pass", 100.0, [], {}

        liquidity = float(token.liquidity_usd or 0.0)
        market_cap = float(token.market_cap_usd or 0.0)
        holders_raw = getattr(token, "holders", None)

        # Do not confuse missing launch data with a bad launch. The pending
        # watcher will retry while the token is still within the rule age.
        if liquidity <= 0.0 or market_cap <= 0.0 or holders_raw is None:
            reasons = [
                "Pump.fun launch data not ready "
                f"(liquidity=${liquidity:,.0f}, "
                f"market_cap=${market_cap:,.0f}, "
                f"holders={holders_raw})"
            ]
            logger.info(
                "pumpfun_quality_gate_deferred",
                extra={
                    "mint": token.mint,
                    "liquidity_usd": liquidity,
                    "market_cap_usd": market_cap,
                    "holders": holders_raw,
                    "reasons": reasons,
                },
            )
            return "defer", 0.0, reasons, {
                "data_ready": False,
            }

        holders = int(holders_raw)
        ratio = market_cap / liquidity

        # Liquidity: 30 points
        if liquidity >= 10_000:
            liquidity_score = 30.0
        elif liquidity >= 5_000:
            liquidity_score = 20.0
        elif liquidity >= 2_500:
            liquidity_score = 10.0
        else:
            liquidity_score = 0.0

        # Holder distribution: 25 points
        if holders >= 35:
            holder_score = 25.0
        elif holders >= 20:
            holder_score = 20.0
        elif holders >= 10:
            holder_score = 15.0
        elif holders >= 5:
            holder_score = 8.0
        else:
            holder_score = 0.0

        # MC/liquidity efficiency: 25 points. Lower is healthier.
        if ratio <= 20.0:
            ratio_score = 25.0
        elif ratio <= 35.0:
            ratio_score = 20.0
        elif ratio <= 50.0:
            ratio_score = 15.0
        elif ratio <= 60.0:
            ratio_score = 8.0
        else:
            ratio_score = 0.0

        # Age/early-launch bonus: 20 points. This rewards entering while
        # the launch is genuinely early, while avoiding an age requirement
        # as a hard rejection by itself.
        age_seconds = float(getattr(token, "age_seconds", 0.0) or 0.0)
        if age_seconds <= 30:
            age_score = 20.0
        elif age_seconds <= 90:
            age_score = 16.0
        elif age_seconds <= 180:
            age_score = 12.0
        elif age_seconds <= 300:
            age_score = 8.0
        else:
            age_score = 0.0

        score = (
            liquidity_score
            + holder_score
            + ratio_score
            + age_score
        )

        breakdown = {
            "liquidity_score": liquidity_score,
            "holder_score": holder_score,
            "market_cap_liquidity_score": ratio_score,
            "early_age_score": age_score,
            "liquidity_usd": liquidity,
            "holders": holders,
            "market_cap_usd": market_cap,
            "market_cap_liquidity_ratio": ratio,
            "age_seconds": age_seconds,
            "threshold": PUMPFUN_QUALITY_SCORE_THRESHOLD,
        }

        if score < PUMPFUN_QUALITY_SCORE_THRESHOLD:
            reasons = [
                "Pump.fun launch-quality score too low "
                f"({score:.0f}/{100:.0f} < "
                f"{PUMPFUN_QUALITY_SCORE_THRESHOLD:.0f})"
            ]
            status = "reject"
        else:
            reasons = []
            status = "pass"

        if status == "pass":
            logger.info(
                "pumpfun_quality_gate_passed_to_rules",
                extra={
                    "mint": token.mint,
                    "score": score,
                },
            )

        logger.info(
            "pumpfun_quality_gate_" + status,
            extra={
                "mint": token.mint,
                "score": score,
                "breakdown": breakdown,
                "reasons": reasons,
            },
        )

        return status, score, reasons, breakdown

    # ------------------------------------------------------------------
    # Pump.fun pre-buy revalidation
    # ------------------------------------------------------------------

    async def _revalidate_pumpfun_before_buy(
        self,
        token,
        rule_row,
        previous_score_result,
    ):
        """Refresh Pump.fun market data immediately before a live buy.

        The launch snapshot can become stale very quickly on Pump.fun.
        Re-read the bonding curve immediately before execution and run the
        same hard filters/qualification score against the fresh market-cap
        and liquidity values. Holder data is retained from the already
        completed enrichment step to avoid adding another expensive RPC call
        in the critical execution path.
        """
        if token.source != SOURCE_PUMPFUN:
            return token, previous_score_result, True

        started = time.monotonic()

        fresh_token = await self._build_pumpfun_snapshot(
            token.mint,
            {
                "creator": getattr(
                    token,
                    "creator_wallet",
                    "",
                ),
            },
            getattr(
                token,
                "created_on",
                datetime.now(timezone.utc),
            ),
        )

        if fresh_token is None:
            logger.warning(
                "pumpfun_prebuy_revalidation_failed",
                extra={
                    "mint": token.mint,
                    "reason": "fresh bonding curve snapshot unavailable",
                },
            )
            return token, previous_score_result, False

        fresh_token.holders = getattr(
            token,
            "holders",
            0,
        )
        fresh_token.volume_24h_usd = getattr(
            token,
            "volume_24h_usd",
            0.0,
        )

        rule_params = repo.rule_row_to_params(
            rule_row
        )
        rule_params = self._apply_late_entry_settings(rule_params)

        strategy = _rule_strategy(rule_row)

        # Fast Sniper revalidation deliberately avoids holder enrichment, the
        # Pump.fun quality score, and the full Smart score threshold. It only
        # rechecks the fresh bonding-curve safety bounds plus the lightweight
        # anti-chase guard before the transaction is sent.
        if strategy == "fast":
            fresh_token.raw_enrichment["late_entry_history"] = (
                self._momentum_history.get(token.mint) or {}
            )
            fast_passed, fast_reasons = evaluate_fast_sniper_filters(
                fresh_token, rule_params
            )
            fast_score_result = compute_score(
                fresh_token, rule_params, settings.creator_watchlist
            )
            elapsed_ms = (time.monotonic() - started) * 1000.0
            logger.info(
                "pumpfun_fast_prebuy_revalidation",
                extra={
                    "mint": token.mint,
                    "rule_id": rule_row.id,
                    "market_cap_usd": fresh_token.market_cap_usd,
                    "liquidity_usd": fresh_token.liquidity_usd,
                    "age_seconds": fresh_token.age_seconds,
                    "passed": fast_passed,
                    "reasons": fast_reasons,
                    "elapsed_ms": elapsed_ms,
                },
            )
            if not fast_passed:
                logger.info(
                    "pumpfun_fast_prebuy_rejected",
                    extra={"mint": token.mint, "rule_id": rule_row.id, "reasons": fast_reasons},
                )
                return fresh_token, fast_score_result, False
            return fresh_token, fast_score_result, True

        # Carry the scanner's observed launch history into the fresh pre-buy
        # snapshot so the final RPC read cannot bypass the anti-chase gate.
        fresh_token.raw_enrichment["late_entry_history"] = (
            self._momentum_history.get(token.mint) or {}
        )
        fresh_token.raw_enrichment["momentum_previous"] = (
            self._momentum_history.get(token.mint) or {}
        )

        passed, reasons = evaluate_hard_filters(
            fresh_token,
            rule_params,
        )

        fresh_score_result = compute_score(
            fresh_token,
            rule_params,
            settings.creator_watchlist,
        )

        (
            quality_status,
            quality_score,
            quality_reasons,
            quality_breakdown,
        ) = self._pumpfun_quality_gate(
            fresh_token
        )

        if quality_status != "pass":
            reasons = list(reasons) + list(
                quality_reasons
            )

        late_passed, late_reasons, late_breakdown = evaluate_late_entry(
            fresh_token,
            rule_params,
        )
        if not late_passed:
            reasons = list(reasons) + list(late_reasons)

        elapsed_ms = (
            time.monotonic() - started
        ) * 1000.0

        initial_mc = float(
            getattr(
                token,
                "market_cap_usd",
                0.0,
            )
            or 0.0
        )
        fresh_mc = float(
            getattr(
                fresh_token,
                "market_cap_usd",
                0.0,
            )
            or 0.0
        )
        initial_liquidity = float(
            getattr(
                token,
                "liquidity_usd",
                0.0,
            )
            or 0.0
        )
        fresh_liquidity = float(
            getattr(
                fresh_token,
                "liquidity_usd",
                0.0,
            )
            or 0.0
        )

        mc_change_pct = (
            (
                (fresh_mc - initial_mc)
                / initial_mc
                * 100.0
            )
            if initial_mc > 0.0
            else None
        )

        logger.info(
            "pumpfun_prebuy_revalidation",
            extra={
                "mint": token.mint,
                "rule_id": rule_row.id,
                "initial_market_cap_usd": initial_mc,
                "prebuy_market_cap_usd": fresh_mc,
                "market_cap_change_pct": mc_change_pct,
                "initial_liquidity_usd": initial_liquidity,
                "prebuy_liquidity_usd": fresh_liquidity,
                "initial_price_usd": getattr(
                    token,
                    "price_usd",
                    0.0,
                ),
                "prebuy_price_usd": getattr(
                    fresh_token,
                    "price_usd",
                    0.0,
                ),
                "hard_filters_passed": passed,
                "quality_status": quality_status,
                "quality_score": quality_score,
                "score": fresh_score_result.score,
                "elapsed_ms": elapsed_ms,
                "reasons": reasons,
            },
        )

        if not passed:
            logger.info(
                "pumpfun_prebuy_revalidation_rejected",
                extra={
                    "mint": token.mint,
                    "rule_id": rule_row.id,
                    "reasons": reasons,
                    "prebuy_market_cap_usd": fresh_mc,
                },
            )
            return fresh_token, fresh_score_result, False

        if (
            fresh_score_result.score
            < rule_params.qualify_score_threshold
        ):
            logger.info(
                "pumpfun_prebuy_revalidation_rejected",
                extra={
                    "mint": token.mint,
                    "rule_id": rule_row.id,
                    "reason": "qualification score fell below threshold",
                    "score": fresh_score_result.score,
                    "threshold": rule_params.qualify_score_threshold,
                    "prebuy_market_cap_usd": fresh_mc,
                },
            )
            return fresh_token, fresh_score_result, False

        if quality_status != "pass":
            logger.info(
                "pumpfun_prebuy_revalidation_rejected",
                extra={
                    "mint": token.mint,
                    "rule_id": rule_row.id,
                    "reason": "Pump.fun quality gate no longer passes",
                    "quality_status": quality_status,
                    "quality_score": quality_score,
                    "prebuy_market_cap_usd": fresh_mc,
                },
            )
            return fresh_token, fresh_score_result, False

        if not late_passed:
            await guardian.record("rejected", owner_id=rule.created_by, reason="late_entry")
            logger.info(
                "pumpfun_prebuy_revalidation_rejected",
                extra={
                    "mint": token.mint,
                    "rule_id": rule_row.id,
                    "reason": "anti-late-entry gate rejected the fresh price",
                    "late_entry_reasons": late_reasons,
                    "late_entry_breakdown": late_breakdown,
                    "prebuy_market_cap_usd": fresh_mc,
                },
            )
            return fresh_token, fresh_score_result, False

        logger.info(
            "pumpfun_prebuy_revalidation_passed",
            extra={
                "mint": token.mint,
                "rule_id": rule_row.id,
                "market_cap_usd": fresh_mc,
                "score": fresh_score_result.score,
                "elapsed_ms": elapsed_ms,
            },
        )

        return fresh_token, fresh_score_result, True

    # ------------------------------------------------------------------
    # Trade
    # ------------------------------------------------------------------

    async def _maybe_trade(
        self,
        token,
        rule_row,
        score_result,
    ) -> bool:

        state = (
            await repo.get_or_create_bot_state(rule_row.created_by)
        )

        if not state.trading_enabled:
            await repo.save_trade_decision(
                token.mint, rule_row.id, "skip",
                "global trading disabled for this admin", score_result.score
            )
            return False

        # Defense-in-depth: the pending-launch dispatcher already filters these
        # lanes, but execution must enforce the per-admin strategy switch too.
        if token.source == SOURCE_PUMPFUN:
            strategy = _rule_strategy(rule_row)
            if not state.pumpfun_trading_enabled:
                await repo.save_trade_decision(
                    token.mint, rule_row.id, "skip",
                    "Pump.fun master switch disabled for this admin", score_result.score
                )
                return False
            if strategy == "fast" and not getattr(state, "pumpfun_fast_enabled", False):
                await repo.save_trade_decision(
                    token.mint, rule_row.id, "skip",
                    "Fast Sniper disabled for this admin", score_result.score
                )
                return False
            if strategy == "smart" and not getattr(state, "pumpfun_smart_enabled", True):
                await repo.save_trade_decision(
                    token.mint, rule_row.id, "skip",
                    "Smart Filter disabled for this admin", score_result.score
                )
                return False

        if (
            _is_anoncoin_source(token.source)
            and not state.anoncoin_trading_enabled
        ):

            await repo.save_trade_decision(
                token.mint,
                rule_row.id,
                "skip",
                (
                    "Anoncoin trading is currently "
                    "disabled (/enableanoncoin to "
                    "resume) - skipped"
                ),
                score_result.score,
            )

            return True

        if token.source == SOURCE_FOURMEME and (
            not state.fourmeme_trading_enabled
            or not settings.fourmeme_trading_enabled
        ):
            await repo.save_trade_decision(
                token.mint, rule_row.id, "skip",
                "Four.meme trading is disabled", score_result.score
            )
            return True

        if (
            token.source == SOURCE_PUMPFUN
            and not state.pumpfun_trading_enabled
        ):

            await repo.save_trade_decision(
                token.mint,
                rule_row.id,
                "skip",
                (
                    "Pump.fun trading is currently "
                    "disabled (/enablepumpfun to "
                    "resume) - skipped"
                ),
                score_result.score,
            )

            return True

        if (
            state.mode == "live"
            and token.source == SOURCE_MOCK
        ):

            await repo.save_trade_decision(
                token.mint,
                rule_row.id,
                "skip",
                (
                    "simulated token has no real "
                    "on-chain mint - skipped in "
                    "live mode"
                ),
                score_result.score,
            )

            return True

        if await repo.has_open_or_pending_position(
            token.mint,
            rule_row.created_by,
        ):
            return True

        recent_buys = (
            await repo.recent_buy_count(
                hours=1.0,
                owner_user_id=(
                    rule_row.created_by
                ),
            )
        )

        if (
            recent_buys
            >= rule_row.max_trades_per_hour
        ):

            await repo.save_trade_decision(
                token.mint,
                rule_row.id,
                "skip",
                "max trades per hour reached",
                score_result.score,
            )

            return False

        seconds_since = (
            await repo.seconds_since_last_buy(
                owner_user_id=(
                    rule_row.created_by
                )
            )
        )

        if (
            seconds_since is not None
            and seconds_since
            < rule_row.cooldown_seconds
        ):

            await repo.save_trade_decision(
                token.mint,
                rule_row.id,
                "skip",
                "cooldown active",
                score_result.score,
            )

            return False

        # --------------------------------------------------------------
        # FINAL PUMPFUN MARKET-CAP REVALIDATION
        #
        # The launch snapshot used for qualification may already be stale
        # by the time the execution adapter is called. Refresh the bonding
        # curve immediately before placing the order so a rule such as
        # min_market_cap_usd=8000 cannot be satisfied by an old snapshot
        # while the actual buy occurs materially below that level.
        # --------------------------------------------------------------
        if token.source == SOURCE_PUMPFUN and _rule_strategy(rule_row) == "fast":
            # Fast Sniper deliberately avoids a second Python-side bonding-curve
            # read here. The launch snapshot has already passed the fast safety
            # gate and the transaction builder obtains the current execution data.
            # This removes an avoidable RPC round-trip from the hot path.
            logger.info(
                "pumpfun_fast_hot_path_prebuy_revalidation_skipped",
                extra={"mint": token.mint, "rule_id": rule_row.id},
            )
        else:
            (
                token,
                score_result,
                prebuy_valid,
            ) = await self._revalidate_pumpfun_before_buy(
                token,
                rule_row,
                score_result,
            )

            if not prebuy_valid:
                await repo.save_trade_decision(
                    token.mint,
                    rule_row.id,
                    "skip",
                    "Pump.fun pre-buy market data changed or no longer passed rules",
                    score_result.score,
                )
                return False

        if token.source == SOURCE_FOURMEME:
            # Four.meme rules use BNB. The legacy order column remains named
            # requested_amount_sol for DB compatibility, but stores the native
            # amount used by the selected execution adapter.
            amount_native = float(
                getattr(rule_row, "max_buy_size_bnb", 0.01) or 0.01
            )
            amount_unit = "BNB"
        else:
            amount_native = min(
                rule_row.max_buy_size_sol,
                (
                    state.paper_balance_sol
                    if state.mode == "paper"
                    else rule_row.max_buy_size_sol
                ),
            )
            amount_unit = "SOL"

        # --------------------------------------------------------------
        # IMPORTANT:
        #
        # Pass the launch source into the execution router.
        #
        # Anoncoin -> existing WalletExecutionAdapter
        # Pump.fun  -> PumpFunExecutionUnavailableAdapter for now
        #
        # This prevents a Pump.fun token from falling through into the
        # Anoncoin/Meteora execution path.
        # --------------------------------------------------------------

        adapter = (
            await self._execution_router
            .get_adapter(
                state.mode,
                rule_row.created_by,
                source=token.source,
            )
        )

        order = (
            await repo.create_order(
                token.mint,
                "buy",
                state.mode,
                "pending",
                amount_native,
                token.price_usd,
                rule_id=rule_row.id,
                owner_user_id=(
                    rule_row.created_by
                ),
            )
        )

        logger.info(
            "buy_placed",
            extra={
                "mint": token.mint,
                "rule_id": rule_row.id,
                "amount_native": amount_native,
                "amount_unit": amount_unit,
                "mode": state.mode,
                "source": token.source,
            },
        )

        await self._notifier.buy_placed(
            rule_row.created_by,
            (
                token.ticker_symbol
                or token.mint[:8]
            ),
            amount_native,
            state.mode,
        )

        await guardian.record("buy_attempt", owner_id=rule_row.created_by, mint=token.mint, source=token.source)
        try:

            result = await asyncio.wait_for(
                adapter.buy(
                    token,
                    amount_native,
                ),
                timeout=(
                    settings.execution_timeout_seconds
                ),
            )

        except asyncio.TimeoutError:

            logger.error(
                "buy_execution_timeout",
                extra={
                    "mint": token.mint,
                    "rule_id": rule_row.id,
                    "source": token.source,
                },
            )

            result = OrderResult(
                success=False,
                status="failed",
                error_message=(
                    "execution did not resolve "
                    f"within "
                    f"{settings.execution_timeout_seconds}s - "
                    "outcome unknown, verify "
                    "wallet balance / Solscan manually"
                ),
            )

        if result.success:

            # --------------------------------------------------------------
            # LIVE FILL RECONCILIATION
            #
            # Do not use token.price_usd as the Pump.fun entry price.
            # That value was observed before the transaction landed and can
            # differ materially from the actual execution.
            #
            # The confirmed transaction is authoritative: derive the actual
            # SOL spent (excluding the network fee) and actual token amount
            # received from pre/post balances.
            # --------------------------------------------------------------
            fill_price = (
                result.price_usd
                or token.price_usd
            )

            amount_tokens = None
            actual_amount_sol = amount_native
            execution = None
            sol_price = 0.0

            if (
                token.source == SOURCE_PUMPFUN
                and result.tx_signature
            ):
                try:
                    confirmed_tx = (
                        await get_transaction_details(
                            getattr(
                                adapter,
                                "_rpc_url",
                                getattr(
                                    settings,
                                    "solana_rpc_url",
                                    "",
                                ),
                            ),
                            result.tx_signature,
                        )
                    )

                    execution = (
                        extract_wallet_trade_execution(
                            confirmed_tx,
                            str(
                                getattr(
                                    adapter,
                                    "_pubkey",
                                    getattr(
                                        settings,
                                        "wallet_public_key",
                                        "",
                                    ),
                                )
                            ),
                            token.mint,
                        )
                    )

                    if execution:
                        actual_amount_sol = (
                            execution[
                                "sol_spent_excluding_fee_lamports"
                            ]
                            / 1_000_000_000
                        )

                        amount_tokens = (
                            execution[
                                "token_received_raw"
                            ]
                            / (
                                10
                                ** execution[
                                    "token_decimals"
                                ]
                            )
                        )

                        sol_price = (
                            await price_feed.get_sol_usd_price(
                                settings.jupiter_price_url
                            )
                        )

                        if sol_price > 0 and amount_tokens > 0:
                            fill_price = (
                                actual_amount_sol
                                * sol_price
                                / amount_tokens
                            )

                        logger.info(
                            "pumpfun_buy_actual_fill_reconciled",
                            extra={
                                "mint": token.mint,
                                "tx_signature": result.tx_signature,
                                "requested_amount_sol": amount_native,
                                "actual_amount_sol": actual_amount_sol,
                                "actual_amount_tokens": amount_tokens,
                                "actual_fill_price_usd": fill_price,
                                "network_fee_lamports": execution["fee_lamports"],
                            },
                        )

                except Exception as exc:
                    logger.warning(
                        "pumpfun_buy_fill_reconciliation_failed",
                        extra={
                            "mint": token.mint,
                            "tx_signature": result.tx_signature,
                            "error": str(exc),
                        },
                    )

            # Fallback for non-Pump.fun paths or if reconciliation was not
            # available. Existing behavior remains unchanged for those paths.
            if amount_tokens is None:
                sol_price = (
                    await price_feed.get_sol_usd_price(
                        settings.jupiter_price_url
                    )
                )

                if sol_price <= 0:
                    logger.warning(
                        "invalid_sol_price_for_position",
                        extra={
                            "mint": token.mint,
                            "sol_price": sol_price,
                        },
                    )
                    return False

                amount_tokens = (
                    amount_native
                    * sol_price
                    / max(
                        fill_price,
                        1e-12,
                    )
                )

            await repo.create_position(
                token.mint,
                rule_row.id,
                state.mode,
                fill_price,
                amount_tokens,
                actual_amount_sol,
                owner_user_id=(
                    rule_row.created_by
                ),
                entry_cost_usd=(
                    actual_amount_sol * float(sol_price)
                    + (
                        execution["fee_lamports"] / 1_000_000_000 * float(sol_price)
                        if token.source == SOURCE_PUMPFUN and execution else 0.0
                    )
                ),
                entry_fee_usd=(
                    execution["fee_lamports"] / 1_000_000_000 * float(sol_price)
                    if token.source == SOURCE_PUMPFUN and execution else 0.0
                ),
                entry_volume_24h_usd=(
                    token.volume_24h_usd
                ),
                source=token.source,
            )

            await repo.update_order(
                order.id,
                "filled",
                tx_signature=(
                    result.tx_signature
                ),
            )

            await repo.save_trade_decision(
                token.mint,
                rule_row.id,
                "buy",
                "passed rules",
                score_result.score,
            )

            metrics.trades_placed += 1

            logger.info(
                "buy_filled",
                extra={
                    "mint": token.mint,
                    "rule_id": rule_row.id,
                    "source": token.source,
                    "tx_signature": (
                        result.tx_signature
                    ),
                },
            )

            await guardian.record("buy_success", owner_id=rule_row.created_by, mint=token.mint, tx_signature=result.tx_signature)
            await self._notifier.buy_filled(
                rule_row.created_by,
                (
                    token.ticker_symbol
                    or token.mint[:8]
                ),
                fill_price,
                state.mode,
                result.tx_signature,
            )

            return True

        await repo.update_order(
            order.id,
            "failed",
            error_message=(
                result.error_message
            ),
        )

        await repo.save_trade_decision(
            token.mint,
            rule_row.id,
            "buy_failed",
            (
                result.error_message
                or "unknown error"
            ),
            score_result.score,
        )

        logger.warning(
            "buy_failed",
            extra={
                "mint": token.mint,
                "rule_id": rule_row.id,
                "source": token.source,
                "error": (
                    result.error_message
                ),
            },
        )

        await guardian.record("buy_failed", owner_id=rule_row.created_by, mint=token.mint, error=result.error_message or "unknown")
        await self._notifier.buy_failed(
            rule_row.created_by,
            (
                token.ticker_symbol
                or token.mint[:8]
            ),
            (
                result.error_message
                or "unknown error"
            ),
        )

        return False

    def _update_late_entry_history(self, token) -> dict:
        """Maintain a tiny per-mint price history for anti-chase checks."""
        now = time.monotonic()
        current_price = float(getattr(token, "price_usd", 0.0) or 0.0)
        current_mc = float(getattr(token, "market_cap_usd", 0.0) or 0.0)
        state = self._momentum_history.get(token.mint) or {}
        previous = {
            "timestamp": state.get("timestamp", 0.0),
            "price_usd": state.get("price_usd", 0.0),
            "market_cap_usd": state.get("market_cap_usd", 0.0),
        }
        first_price = float(state.get("first_price_usd", 0.0) or 0.0)
        first_mc = float(state.get("first_market_cap_usd", 0.0) or 0.0)
        if first_price <= 0 and current_price > 0:
            first_price = current_price
        if first_mc <= 0 and current_mc > 0:
            first_mc = current_mc
        peak_price = max(
            float(state.get("peak_price_usd", 0.0) or 0.0),
            current_price,
        )
        history = {
            "timestamp": now,
            "previous": previous,
            "price_usd": current_price,
            "market_cap_usd": current_mc,
            "first_price_usd": first_price,
            "first_market_cap_usd": first_mc,
            "peak_price_usd": peak_price,
        }
        self._momentum_history[token.mint] = {
            **state,
            **history,
            "liquidity_usd": float(getattr(token, "liquidity_usd", 0.0) or 0.0),
            "holders": int(getattr(token, "holders", 0) or 0),
            "volume_24h_usd": float(getattr(token, "volume_24h_usd", 0.0) or 0.0),
        }
        return history

    def _apply_late_entry_settings(self, rule_params):
        """Apply deployment-level anti-late-entry overrides to a rule."""
        rule_params.late_entry_enabled = settings.late_entry_enabled
        rule_params.late_entry_max_age_seconds = settings.late_entry_max_age_seconds
        rule_params.late_entry_soft_market_cap_usd = settings.late_entry_soft_market_cap_usd
        rule_params.late_entry_hard_market_cap_usd = settings.late_entry_hard_market_cap_usd
        rule_params.late_entry_near_high_pct = settings.late_entry_near_high_pct
        rule_params.late_entry_required_pullback_pct = settings.late_entry_required_pullback_pct
        rule_params.late_entry_max_short_runup_pct = settings.late_entry_max_short_runup_pct
        rule_params.late_entry_max_runup_from_first_pct = settings.late_entry_max_runup_from_first_pct
        return rule_params

    # ------------------------------------------------------------------
    # Screening
    # ------------------------------------------------------------------

    async def _screen_and_maybe_trade(
        self,
        token,
        rule,
        notify_on_fail: bool,
    ) -> bool:

        if not rule:
            return False

        from app.storage.repository import (
            rule_row_to_params,
        )

        rule_params = (
            rule_row_to_params(
                rule
            )
        )
        rule_params = self._apply_late_entry_settings(rule_params)

        late_history = self._update_late_entry_history(token)
        token.raw_enrichment["late_entry_history"] = late_history

        # Attach the PREVIOUS snapshot so the scorer measures acceleration
        # rather than comparing the token with itself.
        previous = late_history.get("previous") or {}
        previous_timestamp = float(previous.get("timestamp", 0.0) or 0.0)
        if previous and previous_timestamp and time.monotonic() - previous_timestamp <= 15.0:
            token.raw_enrichment["momentum_previous"] = previous
        else:
            token.raw_enrichment.pop("momentum_previous", None)

        strategy = _rule_strategy(rule)

        # ------------------------------------------------------------------
        # SMART MONEY BLIND COPY PATH
        # ------------------------------------------------------------------
        # A tracked Smart Money wallet is itself the signal. Do NOT run the
        # normal hard filters, Fast filters, holder enrichment, quality gate,
        # late-entry checks, or score threshold before dispatching the buy.
        # The rule is retained only for execution parameters (buy amount,
        # slippage, priority fee, etc.) and the global Pump.fun/trading safety
        # switches enforced by _maybe_trade().
        if token.source == SOURCE_PUMPFUN and token.raw_enrichment.get("smart_money"):
            score_result = compute_score(token, rule_params, settings.creator_watchlist)
            logger.info(
                "smart_money_blind_buy_dispatch",
                extra={
                    "mint": token.mint,
                    "rule_id": rule.id,
                    "wallet": token.raw_enrichment.get("smart_money_wallet"),
                    "tx_signature": token.raw_enrichment.get("smart_money_tx_signature"),
                    "telemetry_score": score_result.score,
                    "reason": "tracked_smart_money_buy_bypasses_normal_filters",
                },
            )
            metrics.tokens_qualified += 1
            await self._notifier.new_qualified_token(
                rule.created_by,
                token.ticker_symbol or token.mint[:8],
                token.mint,
                score_result.score,
                token.source,
            )
            return await self._maybe_trade(token, rule, score_result)

        if token.source == SOURCE_PUMPFUN and strategy == "fast":
            # First-snapshot early entry: try the aggressive gate before the
            # generic Fast filter. This prevents a high configured min-MC from
            # forcing the bot to wait for the first pump candle.
            early_passed, early_reasons = _evaluate_fast_early_entry(
                token, rule_params
            )

            if early_passed:
                score_result = compute_score(
                    token, rule_params, settings.creator_watchlist
                )
                logger.info(
                    "fast_sniper_early_entry_dispatch",
                    extra={
                        "mint": token.mint,
                        "rule_id": rule.id,
                        "market_cap_usd": token.market_cap_usd,
                        "liquidity_usd": token.liquidity_usd,
                        "age_seconds": token.age_seconds,
                        "score_telemetry": score_result.score,
                        "entry_mode": "early_first_snapshot",
                    },
                )
                metrics.tokens_qualified += 1
                await self._notifier.new_qualified_token(
                    rule.created_by,
                    token.ticker_symbol or token.mint[:8],
                    token.mint,
                    score_result.score,
                    token.source,
                )
                return await self._maybe_trade(
                    token, rule, score_result
                )

            # Fall back to the existing Fast safety gate when the early gate
            # does not pass. This preserves the existing Fast behavior for
            # candidates that are not quite early enough.
            passed, reasons = evaluate_fast_sniper_filters(
                token, rule_params
            )
            reasons = list(early_reasons) + list(reasons)
            score_result = compute_score(
                token, rule_params, settings.creator_watchlist
            )
            logger.info(
                "fast_sniper_rule_evaluation",
                extra={
                    "mint": token.mint,
                    "rule_id": rule.id,
                    "score_telemetry": score_result.score,
                    "market_cap_usd": token.market_cap_usd,
                    "liquidity_usd": token.liquidity_usd,
                    "age_seconds": token.age_seconds,
                    "passed": passed,
                    "early_passed": early_passed,
                    "early_reasons": early_reasons,
                    "reasons": reasons,
                },
            )
            if not passed:
                logger.info(
                    "fast_sniper_rejected",
                    extra={
                        "mint": token.mint,
                        "rule_id": rule.id,
                        "reasons": reasons,
                    },
                )
                return False

            metrics.tokens_qualified += 1
            await self._notifier.new_qualified_token(
                rule.created_by,
                token.ticker_symbol or token.mint[:8],
                token.mint,
                score_result.score,
                token.source,
            )
            logger.info(
                "fast_sniper_buy_dispatch",
                extra={
                    "mint": token.mint,
                    "rule_id": rule.id,
                    "score_telemetry": score_result.score,
                    "entry_mode": "standard_fast",
                },
            )
            return await self._maybe_trade(token, rule, score_result)

        # Evaluate the normal hard filters before scoring/late-entry checks.
        # ``passed`` and ``reasons`` are used by the telemetry, persistence,
        # and hard-filter rejection path below.
        passed, reasons = evaluate_hard_filters(
            token,
            rule_params,
        )

        score_result = compute_score(
            token,
            rule_params,
            settings.creator_watchlist,
        )

        late_passed, late_reasons, late_breakdown = evaluate_late_entry(
            token,
            rule_params,
        )
        if not late_passed:
            reasons.extend(late_reasons)

        # ------------------------------------------------------------------
        # DETAILED RULE-EVALUATION TELEMETRY
        #
        # Keep the trading logic unchanged. These logs make it possible to
        # see exactly why every candidate was accepted/rejected and, most
        # importantly for fast Pump.fun launches, which market-cap/liquidity/
        # holder/age values the rule engine actually evaluated.
        # ------------------------------------------------------------------
        def _param(name, default=None):
            return getattr(rule_params, name, default)

        evaluation_log = {
            "mint": token.mint,
            "ticker": (
                getattr(token, "ticker_symbol", None)
                or token.mint[:8]
            ),
            "source": token.source,
            "rule_id": rule.id,

            # Actual token data seen by the rule engine.
            "market_cap_usd": float(
                getattr(token, "market_cap_usd", 0.0) or 0.0
            ),
            "liquidity_usd": float(
                getattr(token, "liquidity_usd", 0.0) or 0.0
            ),
            "holders": getattr(token, "holders", None),
            "price_usd": float(
                getattr(token, "price_usd", 0.0) or 0.0
            ),
            "age_seconds": float(
                getattr(token, "age_seconds", 0.0) or 0.0
            ),
            "created_on": getattr(token, "created_on", None),

            # Rule values supplied by /setrule.
            "rule_min_liquidity_usd": _param(
                "min_liquidity_usd"
            ),
            "rule_min_holders": _param(
                "min_holders"
            ),
            "rule_max_age_seconds": _param(
                "max_age_seconds"
            ),
            "rule_min_market_cap_usd": _param(
                "min_market_cap_usd"
            ),
            "rule_max_market_cap_usd": _param(
                "max_market_cap_usd"
            ),
            "rule_bonding_curve_phase": _param(
                "bonding_curve_phase"
            ),
            "rule_creator_allowlist": _param(
                "creator_allowlist"
            ),
            "rule_creator_denylist": _param(
                "creator_denylist"
            ),

            # Results of the two separate qualification layers.
            "hard_filters_passed": passed,
            "hard_filter_reasons": reasons,
            "score": score_result.score,
            "qualification_threshold": (
                rule_params.qualify_score_threshold
            ),
            "score_passed": (
                score_result.score
                >= rule_params.qualify_score_threshold
            ),
            "creator_match": score_result.creator_match,
            "score_breakdown": score_result.breakdown,
            "late_entry_passed": late_passed,
            "late_entry_reasons": late_reasons,
            "late_entry_breakdown": late_breakdown,
        }

        # IMPORTANT: Railway's current log formatter only renders the
        # message text, not logging ``extra`` fields. Keep ``extra`` for
        # structured logging, but also put the diagnostic fields directly
        # into the message so they are visible in Railway logs.
        evaluation_message = (
            "rule_evaluation "
            + json.dumps(
                evaluation_log,
                default=str,
                separators=(",", ":"),
                sort_keys=True,
            )
        )

        logger.info(
            evaluation_message,
            extra=evaluation_log,
        )

        await repo.save_screening_result(
            token.mint,
            passed,
            score_result.score,
            reasons,
            token.liquidity_usd,
            token.holders,
            token.market_cap_usd,
            score_result.creator_match,
            {
                "source": token.source,
                "breakdown": (
                    score_result.breakdown
                ),
                "rule_id": rule.id,
            },
        )

        if not passed:

            await guardian.record("rejected", owner_id=rule.created_by, reason="hard_filter")
            rejection_log = {
                "mint": token.mint,
                "rule_id": rule.id,
                "source": token.source,
                "market_cap_usd": getattr(
                    token, "market_cap_usd", 0.0
                ),
                "liquidity_usd": getattr(
                    token, "liquidity_usd", 0.0
                ),
                "holders": getattr(
                    token, "holders", None
                ),
                "age_seconds": getattr(
                    token, "age_seconds", 0.0
                ),
                "reasons": reasons,
            }

            logger.info(
                "rule_rejected_hard_filter "
                + json.dumps(
                    rejection_log,
                    default=str,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                extra=rejection_log,
            )

            if notify_on_fail:

                await self._notifier.rule_violation(
                    rule.created_by,
                    (
                        token.ticker_symbol
                        or token.mint[:8]
                    ),
                    reasons,
                )

            return False

        if not late_passed:
            logger.info(
                "rule_rejected_late_entry "
                + json.dumps({
                    "mint": token.mint,
                    "rule_id": rule.id,
                    "source": token.source,
                    "reasons": late_reasons,
                    "late_entry": late_breakdown,
                }, default=str, separators=(",", ":"), sort_keys=True),
                extra={
                    "mint": token.mint,
                    "rule_id": rule.id,
                    "source": token.source,
                    "reasons": late_reasons,
                    "late_entry": late_breakdown,
                },
            )
            return False

        # The final qualification threshold is authoritative.
        # Smart-money confirmation is currently disabled, so a candidate
        # below the configured score threshold must not proceed to trading.
        if score_result.score < rule_params.qualify_score_threshold:
            await guardian.record("rejected", owner_id=rule.created_by, reason="score")
            score_rejection = {
                "mint": token.mint,
                "rule_id": rule.id,
                "source": token.source,
                "score": score_result.score,
                "threshold": rule_params.qualify_score_threshold,
                "market_cap_usd": getattr(token, "market_cap_usd", 0.0),
                "liquidity_usd": getattr(token, "liquidity_usd", 0.0),
                "holders": getattr(token, "holders", None),
                "age_seconds": getattr(token, "age_seconds", 0.0),
                "breakdown": score_result.breakdown,
            }
            logger.info(
                "rule_rejected_score " + json.dumps(
                    score_rejection, default=str, separators=(",", ":"), sort_keys=True
                ),
                extra=score_rejection,
            )
            return False

        passed_log = {
            "mint": token.mint,
            "rule_id": rule.id,
            "source": token.source,
            "score": score_result.score,
            "threshold": rule_params.qualify_score_threshold,
            "market_cap_usd": getattr(token, "market_cap_usd", 0.0),
            "liquidity_usd": getattr(token, "liquidity_usd", 0.0),
            "holders": getattr(token, "holders", None),
            "age_seconds": getattr(token, "age_seconds", 0.0),
            "breakdown": score_result.breakdown,
        }
        logger.info(
            "rule_passed " + json.dumps(
                passed_log, default=str, separators=(",", ":"), sort_keys=True
            ),
            extra=passed_log,
        )

        logger.info(
            "token_qualified_candidate",
            extra={
                "mint": token.mint,
                "rule_id": rule.id,
                "score": score_result.score,
                "source": token.source,
            },
        )

        # --------------------------------------------------------------
        # Smart-money filter DISABLED
        # --------------------------------------------------------------
        # Smart-money addresses have not been configured yet. Do not query
        # Solana Tracker, do not require a smart-money signal, and do not
        # modify the score. Once the normal hard filters and score threshold
        # pass, the token proceeds directly to qualification and trading.
        # --------------------------------------------------------------
        metrics.tokens_qualified += 1
        await guardian.record("qualified", owner_id=rule.created_by, mint=token.mint, score=score_result.score)

        await self._notifier.new_qualified_token(
            rule.created_by,
            token.ticker_symbol or token.mint[:8],
            token.mint,
            score_result.score,
            token.source,
        )

        execution_log = {
            "mint": token.mint,
            "rule_id": rule.id,
            "source": token.source,
            "score": score_result.score,
            "threshold": rule_params.qualify_score_threshold,
            "action": "buy_dispatch",
        }
        logger.info(
            "qualified_buy_dispatch " + json.dumps(
                execution_log, default=str, separators=(",", ":"), sort_keys=True
            ),
            extra=execution_log,
        )

        return await self._maybe_trade(
            token,
            rule,
            score_result,
        )

    # ------------------------------------------------------------------
    # Pending launches
    # ------------------------------------------------------------------

    async def _process_watched_wallet_pending(
        self,
        active_rules: list,
    ):

        now = datetime.now(
            timezone.utc
        )

        fallback_max_age = 3600

        for mint in list(
            self._pending_watch.keys()
        ):

            watch = (
                self._pending_watch[
                    mint
                ]
            )

            first_seen = watch[
                "first_seen"
            ]

            source = watch[
                "source"
            ]

            metadata = watch.get(
                "metadata",
                {},
            )

            age_seconds = (
                now
                - first_seen
            ).total_seconds()

            if not active_rules:

                if (
                    age_seconds
                    > fallback_max_age
                ):

                    del self._pending_watch[
                        mint
                    ]

                continue

            due_rules = []
            for rule in active_rules:
                if not _rule_matches_source(rule, source):
                    continue
                # Pump.fun has three independent per-admin lanes: Fast, Smart, Smart Money.
                if source == SOURCE_PUMPFUN:
                    state = await repo.get_or_create_bot_state(rule.created_by)
                    if not state.trading_enabled or not state.pumpfun_trading_enabled:
                        continue
                    strategy = _rule_strategy(rule)
                    is_smart_money = bool(watch.get("smart_money"))
                    if is_smart_money:
                        assigned = await repo.get_active_smart_money_rule(rule.created_by)
                        if not assigned or assigned.id != rule.id or not getattr(state, "smart_money_copy_enabled", False):
                            continue
                    elif strategy == "fast" and not getattr(state, "pumpfun_fast_enabled", False):
                        continue
                    elif strategy == "smart" and not getattr(state, "pumpfun_smart_enabled", True):
                        continue
                    # Smart-money copy is event-driven: do not apply the normal
                    # rule age window. The tracked wallet already made the buy,
                    # so give the copy path enough time to build the on-chain
                    # snapshot even when RPC data is temporarily delayed.
                    if is_smart_money:
                        effective_age = fallback_max_age
                    else:
                        effective_age = min(rule.max_age_seconds, 2) if strategy == "fast" else rule.max_age_seconds
                else:
                    effective_age = rule.max_age_seconds
                if age_seconds <= effective_age:
                    due_rules.append(rule)

            if not due_rules:

                del self._pending_watch[
                    mint
                ]

                for rule in active_rules:

                    self._notified_fail.discard(
                        (
                            mint,
                            rule.id,
                        )
                    )

                continue

            token = (
                await self._build_onchain_snapshot(
                    mint,
                    source,
                    metadata,
                    first_seen,
                )
            )

            if token is None:

                # Keep it pending.
                #
                # This is particularly important for Pump.fun because
                # the bonding curve may not be immediately readable on
                # the first RPC request after launch.

                continue

            await repo.save_token(
                token
            )

            # ----------------------------------------------------------
            # FAST SNIPER HOT PATH
            # ----------------------------------------------------------
            # Process Fast rules immediately from the first bonding-curve
            # snapshot. Do NOT call Helius holder enrichment or the Smart
            # quality gate before this path.
            if watch.get("smart_money"):
                smart_money_rules = list(due_rules)
                fast_rules = []
                smart_rules = []
            else:
                smart_money_rules = []
                fast_rules = [r for r in due_rules if _rule_strategy(r) == "fast"]
                smart_rules = [r for r in due_rules if _rule_strategy(r) != "fast"]

            fast_settled = True
            if source == SOURCE_PUMPFUN and fast_rules:
                for rule in fast_rules:
                    key = (mint, rule.id)
                    done = await self._screen_and_maybe_trade(
                        token, rule, notify_on_fail=False
                    )
                    self._notified_fail.add(key)
                    if done:
                        self._notified_fail.discard(key)
                    else:
                        fast_settled = False

            # ----------------------------------------------------------
            # SMART MONEY COPY PATH
            # ----------------------------------------------------------
            if smart_money_rules:
                for rule in smart_money_rules:
                    logger.info(
                        "smart_money_rule_evaluation",
                        extra={
                            "mint": mint,
                            "rule_id": rule.id,
                            "wallet": metadata.get("smart_money_wallet"),
                            "tx_signature": metadata.get("smart_money_tx_signature"),
                        },
                    )
                    done = await self._screen_and_maybe_trade(token, rule, notify_on_fail=False)
                    logger.info(
                        "smart_money_rule_result",
                        extra={
                            "mint": mint, "rule_id": rule.id, "done": done,
                            "wallet": metadata.get("smart_money_wallet"),
                            "tx_signature": metadata.get("smart_money_tx_signature"),
                        },
                    )

            # ----------------------------------------------------------
            # SMART FILTER PATH
            # ----------------------------------------------------------
            # Only the Smart lane pays the holder-enrichment/quality-gate
            # latency cost.
            if smart_rules:
                token = await self._enrich_holders(token)

            # Apply the Pump.fun launch-quality score after holder enrichment.
            # Unlike the previous 5k/10-holder/50x hard gate, this score lets
            # decent early launches through while filtering the weakest
            # launches. Temporary missing snapshot data is deferred rather
            # than permanently discarded.
            if smart_rules and not watch.get("smart_money"):
                (
                    quality_status,
                    quality_score,
                    quality_reasons,
                    quality_breakdown,
                ) = self._pumpfun_quality_gate(token)
            else:
                quality_status, quality_score, quality_reasons, quality_breakdown = ("pass", 100.0, [], {})

            if smart_rules and not watch.get("smart_money") and quality_status == "defer":
                # Keep the token in _pending_watch for another snapshot.
                continue

            if smart_rules and not watch.get("smart_money") and quality_status == "reject":
                for rule in smart_rules:
                    await repo.save_screening_result(
                        token.mint,
                        False,
                        quality_score,
                        quality_reasons,
                        token.liquidity_usd,
                        token.holders,
                        token.market_cap_usd,
                        False,
                        {
                            "source": token.source,
                            "rule_id": rule.id,
                            "pumpfun_quality_gate": True,
                            "pumpfun_quality_score": quality_score,
                            "pumpfun_quality_breakdown": quality_breakdown,
                        },
                    )

                logger.info(
                    "pumpfun_quality_gate_rejected",
                    extra={
                        "mint": token.mint,
                        "score": quality_score,
                        "breakdown": quality_breakdown,
                        "reasons": quality_reasons,
                    },
                )

                del self._pending_watch[mint]
                for rule in active_rules:
                    self._notified_fail.discard((mint, rule.id))
                continue

            all_settled = fast_settled

            for rule in smart_rules:

                key = (
                    mint,
                    rule.id,
                )

                done = (
                    await self._screen_and_maybe_trade(
                        token,
                        rule,
                        # Pump.fun is intentionally silent for rejected
                        # tokens. Only qualified Pump.fun launches should
                        # reach Telegram. Keep the existing Anoncoin
                        # rejection notifications unchanged.
                        notify_on_fail=(
                            source != SOURCE_PUMPFUN
                            and key
                            not in self._notified_fail
                        ),
                    )
                )

                self._notified_fail.add(
                    key
                )

                if done:

                    self._notified_fail.discard(
                        key
                    )

                else:

                    all_settled = False

            if all_settled:

                del self._pending_watch[
                    mint
                ]

                for rule in active_rules:

                    self._notified_fail.discard(
                        (
                            mint,
                            rule.id,
                        )
                    )

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    async def scan_once(
        self,
    ):

        active_rules = (
            await repo.get_all_active_rules()
        )

        await self._watch_wallets_for_new_mints()

        await self._process_watched_wallet_pending(
            active_rules
        )

        # Existing Anoncoin API discovery / mock feed.
        for token in await self._fetch_new_tokens():

            if await repo.token_already_seen(
                token.mint
            ):
                logger.debug(
                    "anoncoin_discovery_duplicate",
                    extra={"mint": token.mint},
                )
                continue

            logger.info(
                "anoncoin_legacy_candidate_discovered",
                extra={"mint": token.mint, "source": SOURCE_ANONCOIN},
            )

            await repo.save_token(
                token
            )

            metrics.tokens_scanned += 1

            token = (
                await self._enrich_holders(
                    token
                )
            )

            for rule in active_rules:
                if not _rule_matches_source(rule, token.source):
                    continue
                await self._screen_and_maybe_trade(
                    token,
                    rule,
                    notify_on_fail=True,
                )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run_forever(
        self,
    ):
        """Run Pump.fun polling quickly without hammering Anoncoin."""
        last_anoncoin_scan = 0.0

        while True:
            try:
                await guardian.tick()
                active_rules = await repo.get_all_active_rules()

                await self._watch_wallets_for_new_mints()
                await self._process_watched_wallet_pending(active_rules)

                now = time.monotonic()
                if now - last_anoncoin_scan >= settings.scan_interval_seconds:
                    for token in await self._fetch_new_tokens():
                        if await repo.token_already_seen(token.mint):
                            logger.debug(
                                "anoncoin_discovery_duplicate",
                                extra={"mint": token.mint},
                            )
                            continue
                        logger.info(
                            "anoncoin_legacy_candidate_discovered",
                            extra={"mint": token.mint, "source": SOURCE_ANONCOIN},
                        )
                        await repo.save_token(token)
                        metrics.tokens_scanned += 1
                        token = await self._enrich_holders(token)
                        for rule in active_rules:
                            if not _rule_matches_source(rule, token.source):
                                continue
                            await self._screen_and_maybe_trade(
                                token, rule, notify_on_fail=True
                            )
                    last_anoncoin_scan = now

            except Exception as exc:
                metrics.error_count += 1
                await guardian.record("scanner_error", error=f"{type(exc).__name__}: {exc}")
                logger.exception("scan_cycle_failed")
                await self._notifier.api_error("scanner", str(exc))

            await asyncio.sleep(settings.pumpfun_scan_interval_seconds)

    # ------------------------------------------------------------------
    # Daily summary
    # ------------------------------------------------------------------

    async def daily_summary_loop(
        self,
    ):

        while True:

            await asyncio.sleep(
                300
            )

            try:

                await (
                    self._maybe_send_daily_summary()
                )

            except Exception:

                logger.exception(
                    "daily_summary_failed"
                )

    async def _maybe_send_daily_summary(
        self,
    ):

        state = (
            await repo.get_or_create_bot_state()
        )

        now = datetime.now(
            timezone.utc
        )

        already_sent_today = (
            state.last_daily_summary_at
            is not None
            and (
                state.last_daily_summary_at
                .astimezone(
                    timezone.utc
                )
                .date()
                == now.date()
            )
        )

        if (
            now.hour
            != settings.daily_summary_hour_utc
            or already_sent_today
        ):
            return

        closed = (
            await repo.get_closed_positions()
        )

        wins = sum(
            1
            for position in closed
            if position.realized_pnl_usd
            > 0
        )

        total_pnl = sum(
            position.realized_pnl_usd
            for position in closed
        )

        win_rate = (
            wins
            / len(closed)
            * 100
            if closed
            else 0.0
        )

        text = (
            f"Tokens scanned: "
            f"{metrics.tokens_scanned}\n"
            f"Tokens qualified: "
            f"{metrics.tokens_qualified}\n"
            f"Trades placed: "
            f"{metrics.trades_placed}\n"
            f"Win rate: "
            f"{win_rate:.1f}%\n"
            f"Total realized PnL: "
            f"${total_pnl:.2f}\n"
            f"Errors: "
            f"{metrics.error_count}"
        )

        await self._notifier.daily_summary(
            text
        )

        await repo.update_bot_state(
            last_daily_summary_at=now
        )
