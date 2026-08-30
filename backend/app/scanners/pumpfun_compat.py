"""Pump.fun compatibility patch for provider-specific transaction decoding.

The Pump.fun websocket receives a CreateEvent from the official Pump.fun program containing the
mint and creator. Some RPC providers can return a transaction shape that the
secondary instruction decoder cannot parse even though the CreateEvent is
valid. In that case the watcher used to discard the launch before the global
launch-safety gate could run.

This module installs narrow compatibility wrappers around the existing
watcher/execution functions. It deliberately avoids replacing the large
Pump.fun scanner modules so their existing safety, Graduation Hunter,
Smart Money, and trading behavior remain untouched.
"""

from __future__ import annotations

import asyncio
import logging

from app.scanners import onchain_watcher

logger = logging.getLogger("app.scanners.pumpfun_compat")


_INSTALLED = False
_ORIGINAL_EXTRACT = onchain_watcher.extract_pumpfun_create
_ORIGINAL_TX_FETCH = onchain_watcher._get_confirmed_transaction_with_fallback


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

    return None


def _fallback_extract(tx):
    logs = _get_logs(tx)
    if not logs:
        return None

    event = onchain_watcher._extract_pumpfun_event_from_logs(logs)
    if not event:
        return None

    instruction = _infer_instruction(tx)
    if instruction not in {"create", "create_v2"}:
        return None
    event["instruction"] = instruction
    event["source"] = "pumpfun"
    event["discovery"] = "websocket_create_event_rpc_fallback"
    return event


def _extract_with_fallback(tx):
    """Accept only transaction-proven Pump.fun create/create_v2 launches.

    CreateEvent/log-only recovery is retained for diagnostics, but it is never
    promoted into a tradable launch without a verified Pump.fun instruction.
    """
    decoded = _ORIGINAL_EXTRACT(tx)
    if decoded:
        return decoded

    fallback = _fallback_extract(tx)
    if fallback:
        logger.warning(
            "pumpfun_launch_rejected_event_only_unverified",
            extra={
                "mint": fallback.get("mint"),
                "instruction": fallback.get("instruction"),
                "reason": "no_verified_pumpfun_create_instruction",
            },
        )
    return None


async def _tx_fetch_without_launch_version_probe(rpc_url: str, signature: str, *, purpose: str):
    """Skip the redundant launch-version getTransaction hot-path.

    The Pump.fun CreateEvent already carries the mint/creator and is the
authoritative discovery signal used by the websocket worker. Launch
safety still performs its own transaction retrieval later. Keeping those
two responsibilities separate avoids one extra Helius getTransaction per
launch while preserving all safety checks.
    """
    if purpose == "pumpfun_launch_version":
        return None
    return await _ORIGINAL_TX_FETCH(rpc_url, signature, purpose=purpose)


class _SuppressRedundantLaunchVersionLog(logging.Filter):
    """Hide the old informational fallback message after the probe is removed."""

    _TARGET = "pumpfun_launch_version_verification_unavailable_using_event"

    def filter(self, record: logging.LogRecord) -> bool:
        return self._TARGET not in record.getMessage()


async def _get_launch_transactions_with_refresh(original, rpc_url: str, curve_address: str, *, limit: int):
    """Retry launch transaction discovery when signatures race transaction indexing."""
    first = await original(rpc_url, curve_address, limit=limit)
    if first:
        return first
    for delay in (1.5, 3.0):
        await asyncio.sleep(delay)
        refreshed = await original(rpc_url, curve_address, limit=limit)
        if refreshed:
            logger.info(
                "pumpfun_launch_safety_transaction_refresh_recovered",
                extra={
                    "curve": curve_address,
                    "delay_seconds": delay,
                    "transactions": len(refreshed),
                },
            )
            return refreshed
    logger.warning(
        "pumpfun_launch_safety_transaction_refresh_exhausted",
        extra={"curve": curve_address, "attempts": 3},
    )
    return first


