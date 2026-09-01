from app.arbitrage.engine import ArbitrageConfig, find_two_venue_opportunity
from app.arbitrage.models import Quote


def _buy(venue, amount, output, fee=30.0, impact=0.0):
    return Quote(venue, "SOL", "TOKEN", amount, output, fee, impact)


def _sell(venue, amount, output, fee=30.0, impact=0.0):
    return Quote(venue, "TOKEN", "SOL", amount, output, fee, impact)


def test_profitable_spread_is_qualified_without_double_counting_quote_costs():
    buy = _buy("raydium", 100_000_000, 1_000_000_000, fee=30.0, impact=20.0)
    sell = _sell("orca", 1_000_000_000, 106_000_000, fee=30.0, impact=20.0)

    result = find_two_venue_opportunity(
        "TOKEN",
        buy,
        sell,
        ArbitrageConfig(
            min_profit_bps=35.0,
            min_profit_atomic=2_000_000,
            estimated_priority_fee_atomic=50_000,
            estimated_jito_tip_atomic=100_000,
        ),
    )

    assert result.gross_profit_atomic == 6_000_000
    assert result.total_cost_atomic == 150_000
    assert result.net_profit_atomic == 5_850_000
    assert result.gross_profit_bps == 600.0
    assert result.execution_cost_bps == 15.0
    assert result.required_gross_profit_bps == 60.0
    assert result.executable is True
    assert result.reason == "profit_threshold_met"


def test_execution_cost_bps_is_dynamic_with_trade_size():
    buy = _buy("raydium", 1_000_000_000, 10_000_000_000)
    sell = _sell("orca", 10_000_000_000, 1_010_000_000)

    result = find_two_venue_opportunity(
        "TOKEN",
        buy,
        sell,
        ArbitrageConfig(
            min_profit_bps=35.0,
            min_profit_atomic=2_000_000,
            estimated_priority_fee_atomic=50_000,
            estimated_jito_tip_atomic=100_000,
            execution_safety_bps=10.0,
        ),
    )

    # Same 150k fixed execution cost is only 1.5 bps at 1 SOL.
    assert result.execution_cost_bps == 1.5
    assert result.gross_profit_bps == 100.0
    assert result.net_profit_bps == 98.5
    assert result.executable is True


def test_tiny_trade_requires_large_gross_edge_to_cover_fixed_costs():
    buy = _buy("raydium", 10_000_000, 100_000_000)
    sell = _sell("orca", 100_000_000, 10_100_000)

    result = find_two_venue_opportunity(
        "TOKEN",
        buy,
        sell,
        ArbitrageConfig(
            min_profit_bps=35.0,
            min_profit_atomic=2_000_000,
            estimated_priority_fee_atomic=50_000,
            estimated_jito_tip_atomic=100_000,
            execution_safety_bps=10.0,
        ),
    )

    # 150k fixed cost on 0.01 SOL = 150 bps before safety/min-profit.
    assert result.execution_cost_bps == 150.0
    assert result.executable is False
    assert result.reason == "profit_threshold_not_met"


def test_high_price_impact_is_still_rejected():
    buy = _buy("raydium", 100_000_000, 1_000_000_000, impact=100.0)
    sell = _sell("orca", 1_000_000_000, 110_000_000)

    result = find_two_venue_opportunity("TOKEN", buy, sell)

    assert result.executable is False
    assert result.reason == "price_impact_too_high"


def test_quote_size_mismatch_is_rejected():
    buy = _buy("raydium", 100_000_000, 1_000_000_000)
    sell = _sell("orca", 999_999_999, 101_000_000)

    result = find_two_venue_opportunity("TOKEN", buy, sell)

    assert result.executable is False
    assert result.reason == "quote_size_mismatch"


def test_negative_external_costs_cannot_increase_profit():
    buy = _buy("raydium", 100_000_000, 1_000_000_000)
    sell = _sell("orca", 1_000_000_000, 101_000_000)

    result = find_two_venue_opportunity(
        "TOKEN",
        buy,
        sell,
        ArbitrageConfig(
            min_profit_bps=0.0,
            min_profit_atomic=0,
            estimated_priority_fee_atomic=-10_000,
            estimated_jito_tip_atomic=-20_000,
        ),
    )

    assert result.total_cost_atomic == 0
    assert result.net_profit_atomic == 1_000_000
