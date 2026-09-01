import pytest

from app.arbitrage.discovery import ArbitrageDiscovery
from app.arbitrage.models import Quote


class FakeHealth:
    async def check(self):
        return type("Health", (), {"healthy": True, "provider": "test", "slot": 1, "error": None})()


class FakeProvider:
    async def aclose(self):
        pass

    async def unrestricted_quote(self, input_mint, output_mint, input_amount_atomic, slippage_bps=30):
        if output_mint == "TOKEN":
            return Quote("jupiter_best_route", input_mint, output_mint, input_amount_atomic, 1_000_000, 0.0, 5.0, "Raydium>Meteora DLMM")
        return Quote("jupiter_best_route", input_mint, output_mint, input_amount_atomic, 101_000_000, 0.0, 5.0, "Meteora DLMM>Raydium")


@pytest.mark.asyncio
async def test_discovery_uses_unrestricted_routes_and_returns_route_ids():
    discovery = ArbitrageDiscovery(provider=FakeProvider(), rpc_health=FakeHealth())
    result = await discovery.discover("T" * 32, 0.1)
    await discovery.close()

    assert result.error is None
    assert result.buy_quote is not None
    assert result.sell_quote is not None
    assert result.buy_quote.route_id == "Raydium>Meteora DLMM"
    assert result.sell_quote.route_id == "Meteora DLMM>Raydium"
    assert result.opportunity is not None


class NoRouteProvider(FakeProvider):
    async def unrestricted_quote(self, input_mint, output_mint, input_amount_atomic, slippage_bps=30):
        return None


@pytest.mark.asyncio
async def test_discovery_reports_no_buy_route_without_execution():
    discovery = ArbitrageDiscovery(provider=NoRouteProvider(), rpc_health=FakeHealth())
    result = await discovery.discover("T" * 32, 0.01)
    await discovery.close()

    assert result.buy_quote is None
    assert result.sell_quote is None
    assert result.opportunity is None
    assert result.error == "no buy route"
