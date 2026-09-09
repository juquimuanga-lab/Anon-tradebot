"""LongLauncher discovery for the Robinhood Doppler lane.

The canonical Doppler Airlock emits Create events, but most Robinhood
Doppler/Long launches are routed through LongLauncher first. This module
adds the LongLauncher LaunchCreated event as a second discovery path without
coupling the trading lane to Pons.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from web3 import Web3

from app.config.settings import settings
from app.connectors.doppler import (
    DOPPLER_INITIALIZER,
    LONG_LAUNCHER,
    DopplerClient,
    _w3,
)

logger = logging.getLogger("app.connectors.doppler_long_discovery")

# Verified LongLauncher event shape. The first three fields are indexed;
# deployedAt/reservedUntil are uint48 values on the canonical contract.
LAUNCH_CREATED_ABI = [{
    "anonymous": False,
    "inputs": [
        {"indexed": True, "name": "poolOrHook", "type": "address"},
        {"indexed": True, "name": "asset", "type": "address"},
        {"indexed": True, "name": "numeraire", "type": "address"},
        {"indexed": False, "name": "poolInitializer", "type": "address"},
        {"indexed": False, "name": "launcher", "type": "address"},
        {"indexed": False, "name": "tickerKey", "type": "bytes32"},
        {"indexed": False, "name": "deployedAt", "type": "uint48"},
        {"indexed": False, "name": "reservedUntil", "type": "uint48"},
        {"indexed": False, "name": "normalizedTicker", "type": "string"},
    ],
    "name": "LaunchCreated",
    "type": "event",
}]


def _window_end_start(previous_watermark: int, max_blocks: int, latest: int) -> tuple[int, int]:
    configured = int(getattr(settings, "doppler_factory_start_block", 0) or 0)
    start = previous_watermark or configured or max(0, latest - 10)
    if latest - start > max_blocks:
        start = latest - max_blocks
    return start, latest


def _normalise_address(value: Any) -> str:
    return Web3.to_checksum_address(value)


def _launch_from_log(log: Any, tx: Any) -> dict[str, Any]:
    args = log["args"]
    tx_hash = log["transactionHash"].hex()
    return {
        "mint": _normalise_address(args["asset"]),
        "creator": tx.get("from") or "",
        "numeraire": _normalise_address(args["numeraire"]),
        "initializer": _normalise_address(args["poolInitializer"]),
        "pool_or_hook": _normalise_address(args["poolOrHook"]),
        # Long's event records the integrator/front-end attribution here.
        "launcher": _normalise_address(args["launcher"]),
        "long_launcher": LONG_LAUNCHER,
        "ticker_key": "0x" + bytes(args["tickerKey"]).hex(),
        "normalized_ticker": str(args["normalizedTicker"]),
        "deployed_at": int(args["deployedAt"]),
        "reserved_until": int(args["reservedUntil"]),
        "tx_hash": tx_hash,
        "block_number": int(log["blockNumber"]),
        "launch_block": int(log["blockNumber"]),
        "log_index": int(log["logIndex"]),
        "created_on": datetime.now(timezone.utc),
        "source": "doppler",
        "source_event": "long_launch_created",
        "venue": "doppler_v4",
    }


async def _poll_long_launches(
    client: DopplerClient,
    w3: Web3,
    start: int,
    latest: int,
) -> list[dict[str, Any]]:
    long_contract = w3.eth.contract(
        address=Web3.to_checksum_address(LONG_LAUNCHER),
        abi=LAUNCH_CREATED_ABI,
    )
    event = long_contract.events.LaunchCreated()
    logs = await client._logs(event, start, latest)

    logger.info(
        "doppler_longlauncher_poll",
        extra={
            "latest_block": latest,
            "from_block": start,
            "to_block": latest,
            "launch_created_logs": len(logs),
            "long_launcher": LONG_LAUNCHER,
        },
    )

    result: list[dict[str, Any]] = []
    rejected = 0
    for log in logs:
        args = log["args"]
        initializer = _normalise_address(args["poolInitializer"])
        asset = _normalise_address(args["asset"])
        numeraire = _normalise_address(args["numeraire"])
        tx_hash = log["transactionHash"].hex()

        logger.info(
            "doppler_longlauncher_event_seen",
            extra={
                "asset": asset,
                "numeraire": numeraire,
                "initializer": initializer,
                "pool_or_hook": _normalise_address(args["poolOrHook"]),
                "launcher": _normalise_address(args["launcher"]),
                "tx_hash": tx_hash,
                "block_number": int(log["blockNumber"]),
                "log_index": int(log["logIndex"]),
                "normalized_ticker": str(args["normalizedTicker"]),
            },
        )

        if initializer.lower() != DOPPLER_INITIALIZER.lower():
            rejected += 1
            logger.info(
                "doppler_longlauncher_event_rejected_initializer",
                extra={
                    "asset": asset,
                    "numeraire": numeraire,
                    "initializer": initializer,
                    "required_initializer": DOPPLER_INITIALIZER,
                    "tx_hash": tx_hash,
                },
            )
            continue

        try:
            tx = await asyncio.to_thread(lambda h=tx_hash: w3.eth.get_transaction(h))
        except Exception as exc:
            tx = {"from": ""}
            logger.warning(
                "doppler_longlauncher_transaction_lookup_failed",
                extra={"tx_hash": tx_hash, "error": str(exc)},
            )

        launch = _launch_from_log(log, tx)
        result.append(launch)
        logger.info("doppler_longlauncher_event_accepted", extra=launch)

    logger.info(
        "doppler_longlauncher_launches_decoded",
        extra={
            "launch_created_logs": len(logs),
            "doppler_launches": len(result),
            "initializer_rejected": rejected,
        },
    )
    return result


def install_long_discovery() -> None:
    """Patch DopplerClient polling once, preserving the existing Airlock path."""
    if getattr(DopplerClient, "_long_discovery_installed", False):
        return

    original = DopplerClient.poll_new_launches

    async def _poll_with_long(self: DopplerClient, max_blocks: int = 300):
        previous_watermark = int(getattr(self, "_watermark", 0) or 0)
        airlock_launches = await original(self, max_blocks=max_blocks)

        try:
            w3 = await asyncio.to_thread(_w3)
            latest = int(await asyncio.to_thread(lambda: w3.eth.block_number))
            start, end = _window_end_start(previous_watermark, max_blocks, latest)
            if start > end:
                return airlock_launches

            long_launches = await _poll_long_launches(self, w3, start, end)
        except Exception as exc:
            logger.exception(
                "doppler_longlauncher_poll_failed",
                extra={"error": str(exc)},
            )
            return airlock_launches

        # Airlock Create is still authoritative for duplicate launches. Keep
        # one result per transaction+asset so the new path cannot double-snipe.
        seen = {
            (str(item.get("tx_hash", "")).lower(), str(item.get("mint", "")).lower())
            for item in airlock_launches
        }
        merged = list(airlock_launches)
        duplicates = 0
        for launch in long_launches:
            key = (str(launch.get("tx_hash", "")).lower(), str(launch.get("mint", "")).lower())
            if key in seen:
                duplicates += 1
                logger.info(
                    "doppler_longlauncher_duplicate_suppressed",
                    extra={"tx_hash": launch.get("tx_hash"), "mint": launch.get("mint")},
                )
                continue
            seen.add(key)
            merged.append(launch)

        logger.info(
            "doppler_dual_discovery_batch",
            extra={
                "airlock_launches": len(airlock_launches),
                "longlauncher_launches": len(long_launches),
                "duplicates_suppressed": duplicates,
                "merged_launches": len(merged),
            },
        )
        return merged

    DopplerClient.poll_new_launches = _poll_with_long
    DopplerClient._long_discovery_installed = True
    logger.info(
        "doppler_long_discovery_installed",
        extra={
            "airlock": "0xeb7c034704ef8dcd2d32324c1545f62fb4ad0862",
            "long_launcher": LONG_LAUNCHER,
            "initializer": DOPPLER_INITIALIZER,
        },
    )


install_long_discovery()
