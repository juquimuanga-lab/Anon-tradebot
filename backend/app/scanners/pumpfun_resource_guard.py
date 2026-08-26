"""Pump.fun resource guard.

Keeps the existing Pump.fun discovery/safety pipeline intact while removing
avoidable RPC work and duplicate launch pressure.

The websocket CreateEvent already contains the mint/creator. Therefore the
optional per-event getTransaction used only for launch-version verification
is disabled here; HTTP/RPC recovery still performs authoritative transaction
parsing when the stream is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

from app.scanners import onchain_watcher

logger = logging.getLogger("app.scanners.pumpfun_resource_guard")

_INSTALLED = False
_ORIGINAL_RPC_VERIFY = onchain_watcher._get_confirmed_transaction_with_fallback
_ORIGINAL_QUEUE = onchain_watcher.asyncio.Queue
_ORIGINAL_GET_OR_CREATE = onchain_watcher._get_or_create_pumpfun_stream


class _MintDedupQueue(_ORIGINAL_QUEUE):
    """Bounded Pump.fun queue that keeps only one pending event per mint."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pending_mints: OrderedDict[str, None] = OrderedDict()

    @staticmethod
    def _mint(item) -> str | None:
        if isinstance(item, dict):
            value = item.get("mint")
            return str(value) if value else None
        return None

    def put_nowait(self, item):
        mint = self._mint(item)
        if mint and mint in self._pending_mints:
            # Duplicate launch notification: don't consume queue capacity.
            self._pending_mints.move_to_end(mint)
            return

        if self.full():
            try:
                old = self.get_nowait()
                old_mint = self._mint(old)
                if old_mint:
                    self._pending_mints.pop(old_mint, None)
            except asyncio.QueueEmpty:
                pass

        super().put_nowait(item)
        if mint:
            self._pending_mints[mint] = None

    def get_nowait(self):
        item = super().get_nowait()
        mint = self._mint(item)
        if mint:
            self._pending_mints.pop(mint, None)
        return item


async def _resource_guard_rpc(rpc_url, signature, *, purpose):
    """Skip only the optional websocket launch-version RPC lookup.

    Smart-money verification, recovery polling and all other RPC purposes keep
    the original implementation unchanged.
    """
    if purpose == "pumpfun_launch_version":
        return None
    return await _ORIGINAL_RPC_VERIFY(rpc_url, signature, purpose=purpose)


def _get_or_create_with_guard(rpc_url, mint_authority):
    """Create the normal stream but give it a mint-deduplicating queue."""
    # The original function creates the queue synchronously and immediately
    # starts the worker. Temporarily replace the Queue constructor so that the
    # worker receives our bounded/deduplicating queue from the beginning.
    onchain_watcher.asyncio.Queue = _MintDedupQueue
    try:
        return _ORIGINAL_GET_OR_CREATE(rpc_url, mint_authority)
    finally:
        onchain_watcher.asyncio.Queue = _ORIGINAL_QUEUE


def install_pumpfun_resource_guard() -> None:
    """Install once during startup, before ScannerService creates streams."""
    global _INSTALLED
    if _INSTALLED:
        return

    onchain_watcher._get_confirmed_transaction_with_fallback = _resource_guard_rpc
    onchain_watcher._get_or_create_pumpfun_stream = _get_or_create_with_guard

    # A smaller bounded queue is intentional: stale launches should be dropped
    # rather than allowed to accumulate and increase decision latency.
    onchain_watcher.PUMPFUN_EVENT_QUEUE_MAXSIZE = 250

    _INSTALLED = True
    logger.info(
        "pumpfun_resource_guard_installed",
        extra={
            "queue_maxsize": 250,
            "launch_verification_rpc": "disabled_on_stream_event",
            "mint_deduplication": True,
        },
    )
