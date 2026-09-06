from types import SimpleNamespace

from app.arbitrage.continuous_telegram import _format_execution_diagnostics


def test_execution_diagnostics_breaks_down_known_fees():
    execution = SimpleNamespace(
        input_lamports=1_000_000_000,
        gross_profit_lamports=0,
        estimated_net_profit_lamports=-16_797_548,
        base_fee_lamports=10_000,
        priority_fee_lamports=16_787_548,
        jito_tip_lamports=0,
        market_tip_lamports=0,
    )

    message = _format_execution_diagnostics(execution)

    assert "Gross after live re-quote: `0.000000000 SOL` (`0.00 bps`)" in message
    assert "Base fees (2 signatures): `0.000010000 SOL`" in message
    assert "Priority fees: `0.016787548 SOL` (`167.88 bps`)" in message
    assert "Jito market tip considered: `0.000000000 SOL` (`0.00 bps`)" in message
    assert "Jito tip charged: `0.000000000 SOL` (`0.00 bps`)" in message
    assert "Total known fees: `0.016797548 SOL` (`167.98 bps`)" in message
    assert "Final net: `-0.016797548 SOL` (`-167.98 bps`)" in message


def test_execution_diagnostics_can_show_positive_net_and_tip():
    execution = SimpleNamespace(
        input_lamports=1_000_000_000,
        gross_profit_lamports=106_294_981,
        estimated_net_profit_lamports=106_159_981,
        base_fee_lamports=10_000,
        priority_fee_lamports=100_000,
        jito_tip_lamports=25_000,
        market_tip_lamports=250_000,
    )

    message = _format_execution_diagnostics(execution)

    assert "Gross after live re-quote: `0.106294981 SOL` (`1062.95 bps`)" in message
    assert "Jito market tip considered: `0.000250000 SOL` (`2.50 bps`)" in message
    assert "Jito tip charged: `0.000025000 SOL` (`0.25 bps`)" in message
    assert "Final net: `+0.106159981 SOL` (`+1061.60 bps`)" in message


def test_execution_diagnostics_reports_market_tip_policy():
    execution = SimpleNamespace(
        input_lamports=1_000_000_000,
        gross_profit_lamports=10_135_000,
        estimated_net_profit_lamports=10_000_000,
        base_fee_lamports=10_000,
        priority_fee_lamports=100_000,
        jito_tip_lamports=25_000,
        market_tip_lamports=250_000,
    )
    executor = SimpleNamespace(
        _tip_percentile=50,
        _tip_multiplier=1.0,
    )

    message = _format_execution_diagnostics(execution, executor)

    assert "Jito market tip considered: `0.000250000 SOL` (`2.50 bps`)" in message
    assert "Jito policy: `50th percentile × 1x`" in message
