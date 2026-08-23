"""Pump.fun compatibility patch for provider-specific transaction decoding.

The Pump.fun websocket receives an authoritative CreateEvent containing the
mint and creator. Some RPC providers can return a transaction shape that the
secondary instruction decoder cannot parse even though the CreateEvent is
valid. In that case the watcher used to discard the launch before the global
launch-safety gate could run.

This module installs a narrow fallback around extract_pumpfun_create:
- use the existing instruction decoder first;
- if it cannot decode the transaction, recover the CreateEvent from the
  transaction log messages already returned by getTransaction;
- infer create/create_v2 from instruction data when possible, otherwise use
  legacy create as the conservative compatibility label.

It does not bypass the launch-safety filter or admin ruleset.
"""

from __future__ import annotations

import logging

from app.scanners import onchain_watcher

logger = logging.getLogger("app.scanners.pumpfun_compat")


_INSTALLED = False
_ORIGINAL_EXTRACT = onchain_watcher.extract_pumpfun_create


def _get_logs(tx) -> list[str]:
    """Extract log messages from native or dict getTransaction responses."""
    try:
        if isinstance(tx, dict):
            transaction = tx.get("transaction") or {}
            meta = transaction.get("meta") or tx.get("meta") or {}
            logs = meta.get("logMessages") or meta.get("log_messages") or []
            return [str(item) for item in logs if item is not None]

        outer_transaction = getattr(tx, "transaction", None)
        meta = getattr(outer_transaction, "meta", None)
        if meta is None:
            meta = getattr(tx, "meta", None)
        logs = getattr(meta, "log_messages", None)
        if logs is None:
            logs = getattr(meta, "logMessages", None)
        return [str(item) for item in (logs or []) if item is not None]
    except Exception:
        return []


def _infer_instruction(tx) -> str:
    """Infer Pump.fun create version from instruction discriminator when possible."""
    try:
        if isinstance(tx, dict):
            instructions = (
                ((tx.get("transaction") or {}).get("message") or {}).get("instructions")
                or []
            )
        else:
            outer_transaction = getattr(tx, "transaction", None)
            message = getattr(outer_transaction, "message", None)
            if message is None:
                message = getattr(tx, "message", None)
            instructions = getattr(message, "instructions", None) or []

        for instruction in instructions:
            data = onchain_watcher._instruction_data_bytes(instruction)
            if data.startswith(onchain_watcher.PUMPFUN_CREATE_V2_DISCRIMINATOR):
                return "create_v2"
            if data.startswith(onchain_watcher.PUMPFUN_CREATE_DISCRIMINATOR):
                return "create"
    except Exception:
        pass

    return "create"


def _fallback_extract(tx):
    logs = _get_logs(tx)
    if not logs:
        return None

    event = onchain_watcher._extract_pumpfun_event_from_logs(logs)
    if not event:
        return None

    event["instruction"] = _infer_instruction(tx)
    event["source"] = "pumpfun"
    event["discovery"] = "websocket_create_event_rpc_fallback"
    return event


def _extract_with_fallback(tx):
    decoded = _ORIGINAL_EXTRACT(tx)
    if decoded:
        return decoded

    fallback = _fallback_extract(tx)
    if fallback:
        logger.info(
            "pumpfun_launch_version_verification_fallback",
            extra={
                "mint": fallback.get("mint"),
                "instruction": fallback.get("instruction"),
            },
        )
    return fallback


def install_pumpfun_compat() -> None:
    """Install the fallback exactly once during application startup."""
    global _INSTALLED
    if _INSTALLED:
        return

    onchain_watcher.extract_pumpfun_create = _extract_with_fallback
    _INSTALLED = True
    logger.info("pumpfun_compat_fallback_installed")
