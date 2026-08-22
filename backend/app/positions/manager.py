"""Monitors open positions and triggers automated exits.

Exit rules are evaluated against the exact rule attached to the position
when it was opened.

Take-profit examples:

    60:60
        At +60% PnL, sell 60% of the original position.

    60:60,100:40
        At +60% PnL, sell 60% of the original position.
        At +100% PnL, sell the remaining 40%.

Safety rules:

- Live positions must use a real market price for TP/SL/trailing decisions.
- Simulated/stale prices must never trigger real-money price exits.
- Simulated volume must never trigger real-money volume exits.
- A TP level is only marked as hit after the sell succeeds.
- Failed sells leave the TP level pending.
- Live positions are reconciled against the actual wallet token balance.
- Manual/external wallet sales close or reduce the tracked position.
- Only one exit can execute for a position at a time.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.config.settings import settings
from app.connectors.anoncoin import AnoncoinClient
from app.execution.base import OrderResult
from app.execution.onchain.solana_rpc import (
    SolanaTxError,
    extract_wallet_sell_execution,
    get_token_balance,
    get_transaction_details,
)
from app.execution.price_source import (
    get_current_price_usd,
    get_current_volume_usd,
)
from app.scanners.price_feed import (
    get_sol_usd_price,
)
from app.execution.router import ExecutionRouter
from app.scoring.rules import RuleParams, TakeProfitLevel, TokenSnapshot
from app.storage import repository as repo


logger = logging.getLogger(
    "app.positions.manager"
)


# If the wallet balance is within this percentage of the expected balance,
# do not treat tiny RPC/token-account differences as an external sale.
WALLET_RECONCILIATION_TOLERANCE_PCT = 0.5

# Flag stop-loss fills that execute materially worse than the price that
# triggered the exit. This is diagnostic only: once a transaction is
# submitted, the manager cannot undo an on-chain fill.
CATASTROPHIC_STOP_EXECUTION_GAP_PCT = 20.0


class PositionManager:
    def __init__(
        self,
        notifier,
        anoncoin: AnoncoinClient,
        execution_router: ExecutionRouter,
    ):
        self._notifier = notifier
        self._anoncoin = anoncoin
        self._execution_router = execution_router
        self._tick = 0

        # Prevent two exits for the same position from running concurrently.
        #
        # Example:
        #
        #   TP triggers
        #       +
        #   stop loss/manual close fires at nearly the same time
        #
        # Only the first exit is allowed to execute.
        self._exit_in_progress: set[int] = set()

        # Failed automated exits should not hammer the same position every
        # monitoring tick. Manual closes are unaffected.
        self._exit_retry_after: dict[int, float] = {}

        # Cache wallet public keys so we don't have to resolve the wallet
        # through the execution router on every 5-second monitoring cycle.
        self._wallet_pubkeys: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Rule loading
    # ------------------------------------------------------------------

    async def _rule_for_position(
        self,
        position,
    ) -> RuleParams:
        """Load the exact rule that created this position.

        We intentionally use position.rule_id rather than the admin's
        currently active rule.

        Changing an admin's rule therefore does not retroactively change
        positions that are already open.
        """

        if position.rule_id:
            rules = await repo.get_all_rules()

            for rule in rules:

                if rule.id == position.rule_id:

                    from app.storage.repository import (
                        rule_row_to_params,
                    )

                    return rule_row_to_params(rule)

        logger.warning(
            "position_has_no_rule",
            extra={
                "position_id": position.id,
                "mint": position.mint,
            },
        )

        return RuleParams()

    # ------------------------------------------------------------------
    # PnL
    # ------------------------------------------------------------------

    def _pnl_pct(
        self,
        entry_price: float,
        current_price: float,
    ) -> float:
        """Calculate percentage gain/loss from entry."""

        if entry_price <= 0:
            return 0.0

        return (
            (current_price - entry_price)
            / entry_price
            * 100
        )

    # ------------------------------------------------------------------
    # Sell percentage
    # ------------------------------------------------------------------

    def _sellable_pct(
        self,
        position,
        requested_sell_pct: float,
    ) -> float:
        """Return the percentage of the original position that can be sold."""

        remaining_pct = max(
            0.0,
            float(
                position.remaining_pct or 0.0
            ),
        )

        requested_pct = max(
            0.0,
            float(
                requested_sell_pct or 0.0
            ),
        )

        return min(
            requested_pct,
            remaining_pct,
        )

    # ------------------------------------------------------------------
    # Wallet reconciliation
    # ------------------------------------------------------------------

    async def _get_wallet_pubkey(
        self,
        position,
    ) -> str | None:
        """Resolve and cache the live wallet public key for a position."""

        if position.mode != "live":
            return None

        owner_user_id = (
            position.owner_user_id
        )

        if owner_user_id is None:
            logger.warning(
                "live_position_missing_wallet_owner",
                extra={
                    "position_id": position.id,
                    "mint": position.mint,
                },
            )
            return None

        cached = self._wallet_pubkeys.get(
            owner_user_id
        )

        if cached:
            return cached

        try:

            adapter = await (
                self._execution_router.get_adapter(
                    position.mode,
                    owner_user_id,
                    source=position.source,
                )
            )

        except Exception as exc:

            logger.warning(
                "wallet_adapter_lookup_failed",
                extra={
                    "position_id": position.id,
                    "owner_user_id": owner_user_id,
                    "error": str(exc),
                },
            )

            return None

        # WalletExecutionAdapter intentionally keeps the public key in
        # memory as _pubkey. No private key is exposed here.
        pubkey = getattr(
            adapter,
            "_pubkey",
            None,
        )

        if not pubkey:
            logger.warning(
                "live_wallet_public_key_unavailable",
                extra={
                    "position_id": position.id,
                    "owner_user_id": owner_user_id,
                },
            )
            return None

        self._wallet_pubkeys[
            owner_user_id
        ] = str(pubkey)

        return str(pubkey)

    async def _reconcile_live_position(
        self,
        position,
    ) -> bool:
        if position.source == "fourmeme":
            # BSC positions are reconciled by the Four.meme adapter on sell;
            # the Solana SPL balance reconciler must never touch an EVM token.
            return False
        """Reconcile a live position against the actual wallet balance.

        Returns:

            True:
                The position was changed/closed because the wallet balance
                differs materially from the tracked position.

            False:
                No reconciliation was necessary.

        IMPORTANT:

        We only reduce the tracked position.

        We never increase it based on wallet balance because the wallet may
        contain tokens acquired outside this bot.
        """

        if position.mode != "live":
            return False

        if position.amount_tokens <= 0:
            return False

        wallet_pubkey = await self._get_wallet_pubkey(
            position
        )

        if not wallet_pubkey:
            return False

        try:

            actual_balance = (
                await get_token_balance(
                    settings.solana_rpc_url,
                    wallet_pubkey,
                    position.mint,
                )
            )

        except SolanaTxError as exc:

            logger.warning(
                "wallet_token_reconciliation_failed",
                extra={
                    "position_id": position.id,
                    "mint": position.mint,
                    "error": str(exc),
                },
            )

            # Never close a position merely because the RPC lookup failed.
            return False

        except Exception as exc:

            logger.warning(
                "wallet_token_reconciliation_unexpected_error",
                extra={
                    "position_id": position.id,
                    "mint": position.mint,
                    "error": str(exc),
                },
            )

            return False

        original_amount = max(
            0.0,
            float(
                position.amount_tokens
            ),
        )

        tracked_remaining_pct = max(
            0.0,
            float(
                position.remaining_pct or 0.0
            ),
        )

        expected_balance = (
            original_amount
            * (
                tracked_remaining_pct
                / 100.0
            )
        )

        # Never increase a position based on an external wallet balance.
        if actual_balance >= expected_balance:
            return False

        # Ignore tiny differences caused by token/RPC rounding.
        if expected_balance > 0:

            difference_pct = (
                (
                    expected_balance
                    - actual_balance
                )
                / expected_balance
                * 100.0
            )

            if (
                difference_pct
                < WALLET_RECONCILIATION_TOLERANCE_PCT
            ):
                return False

        actual_remaining_pct = (
            actual_balance
            / original_amount
            * 100.0
        )

        actual_remaining_pct = max(
            0.0,
            min(
                actual_remaining_pct,
                tracked_remaining_pct,
            ),
        )

        # --------------------------------------------------------------
        # Wallet has no tokens left.
        #
        # This is the Phantom/manual-sale case from the user's screenshot.
        # --------------------------------------------------------------

        if actual_balance <= 0:

            await repo.update_position(
                position.id,
                status="closed",
                remaining_pct=0.0,
                closed_at=datetime.now(
                    timezone.utc
                ),
                close_reason=(
                    "position closed externally "
                    "from wallet"
                ),
            )

            position.remaining_pct = 0.0
            position.status = "closed"

            logger.info(
                "position_reconciled_external_close",
                extra={
                    "position_id": position.id,
                    "mint": position.mint,
                    "previous_remaining_pct": (
                        tracked_remaining_pct
                    ),
                    "actual_wallet_balance": 0.0,
                    "wallet": wallet_pubkey,
                },
            )

            return True

        # --------------------------------------------------------------
        # Wallet contains fewer tokens than the database expects.
        #
        # Example:
        #
        # Database: 100%
        # Wallet:    40%
        #
        # This means approximately 60% was sold externally.
        # --------------------------------------------------------------

        await repo.update_position(
            position.id,
            remaining_pct=(
                actual_remaining_pct
            ),
        )

        position.remaining_pct = (
            actual_remaining_pct
        )

        logger.info(
            "position_reconciled_external_partial_sell",
            extra={
                "position_id": position.id,
                "mint": position.mint,
                "previous_remaining_pct": (
                    tracked_remaining_pct
                ),
                "actual_remaining_pct": (
                    actual_remaining_pct
                ),
                "actual_wallet_balance": (
                    actual_balance
                ),
                "wallet": wallet_pubkey,
            },
        )

        return True

    # ------------------------------------------------------------------
    # Exit lock
    # ------------------------------------------------------------------

    def _try_begin_exit(
        self,
        position_id: int,
    ) -> bool:
        """Atomically claim an exit slot for a position."""

        if position_id in self._exit_in_progress:

            logger.debug(
                "position_exit_already_in_progress",
                extra={
                    "position_id": position_id,
                },
            )

            return False

        self._exit_in_progress.add(
            position_id
        )

        return True

    def _finish_exit(
        self,
        position_id: int,
    ) -> None:
        """Release the position exit lock."""

        self._exit_in_progress.discard(
            position_id
        )

    def _automated_exit_on_cooldown(
        self,
        position_id: int,
    ) -> bool:
        """Return True while a failed automated exit is cooling down."""
        retry_after = self._exit_retry_after.get(position_id)
        if retry_after is None:
            return False

        if time.monotonic() >= retry_after:
            self._exit_retry_after.pop(position_id, None)
            return False

        return True

    # ------------------------------------------------------------------
    # Sell execution reconciliation
    # ------------------------------------------------------------------

    async def _actual_sell_execution(
        self,
        position,
        token,
        result,
        trigger_price_usd: float,
    ) -> dict | None:
        """Reconcile a confirmed live SELL against its on-chain transaction.

        The trigger price is the price used by the position manager to decide
        that an exit condition was met. The transaction price is calculated
        independently from confirmed token/SOL balance deltas so we can see
        whether execution slippage or price impact caused a materially worse
        fill.
        """
        if (
            position.mode != "live"
            or not result.tx_signature
            or position.source == "fourmeme"
        ):
            return None

        wallet_pubkey = await self._get_wallet_pubkey(
            position
        )

        if not wallet_pubkey:
            return None

        transaction = await get_transaction_details(
            settings.solana_rpc_url,
            result.tx_signature,
        )

        execution = extract_wallet_sell_execution(
            transaction,
            wallet_pubkey,
            token.mint,
        )

        if not execution:
            logger.warning(
                "sell_execution_reconciliation_unavailable",
                extra={
                    "mint": token.mint,
                    "position_id": position.id,
                    "tx_signature": result.tx_signature,
                    "trigger_price_usd": trigger_price_usd,
                },
            )
            return None

        sold_tokens = (
            execution["token_sold_raw"]
            / (
                10 ** int(
                    execution["token_decimals"]
                    or 0
                )
            )
        )

        sol_received = (
            execution["sol_received_lamports"]
            / 1_000_000_000
        )

        sol_usd = 0.0

        try:
            sol_usd = float(
                await get_sol_usd_price(
                    settings.jupiter_price_url
                )
            )
        except Exception as exc:
            logger.warning(
                "sell_execution_sol_price_unavailable",
                extra={
                    "mint": token.mint,
                    "position_id": position.id,
                    "tx_signature": result.tx_signature,
                    "error": str(exc),
                },
            )

        actual_execution_price_usd = None

        if (
            sold_tokens > 0
            and sol_received > 0
            and sol_usd > 0
        ):
            actual_execution_price_usd = (
                sol_received
                * sol_usd
                / sold_tokens
            )

        trigger_vs_execution_pct = None

        if (
            actual_execution_price_usd is not None
            and trigger_price_usd > 0
        ):
            trigger_vs_execution_pct = (
                (
                    actual_execution_price_usd
                    - trigger_price_usd
                )
                / trigger_price_usd
                * 100.0
            )

        logger.info(
            "sell_execution_reconciled",
            extra={
                "mint": token.mint,
                "position_id": position.id,
                "tx_signature": result.tx_signature,
                "trigger_price_usd": trigger_price_usd,
                "actual_execution_price_usd": (
                    actual_execution_price_usd
                ),
                "trigger_vs_execution_pct": (
                    trigger_vs_execution_pct
                ),
                "sol_received": sol_received,
                "wallet_net_sol_change": (
                    execution[
                        "wallet_net_sol_change_lamports"
                    ]
                    / 1_000_000_000
                ),
                "fee_sol": (
                    execution["fee_lamports"]
                    / 1_000_000_000
                ),
                "tokens_sold": sold_tokens,
                "token_decimals": (
                    execution["token_decimals"]
                ),
                "sol_usd": sol_usd,
            },
        )

        return {
            "actual_execution_price_usd": (
                actual_execution_price_usd
            ),
            "sol_received": sol_received,
            "wallet_net_sol_change": (
                execution[
                    "wallet_net_sol_change_lamports"
                ]
                / 1_000_000_000
            ),
            "fee_sol": (
                execution["fee_lamports"]
                / 1_000_000_000
            ),
            "tokens_sold": sold_tokens,
            "token_decimals": (
                execution["token_decimals"]
            ),
            "trigger_price_usd": trigger_price_usd,
            "sol_usd": sol_usd,
        }

    async def _close_position(
        self,
        position,
        token: TokenSnapshot,
        current_price: float,
        sell_pct: float,
        reason: str,
        reconcile_wallet: bool = True,
    ) -> bool:
        """Execute a partial/full sell.

        Returns:

            True:
                Sell successfully executed.

            False:
                Sell failed or another exit is already in progress.

        A failed sell MUST NOT:

            - reduce remaining_pct
            - close the position
            - mark a TP as hit
        """

        if not self._try_begin_exit(
            position.id
        ):
            return False

        try:

            # ----------------------------------------------------------
            # Never attempt to sell something the wallet no longer owns.
            #
            # This catches manual Phantom sales even if reconciliation
            # happened between monitoring cycles.
            # ----------------------------------------------------------

            if position.mode == "live" and reconcile_wallet:

                reconciled = (
                    await self._reconcile_live_position(
                        position
                    )
                )

                if reconciled:

                    if (
                        position.status == "closed"
                        or float(
                            position.remaining_pct
                            or 0.0
                        ) <= 0.01
                    ):
                        logger.info(
                            "sell_cancelled_position_already_closed_externally",
                            extra={
                                "position_id": (
                                    position.id
                                ),
                                "mint": token.mint,
                                "reason": reason,
                            },
                        )

                        return False

                    # Position was partially sold externally.
                    # Recalculate the requested amount against the new
                    # remaining percentage.
                    sell_pct = self._sellable_pct(
                        position,
                        sell_pct,
                    )

            sell_pct = self._sellable_pct(
                position,
                sell_pct,
            )

            if sell_pct <= 0:

                logger.info(
                    "sell_skipped_no_remaining_position",
                    extra={
                        "mint": token.mint,
                        "position_id": position.id,
                        "reason": reason,
                        "remaining_pct": (
                            position.remaining_pct
                        ),
                    },
                )

                return False

            adapter = await (
                self._execution_router.get_adapter(
                    position.mode,
                    position.owner_user_id,
                    source=position.source,
                )
            )

            amount_to_sell = (
                position.amount_tokens
                * (
                    sell_pct
                    / 100.0
                )
            )

            logger.info(
                "sell_attempt",
                extra={
                    "mint": token.mint,
                    "position_id": position.id,
                    "sell_pct": sell_pct,
                    "remaining_pct_before": (
                        position.remaining_pct
                    ),
                    "reason": reason,
                    "mode": position.mode,
                    "entry_price_usd": position.entry_price_usd,
                    "trigger_price_usd": current_price,
                    "trigger_pnl_pct": self._pnl_pct(
                        position.entry_price_usd,
                        current_price,
                    ),
                    "amount_tokens": amount_to_sell,
                    "wallet_reconciliation_skipped": not reconcile_wallet,
                },
            )

            try:

                # Pump.fun stop-loss/trailing exits need to preserve the
                # configured slippage ceiling.  The Pump.fun live adapter
                # supports an optional exit_reason so it can distinguish
                # risk-control exits from normal TP/manual sells.  Keep the
                # base ExecutionAdapter contract unchanged for all other
                # execution sources.
                if (
                    position.mode == "live"
                    and position.source == "pumpfun"
                ):
                    result = await asyncio.wait_for(
                        adapter.sell(
                            token,
                            amount_to_sell,
                            sell_pct,
                            exit_reason=reason,
                        ),
                        timeout=(
                            settings.execution_timeout_seconds
                        ),
                    )
                else:
                    result = await asyncio.wait_for(
                        adapter.sell(
                            token,
                            amount_to_sell,
                            sell_pct,
                        ),
                        timeout=(
                            settings.execution_timeout_seconds
                        ),
                    )

            except asyncio.TimeoutError:

                logger.error(
                    "sell_execution_timeout",
                    extra={
                        "mint": token.mint,
                        "position_id": position.id,
                        "sell_pct": sell_pct,
                        "reason": reason,
                    },
                )

                result = OrderResult(
                    success=False,
                    status="failed",
                    error_message=(
                        "execution did not resolve within "
                        f"{settings.execution_timeout_seconds}s - "
                        "outcome unknown; verify wallet balance "
                        "and transaction status manually"
                    ),
                )

            # ----------------------------------------------------------
            # Reconcile the confirmed LIVE sell against the blockchain.
            #
            # current_price is the price that triggered the exit. It is NOT
            # assumed to be the price the transaction actually achieved.
            # ----------------------------------------------------------

            actual_execution = None

            if result.success:
                actual_execution = (
                    await self._actual_sell_execution(
                        position,
                        token,
                        result,
                        current_price,
                    )
                )

                # ------------------------------------------------------
                # Catastrophic execution diagnostic.
                #
                # The stop is triggered from the live price feed, but the
                # blockchain fill can be materially worse because of rapid
                # price movement, liquidity loss, or execution/quote issues.
                # Do not silently treat that as an ordinary stop. Record the
                # exact gap so we can distinguish a bad trigger from a bad
                # execution. We deliberately do NOT attempt a second sell or
                # alter the already-confirmed transaction here.
                # ------------------------------------------------------
                if actual_execution:
                    trigger_vs_execution_pct = actual_execution.get(
                        "trigger_vs_execution_pct"
                    )
                    if (
                        reason.startswith("defensive stop")
                        or reason.startswith("hard stop")
                        or reason == "stop loss hit"
                    ) and (
                        trigger_vs_execution_pct is not None
                        and trigger_vs_execution_pct
                        <= -CATASTROPHIC_STOP_EXECUTION_GAP_PCT
                    ):
                        logger.error(
                            "catastrophic_stop_execution_gap",
                            extra={
                                "mint": token.mint,
                                "position_id": position.id,
                                "reason": reason,
                                "entry_price_usd": position.entry_price_usd,
                                "trigger_price_usd": current_price,
                                "actual_execution_price_usd": actual_execution.get(
                                    "actual_execution_price_usd"
                                ),
                                "trigger_vs_execution_pct": trigger_vs_execution_pct,
                                "gap_threshold_pct": CATASTROPHIC_STOP_EXECUTION_GAP_PCT,
                                "tokens_sold": actual_execution.get("tokens_sold"),
                                "sol_received": actual_execution.get("sol_received"),
                                "tx_signature": result.tx_signature,
                            },
                        )

            # ----------------------------------------------------------
            # Determine exit price.
            # ----------------------------------------------------------

            exit_price = (
                actual_execution[
                    "actual_execution_price_usd"
                ]
                if (
                    actual_execution
                    and actual_execution.get(
                        "actual_execution_price_usd"
                    )
                )
                else (
                    result.price_usd
                    if result.success
                    and result.price_usd
                    else current_price
                )
            )

            # ----------------------------------------------------------
            # Calculate portion of original investment being sold.
            # ----------------------------------------------------------

            # Cost basis is allocated from the remaining ledger, not from
            # the entry quote. This keeps partial-sell PNL correct.
            remaining_basis = float(getattr(position, "remaining_cost_basis_usd", 0.0) or 0.0)
            remaining_position_pct = max(float(position.remaining_pct or 100.0), 0.000001)
            invested_portion = (
                remaining_basis * (sell_pct / remaining_position_pct)
                if remaining_basis > 0
                else position.amount_sol_invested * (sell_pct / 100.0)
            )

            pnl_amount = (
                invested_portion
                * (
                    exit_price
                    - position.entry_price_usd
                )
                / max(
                    position.entry_price_usd,
                    1e-12,
                )
            )

            # For live trades, confirmed wallet proceeds are authoritative.
            # Net PNL = actual proceeds - allocated cost basis - transaction fee.
            if actual_execution and actual_execution.get("sol_usd", 0) > 0:
                actual_proceeds_usd = actual_execution["sol_received"] * actual_execution["sol_usd"]
                sell_fee_usd = actual_execution.get("fee_sol", 0.0) * actual_execution["sol_usd"]
                pnl_amount = actual_proceeds_usd - invested_portion - sell_fee_usd
                proceeds = actual_proceeds_usd
            else:
                proceeds = invested_portion + pnl_amount
                sell_fee_usd = 0.0

            # ----------------------------------------------------------
            # Record order attempt.
            # ----------------------------------------------------------

            await repo.create_order(
                position.mint,
                "sell",
                position.mode,
                (
                    "filled"
                    if result.success
                    else "failed"
                ),
                invested_portion,
                exit_price,
                result.tx_signature,
                result.error_message,
                rule_id=position.rule_id,
                owner_user_id=position.owner_user_id,
            )

            # ----------------------------------------------------------
            # SELL FAILED
            #
            # Do NOT modify position state.
            # Do NOT mark TP as hit.
            #
            # Also do NOT send "Sell triggered" here. The old behavior
            # produced the repeated Triggered -> Failed -> Triggered ->
            # Failed spam visible in Telegram.
            # ----------------------------------------------------------

            if not result.success:

                automated_reasons = {
                    "time-based exit",
                    "stop loss hit",
                    "trailing stop hit",
                    "volume drop exit",
                    "take profit hit",
                }

                if (
                    reason in automated_reasons
                    or reason.startswith(
                        "take profit level "
                    )
                ):
                    self._exit_retry_after[
                        position.id
                    ] = time.monotonic() + 60.0

                    logger.warning(
                        "automated_sell_retry_cooldown",
                        extra={
                            "mint": token.mint,
                            "position_id": position.id,
                            "reason": reason,
                            "retry_after_seconds": 60,
                        },
                    )

                logger.warning(
                    "sell_failed_position_unchanged",
                    extra={
                        "mint": token.mint,
                        "position_id": position.id,
                        "sell_pct": sell_pct,
                        "reason": reason,
                        "error": result.error_message,
                    },
                )

                await self._notifier.sell_failed(
                    position.owner_user_id,
                    token.ticker_symbol
                    or token.mint[:8],
                    (
                        result.error_message
                        or "unknown error"
                    ),
                )

                return False

            # ----------------------------------------------------------
            # SELL SUCCESS
            #
            # Only now modify position state.
            # ----------------------------------------------------------

            self._exit_retry_after.pop(
                position.id,
                None,
            )

            remaining_pct = max(
                0.0,
                float(
                    position.remaining_pct
                    or 0.0
                )
                - sell_pct,
            )

            # Paper trading balance handling.
            if position.mode == "paper":

                from app.execution.paper import (
                    PaperExecutionAdapter,
                )

                if isinstance(
                    adapter,
                    PaperExecutionAdapter,
                ):
                    await adapter.credit_balance(
                        proceeds
                    )

            realized_pnl = float(position.realized_pnl_usd or 0.0) + pnl_amount
            new_remaining_basis = max(0.0, remaining_basis - invested_portion)
            total_proceeds = float(getattr(position, "total_proceeds_usd", 0.0) or 0.0) + float(proceeds)
            total_fees = float(getattr(position, "total_fees_usd", 0.0) or 0.0) + float(sell_fee_usd)
            total_network_fees = float(getattr(position, "total_network_fee_usd", 0.0) or 0.0) + float(sell_fee_usd)

            if remaining_pct <= 0.01:

                await repo.update_position(
                    position.id,
                    status="closed",
                    remaining_pct=0.0,
                    closed_at=datetime.now(
                        timezone.utc
                    ),
                    close_reason=reason,
                    realized_pnl_usd=realized_pnl,
                    remaining_cost_basis_usd=0.0,
                    total_proceeds_usd=total_proceeds,
                    total_fees_usd=total_fees,
                    total_network_fee_usd=total_network_fees,
                )

                position.remaining_pct = 0.0
                position.status = "closed"

                logger.info(
                    "position_closed",
                    extra={
                        "mint": token.mint,
                        "position_id": position.id,
                        "sell_pct": sell_pct,
                        "reason": reason,
                        "realized_pnl": pnl_amount,
                        "tx_signature": (
                            result.tx_signature
                        ),
                    },
                )

            else:

                await repo.update_position(
                    position.id,
                    remaining_pct=(
                        remaining_pct
                    ),
                    realized_pnl_usd=realized_pnl,
                    remaining_cost_basis_usd=new_remaining_basis,
                    total_proceeds_usd=total_proceeds,
                    total_fees_usd=total_fees,
                    total_network_fee_usd=total_network_fees,
                )

                position.remaining_pct = (
                    remaining_pct
                )

                logger.info(
                    "partial_sell_filled",
                    extra={
                        "mint": token.mint,
                        "position_id": position.id,
                        "sell_pct": sell_pct,
                        "remaining_pct": (
                            remaining_pct
                        ),
                        "reason": reason,
                        "realized_pnl": pnl_amount,
                        "tx_signature": (
                            result.tx_signature
                        ),
                    },
                )

            # ----------------------------------------------------------
            # Only notify "triggered" after the sell actually succeeded.
            # ----------------------------------------------------------

            await self._notifier.sell_triggered(
                position.owner_user_id,
                token.ticker_symbol
                or token.mint[:8],
                reason,
                sell_pct,
            )

            await self._notifier.sell_filled(
                position.owner_user_id,
                token.ticker_symbol
                or token.mint[:8],
                exit_price,
                pnl_amount,
                result.tx_signature,
            )

            return True

        finally:

            self._finish_exit(
                position.id
            )

    # ------------------------------------------------------------------
    # Position evaluation
    # ------------------------------------------------------------------

    async def check_position(
        self,
        position,
    ):
        """Evaluate automated exits for one open position."""

        # Failed automated sells are retried at most once per 60 seconds.
        if self._automated_exit_on_cooldown(position.id):
            return

        # --------------------------------------------------------------
        # First thing we do for a live position:
        #
        # Check what the wallet ACTUALLY owns.
        #
        # This is intentionally before the price lookup. A manual Phantom
        # sale should be detected even if the price feed is temporarily
        # unavailable.
        # --------------------------------------------------------------

        if position.mode == "live":

            reconciled = (
                await self._reconcile_live_position(
                    position
                )
            )

            if reconciled:

                if (
                    position.status == "closed"
                    or float(
                        position.remaining_pct
                        or 0.0
                    ) <= 0.01
                ):
                    return

        # --------------------------------------------------------------
        # Global kill switch.
        #
        # /disable (trading_enabled=False) already told the scanner to
        # stop placing new buys. This is the other half: it also pauses
        # every automated exit (stop loss, take profit, trailing stop,
        # time-based) for positions that are already open, so /disable
        # actually stops the bot as its own confirmation text promises,
        # instead of leaving position monitoring - and its notifications
        # - running underneath.
        #
        # Wallet reconciliation above still ran either way: it only
        # detects and records reality, it never places a trade, so a
        # manual/external sale is still picked up correctly even while
        # paused.
        #
        # Manual closes (/positions close <id>) are a separate code
        # path and are never affected by this - an explicit admin
        # action should always go through.
        # --------------------------------------------------------------

        bot_state = (
            await repo.get_or_create_bot_state(position.owner_user_id)
        )

        if not bot_state.trading_enabled:
            return

        # --------------------------------------------------------------
        # Token
        # --------------------------------------------------------------

        token_row = await repo.get_token(
            position.mint
        )

        if not token_row:

            logger.warning(
                "position_token_not_found",
                extra={
                    "position_id": position.id,
                    "mint": position.mint,
                },
            )

            return

        token = TokenSnapshot(
            mint=position.mint,
            ticker_symbol=(
                token_row.ticker_symbol
            ),
            ticker_name=(
                token_row.ticker_name
            ),
            creator_wallet=(
                token_row.creator_wallet
            ),
            price_usd=(
                position.entry_price_usd
            ),
            volume_24h_usd=(
                position.entry_volume_24h_usd
            ),
            source=token_row.source,
        )

        # --------------------------------------------------------------
        # CURRENT PRICE
        # --------------------------------------------------------------

        (
            current_price,
            is_simulated_price,
        ) = await get_current_price_usd(
            self._anoncoin,
            token,
            self._tick,
        )

        token.price_usd = current_price

        # --------------------------------------------------------------
        # CURRENT VOLUME
        # --------------------------------------------------------------

        (
            current_volume,
            is_simulated_volume,
        ) = await get_current_volume_usd(
            self._anoncoin,
            token,
            self._tick,
        )

        token.volume_24h_usd = (
            current_volume
        )

        # --------------------------------------------------------------
        # Load exact rule attached to position.
        # --------------------------------------------------------------

        rule = await self._rule_for_position(
            position
        )

        # --------------------------------------------------------------
        # PnL
        # --------------------------------------------------------------

        pnl_pct = self._pnl_pct(
            position.entry_price_usd,
            current_price,
        )

        logger.debug(
            "position_evaluation",
            extra={
                "mint": position.mint,
                "position_id": position.id,
                "entry_price": (
                    position.entry_price_usd
                ),
                "current_price": (
                    current_price
                ),
                "pnl_pct": pnl_pct,
                "remaining_pct": (
                    position.remaining_pct
                ),
                "rule": rule.name,
                "rule_id": position.rule_id,
                "simulated_price": (
                    is_simulated_price
                ),
                "simulated_volume": (
                    is_simulated_volume
                ),
            },
        )

        # --------------------------------------------------------------
        # Peak price
        # --------------------------------------------------------------

        if not is_simulated_price:

            peak_price = max(
                float(
                    position.peak_price_usd
                    or 0.0
                ),
                current_price,
            )

            if (
                peak_price
                != position.peak_price_usd
            ):

                await repo.update_position(
                    position.id,
                    peak_price_usd=peak_price,
                )

                position.peak_price_usd = (
                    peak_price
                )

        else:

            peak_price = float(
                position.peak_price_usd
                or 0.0
            )

        # --------------------------------------------------------------
        # Peak volume
        # --------------------------------------------------------------

        if not is_simulated_volume:

            peak_volume = max(
                float(
                    position.peak_volume_24h_usd
                    or 0.0
                ),
                current_volume,
            )

            if (
                peak_volume
                != position.peak_volume_24h_usd
            ):

                await repo.update_position(
                    position.id,
                    peak_volume_24h_usd=(
                        peak_volume
                    ),
                )

                position.peak_volume_24h_usd = (
                    peak_volume
                )

        else:

            peak_volume = float(
                position.peak_volume_24h_usd
                or 0.0
            )

        # --------------------------------------------------------------
        # TIME-BASED EXIT
        # --------------------------------------------------------------

        if rule.time_based_exit_seconds:

            opened_at = (
                position.opened_at
            )

            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(
                    tzinfo=timezone.utc
                )

            age = (
                datetime.now(timezone.utc)
                - opened_at
            ).total_seconds()

            if (
                age
                >= rule.time_based_exit_seconds
            ):

                logger.info(
                    "time_based_exit_triggered",
                    extra={
                        "mint": token.mint,
                        "position_id": (
                            position.id
                        ),
                        "age_seconds": age,
                        "limit_seconds": (
                            rule.time_based_exit_seconds
                        ),
                    },
                )

                await self._close_position(
                    position,
                    token,
                    current_price,
                    position.remaining_pct,
                    "time-based exit",
                    reconcile_wallet=False,
                )

                return

        # --------------------------------------------------------------
        # PRICE EXITS REQUIRE REAL PRICE
        # --------------------------------------------------------------

        if is_simulated_price:

            logger.warning(
                "price_based_exits_skipped_price_unavailable",
                extra={
                    "mint": token.mint,
                    "position_id": (
                        position.id
                    ),
                    "source": token.source,
                    "last_known_price": (
                        current_price
                    ),
                },
            )

            return

        # --------------------------------------------------------------
        # ADAPTIVE RISK ENGINE
        #
        # A tiny fixed stop is unreliable on fast launch markets because
        # normal micro-wicks can exceed it before the sell lands. Instead:
        #   - take a one-time 50% defensive exit at -15%;
        #   - hard-close the remainder at -25%;
        #   - once the trade proves itself, lock profit progressively;
        #   - use an adaptive trailing distance on larger winners.
        # --------------------------------------------------------------

        if (
            not position.defensive_exit_done
            and pnl_pct <= -abs(settings.defensive_stop_loss_pct)
        ):
            logger.info(
                "defensive_stop_triggered",
                extra={
                    "mint": token.mint,
                    "position_id": position.id,
                    "pnl_pct": pnl_pct,
                    "threshold": settings.defensive_stop_loss_pct,
                    "sell_pct": settings.defensive_stop_sell_pct,
                },
            )
            sell_success = await self._close_position(
                position, token, current_price,
                settings.defensive_stop_sell_pct,
                f"defensive stop -{settings.defensive_stop_loss_pct:g}% partial",
                reconcile_wallet=False,
            )
            if sell_success:
                await repo.update_position(
                    position.id, defensive_exit_done=True
                )
                position.defensive_exit_done = True
            return

        if pnl_pct <= -abs(settings.hard_stop_loss_pct):
            logger.info(
                "hard_stop_triggered",
                extra={
                    "mint": token.mint,
                    "position_id": position.id,
                    "pnl_pct": pnl_pct,
                    "threshold": settings.hard_stop_loss_pct,
                },
            )
            await self._close_position(
                position, token, current_price,
                position.remaining_pct,
                f"hard stop -{settings.hard_stop_loss_pct:g}%",
                reconcile_wallet=False,
            )
            return

        peak_pnl_pct = self._pnl_pct(
            position.entry_price_usd, peak_price
        )

        # --------------------------------------------------------------
        # PROFIT PROTECTION / ADAPTIVE TRAILING
        #
        # Do not use a fixed "profit lock" as a separate full-position
        # exit. On fast launches a brief wick can cross that floor and a
        # market sell can then fill far below the trigger. Instead the
        # protection level is derived from the highest confirmed PnL and
        # acts as the trailing floor for the remaining position.
        #
        # The floor is deliberately progressive:
        #   +10% peak -> protect -2% (near breakeven)
        #   +20% peak -> protect +5%
        #   +40% peak -> protect +10%
        #   +75% peak -> protect +35%
        #
        # This keeps the runner alive during normal volatility while
        # preventing a large winner from round-tripping into a loss.
        # --------------------------------------------------------------
        protected_pnl = None
        protection_stage = None
        if peak_pnl_pct >= 75.0:
            protected_pnl = settings.strong_runner_lock_pct
            protection_stage = "strong_runner"
        elif peak_pnl_pct >= settings.strong_profit_trigger_pct:
            protected_pnl = settings.strong_profit_lock_pct
            protection_stage = "strong_profit"
        elif peak_pnl_pct >= settings.profit_lock_trigger_pct:
            protected_pnl = settings.profit_lock_pct
            protection_stage = "profit"
        elif peak_pnl_pct >= settings.breakeven_trigger_pct:
            protected_pnl = settings.breakeven_lock_pct
            protection_stage = "breakeven"

        if protected_pnl is not None and pnl_pct <= protected_pnl:
            logger.info(
                "adaptive_profit_floor_triggered",
                extra={
                    "mint": token.mint,
                    "position_id": position.id,
                    "pnl_pct": pnl_pct,
                    "peak_pnl_pct": peak_pnl_pct,
                    "protected_pnl_pct": protected_pnl,
                    "protection_stage": protection_stage,
                    "remaining_pct": position.remaining_pct,
                },
            )
            await self._close_position(
                position, token, current_price,
                position.remaining_pct,
                f"adaptive profit floor {protected_pnl:+.1f}%",
                reconcile_wallet=False,
            )
            return

        # Keep a secondary price-distance trailing guard for very large
        # winners. This catches a rapid collapse even if the PnL floor is
        # skipped between monitoring ticks. The profit floor above is the
        # primary protection for normal winners.
        if peak_pnl_pct >= 75.0:
            trailing_pct = settings.adaptive_trailing_max_pct
        elif peak_pnl_pct >= 40.0:
            trailing_pct = settings.adaptive_trailing_strong_pct
        elif peak_pnl_pct >= 20.0:
            trailing_pct = settings.adaptive_trailing_mid_pct
        elif peak_pnl_pct >= 10.0:
            trailing_pct = settings.adaptive_trailing_min_pct
        else:
            trailing_pct = None

        if trailing_pct is not None and peak_price > 0:
            drop_from_peak = (
                (peak_price - current_price)
                / peak_price
                * 100.0
            )
            if drop_from_peak >= trailing_pct:
                logger.info(
                    "adaptive_trailing_stop_triggered",
                    extra={
                        "mint": token.mint,
                        "position_id": position.id,
                        "pnl_pct": pnl_pct,
                        "peak_pnl_pct": peak_pnl_pct,
                        "drop_from_peak": drop_from_peak,
                        "trailing_stop_pct": trailing_pct,
                    },
                )
                await self._close_position(
                    position, token, current_price,
                    position.remaining_pct,
                    f"adaptive trailing {trailing_pct:.1f}%",
                    reconcile_wallet=False,
                )
                return

        # --------------------------------------------------------------
        # VOLUME DROP EXIT
        # --------------------------------------------------------------

        if (
            not is_simulated_volume
            and rule.sell_on_volume_drop_pct
            and peak_volume > 0
        ):

            volume_drop_pct = (
                (
                    peak_volume
                    - current_volume
                )
                / peak_volume
                * 100
            )

            if (
                volume_drop_pct
                >= rule.sell_on_volume_drop_pct
            ):

                logger.info(
                    "volume_drop_exit_triggered",
                    extra={
                        "mint": token.mint,
                        "position_id": (
                            position.id
                        ),
                        "volume_drop_pct": (
                            volume_drop_pct
                        ),
                        "threshold": (
                            rule.sell_on_volume_drop_pct
                        ),
                    },
                )

                await self._close_position(
                    position,
                    token,
                    current_price,
                    position.remaining_pct,
                    "volume drop exit",
                    reconcile_wallet=False,
                )

                return

        # --------------------------------------------------------------
        # TAKE PROFIT
        #
        # Example:
        #
        # 60:60,100:40
        #
        # +60%:
        #     sell 60%
        #
        # +100%:
        #     sell remaining 40%
        #
        # A TP level is only consumed after the sell succeeds.
        # --------------------------------------------------------------

        tp_hit_indexes = set(
            position.tp_hit_indexes or []
        )

        # Pump.fun uses the launch-specific staged profit plan. It takes
        # modest profits early, then leaves 30% of the original position as
        # a runner for large moves. Other sources keep the exact user rule.
        if token.source == "pumpfun":
            take_profit_levels = [
                # Give Pump.fun winners room to develop. The final 30% is
                # intentionally left as a runner for large moves.
                TakeProfitLevel(gain_pct=30.0, sell_pct=20.0),
                TakeProfitLevel(gain_pct=60.0, sell_pct=25.0),
                TakeProfitLevel(gain_pct=100.0, sell_pct=25.0),
            ]
        else:
            take_profit_levels = rule.take_profit_levels

        for idx, level in enumerate(
            take_profit_levels
        ):

            if idx in tp_hit_indexes:
                continue

            gain_pct = max(
                0.0,
                float(
                    level.gain_pct
                ),
            )

            requested_sell_pct = max(
                0.0,
                float(
                    level.sell_pct
                ),
            )

            if requested_sell_pct <= 0:

                logger.warning(
                    "invalid_take_profit_sell_percentage",
                    extra={
                        "mint": token.mint,
                        "position_id": (
                            position.id
                        ),
                        "tp_index": idx,
                        "sell_pct": (
                            level.sell_pct
                        ),
                    },
                )

                continue

            # Threshold not reached.
            if pnl_pct < gain_pct:
                continue

            actual_sell_pct = (
                self._sellable_pct(
                    position,
                    requested_sell_pct,
                )
            )

            if actual_sell_pct <= 0:

                logger.warning(
                    "take_profit_no_remaining_position",
                    extra={
                        "mint": token.mint,
                        "position_id": (
                            position.id
                        ),
                        "tp_index": idx,
                        "pnl_pct": pnl_pct,
                        "gain_pct": gain_pct,
                        "requested_sell_pct": (
                            requested_sell_pct
                        ),
                        "remaining_pct": (
                            position.remaining_pct
                        ),
                    },
                )

                new_tp_indexes = list(
                    position.tp_hit_indexes
                    or []
                )

                if idx not in new_tp_indexes:

                    new_tp_indexes.append(
                        idx
                    )

                    await repo.update_position(
                        position.id,
                        tp_hit_indexes=(
                            new_tp_indexes
                        ),
                    )

                    position.tp_hit_indexes = (
                        new_tp_indexes
                    )

                continue

            logger.info(
                "take_profit_triggered",
                extra={
                    "mint": token.mint,
                    "position_id": (
                        position.id
                    ),
                    "tp_index": idx,
                    "pnl_pct": pnl_pct,
                    "gain_threshold": gain_pct,
                    "requested_sell_pct": (
                        requested_sell_pct
                    ),
                    "actual_sell_pct": (
                        actual_sell_pct
                    ),
                    "remaining_pct_before": (
                        position.remaining_pct
                    ),
                },
            )

            sell_success = (
                await self._close_position(
                    position,
                    token,
                    current_price,
                    actual_sell_pct,
                    f"take profit level {idx + 1}",
                )
            )

            # Only consume TP after confirmed successful execution.
            if sell_success:

                new_tp_indexes = list(
                    position.tp_hit_indexes
                    or []
                )

                if idx not in new_tp_indexes:

                    new_tp_indexes.append(
                        idx
                    )

                    await repo.update_position(
                        position.id,
                        tp_hit_indexes=(
                            new_tp_indexes
                        ),
                    )

                    position.tp_hit_indexes = (
                        new_tp_indexes
                    )

                # _close_position already updated the live position.
                position.remaining_pct = max(
                    0.0,
                    float(
                        position.remaining_pct
                        or 0.0
                    ),
                )

                logger.info(
                    "take_profit_filled",
                    extra={
                        "mint": token.mint,
                        "position_id": (
                            position.id
                        ),
                        "tp_index": idx,
                        "sell_pct": (
                            actual_sell_pct
                        ),
                        "remaining_pct_after": (
                            position.remaining_pct
                        ),
                    },
                )

            else:

                # Do NOT mark the TP as hit.
                #
                # It remains pending and can be retried on the next
                # monitoring cycle.
                logger.warning(
                    "take_profit_sell_failed_will_retry",
                    extra={
                        "mint": token.mint,
                        "position_id": (
                            position.id
                        ),
                        "tp_index": idx,
                        "sell_pct": (
                            actual_sell_pct
                        ),
                        "pnl_pct": pnl_pct,
                    },
                )

            # Only one exit attempt per monitoring cycle.
            return

    # ------------------------------------------------------------------
    # Manual close
    # ------------------------------------------------------------------

    async def close_position_manually(
        self,
        position_id: int,
    ) -> bool:
        """Manually close an open position."""

        positions = (
            await repo.get_open_positions()
        )

        target = next(
            (
                position
                for position in positions
                if position.id == position_id
            ),
            None,
        )

        if not target:
            return False

        # Reconcile first.

        if target.mode == "live":

            reconciled = (
                await self._reconcile_live_position(
                    target
                )
            )

            if reconciled:

                if (
                    target.status == "closed"
                    or float(
                        target.remaining_pct
                        or 0.0
                    ) <= 0.01
                ):
                    logger.info(
                        "manual_close_skipped_position_already_closed",
                        extra={
                            "position_id": (
                                target.id
                            ),
                            "mint": target.mint,
                        },
                    )

                    return False

        token_row = await repo.get_token(
            target.mint
        )

        token = TokenSnapshot(
            mint=target.mint,
            ticker_symbol=(
                token_row.ticker_symbol
                if token_row
                else target.mint[:8]
            ),
            ticker_name=(
                token_row.ticker_name
                if token_row
                else None
            ),
            creator_wallet=(
                token_row.creator_wallet
                if token_row
                else None
            ),
            price_usd=(
                target.entry_price_usd
            ),
            volume_24h_usd=(
                target.entry_volume_24h_usd
            ),
            source=(
                token_row.source
                if token_row
                else "mock_simulated"
            ),
        )

        (
            current_price,
            is_simulated_price,
        ) = await get_current_price_usd(
            self._anoncoin,
            token,
            self._tick,
        )

        if is_simulated_price:

            logger.warning(
                "manual_close_price_unavailable",
                extra={
                    "position_id": (
                        target.id
                    ),
                    "mint": target.mint,
                },
            )

        token.price_usd = current_price

        return await self._close_position(
            target,
            token,
            current_price,
            target.remaining_pct,
            "manual close via /positions",
        )

    # ------------------------------------------------------------------
    # Continuous monitoring
    # ------------------------------------------------------------------

    async def run_forever(self):
        """Continuously monitor open positions."""

        while True:

            self._tick += 1

            try:

                positions = (
                    await repo.get_open_positions()
                )

                for position in positions:

                    try:

                        await self.check_position(
                            position
                        )

                    except Exception:

                        # One broken position must never stop monitoring
                        # all other positions.
                        logger.exception(
                            "position_check_failed",
                            extra={
                                "position_id": (
                                    position.id
                                ),
                                "mint": (
                                    position.mint
                                ),
                            },
                        )

            except Exception:

                logger.exception(
                    "position_check_cycle_failed"
                )

            await asyncio.sleep(
                settings.position_check_interval_seconds
            )
