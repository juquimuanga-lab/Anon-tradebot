import pytest

from app.arbitrage.discovery import ArbitrageDiscovery, configured_discovery_sizes
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
    assert len(result.candidates) == 1


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
    assert len(result.candidates) == 1


class SizeAwareProvider:
    async def aclose(self):
        pass

    async def unrestricted_quote(self, input_mint, output_mint, input_amount_atomic, slippage_bps=30):
        if output_mint == "TOKEN":
            return Quote("jupiter_best_route", input_mint, output_mint, input_amount_atomic, input_amount_atomic * 10, 0.0, 5.0, "BuyRoute")
        # 0.02 SOL: +0.10%; 0.10 SOL: +0.20%; larger sizes: slightly worse.
        if input_amount_atomic == 200_000_000:
            output = 20_020_000
        elif input_amount_atomic == 1_000_000_000:
            output = 100_200_000
        else:
            output = input_amount_atomic // 10
        return Quote("jupiter_best_route", input_mint, output_mint, input_amount_atomic, output, 0.0, 5.0, "SellRoute")


@pytest.mark.asyncio
async def test_discovery_size_sweep_selects_best_net_profit():
    discovery = ArbitrageDiscovery(provider=SizeAwareProvider(), rpc_health=FakeHealth())
    result = await discovery.discover(
        "T" * 32,
        None,
        config=__import__("app.arbitrage.engine", fromlist=["ArbitrageConfig"]).ArbitrageConfig(
            min_profit_bps=0.0,
            min_profit_atomic=0,
            estimated_priority_fee_atomic=0,
            estimated_jito_tip_atomic=0,
            execution_safety_bps=0.0,
        ),
    )
    await discovery.close()

    assert len(result.candidates) == 6
    assert result.amount_sol == 0.1
    assert result.opportunity is not None
    assert result.opportunity.net_profit_atomic == 200_000


def test_discovery_sizes_can_be_overridden(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_DISCOVERY_SIZES_SOL", "0.01,0.05,0.05,0.25")
    assert configured_discovery_sizes() == (0.01, 0.05, 0.25)
