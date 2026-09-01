"""Pure arbitrage math and opportunity gating.

No RPC calls and no wallet access happen here. Keeping the core calculation
pure makes it safe to unit test and prevents arbitrage logic from altering the
existing sniper decision path.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

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
    """Evaluate a buy quote followed by a sell quote for the same token.

    Quotes must already represent executable amounts for the same input size.
    A positive raw spread is not enough: expected fees, price impact and
    execution costs must leave the configured minimum net profit.
    """
    if buy_quote.output_amount_atomic <= 0:
        return _rejected(token_mint, buy_quote, sell_quote, "buy_quote_zero_output")
    if sell_quote.output_amount_atomic <= 0:
        return _rejected(token_mint, buy_quote, sell_quote, "sell_quote_zero_output")
    if sell_quote.input_amount_atomic != buy_quote.output_amount_atomic:
        return _rejected(token_mint, buy_quote, sell_quote, "quote_size_mismatch")

    gross_profit = sell_quote.output_amount_atomic - buy_quote.input_amount_atomic
    costs = (
        int(buy_quote.input_amount_atomic * max(buy_quote.fee_bps, 0.0) / 10_000)
        + int(sell_quote.output_amount_atomic * max(sell_quote.fee_bps, 0.0) / 10_000)
        + int(buy_quote.input_amount_atomic * max(buy_quote.price_impact_bps, 0.0) / 10_000)
        + int(sell_quote.input_amount_atomic * max(sell_quote.price_impact_bps, 0.0) / 10_000)
        + config.estimated_priority_fee_atomic
        + config.estimated_jito_tip_atomic
    )
    net_profit = gross_profit - costs
    net_profit_bps = (net_profit / buy_quote.input_amount_atomic * 10_000) if buy_quote.input_amount_atomic else 0.0

    if max(buy_quote.price_impact_bps, sell_quote.price_impact_bps) > config.max_price_impact_bps:
        return ArbitrageOpportunity(
            token_mint=token_mint,
            buy_venue=buy_quote.venue,
            sell_venue=sell_quote.venue,
            input_amount_atomic=buy_quote.input_amount_atomic,
            buy_output_atomic=buy_quote.output_amount_atomic,
            final_output_atomic=sell_quote.output_amount_atomic,
            gross_profit_atomic=gross_profit,
            total_cost_atomic=costs,
            net_profit_atomic=net_profit,
            net_profit_bps=net_profit_bps,
            estimated_priority_fee_atomic=config.estimated_priority_fee_atomic,
            estimated_jito_tip_atomic=config.estimated_jito_tip_atomic,
            executable=False,
            reason="price_impact_too_high",
        )

    executable = net_profit >= config.min_profit_atomic and net_profit_bps >= config.min_profit_bps
    reason = "profit_threshold_met" if executable else "profit_threshold_not_met"
    return ArbitrageOpportunity(
        token_mint=token_mint,
        buy_venue=buy_quote.venue,
        sell_venue=sell_quote.venue,
        input_amount_atomic=buy_quote.input_amount_atomic,
        buy_output_atomic=buy_quote.output_amount_atomic,
        final_output_atomic=sell_quote.output_amount_atomic,
        gross_profit_atomic=gross_profit,
        total_cost_atomic=costs,
        net_profit_atomic=net_profit,
        net_profit_bps=net_profit_bps,
        estimated_priority_fee_atomic=config.estimated_priority_fee_atomic,
        estimated_jito_tip_atomic=config.estimated_jito_tip_atomic,
        executable=executable,
        reason=reason,
    )


def rank_opportunities(opportunities: Iterable[ArbitrageOpportunity]) -> list[ArbitrageOpportunity]:
    """Rank executable opportunities first, then by net profit bps."""
    return sorted(opportunities, key=lambda item: (item.executable, item.net_profit_bps, item.net_profit_atomic), reverse=True)


def _rejected(token_mint: str, buy_quote: Quote, sell_quote: Quote, reason: str) -> ArbitrageOpportunity:
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
