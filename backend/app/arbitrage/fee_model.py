"""Shared arbitrage execution-cost and profitability calculations."""
from __future__ import annotations

from dataclasses import dataclass


DEFAULT_BASE_FEE_LAMPORTS_PER_SIGNATURE = 5_000
MIN_JITO_TIP_LAMPORTS = 1_000


@dataclass(frozen=True)
class ProfitabilityBreakdown:
    """One consistent accounting view used by discovery and live execution."""

    input_atomic: int
    final_output_atomic: int
    gross_profit_atomic: int
    venue_cost_atomic: int
    base_fee_atomic: int
    priority_fee_atomic: int
    jito_tip_atomic: int

    @property
    def total_cost_atomic(self) -> int:
        return (
            max(self.venue_cost_atomic, 0)
            + max(self.base_fee_atomic, 0)
            + max(self.priority_fee_atomic, 0)
            + max(self.jito_tip_atomic, 0)
        )

    @property
    def net_profit_atomic(self) -> int:
        return self.gross_profit_atomic - self.total_cost_atomic

    @property
    def net_profit_bps(self) -> float:
        return (
            self.net_profit_atomic / self.input_atomic * 10_000
            if self.input_atomic
            else 0.0
        )


def calculate_profitability(
    *,
    input_atomic: int,
    final_output_atomic: int,
    venue_cost_atomic_value: int,
    base_fee_atomic: int = 0,
    priority_fee_atomic: int = 0,
    jito_tip_atomic: int = 0,
) -> ProfitabilityBreakdown:
    """Calculate gross and final net profit without artificial profit floors."""
    return ProfitabilityBreakdown(
        input_atomic=max(input_atomic, 0),
        final_output_atomic=max(final_output_atomic, 0),
        gross_profit_atomic=final_output_atomic - input_atomic,
        venue_cost_atomic=max(venue_cost_atomic_value, 0),
        base_fee_atomic=max(base_fee_atomic, 0),
        priority_fee_atomic=max(priority_fee_atomic, 0),
        jito_tip_atomic=max(jito_tip_atomic, 0),
    )


def max_affordable_jito_tip(
    *,
    gross_profit_atomic: int,
    venue_cost_atomic_value: int,
    base_fee_atomic: int,
    priority_fee_atomic: int,
) -> int:
    """Largest tip that still leaves strictly positive final net profit."""
    return max(
        0,
        gross_profit_atomic
        - max(venue_cost_atomic_value, 0)
        - max(base_fee_atomic, 0)
        - max(priority_fee_atomic, 0)
        - 1,
    )


def max_affordable_priority_budget(
    *,
    gross_profit_atomic: int,
    venue_cost_atomic_value: int,
    base_fee_atomic: int,
    jito_tip_atomic: int = MIN_JITO_TIP_LAMPORTS,
) -> int:
    """Return total priority-fee budget while preserving a positive net.

    This is an execution-budget calculation, not a new profitability floor.
    It simply prevents Jupiter from being asked to spend more priority fee than
    the opportunity can afford after real base fees and the minimum Jito tip.
    """
    return max(
        0,
        gross_profit_atomic
        - max(venue_cost_atomic_value, 0)
        - max(base_fee_atomic, 0)
        - max(jito_tip_atomic, MIN_JITO_TIP_LAMPORTS)
        - 1,
    )
