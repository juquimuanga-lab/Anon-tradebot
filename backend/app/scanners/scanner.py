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
from datetime import datetime, timedelta, timezone

from app.config.settings import settings
from app.connectors.solana_tracker import (
    SolanaTrackerClient,
    SmartMoneySignal,
)

from app.connectors.anoncoin import (
    AnoncoinAPIError,
    AnoncoinClient,
)

from app.connectors.helius import (
    HeliusAPIError,
    HeliusClient,
)

from app.execution.base import OrderResult

from app.execution.onchain import (
    meteora_dbc,
    pumpfun,
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
SOURCE_MOCK = "mock_simulated"


def _is_anoncoin_source(source: str) -> bool:
    """True for any Anoncoin-origin source tag.

    Covers both the on-chain watcher tag (anoncoin_onchain) and the
    legacy Anoncoin-API discovery tag (anoncoin), so the
    anoncoin_trading_enabled toggle applies no matter which path
    produced the token.
    """

    return bool(source) and source.startswith("anoncoin")


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

        # Phase 1 smart-money telemetry. This is intentionally read-only
        # and is queried only after the existing qualification threshold
        # is passed. It never gates the existing trade decision.
        self._smart_money = SolanaTrackerClient()

        self._watermarks = (
            onchain_watcher.WatermarkStore()
        )

        self._pending_watch: dict[
            str,
            dict,
        ] = {}

        # Prevent repeated snapshot RPC calls/warnings for a mint while its
        # newly-created Pump.fun accounts are still propagating.
        self._snapshot_retry: dict[
            str,
            dict,
        ] = {}

        # Prevent duplicate discovery items from being processed more than
        # once in the same scan cycle.
        self._scan_seen_mints: set[str] = set()

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
                    limit=20,
                )
            )

            return [
                from_anoncoin_coin(coin)
                for coin in raw_coins
            ]

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

            # The websocket/polling layer can return the same launch multiple
            # times. De-duplicate before any state/logging work.
            if mint in self._scan_seen_mints:
                continue
            self._scan_seen_mints.add(mint)

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

            logger.info(
                "pumpfun_new_mint_detected",
                extra={
                    "mint": mint,
                    "creator": item.get(
                        "creator"
                    ),
                    "source": SOURCE_PUMPFUN,
                    "tx_signature": item.get(
                        "tx_signature"
                    ),
                },
            )

    # ------------------------------------------------------------------
    # Watch all on-chain sources
    # ------------------------------------------------------------------

    async def _watch_wallets_for_new_mints(
        self,
    ):
        """Poll both supported launch sources."""

        await self._watch_anoncoin_for_new_mints()

        await self._watch_pumpfun_for_new_mints()

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
    # Pump.fun snapshot
    # ------------------------------------------------------------------

    async def _build_pumpfun_snapshot(
        self,
        mint: str,
        metadata: dict,
        first_seen: datetime,
    ) -> TokenSnapshot | None:
        """Build a TokenSnapshot from Pump.fun's bonding curve."""

        # Newly launched Pump.fun accounts can take a short time to become
        # readable from the configured RPC. Avoid hammering the same mint and
        # emitting the same warning on every scan cycle.
        now = datetime.now(timezone.utc)
        retry_state = self._snapshot_retry.get(mint)
        if retry_state and now < retry_state["next_retry"]:
            return None

        try:

            info = (
                await pumpfun.get_pool_info(
                    mint,
                    settings.solana_rpc_url,
                    commitment="processed",
                )
            )

            self._snapshot_retry.pop(mint, None)

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
                state = self._snapshot_retry.get(
                    mint,
                    {"attempts": 0},
                )
                attempts = int(state.get("attempts", 0)) + 1
                delay = min(5.0, 0.25 * (2 ** min(attempts - 1, 4)))

                self._snapshot_retry[mint] = {
                    "attempts": attempts,
                    "next_retry": (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=delay)
                    ),
                }

                logger.warning(
                    (
                        "pumpfun_snapshot_account_not_ready "
                        f"mint={mint} "
                        f"retry_in={delay:.2f}s "
                        f"attempt={attempts}"
                    ),
                    extra={
                        "mint": mint,
                        "error": str(exc),
                        "retry_in_seconds": delay,
                        "attempt": attempts,
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
    # Phase 1 smart-money telemetry
    # ------------------------------------------------------------------

    async def _get_smart_money_signal(
        self,
        token,
        first_seen: datetime | None = None,
    ) -> SmartMoneySignal:
        """Inspect tracked-wallet activity for an already-qualified token.

        This is deliberately called AFTER hard filters and the qualification
        score pass. A failure here returns an empty signal and never changes
        the existing trading path.
        """
        if token.source != SOURCE_PUMPFUN:
            return SmartMoneySignal(detected=False)

        if not settings.smart_money_enabled:
            return SmartMoneySignal(detected=False)

        if not self._smart_money.enabled:
            logger.info(
                "smart_money_disabled_or_unconfigured",
                extra={"mint": token.mint},
            )
            return SmartMoneySignal(detected=False)

        observed_at = (
            first_seen
            or getattr(token, "created_on", None)
            or datetime.now(timezone.utc)
        )

        try:
            signal = await self._smart_money.find_smart_money(
                token.mint,
                first_seen_timestamp=observed_at.timestamp(),
            )
        except Exception as exc:
            logger.exception(
                "smart_money_check_failed",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )
            return SmartMoneySignal(detected=False)

        if signal.detected:
            logger.info(
                "smart_money_signal_detected",
                extra={
                    "mint": token.mint,
                    "score": signal.score,
                    "wallet_count": signal.wallet_count,
                    "wallets": [
                        trade.wallet
                        for trade in signal.trades[:5]
                    ],
                    "buys_usd": [
                        trade.amount_usd
                        for trade in signal.trades[:5]
                    ],
                    "latencies_seconds": [
                        trade.seconds_after_seen
                        for trade in signal.trades[:5]
                    ],
                },
            )
        else:
            logger.info(
                "smart_money_no_signal",
                extra={"mint": token.mint},
            )

        return signal

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
            await repo.get_or_create_bot_state()
        )

        if not state.trading_enabled:
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

        amount_sol = min(
            rule_row.max_buy_size_sol,
            (
                state.paper_balance_sol
                if state.mode == "paper"
                else rule_row.max_buy_size_sol
            ),
        )

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
                amount_sol,
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
                "amount_sol": amount_sol,
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
            amount_sol,
            state.mode,
        )

        try:

            result = await asyncio.wait_for(
                adapter.buy(
                    token,
                    amount_sol,
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

            fill_price = (
                result.price_usd
                or token.price_usd
            )

            amount_tokens = (
                amount_sol
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
                amount_sol,
                owner_user_id=(
                    rule_row.created_by
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

        passed, reasons = (
            evaluate_hard_filters(
                token,
                rule_params,
            )
        )

        score_result = compute_score(
            token,
            rule_params,
            settings.creator_watchlist,
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

        if (
            score_result.score
            < settings.qualify_score_threshold
        ):
            return False

        metrics.tokens_qualified += 1

        logger.info(
            "token_qualified",
            extra={
                "mint": token.mint,
                "rule_id": rule.id,
                "score": score_result.score,
                "source": token.source,
            },
        )

        # --------------------------------------------------------------
        # Phase 1 Smart Money
        #
        # IMPORTANT:
        # - Existing qualification has already passed.
        # - Smart money is telemetry only in Phase 1.
        # - It does NOT gate or alter the BUY decision.
        # - It is only queried for Pump.fun.
        # --------------------------------------------------------------
        smart_money_signal = (
            await self._get_smart_money_signal(
                token,
                first_seen=token.created_on,
            )
        )

        await self._notifier.new_qualified_token(
            rule.created_by,
            (
                token.ticker_symbol
                or token.mint[:8]
            ),
            token.mint,
            score_result.score,
            token.source,
        )

        # Keep the existing Telegram notifier call untouched until its
        # implementation is provided. Smart-money data is available in
        # structured logs for Phase 1 and can be rendered by notifier.py /
        # telegram_app.py without risking the working trade path.
        logger.info(
            "qualified_token_smart_money_summary",
            extra={
                "mint": token.mint,
                "rule_id": rule.id,
                "rule_score": score_result.score,
                "smart_money_detected": (
                    smart_money_signal.detected
                ),
                "smart_money_score": (
                    smart_money_signal.score
                ),
                "smart_money_wallet_count": (
                    smart_money_signal.wallet_count
                ),
            },
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
                    self._snapshot_retry.pop(mint, None)

                continue

            due_rules = [
                rule
                for rule in active_rules
                if age_seconds
                <= rule.max_age_seconds
            ]

            if not due_rules:

                del self._pending_watch[
                    mint
                ]
                self._snapshot_retry.pop(mint, None)

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

            token = (
                await self._enrich_holders(
                    token
                )
            )

            all_settled = True

            for rule in due_rules:

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
                self._snapshot_retry.pop(mint, None)

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

        # This set is only for the current polling cycle.
        self._scan_seen_mints.clear()

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
                continue

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

        while True:

            try:

                await self.scan_once()

            except Exception as exc:

                metrics.error_count += 1

                logger.exception(
                    "scan_cycle_failed"
                )

                await self._notifier.api_error(
                    "scanner",
                    str(exc),
                )

            await asyncio.sleep(
                settings.scan_interval_seconds
            )

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
