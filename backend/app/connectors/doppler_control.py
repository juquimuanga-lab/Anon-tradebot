"""Runtime controls for the isolated Robinhood Doppler/SPCX sniper lane."""
from __future__ import annotations

import math
import os

SPCX_TOKEN = "0x4a0e65a3eccec6dbe60ae065f2e7bb85fae35eea"
ANONCOIN_ADDRESS_SUFFIX = "d09e"

_enabled = False
_buy_size_spcx: float | None = None


def is_enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = bool(value)


def deployment_enabled() -> bool:
    """Return the deployment-level Doppler gate."""
    raw = os.getenv("ROBINHOOD_DOPPLER_TRADING_ENABLED")
    if raw is None:
        raw = os.getenv("ROBINHOOD_PONS_TRADING_ENABLED", "false")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def get_buy_size_spcx() -> float:
    """Return the live per-launch SPCX spend."""
    if _buy_size_spcx is not None:
        return _buy_size_spcx

    raw = os.getenv("DOPPLER_BUY_SIZE_SPCX", "0")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def set_buy_size_spcx(value: float) -> None:
    """Set the live per-launch spend used by the Doppler executor."""
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Doppler buy size must be a finite number greater than zero")

    global _buy_size_spcx
    _buy_size_spcx = value
