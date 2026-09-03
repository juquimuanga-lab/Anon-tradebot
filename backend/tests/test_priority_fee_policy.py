from __future__ import annotations


def test_default_priority_fee_policy_is_low_and_capped(monkeypatch):
    monkeypatch.delenv("ARBITRAGE_LIVE_PRIORITY_LEVEL", raising=False)
    monkeypatch.delenv("ARBITRAGE_LIVE_MAX_PRIORITY_FEE_LAMPORTS", raising=False)
    from app.arbitrage.jupiter_bundle import _priority_fee_config

    assert _priority_fee_config() == {
        "priorityLevelWithMaxLamports": {
            "priorityLevel": "low",
            "maxLamports": 100000,
        }
    }
