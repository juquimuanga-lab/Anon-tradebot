"""Regression tests for live arbitrage RPC compatibility and fee policy."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_live_wallet_balance_uses_shared_rpc_helper(monkeypatch):
    from app.arbitrage.live_executor import ArbitrageLiveExecutor

    calls = []

    async def fake_rpc_request(rpc_url, method, params):
        calls.append((rpc_url, method, params))
        return {"context": {"slot": 1}, "value": 123456}

    monkeypatch.setattr("app.arbitrage.live_executor._rpc_request", fake_rpc_request)
    executor = ArbitrageLiveExecutor()

    class FakeOwner:
        def pubkey(self):
            return "Wallet111111111111111111111111111111111"

    balance = await executor._wallet_balance("https://rpc.example", FakeOwner())
    assert balance == 123456
    assert calls[0][1] == "getBalance"


def test_jupiter_bundle_uses_supported_economical_priority_defaults(monkeypatch):
    monkeypatch.delenv("ARBITRAGE_LIVE_PRIORITY_LEVEL", raising=False)
    monkeypatch.delenv("ARBITRAGE_LIVE_MAX_PRIORITY_FEE_LAMPORTS", raising=False)
    from app.arbitrage.jupiter_bundle import _priority_fee_config

    assert _priority_fee_config() == {
        "priorityLevelWithMaxLamports": {
            "priorityLevel": "medium",
            "maxLamports": 100000,
        }
    }


def test_jupiter_bundle_rejects_unsupported_low_priority_level(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_LIVE_PRIORITY_LEVEL", "low")
    from app.arbitrage.jupiter_bundle import _priority_fee_config

    assert _priority_fee_config()["priorityLevelWithMaxLamports"]["priorityLevel"] == "medium"


def test_jupiter_bundle_priority_policy_is_overridable(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_LIVE_PRIORITY_LEVEL", "high")
    monkeypatch.setenv("ARBITRAGE_LIVE_MAX_PRIORITY_FEE_LAMPORTS", "50000")
    from app.arbitrage.jupiter_bundle import _priority_fee_config

    assert _priority_fee_config()["priorityLevelWithMaxLamports"] == {
        "priorityLevel": "high",
        "maxLamports": 50000,
    }


def test_jupiter_bundle_does_not_depend_on_asyncclient_lookup_method():
    from pathlib import Path

    source = Path("app/arbitrage/jupiter_bundle.py").read_text()
    assert "get_address_lookup_table" not in source
    assert "getAccountInfo" in source
