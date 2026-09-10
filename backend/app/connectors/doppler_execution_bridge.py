"""Independent execution bridge for the Robinhood Doppler/SPCX sniper.

Doppler has its own runtime switch and SPCX spend size, so it must not depend
on a generic Anoncoin/Pons rule being active before a qualifying launch can
reach the live execution adapter. This bridge consumes the already-qualified
Doppler pending item immediately after discovery and records the order/position
with a nullable rule_id.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone

from app.connectors import doppler_control
from app.connectors import doppler_direct_lane
from app.guardian import guardian
from app.storage import repository as repo

logger = logging.getLogger("app.connectors.doppler_execution_bridge")
SOURCE_DOPPLER = "doppler"
_RETRY_SECONDS = 2.0


def _resolve_owner(active_rules=None) -> int | None:
    raw = os.getenv("DOPPLER_OWNER_USER_ID", "").strip()
    if raw.isdigit():
        return int(raw)
    for rule in active_rules or []:
        owner = getattr(rule, "created_by", None)
        if owner is not None:
            return int(owner)
    return None


async def _owner_id(active_rules=None) -> int | None:
    owner = _resolve_owner(active_rules)
    if owner is not None:
        return owner
    try:
        state = await repo.get_or_create_bot_state()
        owner = getattr(state, "owner_user_id", None)
        if owner is not None:
            return int(owner)
    except Exception:
        logger.exception("doppler_owner_state_lookup_failed")
    return None


async def _execute_one(scanner, mint: str, watch: dict, owner_user_id: int) -> bool:
    metadata = watch.get("metadata") or {}
    first_seen = watch.get("first_seen") or datetime.now(timezone.utc)

    if await repo.has_open_or_pending_position(mint, owner_user_id):
        logger.info("doppler_buy_skipped_existing_position", extra={"mint": mint, "owner_user_id": owner_user_id})
        return True

    token = await doppler_direct_lane._snapshot_doppler(mint, metadata, first_seen)
    if token is None:
        logger.warning("doppler_buy_blocked_snapshot_unavailable", extra={"mint": mint})
        return False

    # The live adapter historically consumed the shared Pons-shaped enrichment
    # key. Keep this compatibility alias while the execution path remains
    # Doppler-specific.
    market = (getattr(token, "raw_enrichment", {}) or {}).get("doppler") or {}
    token.raw_enrichment["pons"] = market

    state = await repo.get_or_create_bot_state(owner_user_id)
    if not getattr(state, "trading_enabled", False):
        logger.warning("doppler_buy_blocked_trading_disabled", extra={"mint": mint, "owner_user_id": owner_user_id})
        return False

    amount_spcx = doppler_control.get_buy_size_spcx()
    if amount_spcx <= 0:
        logger.warning("doppler_buy_blocked_invalid_size", extra={"mint": mint, "owner_user_id": owner_user_id})
        return False

    adapter = await scanner._execution_router.get_adapter(
        state.mode,
        owner_user_id,
        source=SOURCE_DOPPLER,
    )

    order = await repo.create_order(
        mint,
        "buy",
        state.mode,
        "pending",
        amount_spcx,
        float(token.price_usd or 0.0),
        rule_id=None,
        owner_user_id=owner_user_id,
    )

    logger.info(
        "doppler_buy_attempt",
        extra={
            "mint": mint,
            "owner_user_id": owner_user_id,
            "order_id": order.id,
            "amount_spcx": amount_spcx,
            "mode": state.mode,
            "numeraire": metadata.get("numeraire"),
            "tx_hash": metadata.get("tx_hash"),
        },
    )
    await guardian.record("buy_attempt", owner_id=owner_user_id, mint=mint, source=SOURCE_DOPPLER)

    try:
        result = await asyncio.wait_for(
            adapter.buy(token, amount_spcx),
            timeout=60.0,
        )
    except Exception as exc:
        logger.exception("doppler_buy_execution_exception", extra={"mint": mint, "error": str(exc)})
        await repo.update_order(order.id, "failed", error_message=str(exc))
        await repo.save_trade_decision(mint, None, "buy_failed", str(exc), 0.0)
        return False

    if not result.success:
        error = result.error_message or "unknown Doppler execution failure"
        await repo.update_order(order.id, "failed", error_message=error)
        await repo.save_trade_decision(mint, None, "buy_failed", error, 0.0)
        logger.warning("doppler_buy_failed", extra={"mint": mint, "order_id": order.id, "error": error})
        await guardian.record("buy_failed", owner_id=owner_user_id, mint=mint, error=error)
        return False

    await repo.update_order(order.id, "filled", tx_signature=result.tx_signature)

    price_quote = float(market.get("price_quote", 0.0) or 0.0)
    amount_tokens = amount_spcx / price_quote if price_quote > 0 else 0.0
    quote_usd = float(market.get("quote_usd", 0.0) or 0.0)
    entry_cost_usd = amount_spcx * quote_usd if quote_usd > 0 else 0.0
    fill_price = float(result.price_usd or token.price_usd or 0.0)

    await repo.create_position(
        mint,
        None,
        state.mode,
        fill_price,
        amount_tokens,
        amount_spcx,
        owner_user_id=owner_user_id,
        entry_volume_24h_usd=token.volume_24h_usd,
        source=SOURCE_DOPPLER,
        entry_cost_usd=entry_cost_usd,
        entry_fee_usd=0.0,
    )
    await repo.save_trade_decision(mint, None, "buy", "qualified Doppler SPCX launch", 0.0)
    logger.info(
        "doppler_buy_filled",
        extra={
            "mint": mint,
            "order_id": order.id,
            "owner_user_id": owner_user_id,
            "amount_spcx": amount_spcx,
            "amount_tokens": amount_tokens,
            "price_usd": fill_price,
            "tx_signature": result.tx_signature,
        },
    )
    await guardian.record("buy_success", owner_id=owner_user_id, mint=mint, tx_signature=result.tx_signature)
    return True


async def _wrapped_watch(scanner, original_watch):
    await original_watch(scanner)
    doppler_items = [
        (mint, dict(watch))
        for mint, watch in scanner._pending_watch.items()
        if watch.get("source") == SOURCE_DOPPLER
    ]
    if not doppler_items:
        return

    try:
        active_rules = await repo.get_all_active_rules()
    except Exception:
        active_rules = []
        logger.exception("doppler_active_rules_lookup_failed")

    owner = await _owner_id(active_rules)
    logger.info(
        "doppler_execution_bridge_batch",
        extra={
            "pending_count": len(doppler_items),
            "owner_user_id": owner,
            "active_rule_count": len(active_rules),
            "dedicated_runtime_enabled": doppler_control.is_enabled(),
        },
    )
    if owner is None:
        logger.warning(
            "doppler_buy_blocked_no_owner",
            extra={"reason": "Set DOPPLER_OWNER_USER_ID or create/activate an admin rule so the wallet owner can be resolved."},
        )
        return

    for mint, watch in doppler_items:
        try:
            await _execute_one(scanner, mint, watch, owner)
        finally:
            # The dedicated bridge owns this source. Do not allow the generic
            # rule dispatcher to delete/reprocess it as an unmatched source.
            scanner._pending_watch.pop(mint, None)


def _install() -> bool:
    try:
        from app.scanners.scanner import ScannerService
        if getattr(ScannerService, "_doppler_execution_bridge_installed", False):
            return True
        if not getattr(ScannerService, "_doppler_direct_lane_installed", False):
            return False
        if getattr(doppler_direct_lane, "_doppler_execution_bridge_wrapped", False):
            ScannerService._doppler_execution_bridge_installed = True
            return True

        original_watch = doppler_direct_lane._watch_doppler_for_new_mints

        async def _watch(scanner):
            await _wrapped_watch(scanner, original_watch)

        doppler_direct_lane._watch_doppler_for_new_mints = _watch
        doppler_direct_lane._doppler_execution_bridge_wrapped = True
        ScannerService._doppler_execution_bridge_installed = True
        logger.info("doppler_execution_bridge_installed")
        return True
    except Exception:
        logger.exception("doppler_execution_bridge_install_failed")
        return False


def _retry_install() -> None:
    if _install():
        logger.info("doppler_execution_bridge_bootstrap_complete")
        return
    logger.info("doppler_execution_bridge_bootstrap_scheduled", extra={"retry_seconds": _RETRY_SECONDS})
    threading.Timer(_RETRY_SECONDS, _retry_install).start()


_retry_install()
