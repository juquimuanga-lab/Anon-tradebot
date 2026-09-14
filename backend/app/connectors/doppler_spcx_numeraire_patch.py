"""Normalize Doppler launch numeraires from authoritative initializer state.

Some launch transactions can expose event metadata that is not sufficient to
reliably classify the live pool.  The Doppler initializer's getState(asset)
is authoritative for the launch numeraire, so the direct SPCX lane should use
that value before applying its SPCX filter.
"""
from __future__ import annotations

import asyncio
import logging

from web3 import Web3

from app.config.settings import settings
from app.execution.onchain.robinhood_wallet import resolve_robinhood_rpc_url

logger = logging.getLogger("app.connectors.doppler_spcx_numeraire_patch")

_INITIALIZED = False


def _w3() -> Web3:
    rpc = resolve_robinhood_rpc_url(settings)
    return Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))


async def _authoritative_numeraire(asset: str) -> str | None:
    from app.connectors.doppler import DOPPLER_INITIALIZER, INITIALIZER_ABI

    w3 = await asyncio.to_thread(_w3)
    initializer = w3.eth.contract(
        address=Web3.to_checksum_address(DOPPLER_INITIALIZER),
        abi=INITIALIZER_ABI,
    )
    state = await asyncio.to_thread(
        lambda: initializer.functions.getState(Web3.to_checksum_address(asset)).call()
    )
    return Web3.to_checksum_address(state[0])


async def _patched_poll(self, *args, **kwargs):
    discovered = await self._doppler_original_poll_new_launches(*args, **kwargs)
    if not discovered:
        return discovered

    normalized = []
    for item in discovered:
        asset = item.get("mint")
        event_numeraire = str(item.get("numeraire") or "")
        if not asset:
            normalized.append(item)
            continue

        try:
            state_numeraire = await _authoritative_numeraire(asset)
        except Exception as exc:
            logger.warning(
                "doppler_spcx_numeraire_state_read_failed",
                extra={"mint": asset, "event_numeraire": event_numeraire, "error": str(exc)},
            )
            normalized.append(item)
            continue

        if state_numeraire and state_numeraire.lower() != event_numeraire.lower():
            logger.warning(
                "doppler_event_numeraire_corrected_from_state",
                extra={
                    "mint": asset,
                    "event_numeraire": event_numeraire,
                    "state_numeraire": state_numeraire,
                    "tx_hash": item.get("tx_hash"),
                    "block_number": item.get("block_number"),
                },
            )
            item = dict(item)
            item["event_numeraire"] = event_numeraire
            item["numeraire"] = state_numeraire
            item["numeraire_source"] = "doppler_initializer_getState"
        else:
            item = dict(item)
            item["numeraire_source"] = "doppler_initializer_getState"

        normalized.append(item)

    logger.info(
        "doppler_spcx_numeraire_normalization_complete",
        extra={"discovered": len(normalized)},
    )
    return normalized


def install() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return

    from app.connectors import doppler

    if getattr(doppler.DopplerClient, "_spcx_numeraire_patch_installed", False):
        _INITIALIZED = True
        return

    original = doppler.DopplerClient.poll_new_launches
    doppler.DopplerClient._doppler_original_poll_new_launches = original
    doppler.DopplerClient.poll_new_launches = _patched_poll
    doppler.DopplerClient._spcx_numeraire_patch_installed = True
    _INITIALIZED = True
    logger.info("doppler_spcx_numeraire_patch_installed")
