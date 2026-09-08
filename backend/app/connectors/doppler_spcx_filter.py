"""Hard filter for the Robinhood Doppler sniper: SPCX quote only."""
from __future__ import annotations

import logging

from app.connectors.pons import pons_client

logger = logging.getLogger("app.connectors.doppler_spcx_filter")

# Canonical Robinhood Chain SPCX stock-token contract.
# Match the contract address, never the ticker string, to reject SPCX lookalikes.
SPCX_TOKEN = "0x4a0e65a3eccec6dbe60ae065f2e7bb85fae35eea"

_previous_poll = pons_client.poll_new_launches


async def _spcx_only_poll(*args, **kwargs):
    launches = await _previous_poll(*args, **kwargs)
    filtered: list[dict] = []
    for launch in launches:
        if launch.get("source") != "doppler":
            filtered.append(launch)
            continue
        numeraire = str(launch.get("numeraire", "")).lower()
        if numeraire == SPCX_TOKEN.lower():
            filtered.append(launch)
        else:
            logger.debug(
                "doppler_launch_rejected_non_spcx",
                extra={"mint": launch.get("mint"), "numeraire": numeraire, "required_numeraire": SPCX_TOKEN},
            )
    return filtered


pons_client.poll_new_launches = _spcx_only_poll
logger.info("doppler_spcx_only_filter_installed", extra={"required_numeraire": SPCX_TOKEN})
