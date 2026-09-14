"""Authoritative SPCX admission patch for the direct Doppler launch lane.

Airlock's Create event contains a numeraire field, but the Doppler initializer
state is the authoritative source for the actual pool configuration. This
patch normalizes a launch from initializer.getState(asset) before the existing
direct-lane admission filter runs, without changing the generic sniper path.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import threading

from web3 import Web3

logger = logging.getLogger("app.connectors.doppler_spcx_admission_patch")
_RETRY_DELAYS = (0.0, 0.25, 0.75)


def _state_for_asset(w3, initializer, mint: str):
    return initializer.functions.getState(Web3.to_checksum_address(mint)).call()


async def _normalize_launch(item: dict, doppler, doppler_control) -> bool:
    mint = item.get("mint")
    if not mint:
        return False

    event_numeraire = str(item.get("numeraire", ""))
    event_numeraire_l = event_numeraire.lower()
    spcx = doppler_control.SPCX_TOKEN.lower()

    # Fast path: event already agrees with the canonical quote. We still do
    # the initializer read so poolKey ordering is authoritative before the
    # launch reaches the execution/snapshot path.
    last_error = None
    for delay in _RETRY_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        try:
            w3 = await asyncio.to_thread(doppler._w3)
            initializer = w3.eth.contract(
                address=Web3.to_checksum_address(doppler.DOPPLER_INITIALIZER),
                abi=doppler.INITIALIZER_ABI,
            )
            state = await asyncio.to_thread(_state_for_asset, w3, initializer, mint)
            numeraire, tokens_on_curve, hook, _calldata, status, key, far_tick = state
            key = tuple(key)
            currency0 = str(key[0])
            currency1 = str(key[1])

            state_spcx = str(numeraire).lower() == spcx
            ordered_asset = currency1.lower() == str(Web3.to_checksum_address(mint)).lower()
            ordered_spcx = currency0.lower() == spcx
            accepted = state_spcx and ordered_spcx and ordered_asset

            logger.info(
                "doppler_launch_spcx_state_checked",
                extra={
                    "mint": mint,
                    "event_numeraire": event_numeraire,
                    "state_numeraire": str(numeraire),
                    "currency0": currency0,
                    "currency1": currency1,
                    "status": int(status),
                    "tokens_on_curve": int(tokens_on_curve),
                    "far_tick": int(far_tick),
                    "state_spcx": state_spcx,
                    "ordered_spcx": ordered_spcx,
                    "ordered_asset": ordered_asset,
                    "accepted": accepted,
                },
            )

            if not accepted:
                return False

            # Normalize metadata so downstream snapshot/execution sees the
            # same authoritative pool configuration that admission validated.
            item["numeraire"] = Web3.to_checksum_address(numeraire)
            item["pool_key"] = {
                "currency0": Web3.to_checksum_address(key[0]),
                "currency1": Web3.to_checksum_address(key[1]),
                "fee": int(key[2]),
                "tickSpacing": int(key[3]),
                "hooks": Web3.to_checksum_address(key[4]),
            }
            item["doppler_state"] = {
                "status": int(status),
                "tokens_on_curve": int(tokens_on_curve),
                "hook": Web3.to_checksum_address(hook),
                "far_tick": int(far_tick),
            }
            return True
        except Exception as exc:
            last_error = exc

    logger.warning(
        "doppler_launch_spcx_state_lookup_failed",
        extra={
            "mint": mint,
            "event_numeraire": event_numeraire,
            "required_numeraire": doppler_control.SPCX_TOKEN,
            "error": str(last_error),
        },
    )
    return False


def install() -> bool:
    try:
        doppler = importlib.import_module("app.connectors.doppler")
        doppler_control = importlib.import_module("app.connectors.doppler_control")
        direct = importlib.import_module("app.connectors.doppler_direct_lane")
        if getattr(direct, "_doppler_spcx_admission_patch_installed", False):
            return True

        original = direct._watch_doppler_for_new_mints

        async def patched_watch(scanner):
            try:
                discovered = await doppler.doppler_client.poll_new_launches()
            except Exception:
                # Let the original watcher own polling failures/logging. This
                # path is only used when we have launches to normalize.
                return await original(scanner)

            if not discovered:
                return await original(scanner)

            normalized = []
            for item in discovered:
                if await _normalize_launch(item, doppler, doppler_control):
                    normalized.append(item)

            # The original watcher polls internally, so temporarily replace
            # the client method with a one-shot result to avoid a second RPC
            # poll and, importantly, preserve all existing admission gates.
            original_poll = doppler.doppler_client.poll_new_launches

            async def _one_shot_poll(*args, **kwargs):
                return normalized

            doppler.doppler_client.poll_new_launches = _one_shot_poll
            try:
                return await original(scanner)
            finally:
                doppler.doppler_client.poll_new_launches = original_poll

        direct._watch_doppler_for_new_mints = patched_watch
        direct._doppler_spcx_admission_patch_installed = True
        logger.info(
            "doppler_spcx_admission_patch_installed",
            extra={"required_numeraire": doppler_control.SPCX_TOKEN, "authoritative_source": "initializer.getState"},
        )
        return True
    except Exception:
        logger.exception("doppler_spcx_admission_patch_install_failed")
        return False


def _bootstrap_later() -> None:
    if install():
        return
    timer = threading.Timer(0.5, _bootstrap_later)
    timer.daemon = True
    timer.start()


_timer = threading.Timer(0.5, _bootstrap_later)
_timer.daemon = True
_timer.start()
