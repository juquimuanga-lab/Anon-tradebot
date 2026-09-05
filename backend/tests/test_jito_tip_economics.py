"""Regression tests for economical Jito tip defaults."""
from __future__ import annotations


def test_live_jito_defaults_are_economical(monkeypatch):
    monkeypatch.delenv("ARBITRAGE_LIVE_JITO_TIP_PERCENTILE", raising=False)
    monkeypatch.delenv("ARBITRAGE_LIVE_JITO_FALLBACK_TIP_LAMPORTS", raising=False)

    from app.arbitrage.live_executor import ArbitrageLiveExecutor

    executor = ArbitrageLiveExecutor()
    assert executor._tip_percentile == 25
    assert executor._fallback_tip_lamports == 1_000


def test_live_jito_percentile_is_clamped_to_supported_range(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_LIVE_JITO_TIP_PERCENTILE", "10")
    from app.arbitrage.live_executor import ArbitrageLiveExecutor
    assert ArbitrageLiveExecutor()._tip_percentile == 25

    monkeypatch.setenv("ARBITRAGE_LIVE_JITO_TIP_PERCENTILE", "100")
    assert ArbitrageLiveExecutor()._tip_percentile == 99
