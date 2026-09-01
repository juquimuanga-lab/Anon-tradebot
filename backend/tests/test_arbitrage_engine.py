from app.arbitrage.engine import ArbitrageConfig, find_two_venue_opportunity
from app.arbitrage.models import Quote


def _quote(venue: str, input_amount: int, output_amount: int, impact_bps: float = 0.0) -> Quote:
    return Quote(
        venue=venue,
        input_mint="SOL",
        output_mint="TOKEN",
        input_amount_atomic=input_amount,
        output_amount_atomic=output_amount,
        fee_bps=30.0,
        price_impact_bps=impact_bps,
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
    assert result.reason == "profit_threshold_met"
    assert result.net_profit_atomic >= 2_000_000


def test_high_price_impact_is_rejected() -> None:
    buy = _quote("raydium", 100_000_000, 1_000_000_000, impact_bps=100.0)
    sell = Quote(
        venue="orca",
        input_mint="TOKEN",
        output_mint="SOL",
        input_amount_atomic=1_000_000_000,
        output_amount_atomic=110_000_000,
        fee_bps=30.0,
        price_impact_bps=0.0,
    )

    result = find_two_venue_opportunity("TOKEN", buy, sell)

    assert result.executable is False
    assert result.reason == "price_impact_too_high"


def test_quote_size_mismatch_is_rejected() -> None:
    buy = _quote("raydium", 100_000_000, 1_000_000_000)
    sell = Quote(
        venue="orca",
        input_mint="TOKEN",
        output_mint="SOL",
        input_amount_atomic=999_999_999,
        output_amount_atomic=101_000_000,
    )

    result = find_two_venue_opportunity("TOKEN", buy, sell)

    assert result.executable is False
    assert result.reason == "quote_size_mismatch"
