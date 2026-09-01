from app.arbitrage.engine import ArbitrageConfig, Quote, find_two_venue_opportunity


def _quote(venue: str, input_amount: int, output_amount: int) -> Quote:
    return Quote(
        venue=venue,
        input_mint="SOL",
        output_mint="TOKEN",
        input_amount_atomic=input_amount,
        output_amount_atomic=output_amount,
        fee_bps=30.0,
        price_impact_bps=0.0,
    )


def test_profitable_spread_is_qualified_after_costs() -> None:
    buy = _quote("raydium", 100_000_000, 1_000_000_000)
    sell = Quote(
        venue="orca",
        input_mint="TOKEN",
        output_mint="SOL",
        input_amount_atomic=1_000_000_000,
        output_amount_atomic=106_000_000,
        fee_bps=30.0,
        price_impact_bps=0.0,
    )

    result = find_two_venue_opportunity(
        "TOKEN",
        buy,
        sell,
        ArbitrageConfig(min_profit_bps=35.0, min_profit_atomic=2_000_000),
    )

    assert result.executable is True
    assert result.reason == "qualified"
    assert result.net_profit_atomic >= 2_000_000


def test_unprofitable_spread_is_rejected() -> None:
    buy = _quote("raydium", 100_000_000, 1_000_000_000)
    sell = Quote(
        venue="orca",
        input_mint="TOKEN",
        output_mint="SOL",
        input_amount_atomic=1_000_000_000,
        output_amount_atomic=100_500_000,
        fee_bps=30.0,
        price_impact_bps=0.0,
    )

    result = find_two_venue_opportunity(
        "TOKEN",
        buy,
        sell,
        ArbitrageConfig(min_profit_bps=35.0, min_profit_atomic=2_000_000),
    )

    assert result.executable is False
    assert result.reason == "profit_threshold_not_met"
