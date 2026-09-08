"""Runtime controls for the isolated Robinhood Doppler/SPCX sniper lane."""
from __future__ import annotations

import os

SPCX_TOKEN = "0x4a0e65a3eccec6dbe60ae065f2e7bb85fae35eea"

_enabled = False
_buy_size_spcx: float | None = None


def is_enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = bool(value)


def get_buy_size_spcx() -> float:
    if _buy_size_spcx is not None:
        return _buy_size_spcx
    raw = os.getenv("DOPPLER_BUY_SIZE_SPCX", "0")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def set_buy_size_spcx(value: float) -> None:
    global _buy_size_spcx
    _buy_size_spcx = float(value)
