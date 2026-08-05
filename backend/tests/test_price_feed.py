import time

import pytest
import respx
from httpx import Response

from app.scanners import price_feed

SOL_MINT = "So11111111111111111111111111111111111111112"
PRICE_URL = "https://lite-api.jup.ag/price/v3"


@pytest.mark.asyncio
@respx.mock
async def test_get_sol_usd_price_parses_response():
    respx.get(PRICE_URL).mock(
        return_value=Response(200, json={SOL_MINT: {"usdPrice": 123.45}})
    )
    price_feed._cache.clear()

    price = await price_feed.get_sol_usd_price(PRICE_URL)

    assert price == pytest.approx(123.45)


@pytest.mark.asyncio
@respx.mock
async def test_get_sol_usd_price_falls_back_to_cache_on_error():
    price_feed._cache.clear()
    price_feed._cache["SOL"] = (50.0, time.monotonic() - 999)  # force stale cache entry
    respx.get(PRICE_URL).mock(return_value=Response(500))

    price = await price_feed.get_sol_usd_price(PRICE_URL)

    assert price == 50.0
