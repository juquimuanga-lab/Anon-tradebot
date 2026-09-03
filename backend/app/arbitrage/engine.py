"""Pure arbitrage math and opportunity gating.

Jupiter quote output amounts are treated as quoted executable outputs. DEX
fees and price impact are not subtracted a second time. Price impact remains
a safety gate, while priority fees and Jito tips remain explicit external
execution costs.

Profitability policy: any strictly positive final net profit is eligible.
There is intentionally no minimum profit-bps floor, absolute-profit floor, or
execution-safety profit buffer. Actual execution costs are still included in
net-profit calculations.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from app.arbitrage.fee_model import calculate_profitability
from app.arbitrage.models import ArbitrageOpportunity, Quote

DEFAULT_MIN_PROFIT_BPS = 0.0
DEFAULT_MIN_PROFIT_LAMPORTS = 0
DEFAULT_MAX_PRICE_IMPACT_BPS = 80.0
DEFAULT_MAX_SLIPPAGE_BPS = 50.0
DEFAULT_ESTIMATED_PRIORITY_FEE_LAMPORTS = 50_000
DEFAULT_ESTIMATED_JITO_TIP_LAMPORTS = 100_000
DEFAULT_EXECUTION_SAFETY_BPS = 0.0


@dataclass(frozen=True)
class ArbitrageConfig:
    min_profit_bps: float = DEFAULT_MIN_PROFIT_BPS
    min_profit_atomic: int = DEFAULT_MIN_PROFIT_LAMPORTS
    max_price_impact_bps: float = DEFAULT_MAX_PRICE_IMPACT_BPS
    max_slippage_bps: float = DEFAULT_MAX_SLIPPAGE_BPS
    estimated_priority_fee_atomic: int = DEFAULT_ESTIMATED_PRIORITY_FEE_LAMPORTS
    estimated_jito_tip_atomic: int = DEFAULT_ESTIMATED_JITO_TIP_LAMPORTS
    execution_safety_bps: float = DEFAULT_EXECUTION_SAFETY_BPS

    @classmethod
    def from_env(cls) -> "ArbitrageConfig":
        def number(name: str, default: float) -> float:
            raw = os.getenv(name)
            try:
                return float(raw) if raw is not None else default
            except ValueError:
                return default

        return cls(
            min_profit_bps=0.0,
            min_profit_atomic=0,
            max_price_impact_bps=max(number("ARBITRAGE_MAX_PRICE_IMPACT_BPS", DEFAULT_MAX_PRICE_IMPACT_BPS), 0.0),
            max_slippage_bps=max(number("ARBITRAGE_MAX_SLIPPAGE_BPS", DEFAULT_MAX_SLIPPAGE_BPS), 0.0),
            estimated_priority_fee_atomic=max(int(number("ARBITRAGE_ESTIMATED_PRIORITY_FEE_LAMPORTS", DEFAULT_ESTIMATED_PRIORITY_FEE_LAMPORTS)), 0),
            estimated_jito_tip_atomic=max(int(number("ARBITRAGE_ESTIMATED_JITO_TIP_LAMPORTS", DEFAULT_ESTIMATED_JITO_TIP_LAMPORTS)), 0),
            execution_safety_bps=0.0,
        )

    @property
    def external_execution_cost_atomic(self) -> int:
        return max(self.estimated_priority_fee_atomic, 0) + max(
            self.estimated_jito_tip_atomic, 0
        )


def find_two_venue_opportunity(
    token_mint: str,
    buy_quote: Quote,
    sell_quote: Quote,
    config: ArbitrageConfig | None = None,
) -> ArbitrageOpportunity:
    """Evaluate a buy quote followed by a sell quote."""
    config = config or ArbitrageConfig.from_env()

    if buy_quote.output_amount_atomic <= 0:
        return _rejected(token_mint, buy_quote, sell_quote, "buy_quote_zero_output")
    if sell_quote.output_amount_atomic <= 0:
        return _rejected(token_mint, buy_quote, sell_quote, "sell_quote_zero_output")
    if sell_quote.input_amount_atomic != buy_quote.output_amount_atomic:
        return _rejected(token_mint, buy_quote, sell_quote, "quote_size_mismatch")

    input_atomic = buy_quote.input_amount_atomic
    gross_profit = sell_quote.output_amount_atomic - input_atomic
    gross_profit_bps = gross_profit / input_atomic * 10_000 if input_atomic else 0.0

    # Jupiter's quoted output already reflects the route's DEX economics, so
    # do not subtract Quote.fee_bps a second time. Only external execution
    # costs are estimated here and re-checked with actual built transactions.
    breakdown = calculate_profitability(
        input_atomic=input_atomic,
        final_output_atomic=sell_quote.output_amount_atomic,
        venue_cost_atomic_value=0,
        priority_fee_atomic=config.estimated_priority_fee_atomic,
        jito_tip_atomic=config.estimated_jito_tip_atomic,
    )
    execution_costs = breakdown.total_cost_atomic
    execution_cost_bps = (
        execution_costs / input_atomic * 10_000 if input_atomic else float("inf")
    )
    net_profit = breakdown.net_profit_atomic
    net_profit_bps = breakdown.net_profit_bps
    required_gross_profit_bps = execution_cost_bps

    max_impact = max(buy_quote.price_impact_bps, sell_quote.price_impact_bps)
    if max_impact > config.max_price_impact_bps:
        return _build(
            token_mint, buy_quote, sell_quote, gross_profit, execution_costs,
            net_profit, net_profit_bps, gross_profit_bps, execution_cost_bps,
            required_gross_profit_bps, config, False, "price_impact_too_high",
        )

    executable = net_profit > 0
    reason = "profit_threshold_met" if executable else "profit_threshold_not_met"

    return _build(
        token_mint, buy_quote, sell_quote, gross_profit, execution_costs,
        net_profit, net_profit_bps, gross_profit_bps, execution_cost_bps,
        required_gross_profit_bps, config, executable, reason,
    )


def rank_opportunities(
    opportunities: Iterable[ArbitrageOpportunity],
) -> list[ArbitrageOpportunity]:
    return sorted(
        opportunities,
        key=lambda item: (
            item.executable,
            item.net_profit_atomic,
            item.net_profit_bps,
        ),
        reverse=True,
    )


def _build(
    token_mint: str,
    buy_quote: Quote,
    sell_quote: Quote,
    gross_profit: int,
    execution_costs: int,
    net_profit: int,
    net_profit_bps: float,
    gross_profit_bps: float,
    execution_cost_bps: float,
    required_gross_profit_bps: float,
    config: ArbitrageConfig,
    executable: bool,
    reason: str,
) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        token_mint=token_mint,
        buy_venue=buy_quote.venue,
        sell_venue=sell_quote.venue,
        input_amount_atomic=buy_quote.input_amount_atomic,
        buy_output_atomic=buy_quote.output_amount_atomic,
        final_output_atomic=sell_quote.output_amount_atomic,
        gross_profit_atomic=gross_profit,
        total_cost_atomic=execution_costs,
        net_profit_atomic=net_profit,
        net_profit_bps=net_profit_bps,
        estimated_priority_fee_atomic=max(config.estimated_priority_fee_atomic, 0),
        estimated_jito_tip_atomic=max(config.estimated_jito_tip_atomic, 0),
        gross_profit_bps=gross_profit_bps,
        execution_cost_bps=execution_cost_bps,
        required_gross_profit_bps=required_gross_profit_bps,
        executable=executable,
        reason=reason,
    )


def _rejected(
    token_mint: str,
    buy_quote: Quote,
    sell_quote: Quote,
    reason: str,
) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        token_mint=token_mint,
        buy_venue=buy_quote.venue,
        sell_venue=sell_quote.venue,
        input_amount_atomic=buy_quote.input_amount_atomic,
        buy_output_atomic=buy_quote.output_amount_atomic,
        final_output_atomic=sell_quote.output_amount_atomic,
        gross_profit_atomic=0,
        total_cost_atomic=0,
        net_profit_atomic=0,
        net_profit_bps=0.0,
        executable=False,
        reason=reason,
    )
