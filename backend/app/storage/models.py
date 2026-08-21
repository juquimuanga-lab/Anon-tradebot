"""Database models covering tokens, screening, rules, orders, positions, state and audit log."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)

from sqlalchemy.orm import (
    Mapped,
    mapped_column,
)

from app.storage.database import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Token(Base):
    __tablename__ = "tokens"

    mint: Mapped[str] = mapped_column(
        String,
        primary_key=True,
    )

    ticker_name: Mapped[str] = mapped_column(
        String,
        default="",
    )

    ticker_symbol: Mapped[str] = mapped_column(
        String,
        default="",
    )

    creator_wallet: Mapped[str] = mapped_column(
        String,
        default="",
        index=True,
    )

    created_on: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    source: Mapped[str] = mapped_column(
        String,
        default="anoncoin",
    )

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )


class ScreeningResult(Base):
    __tablename__ = "screening_results"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    mint: Mapped[str] = mapped_column(
        ForeignKey("tokens.mint"),
        index=True,
    )

    passed_hard_filters: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    score: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    reasons: Mapped[list] = mapped_column(
        JSON,
        default=list,
    )

    liquidity_usd: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    holders: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    market_cap_usd: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    creator_match: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    snapshot: Mapped[dict] = mapped_column(
        JSON,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String,
        default="default",
    )

    # Rule scope: existing rules default to Solana so current Anoncoin/Pump.fun
    # behaviour is preserved. Four.meme rules are isolated to BSC/Four.meme.
    platform: Mapped[str] = mapped_column(
        String,
        default="solana",
        index=True,
    )

    strategy: Mapped[str] = mapped_column(
        String,
        default="smart",
        index=True,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    max_buy_size_sol: Mapped[float] = mapped_column(
        Float,
        default=0.1,
    )

    max_buy_size_bnb: Mapped[float] = mapped_column(
        Float,
        default=0.01,
    )

    min_liquidity_usd: Mapped[float] = mapped_column(
        Float,
        default=2500.0,
    )

    min_holders: Mapped[int] = mapped_column(
        Integer,
        default=30,
    )

    max_age_seconds: Mapped[int] = mapped_column(
        Integer,
        default=8,
    )

    creator_allowlist: Mapped[list] = mapped_column(
        JSON,
        default=list,
    )

    creator_denylist: Mapped[list] = mapped_column(
        JSON,
        default=list,
    )

    bonding_curve_phase: Mapped[str] = mapped_column(
        String,
        default="any",
    )

    min_market_cap_usd: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    max_market_cap_usd: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    max_slippage_pct: Mapped[float] = mapped_column(
        Float,
        default=2.0,
    )

    qualify_score_threshold: Mapped[float] = mapped_column(
        Float,
        default=52.0,
    )

    max_trades_per_hour: Mapped[int] = mapped_column(
        Integer,
        default=5,
    )

    cooldown_seconds: Mapped[int] = mapped_column(
        Integer,
        default=120,
    )

    take_profit_levels: Mapped[list] = mapped_column(
        JSON,
        default=list,
    )

    stop_loss_pct: Mapped[float] = mapped_column(
        Float,
        default=20.0,
    )

    trailing_stop_pct: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    sell_on_volume_drop_pct: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    time_based_exit_seconds: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    created_by: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )


class TradeDecision(Base):
    __tablename__ = "trade_decisions"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    mint: Mapped[str] = mapped_column(
        ForeignKey("tokens.mint"),
        index=True,
    )

    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("rules.id"),
        nullable=True,
    )

    decision: Mapped[str] = mapped_column(
        String,
        default="skip",
    )

    reason: Mapped[str] = mapped_column(
        Text,
        default="",
    )

    score: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    mint: Mapped[str] = mapped_column(
        ForeignKey("tokens.mint"),
        index=True,
    )

    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("rules.id"),
        nullable=True,
    )

    owner_user_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )

    side: Mapped[str] = mapped_column(
        String,
    )

    mode: Mapped[str] = mapped_column(
        String,
    )

    status: Mapped[str] = mapped_column(
        String,
        default="pending",
    )

    requested_amount_sol: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    price_usd: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    tx_signature: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    mint: Mapped[str] = mapped_column(
        ForeignKey("tokens.mint"),
        index=True,
    )

    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("rules.id"),
        nullable=True,
    )

    owner_user_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )

    # ---------------------------------------------------------------
    # Launch source
    #
    # This is critical for exits. A Pump.fun position must be sold
    # through the Pump.fun adapter, while an Anoncoin position must
    # continue using the existing Meteora/Jupiter path.
    # ---------------------------------------------------------------

    source: Mapped[str] = mapped_column(
        String,
        default="anoncoin_onchain",
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String,
        default="open",
    )

    mode: Mapped[str] = mapped_column(
        String,
        default="paper",
    )

    entry_price_usd: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    amount_tokens: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    amount_sol_invested: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    remaining_pct: Mapped[float] = mapped_column(
        Float,
        default=100.0,
    )

    peak_price_usd: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    entry_volume_24h_usd: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    peak_volume_24h_usd: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    tp_hit_indexes: Mapped[list] = mapped_column(
        JSON,
        default=list,
    )

    # True once the one-time defensive partial exit (-8% by default)
    # has successfully executed. Prevents repeated partial stop sells.
    defensive_exit_done: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    realized_pnl_usd: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    # Authoritative accounting ledger for live trades.
    entry_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    remaining_cost_basis_usd: Mapped[float] = mapped_column(Float, default=0.0)
    total_proceeds_usd: Mapped[float] = mapped_column(Float, default=0.0)
    total_fees_usd: Mapped[float] = mapped_column(Float, default=0.0)
    total_network_fee_usd: Mapped[float] = mapped_column(Float, default=0.0)

    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )

    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    close_reason: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )


class BotState(Base):
    __tablename__ = "bot_state"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    owner_user_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )

    mode: Mapped[str] = mapped_column(
        String,
        default="paper",
    )

    trading_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    anoncoin_trading_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    pumpfun_trading_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    # Independent Pump.fun strategy switches. Smart remains enabled by default
    # for backwards compatibility; Fast Sniper is opt-in.
    pumpfun_fast_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    pumpfun_smart_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    # Independent copy-trading trigger for configured smart-money wallets.
    # Disabled by default so adding a wallet never silently enables live buys.
    smart_money_copy_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    # Explicit rule assigned to the independent Smart Money copy-trading lane.
    # Nullable so existing BotState rows remain compatible.
    smart_money_rule_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    fourmeme_trading_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    paper_balance_sol: Mapped[float] = mapped_column(
        Float,
        default=10.0,
    )

    last_daily_summary_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    actor: Mapped[str] = mapped_column(
        String,
        default="system",
    )

    action: Mapped[str] = mapped_column(
        String,
    )

    details: Mapped[dict] = mapped_column(
        JSON,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )


class Secret(Base):
    __tablename__ = "secrets"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    key_name: Mapped[str] = mapped_column(
        String,
        unique=True,
        index=True,
    )

    encrypted_value: Mapped[str] = mapped_column(
        Text,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )
