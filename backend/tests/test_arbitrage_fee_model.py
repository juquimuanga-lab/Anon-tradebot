import asyncio

import pytest

from app.arbitrage.fee_model import (
    MIN_JITO_TIP_LAMPORTS,
    calculate_profitability,
    max_affordable_jito_tip,
)
from app.arbitrage.live_executor import ArbitrageLiveExecutor


class FakeJito:
    async def get_tip_floor(self, url):
        return {"landed_tips_50th_percentile": 0.000075}


class FailingJito:
    async def get_tip_floor(self, url):
        raise RuntimeError("tip service unavailable")


def test_profitability_breakdown_accounts_for_tip_and_priority():
    result = calculate_profitability(
        input_atomic=40_000_000,
        final_output_atomic=40_300_000,
        venue_cost_atomic_value=0,
        priority_fee_atomic=100_000,
        jito_tip_atomic=75_000,
    )
    assert result.gross_profit_atomic == 300_000
    assert result.total_cost_atomic == 175_000
    assert result.net_profit_atomic == 125_000


def test_max_affordable_tip_leaves_strictly_positive_net():
    max_tip = max_affordable_jito_tip(
        gross_profit_atomic=300_000,
        venue_cost_atomic_value=0,
        priority_fee_atomic=100_000,
    )
    assert max_tip == 199_999
    assert max_tip >= MIN_JITO_TIP_LAMPORTS


def test_dynamic_tip_uses_configured_jito_percentile(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_LIVE_JITO_TIP_PERCENTILE", "50")
    executor = ArbitrageLiveExecutor()
    executor._jito = FakeJito()
    tip = asyncio.run(executor._dynamic_jito_tip_lamports())
    assert tip == 75_000


def test_dynamic_tip_falls_back_when_tip_service_is_unavailable(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_LIVE_JITO_FALLBACK_TIP_LAMPORTS", "100000")
    executor = ArbitrageLiveExecutor()
    executor._jito = FailingJito()
    tip = asyncio.run(executor._dynamic_jito_tip_lamports())
    assert tip == 100_000


def test_dynamic_tip_cache_avoids_repeated_tip_requests():
    class CountingJito:
        def __init__(self):
            self.calls = 0

        async def get_tip_floor(self, url):
            self.calls += 1
            return {"landed_tips_50th_percentile": 0.00005}

    fake = CountingJito()
    executor = ArbitrageLiveExecutor()
    executor._jito = fake
    first = asyncio.run(executor._dynamic_jito_tip_lamports())
    second = asyncio.run(executor._dynamic_jito_tip_lamports())
    assert first == 50_000
    assert second == 50_000
    assert fake.calls == 1
