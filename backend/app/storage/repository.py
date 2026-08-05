"""Small repository helpers shared across scanner, positions and bot layers."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from app.scoring.rules import RuleParams, TakeProfitLevel
from app.storage.database import async_session_scope
from app.storage.models import AuditLog, BotState, Order, Position, Rule, ScreeningResult, Token, TradeDecision


async def get_or_create_bot_state() -> BotState:
    async with async_session_scope() as session:
        state = (await session.execute(select(BotState).where(BotState.id == 1))).scalar_one_or_none()
        if not state:
            from app.config.settings import settings

            state = BotState(
                id=1,
                mode=settings.trading_mode,
                trading_enabled=True,
                paper_balance_sol=settings.paper_starting_balance_sol,
            )
            session.add(state)
            await session.commit()
            await session.refresh(state)
        return state


async def update_bot_state(**kwargs) -> BotState:
    await get_or_create_bot_state()
    async with async_session_scope() as session:
        state = (await session.execute(select(BotState).where(BotState.id == 1))).scalar_one_or_none()
        for key, value in kwargs.items():
            setattr(state, key, value)
        state.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(state)
        return state


async def get_active_rule() -> Optional[Rule]:
    async with async_session_scope() as session:
        return (
            await session.execute(select(Rule).where(Rule.is_active.is_(True)).order_by(Rule.id.desc()))
        ).scalars().first()


async def token_already_seen(mint: str) -> bool:
    async with async_session_scope() as session:
        return (
            await session.execute(select(Token).where(Token.mint == mint))
        ).scalar_one_or_none() is not None


async def has_open_or_pending_position(mint: str) -> bool:
    async with async_session_scope() as session:
        existing = (
            await session.execute(
                select(Position).where(Position.mint == mint, Position.status == "open")
            )
        ).scalar_one_or_none()
        return existing is not None


async def recent_buy_count(hours: float = 1.0) -> int:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with async_session_scope() as session:
        rows = (
            await session.execute(
                select(Order).where(Order.side == "buy", Order.created_at >= since, Order.status == "filled")
            )
        ).scalars().all()
        return len(rows)


async def seconds_since_last_buy() -> Optional[float]:
    async with async_session_scope() as session:
        last = (
            await session.execute(
                select(Order).where(Order.side == "buy", Order.status == "filled").order_by(Order.created_at.desc())
            )
        ).scalars().first()
        if not last:
            return None
        return (datetime.now(timezone.utc) - last.created_at.replace(tzinfo=timezone.utc)).total_seconds()


async def write_audit_log(actor: str, action: str, details: dict) -> None:
    async with async_session_scope() as session:
        session.add(AuditLog(actor=actor, action=action, details=details))
        await session.commit()


async def save_token(token) -> None:
    async with async_session_scope() as session:
        existing = (await session.execute(select(Token).where(Token.mint == token.mint))).scalar_one_or_none()
        if existing:
            return
        session.add(
            Token(
                mint=token.mint,
                ticker_name=token.ticker_name,
                ticker_symbol=token.ticker_symbol,
                creator_wallet=token.creator_wallet,
                created_on=token.created_on,
                source=token.source,
            )
        )
        await session.commit()


async def save_screening_result(mint: str, passed: bool, score: float, reasons: list, liquidity_usd: float,
                                 holders: int, market_cap_usd: float, creator_match: bool, snapshot: dict) -> None:
    async with async_session_scope() as session:
        session.add(
            ScreeningResult(
                mint=mint,
                passed_hard_filters=passed,
                score=score,
                reasons=reasons,
                liquidity_usd=liquidity_usd,
                holders=holders,
                market_cap_usd=market_cap_usd,
                creator_match=creator_match,
                snapshot=snapshot,
            )
        )
        await session.commit()


async def save_trade_decision(mint: str, rule_id: Optional[int], decision: str, reason: str, score: float) -> None:
    async with async_session_scope() as session:
        session.add(TradeDecision(mint=mint, rule_id=rule_id, decision=decision, reason=reason, score=score))
        await session.commit()


async def create_order(mint: str, side: str, mode: str, status: str, requested_amount_sol: float,
                        price_usd: float, tx_signature: Optional[str] = None,
                        error_message: Optional[str] = None) -> Order:
    async with async_session_scope() as session:
        order = Order(
            mint=mint,
            side=side,
            mode=mode,
            status=status,
            requested_amount_sol=requested_amount_sol,
            price_usd=price_usd,
            tx_signature=tx_signature,
            error_message=error_message,
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)
        return order


async def create_position(mint: str, rule_id: Optional[int], mode: str, entry_price_usd: float,
                           amount_tokens: float, amount_sol_invested: float) -> Position:
    async with async_session_scope() as session:
        position = Position(
            mint=mint,
            rule_id=rule_id,
            mode=mode,
            entry_price_usd=entry_price_usd,
            amount_tokens=amount_tokens,
            amount_sol_invested=amount_sol_invested,
            peak_price_usd=entry_price_usd,
        )
        session.add(position)
        await session.commit()
        await session.refresh(position)
        return position


async def get_open_positions() -> list[Position]:
    async with async_session_scope() as session:
        return list((await session.execute(select(Position).where(Position.status == "open"))).scalars().all())


async def update_position(position_id: int, **kwargs) -> Position:
    async with async_session_scope() as session:
        position = (await session.execute(select(Position).where(Position.id == position_id))).scalar_one()
        for key, value in kwargs.items():
            setattr(position, key, value)
        await session.commit()
        await session.refresh(position)
        return position


async def get_recent_orders(limit: int = 10) -> list[Order]:
    async with async_session_scope() as session:
        rows = (await session.execute(select(Order).order_by(Order.created_at.desc()).limit(limit))).scalars().all()
        return list(rows)


async def get_token(mint: str) -> Optional[Token]:
    async with async_session_scope() as session:
        return (await session.execute(select(Token).where(Token.mint == mint))).scalar_one_or_none()


async def get_all_rules() -> list[Rule]:
    async with async_session_scope() as session:
        rows = (await session.execute(select(Rule).order_by(Rule.id.desc()))).scalars().all()
        return list(rows)


async def get_closed_positions() -> list[Position]:
    async with async_session_scope() as session:
        rows = (await session.execute(select(Position).where(Position.status == "closed"))).scalars().all()
        return list(rows)


def rule_row_to_params(rule: Rule) -> RuleParams:
    return RuleParams(
        name=rule.name,
        max_buy_size_sol=rule.max_buy_size_sol,
        min_liquidity_usd=rule.min_liquidity_usd,
        min_holders=rule.min_holders,
        max_age_seconds=rule.max_age_seconds,
        creator_allowlist=rule.creator_allowlist or [],
        creator_denylist=rule.creator_denylist or [],
        bonding_curve_phase=rule.bonding_curve_phase,
        min_market_cap_usd=rule.min_market_cap_usd,
        max_market_cap_usd=rule.max_market_cap_usd,
        max_slippage_pct=rule.max_slippage_pct,
        max_trades_per_hour=rule.max_trades_per_hour,
        cooldown_seconds=rule.cooldown_seconds,
        take_profit_levels=[TakeProfitLevel(**lvl) for lvl in (rule.take_profit_levels or [])],
        stop_loss_pct=rule.stop_loss_pct,
        trailing_stop_pct=rule.trailing_stop_pct,
        sell_on_volume_drop_pct=rule.sell_on_volume_drop_pct,
        time_based_exit_seconds=rule.time_based_exit_seconds,
    )


async def create_rule(params: RuleParams, created_by: int, activate: bool = True) -> Rule:
    async with async_session_scope() as session:
        if activate:
            await session.execute(
                Rule.__table__.update().where(Rule.is_active.is_(True)).values(is_active=False)
            )
        rule = Rule(
            name=params.name,
            is_active=activate,
            max_buy_size_sol=params.max_buy_size_sol,
            min_liquidity_usd=params.min_liquidity_usd,
            min_holders=params.min_holders,
            max_age_seconds=params.max_age_seconds,
            creator_allowlist=params.creator_allowlist,
            creator_denylist=params.creator_denylist,
            bonding_curve_phase=params.bonding_curve_phase,
            min_market_cap_usd=params.min_market_cap_usd,
            max_market_cap_usd=params.max_market_cap_usd,
            max_slippage_pct=params.max_slippage_pct,
            max_trades_per_hour=params.max_trades_per_hour,
            cooldown_seconds=params.cooldown_seconds,
            take_profit_levels=[lvl.model_dump() for lvl in params.take_profit_levels],
            stop_loss_pct=params.stop_loss_pct,
            trailing_stop_pct=params.trailing_stop_pct,
            sell_on_volume_drop_pct=params.sell_on_volume_drop_pct,
            time_based_exit_seconds=params.time_based_exit_seconds,
            created_by=created_by,
        )
        session.add(rule)
        await session.commit()
        await session.refresh(rule)
        return rule