def _patch_pumpfun_transaction_recovery() -> None:
    """Patch only the existing launch-transaction helper after import."""
    try:
        from app.execution.onchain import pumpfun
    except Exception:
        logger.debug("pumpfun_transaction_recovery_patch_deferred", exc_info=True)
        return

    original = getattr(pumpfun, "_get_launch_transactions", None)
    if original is None or getattr(original, "_anon_refresh_patch", False):
        return

    async def wrapped(rpc_url: str, curve_address: str, *, limit: int):
        return await _get_launch_transactions_with_refresh(
            original,
            rpc_url,
            curve_address,
            limit=limit,
        )

    wrapped._anon_refresh_patch = True
    pumpfun._get_launch_transactions = wrapped
    logger.info("pumpfun_launch_transaction_refresh_patch_installed")


def _patch_graduation_hunter_fresh_curve_read() -> None:
    """Refresh Pump.fun curve state at the Graduation Hunter boundary.

    The normal scanner keeps a 1.25s cache to protect Helius during launch
    bursts. That is useful for discovery, but the Hunter's real-SOL floor is a
    trading gate, so it must not evaluate an old cached reserve value. This
    wrapper refreshes only Smart Pump.fun candidates that have reached the
    configured Hunter observation age, then updates the existing TokenSnapshot
    in-place before the original screening method continues.
    """
    try:
        from app.scanners.scanner import ScannerService, SOURCE_PUMPFUN
        from app.execution.onchain import pumpfun
        from app.config.settings import settings
    except Exception:
        logger.debug("pumpfun_graduation_fresh_read_patch_deferred", exc_info=True)
        return

    original = getattr(ScannerService, "_screen_and_maybe_trade", None)
    if original is None or getattr(original, "_fresh_curve_patch", False):
        return

    async def wrapped(self, token, rule, notify_on_fail):
        try:
            strategy = getattr(rule, "strategy", "smart") or "smart"
            enabled = getattr(rule, "graduation_hunter_enabled", True)
            age = float(getattr(token, "age_seconds", 0.0) or 0.0)
            min_age = float(
                getattr(
                    rule,
                    "graduation_hunter_min_observation_seconds",
                    20.0,
                )
                or 20.0
            )
            if (
                getattr(token, "source", "") == SOURCE_PUMPFUN
                and strategy == "smart"
                and enabled
                and age >= min_age
            ):
                cache = getattr(pumpfun, "_pumpfun_pool_cache", None)
                if isinstance(cache, dict):
                    cache.pop(token.mint, None)
                info = await pumpfun.get_pool_info(
                    token.mint,
                    settings.solana_rpc_url,
                    commitment="processed",
                )
                token.price_usd = float(info.get("price_usd", token.price_usd) or token.price_usd)
                token.market_cap_usd = float(info.get("market_cap_usd", token.market_cap_usd) or token.market_cap_usd)
                token.liquidity_usd = float(info.get("liquidity_usd", token.liquidity_usd) or token.liquidity_usd)
                token.real_sol_reserves_sol = float(
                    info.get("real_sol_reserves", token.real_sol_reserves_sol) or 0.0
                )
                token.real_sol_progress_pct = min(
                    100.0,
                    max(
                        0.0,
                        token.real_sol_reserves_sol / 85.0 * 100.0,
                    ),
                )
                logger.info(
                    "pumpfun_graduation_hunter_fresh_curve_read",
                    extra={
                        "mint": token.mint,
                        "age_seconds": age,
                        "real_sol_reserves": token.real_sol_reserves_sol,
                        "commitment": "processed",
                        "cache_bypassed": True,
                    },
                )
        except Exception as exc:
            logger.warning(
                "pumpfun_graduation_hunter_fresh_curve_read_failed",
                extra={"mint": getattr(token, "mint", ""), "error": str(exc)},
            )
        return await original(self, token, rule, notify_on_fail)

    wrapped._fresh_curve_patch = True
    ScannerService._screen_and_maybe_trade = wrapped
    logger.info("pumpfun_graduation_hunter_fresh_read_patch_installed")


def install_pumpfun_compat() -> None:
    """Install the narrow compatibility/recovery patches exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return

    onchain_watcher.extract_pumpfun_create = _extract_with_fallback
    onchain_watcher._get_confirmed_transaction_with_fallback = (
        _tx_fetch_without_launch_version_probe
    )
    _patch_pumpfun_transaction_recovery()
    _patch_graduation_hunter_fresh_curve_read()
    onchain_watcher.logger.addFilter(_SuppressRedundantLaunchVersionLog())
    _INSTALLED = True
    logger.info("pumpfun_compat_fallback_installed")
