"""Connector package bootstrap."""

# Deployment-level Pump.fun pause. This is intentionally isolated from the
# strategy/scoring code so Solana discovery can be paused for diagnostics.
from app.config import pumpfun_pause as _pumpfun_pause  # noqa: F401

# Doppler is initialized by its runtime-safe lane bootstrap rather than from
# package import time. This avoids circular-import timing with ScannerService.
