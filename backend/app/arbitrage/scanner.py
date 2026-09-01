"""Venue quote scanner for the isolated Solana arbitrage subsystem."""
from __future__ import annotations

from dataclasses import dataclass

from app.arbitrage.jupiter_quotes import JupiterArbitrageQuoteProvider, VenueConfig, configured_venues
from app.arbitrage.models import ArbitrageOpportunity, Quote
from app.arbitrage.rpc_health import ArbitrageRpcHealth, RpcHealth
from app.arbitrage.service import ArbitrageService
from app.config.settings import settings


LAMPORTS_PER_SOL = 1_000_000_000


@dataclass(frozen=True)
class ScanResult:
    token_mint: str
    input_amount_lamports: int
    opportunities: tuple[ArbitrageOpportunity, ...]
    rpc_health: RpcHealth | None = None


class ArbitrageScanner:
    """Runs venue-constrained quote comparisons without touching sniper state."""

    def __init__(
        self,
        service: ArbitrageService,
        provider: JupiterArbitrageQuoteProvider | None = None,
        rpc_health: ArbitrageRpcHealth | None = None,
    ) -> None:
        self.service = service
        self.provider = provider or JupiterArbitrageQuoteProvider(settings.jupiter_base_url)
        self.rpc_health = rpc_health or ArbitrageRpcHealth()

    async def close(self) -> None:
        await self.provider.aclose()

    async def scan(
        self,
        token_mint: str,
        amount_sol: float,
        venues: tuple[VenueConfig, ...] | None = None,
    ) -> ScanResult:
        if not token_mint or len(token_mint) < 20:
            raise ValueError("token_mint does not look like a Solana mint")
        if amount_sol <= 0:
            raise ValueError("amount_sol must be positive")

        # Verify the Solana data plane before observing quotes. Helius is used
        # as primary when configured; Alchemy is the optional fallback.
        health = await self.rpc_health.check()
        if not health.healthy:
            return ScanResult(
                token_mint=token_mint,
                input_amount_lamports=int(amount_sol * LAMPORTS_PER_SOL),
                opportunities=tuple(),
                rpc_health=health,
            )

        amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)
        selected = venues or configured_venues()
        buy_quotes: dict[str, Quote] = {}
        sell_quotes: dict[str, Quote] = {}

        for venue in selected:
            try:
                buy = await self.provider.quote(
                    venue,
                    "buy",
                    amount_lamports,
                    token_mint,
                )
                if buy:
                    buy_quotes[venue.name] = buy
            except Exception:
                continue

        for venue in selected:
            buy = buy_quotes.get(venue.name)
            if not buy:
                continue
            try:
                sell = await self.provider.quote(
                    venue,
                    "sell",
                    buy.output_amount_atomic,
                    token_mint,
                )
                if sell:
                    sell_quotes[venue.name] = sell
            except Exception:
                continue

        opportunities: list[ArbitrageOpportunity] = []
        for buy_venue, buy_quote in buy_quotes.items():
            for sell_venue, sell_quote in sell_quotes.items():
                if buy_venue == sell_venue:
                    continue
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
                opportunities.append(
                    await self.service.evaluate(token_mint, buy_quote, adjusted_sell)
                )

        opportunities.sort(
            key=lambda item: (
                item.executable,
                item.net_profit_bps,
                item.net_profit_atomic,
            ),
            reverse=True,
        )
        return ScanResult(
            token_mint=token_mint,
            input_amount_lamports=amount_lamports,
            opportunities=tuple(opportunities),
            rpc_health=health,
        )
