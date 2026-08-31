"""Free, keyless SOL/USD price lookup (Jupiter Price API v3), cached briefly
so the scanner doesn't hammer it every cycle."""
import asyncio
import logging
import time

import httpx

logger = logging.getLogger("app.scanners.price_feed")

SOL_MINT = "So11111111111111111111111111111111111111112"
_CACHE_TTL_SECONDS = 30
_cache: dict[str, tuple[float, float]] = {}


async def get_sol_usd_price(base_url: str = "https://lite-api.jup.ag/price/v3") -> float:
    cached = _cache.get("SOL")
    now = time.monotonic()
    if cached and (now - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]

    last_exc = None
    for attempt, delay in enumerate((0.0, 0.25, 0.75), start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(base_url, params={"ids": SOL_MINT})
                resp.raise_for_status()
                data = resp.json()
                price = float(data[SOL_MINT]["usdPrice"])
                if price <= 0:
                    raise ValueError("non-positive SOL/USD price")
                _cache["SOL"] = (price, time.monotonic())
                return price
        except Exception as exc:
            last_exc = exc

    logger.warning(
        "sol_price_fetch_failed",
        extra={"error": f"{type(last_exc).__name__}: {last_exc}", "attempts": 3},
    )
    return cached[0] if cached else 0.0
