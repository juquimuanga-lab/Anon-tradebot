"""Small repository helpers shared across scanner, positions and bot layers."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, text

from app.scoring.rules import RuleParams, TakeProfitLevel
from app.storage.database import async_session_scope
from app.storage.models import (
    AuditLog,
    BotState,
    Order,
    Position,
    Rule,
    ScreeningResult,
    Token,
    TradeDecision,
)


_BOT_STATE_SCHEMA_READY = False
_RULE_SCHEMA_READY = False


async def _ensure_bot_state_schema(session) -> None:
    global _BOT_STATE_SCHEMA_READY
    if _BOT_STATE_SCHEMA_READY:
        return
    for ddl in (
        "ALTER TABLE bot_state ADD COLUMN owner_user_id INTEGER",
        "ALTER TABLE bot_state ADD COLUMN pumpfun_fast_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE bot_state ADD COLUMN pumpfun_smart_enabled BOOLEAN DEFAULT TRUE",
        "ALTER TABLE bot_state ADD COLUMN smart_money_copy_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE bot_state ADD COLUMN smart_money_rule_id INTEGER",
        "ALTER TABLE bot_state ADD COLUMN guardian_enabled BOOLEAN DEFAULT TRUE",
        "ALTER TABLE bot_state ADD COLUMN guardian_auto_pause_enabled BOOLEAN DEFAULT TRUE",
        "ALTER TABLE bot_state ADD COLUMN guardian_last_status VARCHAR DEFAULT 'HEALTHY'",
        "ALTER TABLE bot_state ADD COLUMN guardian_pause_reason VARCHAR",
        "ALTER TABLE positions ADD COLUMN entry_cost_usd FLOAT DEFAULT 0",
        "ALTER TABLE positions ADD COLUMN remaining_cost_basis_usd FLOAT DEFAULT 0",
        "ALTER TABLE positions ADD COLUMN total_proceeds_usd FLOAT DEFAULT 0",
        "ALTER TABLE positions ADD COLUMN total_fees_usd FLOAT DEFAULT 0",
        "ALTER TABLE positions ADD COLUMN total_network_fee_usd FLOAT DEFAULT 0",
    ):
        try:
            await session.execute(text(ddl))
            await session.commit()
        except Exception:
            await session.rollback()
    try:
        await session.execute(text(
            "UPDATE rules SET min_liquidity_usd=2500, min_holders=30, max_market_cap_usd=35000, max_slippage_pct=2, qualify_score_threshold=55 "
            "WHERE platform='solana' AND min_liquidity_usd=2000 AND min_holders=25 AND max_market_cap_usd=55000 AND max_slippage_pct=5"
        ))
        await session.commit()
    except Exception:
        await session.rollback()
    _BOT_STATE_SCHEMA_READY = True


async def _ensure_rule_schema(session) -> None:
    global _RULE_SCHEMA_READY
    if _RULE_SCHEMA_READY:
        return
    for ddl in ("ALTER TABLE rules ADD COLUMN strategy VARCHAR DEFAULT 'smart'",):
        try:
            await session.execute(text(ddl))
            await session.commit()
        except Exception:
            await session.rollback()
    try:
        await session.execute(text("UPDATE rules SET strategy='smart' WHERE strategy IS NULL"))
        await session.commit()
    except Exception:
        await session.rollback()
    _RULE_SCHEMA_READY = True


async def get_or_create_bot_state(owner_user_id: Optional[int] = None) -> BotState:
    async with async_session_scope() as session:
        await _ensure_bot_state_schema(session)
        if owner_user_id is not None:
            state = (await session.execute(select(BotState).where(BotState.owner_user_id == owner_user_id))).scalar_one_or_none()
            if state:
                return state
            legacy = (await session.execute(select(BotState).where(BotState.id == 1))).scalar_one_or_none()
            if legacy:
                state = BotState(owner_user_id=owner_user_id, mode=legacy.mode, trading_enabled=legacy.trading_enabled, anoncoin_trading_enabled=legacy.anoncoin_trading_enabled, pumpfun_trading_enabled=legacy.pumpfun_trading_enabled, pumpfun_fast_enabled=getattr(legacy, "pumpfun_fast_enabled", False), pumpfun_smart_enabled=getattr(legacy, "pumpfun_smart_enabled", True), smart_money_copy_enabled=getattr(legacy, "smart_money_copy_enabled", False), smart_money_rule_id=getattr(legacy, "smart_money_rule_id", None), guardian_enabled=getattr(legacy, "guardian_enabled", True), guardian_auto_pause_enabled=getattr(legacy, "guardian_auto_pause_enabled", True), guardian_last_status=getattr(legacy, "guardian_last_status", "HEALTHY"), guardian_pause_reason=getattr(legacy, "guardian_pause_reason", None), fourmeme_trading_enabled=legacy.fourmeme_trading_enabled, paper_balance_sol=legacy.paper_balance_sol)
            else:
                from app.config.settings import settings
                state = BotState(owner_user_id=owner_user_id, mode=settings.trading_mode, paper_balance_sol=settings.paper_starting_balance_sol)
            session.add(state)
            await session.commit(); await session.refresh(state)
            return state
        state = (await session.execute(select(BotState).where(BotState.id == 1))).scalar_one_or_none()
        if not state:
            from app.config.settings import settings
            state = BotState(id=1, mode=settings.trading_mode, trading_enabled=True, anoncoin_trading_enabled=True, pumpfun_trading_enabled=True, fourmeme_trading_enabled=False, paper_balance_sol=settings.paper_starting_balance_sol)
            session.add(state); await session.commit(); await session.refresh(state)
        return state


async def update_bot_state(owner_user_id: Optional[int] = None, **kwargs) -> BotState:
    state = await get_or_create_bot_state(owner_user_id)
    async with async_session_scope() as session:
        state = (await session.execute(select(BotState).where(BotState.id == state.id))).scalar_one()
        for key, value in kwargs.items(): setattr(state, key, value)
        state.updated_at = datetime.now(timezone.utc)
        await session.commit(); await session.refresh(state)
        return state


async def get_active_rule_for(admin_id: int, platform: str = "solana") -> Optional[Rule]:
    async with async_session_scope() as session:
        await _ensure_rule_schema(session)
        return (await session.execute(select(Rule).where(Rule.is_active.is_(True), Rule.created_by == admin_id, Rule.platform == platform).order_by(Rule.id.desc()))).scalars().first()


async def get_active_rule_for_strategy(admin_id: int, platform: str = "solana", strategy: str = "smart") -> Optional[Rule]:
    if strategy not in ("fast", "smart", "smart_money"):
        strategy = "smart"
    async with async_session_scope() as session:
        await _ensure_rule_schema(session)
        return (await session.execute(select(Rule).where(Rule.is_active.is_(True), Rule.created_by == admin_id, Rule.platform == platform, Rule.strategy == strategy).order_by(Rule.id.desc()))).scalars().first()


async def get_smart_money_rules(platform: str = "solana") -> list[Rule]:
    async with async_session_scope() as session:
        await _ensure_bot_state_schema(session)
        rows = (await session.execute(text("SELECT owner_user_id, smart_money_rule_id FROM bot_state WHERE smart_money_copy_enabled = TRUE AND smart_money_rule_id IS NOT NULL"))).all()
        assignments = {int(row[1]): int(row[0]) for row in rows if row[0] is not None and row[1] is not None}
        if not assignments:
            return []
        rules = (await session.execute(select(Rule).where(Rule.id.in_(list(assignments.keys())), Rule.platform == platform, Rule.strategy == "smart_money"))).scalars().all()
        return [rule for rule in rules if assignments.get(int(rule.id)) == int(rule.created_by)]


async def get_active_smart_money_rule(admin_id: int, platform: str = "solana") -> Optional[Rule]:
    async with async_session_scope() as session:
        await _ensure_bot_state_schema(session)
        state = (await session.execute(select(BotState).where(BotState.owner_user_id == admin_id))).scalar_one_or_none()
        rule_id = state.smart_money_rule_id if state else None
        if not rule_id:
            return None
        return (await session.execute(select(Rule).where(Rule.id == rule_id, Rule.created_by == admin_id, Rule.platform == platform, Rule.strategy == "smart_money"))).scalars().first()


async def set_smart_money_rule(rule_id: int, admin_id: int) -> Optional[Rule]:
    async with async_session_scope() as session:
        await _ensure_bot_state_schema(session)
        target = (await session.execute(select(Rule).where(Rule.id == rule_id, Rule.created_by == admin_id, Rule.platform == "solana", Rule.strategy == "smart_money"))).scalars().first()
        if not target:
            return None
        state = (await session.execute(select(BotState).where(BotState.owner_user_id == admin_id))).scalar_one_or_none()
        if not state:
            legacy = (await session.execute(select(BotState).where(BotState.id == 1))).scalar_one_or_none()
            if legacy:
                state = BotState(owner_user_id=admin_id, mode=legacy.mode, trading_enabled=legacy.trading_enabled, anoncoin_trading_enabled=legacy.anoncoin_trading_enabled, pumpfun_trading_enabled=legacy.pumpfun_trading_enabled, pumpfun_fast_enabled=getattr(legacy, "pumpfun_fast_enabled", False), pumpfun_smart_enabled=getattr(legacy, "pumpfun_smart_enabled", True), smart_money_copy_enabled=getattr(legacy, "smart_money_copy_enabled", False), smart_money_rule_id=getattr(legacy, "smart_money_rule_id", None), fourmeme_trading_enabled=legacy.fourmeme_trading_enabled, paper_balance_sol=legacy.paper_balance_sol)
            else:
                from app.config.settings import settings
                state = BotState(owner_user_id=admin_id, mode=settings.trading_mode, paper_balance_sol=settings.paper_starting_balance_sol)
            session.add(state)
        state.smart_money_rule_id = rule_id; state.updated_at = datetime.now(timezone.utc)
        await session.commit(); await session.refresh(target)
        return target


async def get_all_active_rules() -> list[Rule]:
    async with async_session_scope() as session:
        await _ensure_rule_schema(session)
        return list((await session.execute(select(Rule).where(Rule.is_active.is_(True)))).scalars().all())


async def get_active_rules_for_platform(platform: str) -> list[Rule]:
    async with async_session_scope() as session:
        await _ensure_rule_schema(session)
        return list((await session.execute(select(Rule).where(Rule.is_active.is_(True), Rule.platform == platform))).scalars().all())


async def get_rules_for_admin(admin_id: int) -> list[Rule]:
    async with async_session_scope() as session:
        await _ensure_rule_schema(session)
        return list((await session.execute(select(Rule).where(Rule.created_by == admin_id).order_by(Rule.id.desc()))).scalars().all())


def rule_row_to_params(rule: Rule) -> RuleParams:
    # Critical Pons fix: pass the DB platform through unchanged. RuleParams now
    # accepts "pons" natively, so no Solana coercion or mutation is performed.
    return RuleParams(
        name=rule.name,
        platform=getattr(rule, "platform", "solana") or "solana",
        strategy=getattr(rule, "strategy", "smart") or "smart",
        max_buy_size_sol=rule.max_buy_size_sol,
        max_buy_size_bnb=getattr(rule, "max_buy_size_bnb", 0.01) or 0.01,
        min_liquidity_usd=rule.min_liquidity_usd,
        min_holders=rule.min_holders,
        max_age_seconds=rule.max_age_seconds,
        creator_allowlist=rule.creator_allowlist or [],
        creator_denylist=rule.creator_denylist or [],
        bonding_curve_phase=rule.bonding_curve_phase,
        min_market_cap_usd=rule.min_market_cap_usd,
        max_market_cap_usd=rule.max_market_cap_usd,
        max_slippage_pct=rule.max_slippage_pct,
        qualify_score_threshold=getattr(rule, "qualify_score_threshold", 52.0) or 52.0,
        max_trades_per_hour=rule.max_trades_per_hour,
        cooldown_seconds=rule.cooldown_seconds,
        take_profit_levels=[TakeProfitLevel(**x) if isinstance(x, dict) else x for x in (rule.take_profit_levels or [])],
        stop_loss_pct=rule.stop_loss_pct,
        trailing_stop_pct=rule.trailing_stop_pct,
        sell_on_volume_drop_pct=rule.sell_on_volume_drop_pct,
        time_based_exit_seconds=rule.time_based_exit_seconds,
    )
