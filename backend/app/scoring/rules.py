"""Rule parameters, hard filters, and the token snapshot shape used across the app."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

BondingCurvePhase = Literal["any", "pre_graduation", "post_graduation"]


class TakeProfitLevel(BaseModel):
    gain_pct: float
    sell_pct: float


class RuleParams(BaseModel):
    name: str = "default"
    max_buy_size_sol: float = 0.1
    min_liquidity_usd: float = 1000.0
    min_holders: int = 10
    max_age_seconds: int = 600
    creator_allowlist: List[str] = Field(default_factory=list)
    creator_denylist: List[str] = Field(default_factory=list)
    bonding_curve_phase: BondingCurvePhase = "any"
    min_market_cap_usd: Optional[float] = None
    max_market_cap_usd: Optional[float] = None
    max_slippage_pct: float = 5.0
    max_trades_per_hour: int = 5
    cooldown_seconds: int = 120
    take_profit_levels: List[TakeProfitLevel] = Field(default_factory=list)
    stop_loss_pct: float = 20.0
    trailing_stop_pct: Optional[float] = None
    sell_on_volume_drop_pct: Optional[float] = None
    time_based_exit_seconds: Optional[int] = None


@dataclass
class TokenSnapshot:
    mint: str
    ticker_name: str = ""
    ticker_symbol: str = ""
    creator_wallet: str = ""
    created_on: Optional[datetime] = None
    price_usd: float = 0.0
    market_cap_usd: float = 0.0
    liquidity_usd: float = 0.0
    holders: int = 0
    volume_24h_usd: float = 0.0
    is_migrated: bool = False
    decimals: int = 6
    source: str = "anoncoin"
    raw_anoncoin: dict = field(default_factory=dict)
    raw_solscan: dict = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        if not self.created_on:
            return 0.0
        created = self.created_on
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())

    @property
    def bonding_curve_phase(self) -> str:
        return "post_graduation" if self.is_migrated else "pre_graduation"


def evaluate_hard_filters(token: TokenSnapshot, rule: RuleParams) -> tuple[bool, list[str]]:
    """Reject fast on any hard filter violation. Returns (passed, reasons)."""
    reasons: list[str] = []

    if token.creator_wallet and token.creator_wallet in rule.creator_denylist:
        reasons.append(f"creator {token.creator_wallet} is denylisted")

    if rule.creator_allowlist and token.creator_wallet not in rule.creator_allowlist:
        reasons.append("creator not in allowlist")

    if token.liquidity_usd < rule.min_liquidity_usd:
        reasons.append(f"liquidity ${token.liquidity_usd:,.0f} below min ${rule.min_liquidity_usd:,.0f}")

    if token.holders < rule.min_holders:
        reasons.append(f"holders {token.holders} below min {rule.min_holders}")

    if token.age_seconds > rule.max_age_seconds:
        reasons.append(f"age {token.age_seconds:.0f}s exceeds max {rule.max_age_seconds}s")

    if rule.bonding_curve_phase != "any" and token.bonding_curve_phase != rule.bonding_curve_phase:
        reasons.append(f"bonding curve phase {token.bonding_curve_phase} != required {rule.bonding_curve_phase}")

    if rule.min_market_cap_usd is not None and token.market_cap_usd < rule.min_market_cap_usd:
        reasons.append(f"market cap ${token.market_cap_usd:,.0f} below min ${rule.min_market_cap_usd:,.0f}")

    if rule.max_market_cap_usd is not None and token.market_cap_usd > rule.max_market_cap_usd:
        reasons.append(f"market cap ${token.market_cap_usd:,.0f} above max ${rule.max_market_cap_usd:,.0f}")

    return (len(reasons) == 0, reasons)
