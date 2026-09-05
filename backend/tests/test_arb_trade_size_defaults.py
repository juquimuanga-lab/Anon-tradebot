"""Regression tests for the expanded arbitrage discovery/trade-size defaults."""
from __future__ import annotations


def test_live_max_trade_defaults_to_one_sol(monkeypatch):
    monkeypatch.delenv("ARBITRAGE_LIVE_MAX_SOL", raising=False)
    from app.arbitrage.live_executor import ArbitrageLiveExecutor, LAMPORTS_PER_SOL

    assert ArbitrageLiveExecutor()._max_trade_lamports == LAMPORTS_PER_SOL


def test_hunt_sizes_include_one_sol(monkeypatch):
    monkeypatch.delenv("ARBITRAGE_HUNT_DISCOVERY_SIZES_SOL", raising=False)
    from app.arbitrage.continuous_hunt import _hunt_sizes

    assert _hunt_sizes() == (0.02, 0.04, 0.10, 0.50, 1.00)
