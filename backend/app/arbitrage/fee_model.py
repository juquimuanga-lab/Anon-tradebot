"""Shared arbitrage execution-cost and profitability calculations."""
from __future__ import annotations

from dataclasses import dataclass


MIN_JITO_TIP_LAMPORTS = 1_000


@dataclass(frozen=True)
class ProfitabilityBreakdown:
    """One consistent accounting view used by discovery and live execution."""

    input_atomic: int
    final_output_atomic: int
    gross_profit_atomic: int
    venue_cost_atomic: int
    priority_fee_atomic: int
    jito_tip_atomic: int

    @property
    def total_cost_atomic(self) -> int:
        return (
            max(self.venue_cost_atomic, 0)
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


def venue_cost_atomic(
    input_atomic: int,
    guaranteed_output_atomic: int,
    buy_fee_bps: float,
    sell_fee_bps: float,
) -> int:
    """Estimate explicit venue fees from the amounts actually exchanged."""
    buy_cost = int(max(input_atomic, 0) * max(buy_fee_bps, 0.0) / 10_000)
    sell_cost = int(
        max(guaranteed_output_atomic, 0) * max(sell_fee_bps, 0.0) / 10_000
    )
    return buy_cost + sell_cost


def calculate_profitability(
    *,
    input_atomic: int,
    final_output_atomic: int,
    venue_cost_atomic_value: int,
    priority_fee_atomic: int = 0,
    jito_tip_atomic: int = 0,
) -> ProfitabilityBreakdown:
    """Calculate gross and final net profit without artificial profit floors."""
    return ProfitabilityBreakdown(
        input_atomic=max(input_atomic, 0),
        final_output_atomic=max(final_output_atomic, 0),
        gross_profit_atomic=final_output_atomic - input_atomic,
        venue_cost_atomic=max(venue_cost_atomic_value, 0),
        priority_fee_atomic=max(priority_fee_atomic, 0),
        jito_tip_atomic=max(jito_tip_atomic, 0),
    )


def max_affordable_jito_tip(
    *,
    gross_profit_atomic: int,
    venue_cost_atomic_value: int,
    priority_fee_atomic: int,
) -> int:
    """Return the largest tip that still leaves strictly positive net profit.

    This is an economic cap, not a minimum-profit requirement. The final live
    gate still requires net profit > 0 after the actual built transaction fees.
    """
    return max(
        0,
        gross_profit_atomic
        - max(venue_cost_atomic_value, 0)
        - max(priority_fee_atomic, 0)
        - 1,
    )
