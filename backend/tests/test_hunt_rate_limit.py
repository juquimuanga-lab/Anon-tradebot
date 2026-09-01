from __future__ import annotations

from app.arbitrage.hunt import _hunt_sizes


def test_hunt_uses_compact_default_size_ladder(monkeypatch):
    monkeypatch.delenv("ARBITRAGE_HUNT_DISCOVERY_SIZES_SOL", raising=False)
    assert _hunt_sizes() == (0.02, 0.10, 0.50)


def test_hunt_size_ladder_can_be_overridden(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_HUNT_DISCOVERY_SIZES_SOL", "0.02,0.20,0.50,0.20")
    assert _hunt_sizes() == (0.02, 0.20, 0.50)
