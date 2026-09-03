"""Regression tests for live arbitrage RPC compatibility helpers."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_live_wallet_balance_uses_shared_rpc_helper(monkeypatch):
    from app.arbitrage.live_executor import ArbitrageLiveExecutor

    calls = []

    async def fake_rpc_request(rpc_url, method, params):
        calls.append((rpc_url, method, params))
        return {"value": {"lamports": 123456}}

    monkeypatch.setattr("app.arbitrage.live_executor._rpc_request", fake_rpc_request)
    executor = ArbitrageLiveExecutor()

    class FakeOwner:
        def pubkey(self):
            return "Wallet111111111111111111111111111111111"

    balance = await executor._wallet_balance("https://rpc.example", FakeOwner())
    assert balance == 123456
    assert calls[0][1] == "getBalance"


def test_jupiter_bundle_does_not_depend_on_asyncclient_lookup_method():
    from pathlib import Path

    source = Path("app/arbitrage/jupiter_bundle.py").read_text()
    assert "get_address_lookup_table" not in source
    assert "getAccountInfo" in source
