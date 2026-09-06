"""Data models for Solana arbitrage opportunities."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Quote:
    venue: str
    input_mint: str
    output_mint: str
    input_amount_atomic: int
    output_amount_atomic: int
    fee_bps: float = 0.0
    price_impact_bps: float = 0.0
    route_id: Optional[str] = None
    # Jupiter's exact-input quote also provides the minimum output that the
    # swap instruction will enforce at execution time. Keeping this separate
    # lets discovery distinguish the optimistic quote from the executable
    # slippage-protected amount.
    minimum_output_amount_atomic: Optional[int] = None


@dataclass(frozen=True)
class ArbitrageOpportunity:
    token_mint: str
    buy_venue: str
    sell_venue: str
    input_amount_atomic: int
    buy_output_atomic: int
    final_output_atomic: int
    gross_profit_atomic: int
    total_cost_atomic: int
    net_profit_atomic: int
    net_profit_bps: float
    estimated_base_fee_atomic: int = 0
    estimated_priority_fee_atomic: int = 0
    estimated_jito_tip_atomic: int = 0
    gross_profit_bps: float = 0.0
    execution_cost_bps: float = 0.0
    required_gross_profit_bps: float = 0.0
    executable: bool = False
    reason: str = ""