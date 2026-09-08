"""Connector package bootstrap."""

# Keep the existing Pons scanner path intact while extending its Robinhood
# launch lane with Doppler/Long-style launches.
from app.connectors import doppler_bootstrap as _doppler_bootstrap  # noqa: F401
