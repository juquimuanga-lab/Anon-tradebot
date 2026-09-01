"""Pure arbitrage math and opportunity gating.

Jupiter quote output amounts are treated as the quoted executable outputs.
DEX fees and price impact are not subtracted a second time. Price impact
remains a safety gate, while priority fees and Jito tips remain explicit
external execution costs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.arbitrage.models import ArbitrageOpportunity, Quote


@dataclass(frozen=True)
class ArbitrageConfig:
    min_profit_bps: float = 35.0
    min_profit_atomic: int = 2_000_000
    max_price_impact_bps: float = 80.0
    max_slippage_bps: float = 50.0
    estimated_priority_fee_atomic: int = 50_000
    estimated_jito_tip_atomic: int = 100_000


def find_two_venue_opportunity(
    token_mint: str,
    buy_quote: Quote,
    sell_quote: Quote,
    config: ArbitrageConfig = ArbitrageConfig(),
) -> ArbitrageOpportunity:
    """Evaluate a buy quote followed by a sell quote."""
    if buy_quote.output_amount_atomic <= 0:
        return _rejected(token_mint, buy_quote, sell_quote, "buy_quote_zero_output")
    if sell_quote.output_amount_atomic <= 0:
        return _rejected(token_mint, buy_quote, sell_quote, "sell_quote_zero_output")
    if sell_quote.input_amount_atomic != buy_quote.output_amount_atomic:
        return _rejected(token_mint, buy_quote, sell_quote, "quote_size_mismatch")

    gross_profit = sell_quote.output_amount_atomic - buy_quote.input_amount_atomic

    # Jupiter's quote output already reflects the quoted route economics.
    # Do not subtract DEX fees or price impact again.
    execution_costs = (
        max(config.estimated_priority_fee_atomic, 0)
        + max(config.estimated_jito_tip_atomic, 0)
    )
    net_profit = gross_profit - execution_costs
    net_profit_bps = (
        net_profit / buy_quote.input_amount_atomic * 10_000
        if buy_quote.input_amount_atomic
        else 0.0
    )

    max_impact = max(buy_quote.price_impact_bps, sell_quote.price_impact_bps)
    if max_impact > config.max_price_impact_bps:
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
            estimated_priority_fee_atomic=config.estimated_priority_fee_atomic,
            estimated_jito_tip_atomic=config.estimated_jito_tip_atomic,
            executable=False,
            reason="price_impact_too_high",
        )

    executable = (
        net_profit >= config.min_profit_atomic
        and net_profit_bps >= config.min_profit_bps
    )
    reason = "profit_threshold_met" if executable else "profit_threshold_not_met"

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
        estimated_priority_fee_atomic=config.estimated_priority_fee_atomic,
        estimated_jito_tip_atomic=config.estimated_jito_tip_atomic,
        executable=executable,
        reason=reason,
    )


def rank_opportunities(
    opportunities: Iterable[ArbitrageOpportunity],
) -> list[ArbitrageOpportunity]:
    return sorted(
        opportunities,
        key=lambda item: (
            item.executable,
            item.net_profit_bps,
            item.net_profit_atomic,
        ),
        reverse=True,
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
