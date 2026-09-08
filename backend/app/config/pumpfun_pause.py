"""Deployment switch for pausing Pump.fun Solana discovery without changing strategy code."""
from __future__ import annotations

import os


def pumpfun_scan_enabled() -> bool:
    """Return whether Pump.fun discovery is enabled.

    Defaults to True so existing deployments keep their current behavior.
    Set PUMPFUN_SCAN_ENABLED=false to pause Pump.fun discovery only.
    """
    return os.getenv("PUMPFUN_SCAN_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }
