import pytest
import respx
from httpx import Response

from app.connectors.anoncoin import AnoncoinClient, AnoncoinAPIError, AnoncoinUnavailable

BASE_URL = "https://api.anoncoin.it"


async def _fake_key():
    return "test-anoncoin-key"


@pytest.mark.asyncio
@respx.mock
async def test_get_coins_success():
    respx.get(f"{BASE_URL}/services/v2/coins").mock(
        return_value=Response(200, json=[{"mint": "abc", "tickerSymbol": "ABC"}])
    )
    client = AnoncoinClient(BASE_URL, _fake_key)
    coins = await client.get_coins()
    assert coins[0]["mint"] == "abc"
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_coins_not_yet_live_raises_unavailable():
    respx.get(f"{BASE_URL}/services/v2/coins").mock(return_value=Response(404))
    client = AnoncoinClient(BASE_URL, _fake_key)
    with pytest.raises(AnoncoinUnavailable):
        await client.get_coins()
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_coin_details_sends_api_key_header_not_url():
    route = respx.get(f"{BASE_URL}/services/v2/coin-details").mock(
        return_value=Response(200, json={"data": {"mint": "abc"}})
    )
    client = AnoncoinClient(BASE_URL, _fake_key)
    await client.get_coin_details("abc")
    sent_request = route.calls.last.request
    assert sent_request.headers["x-api-key"] == "test-anoncoin-key"
    await client.aclose()


@pytest.mark.asyncio
async def test_missing_api_key_raises_clear_error():
    async def no_key():
        return None

    client = AnoncoinClient(BASE_URL, no_key)
    with pytest.raises(AnoncoinAPIError):
        await client.get_my_profile()
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_server_error_raises_api_error_without_leaking_key():
    respx.get(f"{BASE_URL}/services/v2/top-holders").mock(return_value=Response(401))
    client = AnoncoinClient(BASE_URL, _fake_key)
    with pytest.raises(AnoncoinAPIError) as exc_info:
        await client.get_top_holders()
    assert "test-anoncoin-key" not in str(exc_info.value)
    await client.aclose()
