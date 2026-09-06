from types import SimpleNamespace

from app.arbitrage.continuous_telegram import _format_execution_diagnostics


def test_execution_diagnostics_breaks_down_known_fees():
    execution = SimpleNamespace(
        input_lamports=1_000_000_000,
        estimated_net_profit_lamports=-16_797_548,
        base_fee_lamports=10_000,
        priority_fee_lamports=16_787_548,
        jito_tip_lamports=0,
    )

    message = _format_execution_diagnostics(execution)

    assert "Gross after re-quote (implied): `0.000000000 SOL` (`0.00 bps`)" in message
    assert "Base fees (2 signatures): `0.000010000 SOL`" in message
    assert "Priority fees: `0.016787548 SOL` (`167.88 bps`)" in message
    assert "Jito tip charged: `0.000000000 SOL` (`0.00 bps`)" in message
    assert "Total known fees: `0.016797548 SOL` (`167.98 bps`)" in message
    assert "Final net: `-0.016797548 SOL` (`-167.98 bps`)" in message


def test_execution_diagnostics_can_show_positive_net_and_tip():
    execution = SimpleNamespace(
        input_lamports=1_000_000_000,
        estimated_net_profit_lamports=106_159_981,
        base_fee_lamports=10_000,
        priority_fee_lamports=100_000,
        jito_tip_lamports=25_000,
    )

    message = _format_execution_diagnostics(execution)

    assert "Gross after re-quote (implied): `0.106294981 SOL` (`1062.95 bps`)" in message
    assert "Jito tip charged: `0.000025000 SOL` (`0.25 bps`)" in message
    assert "Final net: `+0.106159981 SOL` (`+1061.60 bps`)" in message
