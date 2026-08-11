"""Monitors open positions and triggers automated exits.

Exit rules are always evaluated against the exact rule set that was attached
to the position when it was opened. This is important because an admin may
change/activate a different rule later without changing the rules governing
already-open positions.

Take-profit behavior:

    60:60
        At +60% PnL, sell 60% of the original position.

    60:60,100:40
        At +60% PnL, sell 60% of the original position.
        At +100% PnL, sell the remaining 40%.

A take-profit level is only marked as "hit" AFTER the sell succeeds.
If execution fails, the TP remains pending and will be retried on the next
position-check cycle.
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.config.settings import settings
from app.connectors.anoncoin import AnoncoinClient
from app.execution.base import OrderResult
from app.execution.price_source import (
    get_current_price_usd,
    get_current_volume_usd,
)
from app.execution.router import ExecutionRouter
from app.scoring.rules import RuleParams, TokenSnapshot
from app.storage import repository as repo


logger = logging.getLogger("app.positions.manager")


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

    async def _rule_for_position(self, position) -> RuleParams:
        """Load the exact rule that created this position.

        We intentionally use position.rule_id instead of the admin's current
        active rule. Changing an admin's active rule must not retroactively
        change an already-open position.
        """
        if position.rule_id:
            rules = await repo.get_all_rules()

            for rule in rules:
                if rule.id == position.rule_id:
                    from app.storage.repository import rule_row_to_params

                    return rule_row_to_params(rule)

        # Backwards compatibility for positions created before rule_id was
        # properly populated.
        logger.warning(
            "position_has_no_rule",
            extra={
                "position_id": position.id,
                "mint": position.mint,
            },
        )

        return RuleParams()

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

    def _sellable_pct(self, position, requested_sell_pct: float) -> float:
        """Return the percentage of the original position that can safely
        be sold.

        TP percentages are defined relative to the ORIGINAL position.

        Example:

            Original = 100%
            TP1 sells 60%
            Remaining = 40%

        If a malformed/old rule asks TP2 to sell 60%, we must never attempt
        to sell more than the 40% that actually remains.
        """
        remaining_pct = max(
            0.0,
            float(position.remaining_pct or 0.0),
        )

        requested = max(
            0.0,
            float(requested_sell_pct or 0.0),
        )

        return min(requested, remaining_pct)

    async def _close_position(
        self,
        position,
        token: TokenSnapshot,
        current_price: float,
        sell_pct: float,
        reason: str,
    ) -> bool:
        """Execute a partial/full sell.

        Returns:
            True  -> sell successfully executed.
            False -> sell failed and position remains unchanged.

        IMPORTANT:
        A failed sell must NEVER reduce remaining_pct or mark a TP level as
        consumed.
        """
        sell_pct = self._sellable_pct(position, sell_pct)

        if sell_pct <= 0:
            logger.warning(
                "sell_skipped_no_remaining_position",
                extra={
                    "mint": token.mint,
                    "position_id": position.id,
                    "reason": reason,
                    "remaining_pct": position.remaining_pct,
                },
            )
            return False

        adapter = await self._execution_router.get_adapter(
            position.mode,
            position.owner_user_id,
        )

        amount_to_sell = position.amount_tokens * (
            sell_pct / 100
        )

        logger.info(
            "sell_attempt",
            extra={
                "mint": token.mint,
                "position_id": position.id,
                "sell_pct": sell_pct,
                "remaining_pct_before": position.remaining_pct,
                "reason": reason,
                "mode": position.mode,
            },
        )

        try:
            result = await asyncio.wait_for(
                adapter.sell(
                    token,
                    amount_to_sell,
                    sell_pct,
                ),
                timeout=settings.execution_timeout_seconds,
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
                    f"execution did not resolve within "
                    f"{settings.execution_timeout_seconds}s - "
                    "outcome unknown, verify wallet balance / "
                    "Solscan manually"
                ),
            )

        exit_price = (
            result.price_usd
            if result.success and result.price_usd
            else current_price
        )

        invested_portion = (
            position.amount_sol_invested
            * (sell_pct / 100)
        )

        # Approximate PnL using price ratio.
        pnl_amount = (
            invested_portion
            * (exit_price - position.entry_price_usd)
            / max(position.entry_price_usd, 1e-12)
        )

        proceeds = invested_portion + pnl_amount

        await repo.create_order(
            position.mint,
            "sell",
            position.mode,
            "filled" if result.success else "failed",
            invested_portion,
            exit_price,
            result.tx_signature,
            result.error_message,
            rule_id=position.rule_id,
            owner_user_id=position.owner_user_id,
        )

        # ---------------------------------------------------------------
        # CRITICAL:
        #
        # If execution failed, DO NOT modify the position.
        #
        # In particular:
        #   - do not reduce remaining_pct
        #   - do not close the position
        #   - do not mark a TP level as hit
        #
        # The next monitoring cycle will retry the applicable exit rule.
        # ---------------------------------------------------------------
        if not result.success:
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

            await self._notifier.sell_triggered(
                position.owner_user_id,
                token.ticker_symbol or token.mint[:8],
                reason,
                sell_pct,
            )

            await self._notifier.sell_failed(
                position.owner_user_id,
                token.ticker_symbol or token.mint[:8],
                result.error_message or "unknown error",
            )

            return False

        # ---------------------------------------------------------------
        # SELL SUCCEEDED.
        #
        # Only now do we update the position.
        # ---------------------------------------------------------------

        remaining_pct = max(
            0.0,
            float(position.remaining_pct or 0.0)
            - sell_pct,
        )

        if position.mode == "paper":
            from app.execution.paper import PaperExecutionAdapter

            if isinstance(adapter, PaperExecutionAdapter):
                await adapter.credit_balance(proceeds)

        realized_pnl = (
            position.realized_pnl_usd
            + pnl_amount
        )

        if remaining_pct <= 0.01:
            await repo.update_position(
                position.id,
                status="closed",
                remaining_pct=0.0,
                closed_at=datetime.now(timezone.utc),
                close_reason=reason,
                realized_pnl_usd=realized_pnl,
            )

            logger.info(
                "position_closed",
                extra={
                    "mint": token.mint,
                    "position_id": position.id,
                    "sell_pct": sell_pct,
                    "reason": reason,
                    "realized_pnl": pnl_amount,
                    "tx_signature": result.tx_signature,
                },
            )
        else:
            await repo.update_position(
                position.id,
                remaining_pct=remaining_pct,
                realized_pnl_usd=realized_pnl,
            )

            logger.info(
                "partial_sell_filled",
                extra={
                    "mint": token.mint,
                    "position_id": position.id,
                    "sell_pct": sell_pct,
                    "remaining_pct": remaining_pct,
                    "reason": reason,
                    "realized_pnl": pnl_amount,
                    "tx_signature": result.tx_signature,
                },
            )

        await self._notifier.sell_triggered(
            position.owner_user_id,
            token.ticker_symbol or token.mint[:8],
            reason,
            sell_pct,
        )

        await self._notifier.sell_filled(
            position.owner_user_id,
            token.ticker_symbol or token.mint[:8],
            exit_price,
            pnl_amount,
            result.tx_signature,
        )

        return True

    async def check_position(self, position):
        """Evaluate all automated exit rules for one open position."""

        token_row = await repo.get_token(position.mint)

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
            ticker_symbol=token_row.ticker_symbol,
            ticker_name=token_row.ticker_name,
            creator_wallet=token_row.creator_wallet,
            price_usd=position.entry_price_usd,
            volume_24h_usd=position.entry_volume_24h_usd,
            source=token_row.source,
        )

        # ---------------------------------------------------------------
        # Get CURRENT market price.
        # ---------------------------------------------------------------
        current_price, is_simulated_price = await get_current_price_usd(
            self._anoncoin,
            token,
            self._tick,
        )

        token.price_usd = current_price

        # ---------------------------------------------------------------
        # Get CURRENT volume.
        # ---------------------------------------------------------------
        current_volume, is_simulated_volume = await get_current_volume_usd(
            self._anoncoin,
            token,
            self._tick,
        )

        token.volume_24h_usd = current_volume

        rule = await self._rule_for_position(position)

        pnl_pct = self._pnl_pct(
            position.entry_price_usd,
            current_price,
        )

        logger.debug(
            "position_evaluation",
            extra={
                "mint": position.mint,
                "position_id": position.id,
                "entry_price": position.entry_price_usd,
                "current_price": current_price,
                "pnl_pct": pnl_pct,
                "remaining_pct": position.remaining_pct,
                "rule": rule.name,
                "rule_id": position.rule_id,
                "simulated_price": is_simulated_price,
                "simulated_volume": is_simulated_volume,
            },
        )

        # ---------------------------------------------------------------
        # Track peak price / volume.
        # ---------------------------------------------------------------

        peak_price = max(
            position.peak_price_usd,
            current_price,
        )

        peak_volume = max(
            position.peak_volume_24h_usd,
            current_volume,
        )

        peak_updates = {}

        if peak_price != position.peak_price_usd:
            peak_updates["peak_price_usd"] = peak_price

        if peak_volume != position.peak_volume_24h_usd:
            peak_updates["peak_volume_24h_usd"] = peak_volume

        if peak_updates:
            await repo.update_position(
                position.id,
                **peak_updates,
            )

            position.peak_price_usd = peak_price
            position.peak_volume_24h_usd = peak_volume

        # ---------------------------------------------------------------
        # STOP LOSS
        # ---------------------------------------------------------------

        if (
            rule.stop_loss_pct
            and pnl_pct <= -abs(rule.stop_loss_pct)
        ):
            logger.info(
                "stop_loss_triggered",
                extra={
                    "mint": token.mint,
                    "position_id": position.id,
                    "pnl_pct": pnl_pct,
                    "stop_loss_pct": rule.stop_loss_pct,
                },
            )

            await self._close_position(
                position,
                token,
                current_price,
                position.remaining_pct,
                "stop loss hit",
            )
            return

        # ---------------------------------------------------------------
        # TRAILING STOP
        # ---------------------------------------------------------------

        if rule.trailing_stop_pct and pnl_pct > 0:
            drop_from_peak = (
                (peak_price - current_price)
                / peak_price
                * 100
                if peak_price > 0
                else 0
            )

            if drop_from_peak >= rule.trailing_stop_pct:
                logger.info(
                    "trailing_stop_triggered",
                    extra={
                        "mint": token.mint,
                        "position_id": position.id,
                        "pnl_pct": pnl_pct,
                        "drop_from_peak": drop_from_peak,
                        "trailing_stop_pct": rule.trailing_stop_pct,
                    },
                )

                await self._close_position(
                    position,
                    token,
                    current_price,
                    position.remaining_pct,
                    "trailing stop hit",
                )
                return

        # ---------------------------------------------------------------
        # TIME-BASED EXIT
        # ---------------------------------------------------------------

        if rule.time_based_exit_seconds:
            age = (
                datetime.now(timezone.utc)
                - position.opened_at.replace(
                    tzinfo=timezone.utc
                )
            ).total_seconds()

            if age >= rule.time_based_exit_seconds:
                logger.info(
                    "time_based_exit_triggered",
                    extra={
                        "mint": token.mint,
                        "position_id": position.id,
                        "age_seconds": age,
                        "limit_seconds": rule.time_based_exit_seconds,
                    },
                )

                await self._close_position(
                    position,
                    token,
                    current_price,
                    position.remaining_pct,
                    "time-based exit",
                )
                return

        # ---------------------------------------------------------------
        # VOLUME DROP EXIT
        # ---------------------------------------------------------------

        if (
            rule.sell_on_volume_drop_pct
            and peak_volume > 0
        ):
            volume_drop_pct = (
                (peak_volume - current_volume)
                / peak_volume
                * 100
            )

            if volume_drop_pct >= rule.sell_on_volume_drop_pct:
                logger.info(
                    "volume_drop_exit_triggered",
                    extra={
                        "mint": token.mint,
                        "position_id": position.id,
                        "volume_drop_pct": volume_drop_pct,
                        "threshold": rule.sell_on_volume_drop_pct,
                    },
                )

                await self._close_position(
                    position,
                    token,
                    current_price,
                    position.remaining_pct,
                    "volume drop exit",
                )
                return

        # ---------------------------------------------------------------
        # TAKE PROFIT
        #
        # IMPORTANT:
        #
        # We do NOT mark a TP level as hit until _close_position()
        # confirms the sell succeeded.
        #
        # Example:
        #
        #     Rule = 60:60,100:40
        #
        #     +60% → sell 60%
        #     +100% → sell remaining 40%
        #
        # If the +60% sell fails, TP #1 remains pending and will be
        # evaluated again on the next cycle.
        # ---------------------------------------------------------------

        tp_hit_indexes = set(
            position.tp_hit_indexes or []
        )

        for idx, level in enumerate(
            rule.take_profit_levels
        ):
            # Already successfully executed.
            if idx in tp_hit_indexes:
                continue

            # Ignore invalid negative percentages safely.
            gain_pct = max(
                0.0,
                float(level.gain_pct),
            )

            requested_sell_pct = max(
                0.0,
                float(level.sell_pct),
            )

            if requested_sell_pct <= 0:
                logger.warning(
                    "invalid_take_profit_sell_percentage",
                    extra={
                        "mint": token.mint,
                        "position_id": position.id,
                        "tp_index": idx,
                        "sell_pct": level.sell_pct,
                    },
                )
                continue

            if pnl_pct < gain_pct:
                continue

            actual_sell_pct = self._sellable_pct(
                position,
                requested_sell_pct,
            )

            if actual_sell_pct <= 0:
                logger.warning(
                    "take_profit_has_no_remaining_position",
                    extra={
                        "mint": token.mint,
                        "position_id": position.id,
                        "tp_index": idx,
                        "pnl_pct": pnl_pct,
                        "gain_pct": gain_pct,
                        "requested_sell_pct": requested_sell_pct,
                        "remaining_pct": position.remaining_pct,
                    },
                )

                # There is nothing left to sell. We can consider this
                # level settled because the position has already been
                # exhausted by previous successful exits.
                new_tp_indexes = list(
                    position.tp_hit_indexes or []
                )

                if idx not in new_tp_indexes:
                    new_tp_indexes.append(idx)

                    await repo.update_position(
                        position.id,
                        tp_hit_indexes=new_tp_indexes,
                    )

                    position.tp_hit_indexes = new_tp_indexes

                continue

            logger.info(
                "take_profit_triggered",
                extra={
                    "mint": token.mint,
                    "position_id": position.id,
                    "tp_index": idx,
                    "pnl_pct": pnl_pct,
                    "gain_threshold": gain_pct,
                    "requested_sell_pct": requested_sell_pct,
                    "actual_sell_pct": actual_sell_pct,
                    "remaining_pct_before": position.remaining_pct,
                },
            )

            sell_success = await self._close_position(
                position,
                token,
                current_price,
                actual_sell_pct,
                f"take profit level {idx + 1}",
            )

            # -----------------------------------------------------------
            # CRITICAL FIX:
            #
            # Only consume the TP level if the sell really succeeded.
            # -----------------------------------------------------------
            if sell_success:
                new_tp_indexes = list(
                    position.tp_hit_indexes or []
                )

                if idx not in new_tp_indexes:
                    new_tp_indexes.append(idx)

                    await repo.update_position(
                        position.id,
                        tp_hit_indexes=new_tp_indexes,
                    )

                    position.tp_hit_indexes = new_tp_indexes

                # Update our in-memory remaining percentage so that if
                # this function is ever extended to evaluate additional
                # levels in the same cycle, it has the correct state.
                position.remaining_pct = max(
                    0.0,
                    position.remaining_pct
                    - actual_sell_pct,
                )

                logger.info(
                    "take_profit_filled",
                    extra={
                        "mint": token.mint,
                        "position_id": position.id,
                        "tp_index": idx,
                        "sell_pct": actual_sell_pct,
                        "remaining_pct_after": position.remaining_pct,
                    },
                )

            else:
                # IMPORTANT:
                #
                # Do NOT add idx to tp_hit_indexes.
                #
                # The same TP level will therefore be retried during
                # the next position-check cycle.
                logger.warning(
                    "take_profit_sell_failed_will_retry",
                    extra={
                        "mint": token.mint,
                        "position_id": position.id,
                        "tp_index": idx,
                        "sell_pct": actual_sell_pct,
                        "pnl_pct": pnl_pct,
                    },
                )

            # One exit action per monitoring cycle. This prevents multiple
            # TP/exit orders being fired simultaneously against the same
            # position.
            return

    async def close_position_manually(
        self,
        position_id: int,
    ) -> bool:
        """Manually close an open position through Telegram."""

        positions = await repo.get_open_positions()

        target = next(
            (
                p
                for p in positions
                if p.id == position_id
            ),
            None,
        )

        if not target:
            return False

        token_row = await repo.get_token(
            target.mint
        )

        current_price, _ = await get_current_price_usd(
            self._anoncoin,
            TokenSnapshot(
                mint=target.mint,
                price_usd=target.entry_price_usd,
                source=(
                    token_row.source
                    if token_row
                    else "mock_simulated"
                ),
            ),
            self._tick,
        )

        token = TokenSnapshot(
            mint=target.mint,
            ticker_symbol=(
                token_row.ticker_symbol
                if token_row
                else target.mint[:8]
            ),
            price_usd=current_price,
            source=(
                token_row.source
                if token_row
                else "mock_simulated"
            ),
        )

        return await self._close_position(
            target,
            token,
            current_price,
            target.remaining_pct,
            "manual close via /positions",
        )

    async def run_forever(self):
        """Continuously monitor open positions."""

        while True:
            self._tick += 1

            try:
                positions = await repo.get_open_positions()

                for position in positions:
                    try:
                        await self.check_position(
                            position
                        )
                    except Exception:
                        # One broken position must never stop monitoring
                        # every other position.
                        logger.exception(
                            "position_check_failed",
                            extra={
                                "position_id": position.id,
                                "mint": position.mint,
                            },
                        )

            except Exception:
                logger.exception(
                    "position_check_cycle_failed"
                )

            await asyncio.sleep(
                settings.position_check_interval_seconds
            )
