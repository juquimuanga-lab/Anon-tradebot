from datetime import datetime, timezone

from app.scoring.rules import RuleParams, TokenSnapshot
from app.scoring.scorer import CREATOR_MATCH_BONUS, compute_score

CREATOR_WATCHLIST = ["7AbRGzM3NBvvUXi7j1Mga2SraTfjpPBMzGpyHcXSzV3v"]


def make_token(**overrides) -> TokenSnapshot:
    defaults = dict(
        mint="MintABC123",
        creator_wallet="someone_else",
        created_on=datetime.now(timezone.utc),
        liquidity_usd=3000,
        holders=30,
        market_cap_usd=100000,
        volume_24h_usd=1500,
        is_migrated=False,
    )
    defaults.update(overrides)
    return TokenSnapshot(**defaults)


def test_score_within_bounds():
    token = make_token()
    rule = RuleParams(min_liquidity_usd=1000, min_holders=10)
    result = compute_score(token, rule, CREATOR_WATCHLIST)
    assert 0 <= result.score <= 100
    assert result.creator_match is False


def test_creator_watchlist_bonus_applied():
    token = make_token(creator_wallet="7AbRGzM3NBvvUXi7j1Mga2SraTfjpPBMzGpyHcXSzV3v")
    rule = RuleParams(min_liquidity_usd=1000, min_holders=10)
    result_with_creator = compute_score(token, rule, CREATOR_WATCHLIST)
    result_without = compute_score(make_token(), rule, CREATOR_WATCHLIST)

    assert result_with_creator.creator_match is True
    assert result_with_creator.breakdown["creator_watchlist_bonus"] == CREATOR_MATCH_BONUS
    assert result_with_creator.score > result_without.score


def test_higher_liquidity_and_holders_score_higher():
    weak = make_token(liquidity_usd=1000, holders=10)
    strong = make_token(mint="MintXYZ", liquidity_usd=10000, holders=200)
    rule = RuleParams(min_liquidity_usd=1000, min_holders=10)

    weak_score = compute_score(weak, rule, CREATOR_WATCHLIST).score
    strong_score = compute_score(strong, rule, CREATOR_WATCHLIST).score
    assert strong_score > weak_score


def test_score_capped_at_100():
    token = make_token(
        creator_wallet="7AbRGzM3NBvvUXi7j1Mga2SraTfjpPBMzGpyHcXSzV3v",
        liquidity_usd=100000,
        holders=5000,
        volume_24h_usd=200000,
    )
    rule = RuleParams(min_liquidity_usd=1000, min_holders=10, min_market_cap_usd=1000, max_market_cap_usd=1_000_000)
    result = compute_score(token, rule, CREATOR_WATCHLIST)
    assert result.score <= 100.0
