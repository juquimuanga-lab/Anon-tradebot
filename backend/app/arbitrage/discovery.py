"""Unrestricted Jupiter route discovery for observe-only arbitrage analysis."""
from __future__ import annotations

from dataclasses import dataclass

from app.arbitrage.engine import ArbitrageConfig, find_two_venue_opportunity
from app.arbitrage.jupiter_quotes import JupiterArbitrageError, JupiterArbitrageQuoteProvider
from app.arbitrage.models import ArbitrageOpportunity, Quote
from app.arbitrage.rpc_health import ArbitrageRpcHealth, RpcHealth
from app.execution.onchain.jupiter import SOL_MINT

LAMPORTS_PER_SOL = 1_000_000_000


@dataclass(frozen=True)
class DiscoveryResult:
    token_mint: str
    amount_sol: float
    buy_quote: Quote | None
    sell_quote: Quote | None
    opportunity: ArbitrageOpportunity | None
    rpc_health: RpcHealth | None
    error: str | None = None


class ArbitrageDiscovery:
    """Ask Jupiter for its best unrestricted route for both legs."""

    def __init__(self, provider=None, rpc_health=None) -> None:
        self.provider = provider or JupiterArbitrageQuoteProvider()
        self.rpc_health = rpc_health or ArbitrageRpcHealth()

    async def close(self) -> None:
        await self.provider.aclose()

    async def discover(self, token_mint: str, amount_sol: float, config=None) -> DiscoveryResult:
        if not token_mint or len(token_mint) < 20:
            raise ValueError("token_mint does not look like a Solana mint")
        if amount_sol <= 0:
            raise ValueError("amount_sol must be positive")

        health = await self.rpc_health.check()
        if not health.healthy:
            return DiscoveryResult(token_mint, amount_sol, None, None, None, health, health.error)

        amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)
        try:
            buy = await self.provider.unrestricted_quote(
                SOL_MINT, token_mint, amount_lamports, slippage_bps=30
            )
            if buy is None:
                return DiscoveryResult(token_mint, amount_sol, None, None, None, health, "no buy route")

            sell = await self.provider.unrestricted_quote(
                token_mint, SOL_MINT, buy.output_amount_atomic, slippage_bps=30
            )
            if sell is None:
                return DiscoveryResult(token_mint, amount_sol, buy, None, None, health, "no sell route")

            opportunity = find_two_venue_opportunity(
                token_mint, buy, sell, config or ArbitrageConfig()
            )
            return DiscoveryResult(token_mint, amount_sol, buy, sell, opportunity, health)
        except JupiterArbitrageError as exc:
            return DiscoveryResult(token_mint, amount_sol, None, None, None, health, str(exc))


async def discover_arbitrage(token_mint: str, amount_sol: float) -> DiscoveryResult:
    discovery = ArbitrageDiscovery()
    try:
        return await discovery.discover(token_mint, amount_sol)
    finally:
        await discovery.close()
