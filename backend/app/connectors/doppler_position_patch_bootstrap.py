"""Reliable bootstrap for the Doppler EVM position reconciliation patch."""
from __future__ import annotations
import importlib
import logging
import threading
logger = logging.getLogger(__name__)
_INSTALLED = False

def _try_install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        module = importlib.import_module("app.connectors.doppler_position_patch")
        installer = getattr(module, "install", None)
        if installer is None:
            logger.error("doppler_position_patch_missing_installer")
            return False
        _INSTALLED = bool(installer())
        if _INSTALLED:
            logger.info("doppler_position_patch_bootstrap_complete")
        return _INSTALLED
    except Exception:
        logger.exception("doppler_position_patch_bootstrap_failed")
        return False

def _retry() -> None:
    if not _try_install():
        timer = threading.Timer(1.0, _retry)
        timer.daemon = True
        timer.start()

def install() -> None:
    if not _INSTALLED:
        timer = threading.Timer(0.5, _retry)
        timer.daemon = True
        timer.start()

install()
