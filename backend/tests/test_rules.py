from datetime import datetime, timedelta, timezone

from app.scoring.rules import RuleParams, TokenSnapshot, evaluate_hard_filters


def make_token(**overrides) -> TokenSnapshot:
    defaults = dict(
        mint="MintABC123",
        creator_wallet="creatorX",
        created_on=datetime.now(timezone.utc) - timedelta(seconds=30),
        liquidity_usd=5000,
        holders=50,
        market_cap_usd=100000,
        is_migrated=False,
    )
    defaults.update(overrides)
    return TokenSnapshot(**defaults)


def test_passes_when_all_conditions_met():
    token = make_token()
    rule = RuleParams(min_liquidity_usd=1000, min_holders=10, max_age_seconds=600)
    passed, reasons = evaluate_hard_filters(token, rule)
    assert passed is True
    assert reasons == []


def test_fails_on_low_liquidity():
    token = make_token(liquidity_usd=100)
    rule = RuleParams(min_liquidity_usd=1000)
    passed, reasons = evaluate_hard_filters(token, rule)
    assert passed is False
    assert any("liquidity" in r for r in reasons)


def test_fails_on_low_holders():
    token = make_token(holders=2)
    rule = RuleParams(min_holders=10)
    passed, reasons = evaluate_hard_filters(token, rule)
    assert passed is False
    assert any("holders" in r for r in reasons)


def test_fails_on_max_age_exceeded():
    token = make_token(created_on=datetime.now(timezone.utc) - timedelta(seconds=9999))
    rule = RuleParams(max_age_seconds=600)
    passed, reasons = evaluate_hard_filters(token, rule)
    assert passed is False
    assert any("age" in r for r in reasons)


def test_fails_when_creator_in_denylist():
    token = make_token(creator_wallet="bad_creator")
    rule = RuleParams(creator_denylist=["bad_creator"])
    passed, reasons = evaluate_hard_filters(token, rule)
    assert passed is False
    assert any("denylisted" in r for r in reasons)


def test_fails_when_creator_not_in_allowlist():
    token = make_token(creator_wallet="unknown_creator")
    rule = RuleParams(creator_allowlist=["only_this_creator"])
    passed, reasons = evaluate_hard_filters(token, rule)
    assert passed is False
    assert any("allowlist" in r for r in reasons)


def test_fails_on_bonding_curve_phase_mismatch():
    token = make_token(is_migrated=False)
    rule = RuleParams(bonding_curve_phase="post_graduation")
    passed, reasons = evaluate_hard_filters(token, rule)
    assert passed is False
    assert any("bonding curve" in r for r in reasons)


def test_fails_outside_market_cap_range():
    token = make_token(market_cap_usd=10)
    rule = RuleParams(min_market_cap_usd=1000, max_market_cap_usd=500000)
    passed, reasons = evaluate_hard_filters(token, rule)
    assert passed is False
    assert any("market cap" in r for r in reasons)
