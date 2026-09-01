"""Pure arbitrage math and opportunity gating.

Jupiter quote output amounts are treated as quoted executable outputs. DEX
fees and price impact are not subtracted a second time. Price impact remains
a safety gate, while priority fees and Jito tips remain explicit external
execution costs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from app.arbitrage.models import ArbitrageOpportunity, Quote


@dataclass(frozen=True)
class ArbitrageConfig:
    min_profit_bps: float = 35.0
    min_profit_atomic: int = 50_000
    max_price_impact_bps: float = 80.0
    max_slippage_bps: float = 50.0
    estimated_priority_fee_atomic: int = 50_000
    estimated_jito_tip_atomic: int = 100_000
    execution_safety_bps: float = 10.0

    @classmethod
    def from_env(cls) -> "ArbitrageConfig":
        def number(name: str, default: float) -> float:
            raw = os.getenv(name)
            try:
                return float(raw) if raw is not None else default
            except ValueError:
                return default

        return cls(
            min_profit_bps=max(number("ARBITRAGE_MIN_PROFIT_BPS", 35.0), 0.0),
            min_profit_atomic=max(int(number("ARBITRAGE_MIN_PROFIT_LAMPORTS", 50_000)), 0),
            max_price_impact_bps=max(number("ARBITRAGE_MAX_PRICE_IMPACT_BPS", 80.0), 0.0),
            max_slippage_bps=max(number("ARBITRAGE_MAX_SLIPPAGE_BPS", 50.0), 0.0),
            estimated_priority_fee_atomic=max(int(number("ARBITRAGE_ESTIMATED_PRIORITY_FEE_LAMPORTS", 50_000)), 0),
            estimated_jito_tip_atomic=max(int(number("ARBITRAGE_ESTIMATED_JITO_TIP_LAMPORTS", 100_000)), 0),
            execution_safety_bps=max(number("ARBITRAGE_EXECUTION_SAFETY_BPS", 10.0), 0.0),
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

    execution_costs = config.external_execution_cost_atomic
    execution_cost_bps = (
        execution_costs / input_atomic * 10_000 if input_atomic else float("inf")
    )
    safety_atomic = max(
        int(input_atomic * max(config.execution_safety_bps, 0.0) / 10_000), 0
    )
    min_profit_bps_edge = max(config.min_profit_bps, 0.0) + max(
        config.execution_safety_bps, 0.0
    )
    min_profit_atomic_edge = max(config.min_profit_atomic, 0) + safety_atomic
    required_by_bps = execution_cost_bps + min_profit_bps_edge
    required_by_atomic = (
        (execution_costs + min_profit_atomic_edge) / input_atomic * 10_000
        if input_atomic
        else float("inf")
    )
    required_gross_profit_bps = max(required_by_bps, required_by_atomic)

    net_profit = gross_profit - execution_costs
    net_profit_bps = net_profit / input_atomic * 10_000 if input_atomic else 0.0

    max_impact = max(buy_quote.price_impact_bps, sell_quote.price_impact_bps)
    if max_impact > config.max_price_impact_bps:
        return _build(
            token_mint, buy_quote, sell_quote, gross_profit, execution_costs,
            net_profit, net_profit_bps, gross_profit_bps, execution_cost_bps,
            required_gross_profit_bps, config, False, "price_impact_too_high",
        )

    executable = (
        net_profit >= min_profit_atomic_edge
        and net_profit_bps >= min_profit_bps_edge
    )
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
