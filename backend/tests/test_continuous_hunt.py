import asyncio

import pytest

from app.arbitrage.continuous_hunt import ContinuousArbitrageHunt


@pytest.mark.asyncio
async def test_stop_stops_running_hunt(monkeypatch):
    controller = ContinuousArbitrageHunt()
    calls = 0

    class FakeHunter:
        async def hunt(self, limit=None):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            from app.arbitrage.hunt import HuntResult
            return HuntResult((), ())

        async def close(self):
            return None

    monkeypatch.setattr("app.arbitrage.continuous_hunt.ArbitrageHunter", FakeHunter)
    monkeypatch.setenv("ARBITRAGE_HUNT_INTERVAL_SECONDS", "1")

    assert await controller.start()
    await asyncio.sleep(0.02)
    assert controller.running
    assert await controller.stop()
    assert not controller.running
    assert calls >= 1


@pytest.mark.asyncio
async def test_cannot_start_twice(monkeypatch):
    controller = ContinuousArbitrageHunt()

    class FakeHunter:
        async def hunt(self, limit=None):
            from app.arbitrage.hunt import HuntResult
            await asyncio.sleep(0.01)
            return HuntResult((), ())

        async def close(self):
            return None

    monkeypatch.setattr("app.arbitrage.continuous_hunt.ArbitrageHunter", FakeHunter)
    assert await controller.start()
    assert not await controller.start()
    await controller.stop()
