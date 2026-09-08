"""Connector package bootstrap."""

# Keep the existing Pons scanner path intact while extending its Robinhood
# launch lane with Doppler/Long-style launches.
from app.connectors import doppler_bootstrap as _doppler_bootstrap  # noqa: F401

# Hard-filter Doppler discovery to launches pooled against canonical SPCX.
from app.connectors import doppler_spcx_filter as _doppler_spcx_filter  # noqa: F401
