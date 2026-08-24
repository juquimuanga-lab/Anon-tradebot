"""Simple weighted scoring model (0-100) for tokens that pass hard filters."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from app.scoring.rules import RuleParams, TokenSnapshot

CREATOR_MATCH_BONUS = 25.0

WEIGHTS = {
    "liquidity": 20.0,
    "holders": 15.0,
    "freshness": 15.0,
    "volume": 10.0,
    "market_cap_fit": 15.0,
    "momentum": 25.0,
}



@dataclass
class ScoreResult:
    score: float
    creator_match: bool
    breakdown: dict = field(default_factory=dict)


def _liquidity_score(token: TokenSnapshot, rule: RuleParams) -> float:
    if rule.min_liquidity_usd <= 0:
        return WEIGHTS["liquidity"]
    ratio = token.liquidity_usd / rule.min_liquidity_usd
    return min(WEIGHTS["liquidity"], WEIGHTS["liquidity"] * min(ratio, 3) / 3 * 1.5)


def _holders_score(token: TokenSnapshot, rule: RuleParams) -> float:
    if rule.min_holders <= 0:
        return WEIGHTS["holders"]
    ratio = token.holders / rule.min_holders
    return min(WEIGHTS["holders"], WEIGHTS["holders"] * min(ratio, 3) / 3 * 1.5)


def _freshness_score(token: TokenSnapshot, rule: RuleParams) -> float:
    if rule.max_age_seconds <= 0:
        return 0.0
    remaining = max(0.0, rule.max_age_seconds - token.age_seconds)
    return WEIGHTS["freshness"] * (remaining / rule.max_age_seconds)


def _volume_score(token: TokenSnapshot) -> float:
    if token.liquidity_usd <= 0:
        return 0.0
    ratio = token.volume_24h_usd / max(token.liquidity_usd, 1.0)
    return min(WEIGHTS["volume"], WEIGHTS["volume"] * min(ratio, 2) / 2)


def _market_cap_fit_score(token: TokenSnapshot, rule: RuleParams) -> float:
    lo = rule.min_market_cap_usd or 0.0
    hi = rule.max_market_cap_usd or float("inf")
    if lo <= token.market_cap_usd <= hi:
        return WEIGHTS["market_cap_fit"]
    return WEIGHTS["market_cap_fit"] * 0.3


def _momentum_score(token: TokenSnapshot) -> float:
    """Score short-window MC/liquidity/holder/volume acceleration.

    The scanner places a previous snapshot in raw_enrichment. Missing history
    is neutral rather than a penalty, so a token is not rejected solely
    because its first snapshot has no predecessor.
    """
    history = getattr(token, "raw_enrichment", {}) or {}
    previous = history.get("momentum_previous")
    if not previous:
        return 0.0

    def pct(now, old):
        if old is None or float(old) <= 0:
            return 0.0
        return (float(now) - float(old)) / float(old) * 100.0

    mc = pct(token.market_cap_usd, previous.get("market_cap_usd"))
    liq = pct(token.liquidity_usd, previous.get("liquidity_usd"))
    holders = pct(token.holders or 0, previous.get("holders"))
    volume = pct(token.volume_24h_usd, previous.get("volume_24h_usd"))

    # Positive acceleration is rewarded; falling MC/liquidity is strongly
    # de-emphasized. Volume is useful but noisy, so it carries less influence.
    component = (
        max(-25.0, min(100.0, mc)) * 0.45
        + max(-25.0, min(100.0, liq)) * 0.25
        + max(-25.0, min(100.0, holders)) * 0.15
        + max(-25.0, min(100.0, volume)) * 0.15
    )
    normalized = max(0.0, min(1.0, (component + 25.0) / 125.0))
    return WEIGHTS["momentum"] * normalized


def _late_entry_risk(token: TokenSnapshot, rule: RuleParams) -> float:
    """Return a 0-100 diagnostic risk score; the scanner owns rejection."""
    if not rule.late_entry_enabled or token.source != "pumpfun":
        return 0.0
    history = (getattr(token, "raw_enrichment", {}) or {}).get("late_entry_history") or {}
    mc = float(token.market_cap_usd or 0.0)
    age = float(token.age_seconds or 0.0)
    risk = 0.0
    if mc >= rule.late_entry_soft_market_cap_usd:
        risk += 25.0
    if mc >= rule.late_entry_hard_market_cap_usd:
        risk += 25.0
    if age > rule.late_entry_max_age_seconds and mc >= rule.late_entry_soft_market_cap_usd:
        risk += 20.0
    peak = float(history.get("peak_price_usd", 0.0) or 0.0)
    price = float(token.price_usd or 0.0)
    if peak > 0 and price > 0:
        distance = abs((price - peak) / peak * 100.0)
        if distance <= rule.late_entry_near_high_pct:
            risk += 20.0
    prev = history.get("previous") or {}
    prev_price = float(prev.get("price_usd", 0.0) or 0.0)
    prev_ts = float(prev.get("timestamp", 0.0) or 0.0)
    ts = float(history.get("timestamp", 0.0) or 0.0)
    if prev_price > 0 and prev_ts and ts and 0 <= ts-prev_ts <= 3.0:
        runup = (price-prev_price)/prev_price*100.0
        if runup >= rule.late_entry_max_short_runup_pct:
            risk += 20.0
    return min(100.0, round(risk, 2))


def compute_graduation_score(token: TokenSnapshot, rule: RuleParams) -> ScoreResult:
    """Score Pump.fun launches for probability of continued curve progress.

    This is intentionally separate from the legacy 0-100 score. The legacy
    score rewards generic quality/momentum; this score measures the signals
    we actually want for graduation: real curve progress, capital velocity,
    buy pressure, buyer diversity, holder growth and concentration.
    """
    if token.source != "pumpfun":
        return ScoreResult(score=0.0, creator_match=False, breakdown={"applicable": False})

    safety = (getattr(token, "raw_enrichment", {}) or {}).get("pumpfun_launch_safety") or {}
    sig = safety.get("signals") or {}
    history = (getattr(token, "raw_enrichment", {}) or {}).get("graduation_history") or {}

    reserves = float(getattr(token, "real_sol_reserves_sol", 0.0) or 0.0)
    target = max(1.0, float(getattr(rule, "graduation_hunter_target_real_sol", 85.0) or 85.0))
    progress = min(100.0, max(0.0, reserves / target * 100.0))

    min_sol = float(getattr(rule, "graduation_hunter_min_real_sol", 10.0) or 10.0)
    max_sol = float(getattr(rule, "graduation_hunter_max_real_sol", 35.0) or 35.0)
    if min_sol <= reserves <= max_sol:
        progress_score = 10.0 * min(1.0, (reserves - min_sol) / max(max_sol - min_sol, 1.0) * 0.75 + 0.25)
    elif reserves > max_sol:
        progress_score = 0.0
    else:
        progress_score = 0.0

    velocity = float(sig.get("buy_velocity_sol_per_sec", 0.0) or 0.0)
    velocity_score = min(20.0, max(0.0, velocity / 0.20 * 20.0))

    buy_sell = float(sig.get("buy_sell_ratio", 0.0) or 0.0)
    buy_sell_score = min(15.0, max(0.0, buy_sell / 5.0 * 15.0))

    unique_buyers = int(sig.get("unique_buyers", 0) or 0)
    unique_score = min(15.0, unique_buyers / 40.0 * 15.0)

    diversity = float(sig.get("buyer_diversity", 0.0) or 0.0)
    diversity_score = min(10.0, diversity / 0.50 * 10.0)

    holder_growth = float(history.get("holder_growth_per_minute", 0.0) or 0.0)
    holder_score = min(10.0, max(0.0, holder_growth / 15.0 * 10.0))

    top10 = float(sig.get("top10_buyer_sol_share", 1.0) or 1.0)
    concentration_score = 10.0 if top10 <= 0.60 else 5.0 if top10 <= 0.75 else 0.0

    creator_buy = float(sig.get("creator_buy_share", 0.0) or 0.0)
    creator_sell = float(sig.get("creator_sell_share", 0.0) or 0.0)
    if creator_buy > 0.0 and creator_sell < max(0.05, creator_buy * 0.50):
        creator_score = 5.0
    elif creator_buy == 0.0:
        creator_score = 3.0
    else:
        creator_score = 0.0

    same_slot = float(sig.get("same_slot_share", 1.0) or 1.0)
    same_size = float(sig.get("same_size_share", 1.0) or 1.0)
    shared_funder = float(sig.get("shared_funder_volume_share", 0.0) or 0.0)
    bot_score = 5.0
    if same_slot >= 0.80:
        bot_score -= 2.0
    if same_size >= 0.80:
        bot_score -= 2.0
    if shared_funder >= 0.45:
        bot_score -= 3.0
    bot_score = max(0.0, bot_score)

    score = min(100.0, round(
        progress_score + velocity_score + buy_sell_score + unique_score
        + diversity_score + holder_score + concentration_score
        + creator_score + bot_score,
        2,
    ))

    breakdown = {
        "real_sol_progress_score": round(progress_score, 2),
        "buy_velocity_score": round(velocity_score, 2),
        "buy_sell_pressure_score": round(buy_sell_score, 2),
        "unique_buyer_score": round(unique_score, 2),
        "buyer_diversity_score": round(diversity_score, 2),
        "holder_growth_score": round(holder_score, 2),
        "wallet_concentration_score": round(concentration_score, 2),
        "creator_behavior_score": round(creator_score, 2),
        "organic_flow_score": round(bot_score, 2),
        "real_sol_reserves": round(reserves, 4),
        "real_sol_progress_pct": round(progress, 2),
        "buy_velocity_sol_per_sec": round(velocity, 6),
        "buy_sell_ratio": round(buy_sell, 4),
        "unique_buyers": unique_buyers,
        "buyer_diversity": round(diversity, 4),
        "holder_growth_per_minute": round(holder_growth, 2),
        "top10_buyer_sol_share": round(top10, 4),
        "creator_buy_share": round(creator_buy, 4),
        "creator_sell_share": round(creator_sell, 4),
        "threshold": float(getattr(rule, "graduation_hunter_score_threshold", 75.0) or 75.0),
    }
    return ScoreResult(score=score, creator_match=False, breakdown=breakdown)


def compute_score(token: TokenSnapshot, rule: RuleParams, creator_watchlist: List[str]) -> ScoreResult:
    creator_match = bool(token.creator_wallet) and token.creator_wallet in creator_watchlist
    breakdown = {
        "liquidity": round(_liquidity_score(token, rule), 2),
        "holders": round(_holders_score(token, rule), 2),
        "freshness": round(_freshness_score(token, rule), 2),
        "volume": round(_volume_score(token), 2),
        "market_cap_fit": round(_market_cap_fit_score(token, rule), 2),
        "momentum": round(_momentum_score(token), 2),
        "late_entry_risk": _late_entry_risk(token, rule),
    }
    score = sum(v for k, v in breakdown.items() if k != "late_entry_risk")
    if creator_match:
        breakdown["creator_watchlist_bonus"] = CREATOR_MATCH_BONUS
        score += CREATOR_MATCH_BONUS
    score = min(100.0, round(score, 2))
    return ScoreResult(score=score, creator_match=creator_match, breakdown=breakdown)


def compute_fast_sniper_score(token: TokenSnapshot, rule: RuleParams) -> float:
    """Low-latency telemetry score; never gates Fast Sniper execution."""
    liquidity = float(token.liquidity_usd or 0.0)
    market_cap = float(token.market_cap_usd or 0.0)
    score = 50.0
    if rule.min_liquidity_usd > 0:
        score += min(25.0, max(0.0, liquidity / rule.min_liquidity_usd * 25.0))
    if market_cap > 0 and (rule.max_market_cap_usd is None or market_cap <= rule.max_market_cap_usd):
        score += 15.0
    if token.age_seconds <= min(float(rule.max_age_seconds), 2.0):
        score += 10.0
    return round(min(100.0, score), 2)
