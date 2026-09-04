"""Regression tests for arbitrage telemetry and simulation hardening."""
from __future__ import annotations

import asyncio

import pytest



def test_telemetry_records_bounded_latency_and_counters():
    from app.arbitrage.telemetry import ArbitrageTelemetry

    metrics = ArbitrageTelemetry()
    metrics.increment("attempts")
    metrics.increment("attempts", 2)
    for _ in range(205):
        metrics.observe("stage_ms", 1.0)

    snapshot = metrics.snapshot()
    assert snapshot["counters"]["attempts"] == 3
    assert snapshot["latency_ms"]["stage_ms"]["count"] == 200
    assert snapshot["latency_ms"]["stage_ms"]["p95_ms"] == 1.0


@pytest.mark.asyncio
async def test_live_simulation_uses_shared_rpc_helper(monkeypatch):
    from app.arbitrage.live_executor import ArbitrageLiveExecutor

    calls = []

    async def fake_rpc_request(rpc_url, method, params):
        calls.append((rpc_url, method, params))
        return {"value": {"err": None}}

    monkeypatch.setattr("app.arbitrage.live_executor._rpc_request", fake_rpc_request)
    executor = ArbitrageLiveExecutor()
    await executor._simulate("https://rpc.example", b"signed")

    assert calls[0][1] == "simulateTransaction"
    assert calls[0][2][1]["encoding"] == "base64"


@pytest.mark.asyncio
async def test_live_simulation_rejects_transaction_error(monkeypatch):
    from app.arbitrage.live_executor import ArbitrageLiveExecutionError, ArbitrageLiveExecutor

    async def fake_rpc_request(*args, **kwargs):
        return {"value": {"err": {"InstructionError": [0, "failed"]}}}

    monkeypatch.setattr("app.arbitrage.live_executor._rpc_request", fake_rpc_request)
    executor = ArbitrageLiveExecutor()

    with pytest.raises(ArbitrageLiveExecutionError, match="transaction simulation failed"):
        await executor._simulate("https://rpc.example", b"signed")


@pytest.mark.asyncio
async def test_jito_fallback_tip_is_cached(monkeypatch):
    from app.arbitrage.live_executor import ArbitrageLiveExecutor

    executor = ArbitrageLiveExecutor()
    calls = 0

    async def failing_tip_floor(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("tip service unavailable")

    monkeypatch.setattr(executor._jito, "get_tip_floor", failing_tip_floor)
    first = await executor._dynamic_jito_tip_lamports()
    second = await executor._dynamic_jito_tip_lamports()

    assert first == executor._fallback_tip_lamports
    assert second == executor._fallback_tip_lamports
    assert calls == 1
