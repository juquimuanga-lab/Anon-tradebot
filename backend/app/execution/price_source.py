"""Shared price source: real Anoncoin coin-details or a live Meteora DBC pool
read when available, otherwise a labelled simulated random-walk so paper
trading and position monitoring keep working end to end while upstream
discovery/detail endpoints are "Coming Soon".
"""
import logging
import random

from app.config.settings import settings
from app.connectors.anoncoin import AnoncoinClient, AnoncoinUnavailable
from app.execution.onchain import meteora_dbc
from app.execution.onchain.meteora_dbc import DbcBuildError
from app.scanners import price_feed
from app.scoring.rules import TokenSnapshot

logger = logging.getLogger("app.execution.price_source")


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

    if token.source == "anoncoin_onchain":
        try:
            info = await meteora_dbc.get_pool_info(token.mint, settings.solana_rpc_url)
            sol_price = await price_feed.get_sol_usd_price(settings.jupiter_price_url)
            return info["price_sol_per_token"] * sol_price, False
        except DbcBuildError as exc:
            logger.warning("live_pool_price_lookup_failed", extra={"mint": token.mint, "error": str(exc)})

    return _walk_price(token.mint, token.price_usd or 0.000001, tick), True
