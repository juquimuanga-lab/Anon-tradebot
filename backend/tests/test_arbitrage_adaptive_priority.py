"""Regression tests for opportunity-aware arbitrage priority-fee budgeting."""
from __future__ import annotations


def test_max_affordable_priority_budget_preserves_positive_net_profit():
    from app.arbitrage.fee_model import max_affordable_priority_budget

    # 100k gross - 10k base - 5k Jito tip leaves 84,999 lamports of
    # priority-fee headroom while preserving at least 1 lamport of profit.
    assert max_affordable_priority_budget(
        gross_profit_atomic=100_000,
        venue_cost_atomic_value=0,
        base_fee_atomic=10_000,
        jito_tip_atomic=5_000,
    ) == 84_999


def test_max_affordable_priority_budget_never_goes_negative():
    from app.arbitrage.fee_model import max_affordable_priority_budget

    assert max_affordable_priority_budget(
        gross_profit_atomic=10_000,
        venue_cost_atomic_value=0,
        base_fee_atomic=10_000,
        jito_tip_atomic=1_000,
    ) == 0


def test_max_affordable_priority_budget_respects_minimum_jito_tip():
    from app.arbitrage.fee_model import MIN_JITO_TIP_LAMPORTS, max_affordable_priority_budget

    assert max_affordable_priority_budget(
        gross_profit_atomic=20_000,
        venue_cost_atomic_value=0,
        base_fee_atomic=10_000,
        jito_tip_atomic=0,
    ) == 8_999
    assert MIN_JITO_TIP_LAMPORTS == 1_000


def test_jupiter_priority_config_accepts_opportunity_specific_cap():
    from app.arbitrage.jupiter_bundle import _priority_fee_config

    assert _priority_fee_config(17_500) == {
        "priorityLevelWithMaxLamports": {
            "priorityLevel": "medium",
            "maxLamports": 17_500,
        }
    }


def test_live_executor_uses_adaptive_priority_budget():
    from pathlib import Path

    source = Path("app/arbitrage/live_executor.py").read_text()
    assert "max_affordable_priority_budget" in source
    assert "max_priority_fee_lamports=per_leg_priority_cap" in source
