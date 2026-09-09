"""Connector package bootstrap."""

# Deployment-level Pump.fun pause. This is intentionally isolated from the
# strategy/scoring code so Solana discovery can be paused for diagnostics.
from app.config import pumpfun_pause as _pumpfun_pause  # noqa: F401

# Robinhood Chain Doppler/Long launch lane. This is deliberately installed
# independently of the Pons discovery/indexing path.
from app.connectors import doppler_direct_lane as _doppler_direct_lane  # noqa: F401
