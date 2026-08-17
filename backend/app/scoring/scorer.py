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


def compute_score(token: TokenSnapshot, rule: RuleParams, creator_watchlist: List[str]) -> ScoreResult:
    creator_match = bool(token.creator_wallet) and token.creator_wallet in creator_watchlist
    breakdown = {
        "liquidity": round(_liquidity_score(token, rule), 2),
        "holders": round(_holders_score(token, rule), 2),
        "freshness": round(_freshness_score(token, rule), 2),
        "volume": round(_volume_score(token), 2),
        "market_cap_fit": round(_market_cap_fit_score(token, rule), 2),
        "momentum": round(_momentum_score(token), 2),
    }
    score = sum(breakdown.values())
    if creator_match:
        breakdown["creator_watchlist_bonus"] = CREATOR_MATCH_BONUS
        score += CREATOR_MATCH_BONUS
    score = min(100.0, round(score, 2))
    return ScoreResult(score=score, creator_match=creator_match, breakdown=breakdown)
