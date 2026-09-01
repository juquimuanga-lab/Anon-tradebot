import pytest

from app.arbitrage.live_executor import ArbitrageLiveExecutor, ArbitrageLiveExecutionError


def test_live_execution_is_locked_by_default(monkeypatch):
    monkeypatch.delenv("ARBITRAGE_LIVE_TRADING_ENABLED", raising=False)
    executor = ArbitrageLiveExecutor()
    assert executor.live_enabled is False


def test_live_execution_can_only_be_armed_explicitly(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_LIVE_TRADING_ENABLED", "true")
    executor = ArbitrageLiveExecutor()
    assert executor.live_enabled is True


def test_default_trade_size_limit_is_small(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_LIVE_TRADING_ENABLED", "true")
    monkeypatch.delenv("ARBITRAGE_LIVE_MAX_SOL", raising=False)
    executor = ArbitrageLiveExecutor()
    assert executor._max_trade_lamports == 100_000_000


def test_positive_int_rejects_missing_or_non_positive_values():
    with pytest.raises(ArbitrageLiveExecutionError):
        ArbitrageLiveExecutor._positive_int({}, "otherAmountThreshold", "quote")
    with pytest.raises(ArbitrageLiveExecutionError):
        ArbitrageLiveExecutor._positive_int(
            {"otherAmountThreshold": "0"}, "otherAmountThreshold", "quote"
        )


def test_positive_int_accepts_jupiter_atomic_amount():
    assert ArbitrageLiveExecutor._positive_int(
        {"otherAmountThreshold": "12345"}, "otherAmountThreshold", "quote"
    ) == 12345


def test_bundle_reconciliation_rejects_bundle_error():
    import asyncio

    async def run():
        ok, reason, signatures = await ArbitrageLiveExecutor()._reconcile_landed_bundle(
            "https://rpc.example",
            {
                "confirmationStatus": "confirmed",
                "err": {"InstructionError": [0, "Custom"]},
                "transactions": ["sig1", "sig2"],
            },
        )
        assert ok is False
        assert reason.startswith("bundle_error:")
        assert signatures == ("sig1", "sig2")

    asyncio.run(run())
