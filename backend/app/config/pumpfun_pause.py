"""Deployment switch for pausing Pump.fun Solana discovery without changing strategy code."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("app.config.pumpfun_pause")


def pumpfun_scan_enabled() -> bool:
    """Return whether Pump.fun discovery is enabled.

    Defaults to True so existing deployments keep their current behavior.
    Set PUMPFUN_SCAN_ENABLED=false to pause Pump.fun discovery only.
    """
    return os.getenv("PUMPFUN_SCAN_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


def install_pumpfun_pause_hook() -> None:
    """Disable Pump.fun discovery at the watcher boundary when requested.

    This leaves the existing scanner/strategy code intact and prevents the
    Pump.fun Helius stream from being created because the scanner receives no
    Pump.fun discovery calls while the deployment switch is off.
    """
    if pumpfun_scan_enabled():
        logger.info("pumpfun_scan_enabled")
        return

    from app.scanners import onchain_watcher

    async def _paused_poll_new_pumpfun_mints(*args, **kwargs):
        return []

    onchain_watcher.poll_new_pumpfun_mints = _paused_poll_new_pumpfun_mints
    logger.warning(
        "pumpfun_scan_paused",
        extra={"reason": "PUMPFUN_SCAN_ENABLED=false"},
    )


install_pumpfun_pause_hook()
