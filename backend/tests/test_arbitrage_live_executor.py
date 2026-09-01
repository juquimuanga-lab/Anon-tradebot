import os


def test_live_execution_is_locked_by_default(monkeypatch):
    monkeypatch.delenv("ARBITRAGE_LIVE_TRADING_ENABLED", raising=False)
    from app.arbitrage.live_executor import ArbitrageLiveExecutor

    executor = ArbitrageLiveExecutor()
    assert executor.live_enabled is False


def test_live_execution_can_only_be_armed_explicitly(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_LIVE_TRADING_ENABLED", "true")
    from app.arbitrage.live_executor import ArbitrageLiveExecutor

    executor = ArbitrageLiveExecutor()
    assert executor.live_enabled is True
