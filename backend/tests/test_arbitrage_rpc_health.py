import pytest
import respx
from httpx import Response

from app.arbitrage.rpc_health import ArbitrageRpcHealth


@pytest.mark.asyncio
@respx.mock
async def test_helius_health_is_used_when_primary_is_healthy(monkeypatch):
    monkeypatch.setattr(
        "app.arbitrage.rpc_health.settings.solana_rpc_url",
        "https://mainnet.helius-rpc.com/?api-key=test",
    )
    monkeypatch.setattr("app.arbitrage.rpc_health.settings.helius_api_key", "test")
    monkeypatch.setattr(
        "app.arbitrage.rpc_health.settings.alchemy_solana_rpc_url",
        "https://solana-mainnet.g.alchemy.com/v2/fallback",
    )

    async def helius_response(request):
        body = request.content.decode()
        return Response(200, json={"result": 123456 if "getSlot" in body else "ok"})

    health_route = respx.post("https://mainnet.helius-rpc.com/").mock(
        side_effect=helius_response
    )
    result = await ArbitrageRpcHealth().check()

    assert result.healthy is True
    assert result.provider == "helius"
    assert result.slot == 123456
    assert health_route.called
    assert len(respx.calls) == 2
    assert all(call.request.url.host != "solana-mainnet.g.alchemy.com" for call in respx.calls)


@pytest.mark.asyncio
@respx.mock
async def test_alchemy_is_used_when_primary_fails(monkeypatch):
    monkeypatch.setattr(
        "app.arbitrage.rpc_health.settings.solana_rpc_url",
        "https://mainnet.helius-rpc.com/?api-key=test",
    )
    monkeypatch.setattr("app.arbitrage.rpc_health.settings.helius_api_key", "test")
    monkeypatch.setattr(
        "app.arbitrage.rpc_health.settings.alchemy_solana_rpc_url",
        "https://solana-mainnet.g.alchemy.com/v2/fallback",
    )

    respx.post("https://mainnet.helius-rpc.com/").mock(
        return_value=Response(503, text="unavailable")
    )

    def alchemy_response(request):
        body = request.content.decode()
        return Response(200, json={"result": 777777 if "getSlot" in body else "ok"})

    alchemy_route = respx.post(
        "https://solana-mainnet.g.alchemy.com/v2/fallback"
    ).mock(side_effect=alchemy_response)

    result = await ArbitrageRpcHealth().check()

    assert result.healthy is True
    assert result.provider == "alchemy"
    assert result.slot == 777777
    assert alchemy_route.called
    assert len(respx.calls) == 3
