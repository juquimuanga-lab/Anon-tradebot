import pytest
import respx
from httpx import Response

from app.connectors.solscan import SolscanAPIError, SolscanClient

BASE_URL = "https://pro-api.solscan.io/v2.0"


@pytest.mark.asyncio
@respx.mock
async def test_get_token_meta_sends_token_header():
    route = respx.get(f"{BASE_URL}/token/meta").mock(
        return_value=Response(200, json={"data": {"symbol": "ABC"}})
    )
    client = SolscanClient(BASE_URL, "fake-solscan-key")
    data = await client.get_token_meta("mintABC")
    assert data["data"]["symbol"] == "ABC"
    assert route.calls.last.request.headers["token"] == "fake-solscan-key"
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_latest_tokens_with_platform_filter():
    route = respx.get(f"{BASE_URL}/token/latest").mock(
        return_value=Response(200, json={"data": [{"token_address": "abc"}], "total_items": 1})
    )
    client = SolscanClient(BASE_URL, "fake-solscan-key")
    data = await client.get_latest_tokens(platform_id="pumpfun")
    assert data["data"][0]["token_address"] == "abc"
    assert route.calls.last.request.url.params["platform_id"] == "pumpfun"
    await client.aclose()


@pytest.mark.asyncio
async def test_missing_key_raises_clear_error():
    client = SolscanClient(BASE_URL, None)
    with pytest.raises(SolscanAPIError):
        await client.get_token_holders("mintABC")
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_error_does_not_leak_key():
    respx.get(f"{BASE_URL}/token/holders").mock(return_value=Response(429))
    client = SolscanClient(BASE_URL, "fake-solscan-key")
    with pytest.raises(SolscanAPIError) as exc_info:
        await client.get_token_holders("mintABC")
    assert "fake-solscan-key" not in str(exc_info.value)
    await client.aclose()
