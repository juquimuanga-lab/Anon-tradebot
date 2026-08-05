"""Monitors open positions and triggers automated exits."""
import asyncio
import logging

from app.config.settings import settings
from app.connectors.anoncoin import AnoncoinClient
from app.execution.base import ExecutionAdapter
from app.execution.price_source import get_current_price_usd
from app.scoring.rules import RuleParams, TokenSnapshot
from app.storage import repository as repo

logger = logging.getLogger("app.positions.manager")


class PositionManager:
    def __init__(self, notifier, anoncoin: AnoncoinClient, execution_by_mode: dict[str, ExecutionAdapter]):
        self._notifier = notifier
        self._anoncoin = anoncoin
        self._execution_by_mode = execution_by_mode
        self._tick = 0

    async def _rule_for_position(self, position) -> RuleParams:
        if position.rule_id:
            rules = await repo.get_all_rules()
            for r in rules:
                if r.id == position.rule_id:
                    from app.storage.repository import rule_row_to_params

                    return rule_row_to_params(r)
        return RuleParams()

    def _pnl_pct(self, entry_price: float, current_price: float) -> float:
        if entry_price <= 0:
            return 0.0
        return (current_price - entry_price) / entry_price * 100

    async def _close_position(self, position, token: TokenSnapshot, current_price: float, sell_pct: float, reason: str):
        adapter = self._execution_by_mode[position.mode]
        amount_to_sell = position.amount_tokens * (sell_pct / 100)
        result = await adapter.sell(token, amount_to_sell, sell_pct)

        exit_price = result.price_usd if result.success else current_price
        invested_portion = position.amount_sol_invested * (sell_pct / 100)
        # Approximate PnL using price ratio (Anoncoin exposes priceUsd/priceSol
        # interchangeably at MVP scale, so a ratio-based estimate keeps paper
        # trading self-consistent without needing a live SOL/USD feed).
        pnl_amount = invested_portion * (exit_price - position.entry_price_usd) / max(position.entry_price_usd, 1e-12)
        proceeds = invested_portion + pnl_amount

        await repo.create_order(
            position.mint, "sell", position.mode, "filled" if result.success else "failed",
            invested_portion, exit_price, result.tx_signature, result.error_message,
        )

        remaining_pct = max(0.0, position.remaining_pct - sell_pct)
        if position.mode == "paper" and result.success:
            from app.execution.paper import PaperExecutionAdapter

            if isinstance(adapter, PaperExecutionAdapter):
                await adapter.credit_balance(proceeds)

        if remaining_pct <= 0.01:
            import datetime as _dt

            await repo.update_position(
                position.id, status="closed", remaining_pct=0.0, closed_at=_dt.datetime.now(_dt.timezone.utc),
                close_reason=reason, realized_pnl_usd=position.realized_pnl_usd + pnl_amount,
            )
        else:
            await repo.update_position(
                position.id, remaining_pct=remaining_pct, realized_pnl_usd=position.realized_pnl_usd + pnl_amount,
            )

        await self._notifier.sell_triggered(token.ticker_symbol or token.mint[:8], reason, sell_pct)
        await self._notifier.sell_filled(token.ticker_symbol or token.mint[:8], exit_price, pnl_amount)

    async def check_position(self, position):
        token_row = await repo.get_token(position.mint)
        if not token_row:
            return
        token = TokenSnapshot(
            mint=position.mint,
            ticker_symbol=token_row.ticker_symbol,
            ticker_name=token_row.ticker_name,
            creator_wallet=token_row.creator_wallet,
            price_usd=position.entry_price_usd,
            source=token_row.source,
        )
        current_price, _is_sim = await get_current_price_usd(self._anoncoin, token, self._tick)
        token.price_usd = current_price

        rule = await self._rule_for_position(position)
        pnl_pct = self._pnl_pct(position.entry_price_usd, current_price)

        peak = max(position.peak_price_usd, current_price)
        if peak != position.peak_price_usd:
            await repo.update_position(position.id, peak_price_usd=peak)
            position.peak_price_usd = peak

        if rule.stop_loss_pct and pnl_pct <= -abs(rule.stop_loss_pct):
            await self._close_position(position, token, current_price, position.remaining_pct, "stop loss hit")
            return

        if rule.trailing_stop_pct and pnl_pct > 0:
            drop_from_peak = (peak - current_price) / peak * 100 if peak > 0 else 0
            if drop_from_peak >= rule.trailing_stop_pct:
                await self._close_position(position, token, current_price, position.remaining_pct, "trailing stop hit")
                return

        if rule.time_based_exit_seconds:
            import datetime as _dt

            age = (_dt.datetime.now(_dt.timezone.utc) - position.opened_at.replace(tzinfo=_dt.timezone.utc)).total_seconds()
            if age >= rule.time_based_exit_seconds:
                await self._close_position(position, token, current_price, position.remaining_pct, "time-based exit")
                return

        for idx, level in enumerate(rule.take_profit_levels):
            if idx in (position.tp_hit_indexes or []):
                continue
            if pnl_pct >= level.gain_pct:
                await repo.update_position(position.id, tp_hit_indexes=(position.tp_hit_indexes or []) + [idx])
                await self._close_position(position, token, current_price, level.sell_pct, f"take profit level {idx + 1}")
                return

    async def close_position_manually(self, position_id: int) -> bool:
        positions = await repo.get_open_positions()
        target = next((p for p in positions if p.id == position_id), None)
        if not target:
            return False
        token_row = await repo.get_token(target.mint)
        current_price, _ = await get_current_price_usd(
            self._anoncoin,
            TokenSnapshot(mint=target.mint, price_usd=target.entry_price_usd, source=token_row.source if token_row else "mock_simulated"),
            self._tick,
        )
        token = TokenSnapshot(
            mint=target.mint,
            ticker_symbol=token_row.ticker_symbol if token_row else target.mint[:8],
            price_usd=current_price,
            source=token_row.source if token_row else "mock_simulated",
        )
        await self._close_position(target, token, current_price, target.remaining_pct, "manual close via /positions")
        return True

    async def run_forever(self):
        while True:
            self._tick += 1
            try:
                positions = await repo.get_open_positions()
                for position in positions:
                    await self.check_position(position)
            except Exception:
                logger.exception("position_check_cycle_failed")
            await asyncio.sleep(settings.position_check_interval_seconds)
