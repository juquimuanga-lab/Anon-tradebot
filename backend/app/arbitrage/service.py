"""Arbitrage service coordinator.

V1 is deliberately quote-source agnostic. It accepts normalized quotes from
connectors and produces ranked opportunities. Execution is not enabled here.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Optional

from app.arbitrage.engine import ArbitrageConfig, find_two_venue_opportunity, rank_opportunities
from app.arbitrage.models import ArbitrageOpportunity, Quote

logger = logging.getLogger("app.arbitrage")

QuoteProvider = Callable[[str, str, int], Awaitable[Optional[Quote]]]


@dataclass
class ArbitrageStatus:
    enabled: bool
    running: bool
    opportunities_seen: int = 0
    executable_seen: int = 0
    last_reason: str = "not_started"


class ArbitrageService:
    """Owns arbitrage state without sharing mutable state with sniper lanes."""

    def __init__(self, config: ArbitrageConfig | None = None) -> None:
        self.config = config or ArbitrageConfig()
        self._status = ArbitrageStatus(enabled=False, running=False)
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def set_enabled(self, enabled: bool) -> ArbitrageStatus:
        async with self._lock:
            self._status.enabled = enabled
            if not enabled:
                self._status.running = False
        return self.status()

    def status(self) -> ArbitrageStatus:
        return ArbitrageStatus(**self._status.__dict__)

    async def evaluate(self, token_mint: str, buy_quote: Quote, sell_quote: Quote) -> ArbitrageOpportunity:
        async with self._lock:
            self._status.opportunities_seen += 1
        opportunity = find_two_venue_opportunity(token_mint, buy_quote, sell_quote, self.config)
        async with self._lock:
            if opportunity.executable:
                self._status.executable_seen += 1
            self._status.last_reason = opportunity.reason
        return opportunity

    async def scan_pairs(
        self,
        token_mint: str,
        input_amount_atomic: int,
        venues: Iterable[tuple[str, str]],
        quote_provider: QuoteProvider,
    ) -> list[ArbitrageOpportunity]:
        """Fetch normalized quotes and compare every ordered venue pair.

        This is intentionally an orchestration primitive; no network-specific
        connector is embedded so adding venue support cannot affect sniper code.
        """
        venue_list = list(venues)
        quotes: dict[str, Quote] = {}
        for venue, direction in venue_list:
            quote = await quote_provider(venue, direction, input_amount_atomic)
            if quote is not None:
                quotes[venue] = quote

        opportunities: list[ArbitrageOpportunity] = []
        for buy_venue, buy_quote in quotes.items():
            for sell_venue, sell_quote in quotes.items():
                if buy_venue == sell_venue:
                    continue
                # A sell quote must consume exactly what the buy quote returns.
                adjusted_sell = Quote(
                    venue=sell_quote.venue,
                    input_mint=sell_quote.input_mint,
                    output_mint=sell_quote.output_mint,
                    input_amount_atomic=buy_quote.output_amount_atomic,
                    output_amount_atomic=sell_quote.output_amount_atomic,
                    fee_bps=sell_quote.fee_bps,
                    price_impact_bps=sell_quote.price_impact_bps,
                    route_id=sell_quote.route_id,
                )
                opportunities.append(await self.evaluate(token_mint, buy_quote, adjusted_sell))

        return rank_opportunities(opportunities)
