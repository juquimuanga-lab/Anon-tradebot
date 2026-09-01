"""Unrestricted Jupiter route discovery with observe-only size optimization."""
from __future__ import annotations

import os
from dataclasses import dataclass

from app.arbitrage.engine import ArbitrageConfig, find_two_venue_opportunity
from app.arbitrage.jupiter_quotes import JupiterArbitrageError, JupiterArbitrageQuoteProvider
from app.arbitrage.models import ArbitrageOpportunity, Quote
from app.arbitrage.rpc_health import ArbitrageRpcHealth, RpcHealth
from app.execution.onchain.jupiter import SOL_MINT

LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_DISCOVERY_SIZES_SOL = (0.02, 0.05, 0.10, 0.25, 0.50, 1.00)


@dataclass(frozen=True)
class DiscoveryCandidate:
    amount_sol: float
    buy_quote: Quote | None
    sell_quote: Quote | None
    opportunity: ArbitrageOpportunity | None
    error: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    token_mint: str
    amount_sol: float
    buy_quote: Quote | None
    sell_quote: Quote | None
    opportunity: ArbitrageOpportunity | None
    rpc_health: RpcHealth | None
    error: str | None = None
    candidates: tuple[DiscoveryCandidate, ...] = ()


def configured_discovery_sizes() -> tuple[float, ...]:
    raw = os.getenv("ARBITRAGE_DISCOVERY_SIZES_SOL", "").strip()
    if not raw:
        return DEFAULT_DISCOVERY_SIZES_SOL

    values: list[float] = []
    for item in raw.split(","):
        try:
            value = float(item.strip())
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return tuple(dict.fromkeys(values)) or DEFAULT_DISCOVERY_SIZES_SOL


class ArbitrageDiscovery:
    """Ask Jupiter for its best unrestricted route at one or many sizes."""

    def __init__(self, provider=None, rpc_health=None) -> None:
        self.provider = provider or JupiterArbitrageQuoteProvider()
        self.rpc_health = rpc_health or ArbitrageRpcHealth()

    async def close(self) -> None:
        await self.provider.aclose()

    async def discover(
        self,
        token_mint: str,
        amount_sol: float | None = None,
        config: ArbitrageConfig | None = None,
        sizes_sol: tuple[float, ...] | None = None,
    ) -> DiscoveryResult:
        if not token_mint or len(token_mint) < 20:
            raise ValueError("token_mint does not look like a Solana mint")
        if amount_sol is not None and amount_sol <= 0:
            raise ValueError("amount_sol must be positive")
        if sizes_sol is not None and any(size <= 0 for size in sizes_sol):
            raise ValueError("sizes_sol must contain only positive values")

        health = await self.rpc_health.check()
        if not health.healthy:
            return DiscoveryResult(token_mint, amount_sol or 0.0, None, None, None, health, health.error)

        sizes = (amount_sol,) if amount_sol is not None else (sizes_sol or configured_discovery_sizes())
        if not sizes:
            raise ValueError("at least one discovery size is required")
        cfg = config or ArbitrageConfig.from_env()
        candidates: list[DiscoveryCandidate] = []

        for size in sizes:
            candidates.append(await self._discover_size(token_mint, size, cfg))

        valid = [c for c in candidates if c.opportunity is not None]
        if not valid:
            first_error = next((c.error for c in candidates if c.error), "no complete unrestricted Jupiter round-trip was found")
            return DiscoveryResult(token_mint, sizes[0], None, None, None, health, first_error, tuple(candidates))

        best = max(
            valid,
            key=lambda c: (
                c.opportunity.net_profit_atomic if c.opportunity else -1,
                c.opportunity.net_profit_bps if c.opportunity else float("-inf"),
            ),
        )
        return DiscoveryResult(
            token_mint,
            best.amount_sol,
            best.buy_quote,
            best.sell_quote,
            best.opportunity,
            health,
            best.error,
            tuple(candidates),
        )

    async def _discover_size(
        self,
        token_mint: str,
        amount_sol: float,
        config: ArbitrageConfig,
    ) -> DiscoveryCandidate:
        amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)
        try:
            buy = await self.provider.unrestricted_quote(
                SOL_MINT, token_mint, amount_lamports, slippage_bps=int(config.max_slippage_bps)
            )
            if buy is None:
                return DiscoveryCandidate(amount_sol, None, None, None, "no buy route")

            sell = await self.provider.unrestricted_quote(
                token_mint, SOL_MINT, buy.output_amount_atomic, slippage_bps=int(config.max_slippage_bps)
            )
            if sell is None:
                return DiscoveryCandidate(amount_sol, buy, None, None, "no sell route")

            opportunity = find_two_venue_opportunity(token_mint, buy, sell, config)
            return DiscoveryCandidate(amount_sol, buy, sell, opportunity)
        except JupiterArbitrageError as exc:
            return DiscoveryCandidate(amount_sol, None, None, None, str(exc))


async def discover_arbitrage(token_mint: str, amount_sol: float | None = None) -> DiscoveryResult:
    discovery = ArbitrageDiscovery()
    try:
        return await discovery.discover(token_mint, amount_sol)
    finally:
        await discovery.close()
