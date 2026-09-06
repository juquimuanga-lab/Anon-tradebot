from types import SimpleNamespace

from app.arbitrage.continuous_telegram import (
    _display_execution_reason,
    _is_retryable_live_requote_failure,
)


def test_negative_zero_tip_gate_is_retryable_live_requote_failure():
    execution = SimpleNamespace(
        success=False,
        reason="jito_tip_profit_gate_failed",
        estimated_net_profit_lamports=-6_689_786,
        jito_tip_lamports=0,
    )

    assert _is_retryable_live_requote_failure(execution) is True
    assert _display_execution_reason(execution) == "live_requote_profitability_failed"


def test_actual_jito_tip_failure_is_not_relabelled_as_requote_failure():
    execution = SimpleNamespace(
        success=False,
        reason="jito_tip_profit_gate_failed",
        estimated_net_profit_lamports=-1_000,
        jito_tip_lamports=1_995,
    )

    assert _is_retryable_live_requote_failure(execution) is False
    assert _display_execution_reason(execution) == "jito_tip_profit_gate_failed"


def test_success_is_never_retried_or_relabelled():
    execution = SimpleNamespace(
        success=True,
        reason="settled",
        estimated_net_profit_lamports=100_000,
        jito_tip_lamports=1_000,
    )

    assert _is_retryable_live_requote_failure(execution) is False
    assert _display_execution_reason(execution) == "settled"
