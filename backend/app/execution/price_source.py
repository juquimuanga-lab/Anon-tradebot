"""Shared price source: real Anoncoin coin-details when available, otherwise a
labelled simulated random-walk so paper trading and position monitoring keep
working end to end while upstream discovery/detail endpoints are "Coming Soon".
"""
import random

from app.connectors.anoncoin import AnoncoinClient, AnoncoinUnavailable
from app.scoring.rules import TokenSnapshot


def _walk_price(mint: str, base_price: float, tick: int) -> float:
    rng = random.Random(f"{mint}:{tick}")
    drift = rng.uniform(-0.06, 0.07)
    return max(base_price * 1e-6, base_price * (1 + drift))


async def get_current_price_usd(client: AnoncoinClient, token: TokenSnapshot, tick: int = 0) -> tuple[float, bool]:
    """Returns (price_usd, is_simulated)."""
    if token.source == "anoncoin":
        try:
            details = await client.get_coin_details(token.mint)
            price = float(details.get("priceUsd", token.price_usd) or token.price_usd)
            return price, False
        except AnoncoinUnavailable:
            pass
    return _walk_price(token.mint, token.price_usd or 0.000001, tick), True
