"""Rule parameters, hard filters, and the token snapshot shape used across the app."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

BondingCurvePhase = Literal["any", "pre_graduation", "post_graduation"]
SniperStrategy = Literal["smart", "fast", "smart_money"]


class TakeProfitLevel(BaseModel):
    gain_pct: float
    sell_pct: float


class RuleParams(BaseModel):
    name: str = "default"
    # Platform-specific rules: existing rules default to Solana (Anoncoin/Pump.fun).
    platform: Literal["solana", "fourmeme"] = "solana"
    # Pump.fun strategy lane. Existing rules default to Smart for compatibility.
    strategy: SniperStrategy = "smart"
    max_buy_size_sol: float = 0.1
    max_buy_size_bnb: float = 0.01
    min_liquidity_usd: float = 2500.0
    min_holders: int = 30
    max_age_seconds: int = 8
    creator_allowlist: List[str] = Field(default_factory=list)
    creator_denylist: List[str] = Field(default_factory=list)
    bonding_curve_phase: BondingCurvePhase = "any"
    min_market_cap_usd: Optional[float] = 7000.0
    max_market_cap_usd: Optional[float] = 35000.0
    max_slippage_pct: float = 2.0
    qualify_score_threshold: float = 55.0
    max_trades_per_hour: int = 5
    cooldown_seconds: int = 120
    take_profit_levels: List[TakeProfitLevel] = Field(default_factory=lambda: [TakeProfitLevel(gain_pct=15.0, sell_pct=80.0)])
    stop_loss_pct: float = 20.0
    trailing_stop_pct: Optional[float] = None
    sell_on_volume_drop_pct: Optional[float] = None
    time_based_exit_seconds: Optional[int] = None

    # Pump.fun anti-late-entry controls. These defaults are intentionally
    # conservative and apply without requiring a database migration.
    late_entry_enabled: bool = True
    late_entry_max_age_seconds: float = 5.0
    late_entry_soft_market_cap_usd: float = 18000.0
    late_entry_hard_market_cap_usd: float = 28000.0
    late_entry_near_high_pct: float = 4.0
    late_entry_required_pullback_pct: float = 8.0
    late_entry_max_short_runup_pct: float = 35.0
    late_entry_max_runup_from_first_pct: float = 90.0

    # Pump.fun Graduation Hunter deployment defaults. These are deliberately
    # rule-level defaults so existing database rows work without a migration.
    graduation_hunter_enabled: bool = True
    graduation_hunter_min_observation_seconds: float = 20.0
    graduation_hunter_max_observation_seconds: float = 800.0
    graduation_hunter_target_real_sol: float = 85.0
    graduation_hunter_min_real_sol: float = 0.05
    graduation_hunter_max_real_sol: float = 35.0
    graduation_hunter_min_buy_sell_ratio: float = 1.2
    graduation_hunter_min_unique_buyers: int = 3
    graduation_hunter_min_buyer_diversity: float = 0.15
    graduation_hunter_max_top10_buyer_share: float = 1.0
    graduation_hunter_min_holder_growth_per_minute: float = 5.0
    graduation_hunter_score_threshold: float = 60.0
    graduation_hunter_momentum_override_enabled: bool = True
    graduation_hunter_momentum_min_buy_pressure: float = 0.70
    graduation_hunter_momentum_min_buy_sell_ratio: float = 2.50
    graduation_hunter_momentum_min_buy_velocity_sol_per_sec: float = 0.015
    graduation_hunter_momentum_min_unique_buyers: int = 5
    graduation_hunter_momentum_max_top_buyer_share: float = 0.50
    graduation_hunter_momentum_max_top3_buyer_share: float = 0.85
    graduation_hunter_momentum_min_buyer_diversity: float = 0.20


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
    # Pump.fun real bonding-curve SOL reserve. This is the progression signal
    # used by Graduation Hunter; virtual reserves are intentionally ignored.
    real_sol_reserves_sol: float = 0.0
    real_sol_progress_pct: float = 0.0
    holders: Optional[int] = None
    volume_24h_usd: float = 0.0
    is_migrated: bool = False
    decimals: int = 6
    source: str = "anoncoin"
    raw_anoncoin: dict = field(default_factory=dict)
    raw_enrichment: dict = field(default_factory=dict)

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

    if token.holders is None:
        reasons.append("holder count unavailable")
    elif token.holders < rule.min_holders:
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



def evaluate_fast_sniper_filters(token: TokenSnapshot, rule: RuleParams) -> tuple[bool, list[str]]:
    """Very small pre-trade safety gate for the Pump.fun Fast Sniper lane.

    This intentionally avoids holder counts and the full quality score. Those
    are lagging signals for a sub-second launch strategy. Only launch-time
    safety and price/liquidity bounds are enforced here.
    """
    reasons: list[str] = []
    if token.source != "pumpfun":
        return False, ["fast sniper is only available for Pump.fun"]

    if token.creator_wallet and token.creator_wallet in rule.creator_denylist:
        reasons.append("creator is denylisted")
    if rule.creator_allowlist and token.creator_wallet not in rule.creator_allowlist:
        reasons.append("creator not in allowlist")

    # Fast lane is intentionally capped at two seconds even if an admin's
    # generic rule was accidentally configured with a much larger age.
    fast_age_limit = min(float(rule.max_age_seconds), 2.0)
    if token.age_seconds > fast_age_limit:
        reasons.append(f"fast entry window expired ({token.age_seconds:.2f}s > {fast_age_limit:.2f}s)")

    if token.liquidity_usd < rule.min_liquidity_usd:
        reasons.append(f"liquidity ${token.liquidity_usd:,.0f} below min ${rule.min_liquidity_usd:,.0f}")

    if rule.min_market_cap_usd is not None and token.market_cap_usd < rule.min_market_cap_usd:
        reasons.append(f"market cap ${token.market_cap_usd:,.0f} below min ${rule.min_market_cap_usd:,.0f}")
    if rule.max_market_cap_usd is not None and token.market_cap_usd > rule.max_market_cap_usd:
        reasons.append(f"market cap ${token.market_cap_usd:,.0f} above max ${rule.max_market_cap_usd:,.0f}")

    if rule.bonding_curve_phase != "any" and token.bonding_curve_phase != rule.bonding_curve_phase:
        reasons.append(f"bonding curve phase {token.bonding_curve_phase} != required {rule.bonding_curve_phase}")

    # Reuse the observed launch history for a lightweight anti-chase guard.
    history = (getattr(token, "raw_enrichment", {}) or {}).get("late_entry_history") or {}
    first_price = float(history.get("first_price_usd", 0.0) or 0.0)
    current_price = float(token.price_usd or 0.0)
    previous = history.get("previous") or {}
    previous_price = float(previous.get("price_usd", 0.0) or 0.0)
    previous_ts = float(previous.get("timestamp", 0.0) or 0.0)
    now_ts = float(history.get("timestamp", 0.0) or 0.0)

    if first_price > 0 and current_price > 0:
        runup = (current_price - first_price) / first_price * 100.0
        # Do not chase a launch that has already moved +60% in the tiny fast window.
        if runup >= 60.0:
            reasons.append(f"fast anti-chase: +{runup:.0f}% from first observed price")

    if previous_price > 0 and current_price > 0 and now_ts and previous_ts:
        dt = now_ts - previous_ts
        if 0 <= dt <= 1.5:
            short_runup = (current_price - previous_price) / previous_price * 100.0
            if short_runup >= 30.0:
                reasons.append(f"fast anti-chase: +{short_runup:.0f}% in {dt:.2f}s")

    return len(reasons) == 0, reasons

def evaluate_late_entry(token: TokenSnapshot, rule: RuleParams) -> tuple[bool, list[str], dict]:
    """Reject Pump.fun entries that are already in a vertical/late phase.

    The normal score rewards momentum. This separate gate prevents momentum
    from becoming a reason to buy the top: a token can score highly and still
    be rejected when its price is extended, close to its tracked local high,
    or accelerating too quickly at a high market cap.

    The scanner supplies a small in-memory history in ``raw_enrichment``.
    Missing history is handled conservatively using age/market-cap guards.
    """
    if not rule.late_entry_enabled or getattr(token, "source", "") != "pumpfun":
        return True, [], {"enabled": False}

    history = (getattr(token, "raw_enrichment", {}) or {}).get("late_entry_history") or {}
    now = float(history.get("timestamp", 0.0) or 0.0)
    previous = history.get("previous") or {}
    first_price = float(history.get("first_price_usd", 0.0) or 0.0)
    first_mc = float(history.get("first_market_cap_usd", 0.0) or 0.0)
    peak_price = float(history.get("peak_price_usd", 0.0) or 0.0)
    current_price = float(getattr(token, "price_usd", 0.0) or 0.0)
    current_mc = float(getattr(token, "market_cap_usd", 0.0) or 0.0)
    age = float(getattr(token, "age_seconds", 0.0) or 0.0)

    if peak_price <= 0:
        peak_price = current_price

    def pct(now_value: float, old_value: float):
        if old_value <= 0:
            return None
        return (now_value - old_value) / old_value * 100.0

    runup_from_first = pct(current_price, first_price)
    mc_runup_from_first = pct(current_mc, first_mc)
    drawdown_from_peak = pct(current_price, peak_price)
    distance_from_peak = abs(drawdown_from_peak) if drawdown_from_peak is not None else None

    previous_price = float(previous.get("price_usd", 0.0) or 0.0)
    previous_timestamp = float(previous.get("timestamp", 0.0) or 0.0)
    sample_age = (now - previous_timestamp) if now and previous_timestamp else None
    short_runup = (
        pct(current_price, previous_price)
        if previous_price > 0 and sample_age is not None and 0 <= sample_age <= 3.0
        else None
    )

    breakdown = {
        "age_seconds": round(age, 2),
        "market_cap_usd": round(current_mc, 2),
        "price_usd": current_price,
        "first_price_usd": first_price,
        "first_market_cap_usd": first_mc,
        "runup_from_first_pct": None if runup_from_first is None else round(runup_from_first, 2),
        "mc_runup_from_first_pct": None if mc_runup_from_first is None else round(mc_runup_from_first, 2),
        "peak_price_usd": peak_price,
        "distance_from_peak_pct": None if distance_from_peak is None else round(distance_from_peak, 2),
        "short_runup_pct": None if short_runup is None else round(short_runup, 2),
    }

    reasons: list[str] = []

    # At the soft market-cap zone, do not enter late in the launch window.
    if (
        current_mc >= rule.late_entry_soft_market_cap_usd
        and age > rule.late_entry_max_age_seconds
    ):
        reasons.append(
            f"late entry: market cap ${current_mc:,.0f} at {age:.1f}s exceeds "
            f"the {rule.late_entry_max_age_seconds:.1f}s entry window"
        )

    # High-cap launches must have pulled back before we chase them.
    if (
        current_mc >= rule.late_entry_hard_market_cap_usd
        and distance_from_peak is not None
        and distance_from_peak < rule.late_entry_required_pullback_pct
    ):
        reasons.append(
            f"late entry: ${current_mc:,.0f} market cap is only "
            f"{distance_from_peak:.1f}% below the local high; "
            f"requires {rule.late_entry_required_pullback_pct:.1f}% pullback"
        )

    # If the launch has already doubled/tripled from the first observed price
    # and is still sitting near its high, treat momentum as exhaustion risk.
    if (
        current_mc >= rule.late_entry_soft_market_cap_usd
        and runup_from_first is not None
        and runup_from_first >= rule.late_entry_max_runup_from_first_pct
        and distance_from_peak is not None
        and distance_from_peak <= rule.late_entry_near_high_pct
    ):
        reasons.append(
            f"late entry: price is +{runup_from_first:.0f}% from first observed "
            f"price and within {distance_from_peak:.1f}% of the local high"
        )

    # A very fast green candle immediately before entry is a classic chase
    # signature. Only apply it once the token is already meaningfully valued.
    if (
        current_mc >= rule.late_entry_soft_market_cap_usd
        and short_runup is not None
        and short_runup >= rule.late_entry_max_short_runup_pct
    ):
        reasons.append(
            f"late entry: price accelerated +{short_runup:.0f}% in ~{sample_age:.1f}s"
        )

    return len(reasons) == 0, reasons, breakdown
