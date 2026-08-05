"""Simple weighted scoring model (0-100) for tokens that pass hard filters."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from app.scoring.rules import RuleParams, TokenSnapshot

CREATOR_MATCH_BONUS = 25.0

WEIGHTS = {
    "liquidity": 25.0,
    "holders": 20.0,
    "freshness": 20.0,
    "volume": 15.0,
    "market_cap_fit": 20.0,
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


def compute_score(token: TokenSnapshot, rule: RuleParams, creator_watchlist: List[str]) -> ScoreResult:
    creator_match = bool(token.creator_wallet) and token.creator_wallet in creator_watchlist
    breakdown = {
        "liquidity": round(_liquidity_score(token, rule), 2),
        "holders": round(_holders_score(token, rule), 2),
        "freshness": round(_freshness_score(token, rule), 2),
        "volume": round(_volume_score(token), 2),
        "market_cap_fit": round(_market_cap_fit_score(token, rule), 2),
    }
    score = sum(breakdown.values())
    if creator_match:
        breakdown["creator_watchlist_bonus"] = CREATOR_MATCH_BONUS
        score += CREATOR_MATCH_BONUS
    score = min(100.0, round(score, 2))
    return ScoreResult(score=score, creator_match=creator_match, breakdown=breakdown)
