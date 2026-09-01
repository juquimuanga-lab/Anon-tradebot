"""Jupiter quote connector for venue-constrained Solana arbitrage scans.

Jupiter documents DEX filtering by venue label. We use that capability to
request separate buy/sell quotes constrained to one liquidity source at a time.
This module only fetches quotes; it never signs or submits transactions.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import httpx

from app.arbitrage.models import Quote
from app.execution.onchain.jupiter import SOL_MINT


class JupiterArbitrageError(Exception):
    """Raised when a venue-constrained Jupiter quote cannot be obtained."""


@dataclass(frozen=True)
class VenueConfig:
    name: str
    jupiter_dex_label: str
    fee_bps: float = 30.0


DEFAULT_VENUES = (
    VenueConfig("raydium", "Raydium", 30.0),
    VenueConfig("raydium_clmm", "Raydium CLMM", 30.0),
    VenueConfig("raydium_cpmm", "Raydium CPMM", 25.0),
    VenueConfig("orca_whirlpool", "Orca Whirlpool", 30.0),
    VenueConfig("meteora_dlmm", "Meteora DLMM", 30.0),
    VenueConfig("pumpswap", "Pumpfun AMM", 30.0),
)


DEFAULT_JUPITER_API_BASE_URL = "https://api.jup.ag/swap/v1"
DEFAULT_JUPITER_LITE_BASE_URL = "https://lite-api.jup.ag/swap/v1"


class JupiterArbitrageQuoteProvider:
    """Fetch exact-input quotes restricted to one Jupiter DEX label."""

    def __init__(self, base_url: str | None = None, timeout_seconds: float = 8.0) -> None:
        configured_base_url = (base_url or os.getenv("JUPITER_BASE_URL", "")).strip()
        api_key = os.getenv("JUPITER_API_KEY", "").strip()
        if configured_base_url:
            resolved_base_url = configured_base_url
        elif api_key:
            # Authenticated Jupiter API endpoint when a production API key is present.
            resolved_base_url = DEFAULT_JUPITER_API_BASE_URL
        else:
            resolved_base_url = DEFAULT_JUPITER_LITE_BASE_URL

        headers = {"x-api-key": api_key} if api_key else {}
        self.base_url = resolved_base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def quote(
        self,
        venue: VenueConfig,
        direction: str,
        input_amount_atomic: int,
        token_mint: str,
        slippage_bps: int = 30,
    ) -> Optional[Quote]:
        if direction not in {"buy", "sell"}:
            raise ValueError("direction must be 'buy' or 'sell'")
        if input_amount_atomic <= 0:
            raise ValueError("input_amount_atomic must be positive")

        input_mint = SOL_MINT if direction == "buy" else token_mint
        output_mint = token_mint if direction == "buy" else SOL_MINT

        response = await self._client.get(
            "/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": input_amount_atomic,
                "slippageBps": slippage_bps,
                "dexes": venue.jupiter_dex_label,
                "restrictIntermediateTokens": "true",
            },
        )
        if response.status_code != 200:
            detail = response.text[:300].replace("\n", " ")
            raise JupiterArbitrageError(
                f"Jupiter quote failed for {venue.name}: HTTP {response.status_code}"
                + (f" - {detail}" if detail else "")
            )

        payload = response.json()
        if payload.get("error"):
            raise JupiterArbitrageError(
                f"Jupiter quote failed for {venue.name}: {payload['error']}"
            )

        output_amount = int(payload.get("outAmount") or 0)
        if output_amount <= 0:
            return None

        try:
            price_impact_bps = float(payload.get("priceImpactPct") or 0.0) * 100.0
        except (TypeError, ValueError):
            price_impact_bps = 0.0

        route_plan = payload.get("routePlan") or []
        route_id = None
        if route_plan:
            labels = []
            for step in route_plan:
                swap_info = step.get("swapInfo") or {}
                label = swap_info.get("label") or swap_info.get("ammKey")
                if label:
                    labels.append(str(label))
            if labels:
                route_id = ">".join(labels)

        return Quote(
            venue=venue.name,
            input_mint=input_mint,
            output_mint=output_mint,
            input_amount_atomic=input_amount_atomic,
            output_amount_atomic=output_amount,
            fee_bps=venue.fee_bps,
            price_impact_bps=price_impact_bps,
            route_id=route_id,
        )


def configured_venues() -> tuple[VenueConfig, ...]:
    """Return configured venues, allowing deployments to override labels.

    ARBITRAGE_JUPITER_VENUES may contain a comma-separated list of
    ``name=jupiter_label[:fee_bps]`` entries. This keeps venue labels out of
    source code when Jupiter changes naming.
    """
    raw = os.getenv("ARBITRAGE_JUPITER_VENUES", "").strip()
    if not raw:
        return DEFAULT_VENUES

    venues: list[VenueConfig] = []
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, rest = item.split("=", 1)
        parts = rest.rsplit(":", 1)
        label = parts[0].strip()
        try:
            fee_bps = float(parts[1]) if len(parts) == 2 else 30.0
        except ValueError:
            fee_bps = 30.0
        if name.strip() and label:
            venues.append(VenueConfig(name.strip(), label, fee_bps))
    return tuple(venues) or DEFAULT_VENUES
