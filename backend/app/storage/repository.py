"""Small repository helpers shared across scanner, positions and bot layers."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, text

from app.scoring.rules import RuleParams, TakeProfitLevel
from app.storage.database import async_session_scope
from app.storage.models import AuditLog, BotState, Order, Position, Rule, ScreeningResult, Token, TradeDecision

_BOT_STATE_SCHEMA_READY = False
_RULE_SCHEMA_READY = False

# NOTE: keep the existing repository implementation intact. The only Pons-specific
# requirement is that rule_row_to_params preserves the database platform value;
# RuleParams now accepts "pons" natively, so no coercion is required.

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
            await session.execute(text(ddl)); await session.commit()
        except Exception:
            await session.rollback()
    try:
        await session.execute(text(
            "UPDATE rules SET min_liquidity_usd=2500, min_holders=30, max_market_cap_usd=35000, max_slippage_pct=2, qualify_score_threshold=55 "
            "WHERE platform='solana' AND min_liquidity_usd=2000 AND min_holders=25 AND max_market_cap_usd=55000 AND max_slippage_pct=5"
        )); await session.commit()
    except Exception:
        await session.rollback()
    _BOT_STATE_SCHEMA_READY = True

async def _ensure_rule_schema(session) -> None:
    global _RULE_SCHEMA_READY
    if _RULE_SCHEMA_READY:
        return
    for ddl in ("ALTER TABLE rules ADD COLUMN strategy VARCHAR DEFAULT 'smart'",):
        try:
            await session.execute(text(ddl)); await session.commit()
        except Exception:
            await session.rollback()
    try:
        await session.execute(text("UPDATE rules SET strategy='smart' WHERE strategy IS NULL")); await session.commit()
    except Exception:
        await session.rollback()
    _RULE_SCHEMA_READY = True

# ... existing repository functions remain unchanged in the deployed file ...


def rule_row_to_params(rule: Rule) -> RuleParams:
    """Convert a DB Rule without platform coercion; Pons is a real platform."""
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
        qualify_score_threshold=rule.qualify_score_threshold,
        max_trades_per_hour=rule.max_trades_per_hour,
        cooldown_seconds=rule.cooldown_seconds,
        take_profit_levels=[TakeProfitLevel(**x) if isinstance(x, dict) else x for x in (rule.take_profit_levels or [])],
        stop_loss_pct=rule.stop_loss_pct,
        trailing_stop_pct=rule.trailing_stop_pct,
        sell_on_volume_drop_pct=rule.sell_on_volume_drop_pct,
        time_based_exit_seconds=rule.time_based_exit_seconds,
    )
