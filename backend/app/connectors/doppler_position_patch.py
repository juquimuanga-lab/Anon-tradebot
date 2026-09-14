"""Robinhood Doppler position reconciliation compatibility patch.

Doppler positions are EVM positions on Robinhood Chain. The legacy position
manager's default reconciliation path is Solana/SPL-oriented, so a Doppler
position could be looked up as a Solana token and produce misleading
position-token-not-found warnings. Keep the generic position manager intact
and intercept only source='doppler' with the existing Robinhood EVM balance
helper.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.config.settings import settings
from app.positions.manager import PositionManager, WALLET_RECONCILIATION_TOLERANCE_PCT
from app.storage import repository as repo

logger = logging.getLogger("app.connectors.doppler_position_patch")


def _install() -> bool:
    if getattr(PositionManager, "_doppler_position_patch_installed", False):
        return True

    original = PositionManager._reconcile_live_position

    async def _reconcile_live_position(self, position):
        if getattr(position, "source", None) != "doppler":
            return await original(self, position)

        if getattr(position, "mode", None) != "live":
            return False

        if float(getattr(position, "amount_tokens", 0.0) or 0.0) <= 0:
            return False

        wallet_pubkey = await self._get_wallet_pubkey(position)
        if not wallet_pubkey:
            return False

        rpc_url = (
            getattr(settings, "robinhood_rpc_url", None)
            or getattr(settings, "robinhood_rpc_override_url", None)
        )
        if not rpc_url:
            logger.warning(
                "doppler_position_reconciliation_rpc_unavailable",
                extra={"position_id": position.id, "mint": position.mint},
            )
            return False

        try:
            from app.execution.pons_live import get_robinhood_token_balance
            actual_balance = await get_robinhood_token_balance(
                rpc_url,
                wallet_pubkey,
                position.mint,
            )
        except Exception as exc:
            logger.warning(
                "doppler_position_reconciliation_failed",
                extra={
                    "position_id": position.id,
                    "mint": position.mint,
                    "wallet": wallet_pubkey,
                    "error": str(exc),
                },
            )
            return False

        original_amount = max(
            0.0,
            float(getattr(position, "amount_tokens", 0.0) or 0.0),
        )
        tracked_remaining_pct = max(
            0.0,
            float(getattr(position, "remaining_pct", 0.0) or 0.0),
        )
        expected_balance = original_amount * tracked_remaining_pct / 100.0

        # Never increase a tracked position from an external wallet balance.
        if actual_balance >= expected_balance:
            return False

        if expected_balance > 0:
            difference_pct = (
                (expected_balance - actual_balance)
                / expected_balance
                * 100.0
            )
            if difference_pct < WALLET_RECONCILIATION_TOLERANCE_PCT:
                return False

        actual_remaining_pct = max(
            0.0,
            min(
                (actual_balance / original_amount * 100.0)
                if original_amount
                else 0.0,
                tracked_remaining_pct,
            ),
        )

        if actual_balance <= 0:
            await repo.update_position(
                position.id,
                status="closed",
                remaining_pct=0.0,
                closed_at=datetime.now(timezone.utc),
                close_reason="position closed externally from Robinhood wallet",
            )
            position.remaining_pct = 0.0
            position.status = "closed"
            logger.info(
                "doppler_position_reconciled_external_close",
                extra={
                    "position_id": position.id,
                    "mint": position.mint,
                    "wallet": wallet_pubkey,
                    "source": "doppler",
                },
            )
            return True

        await repo.update_position(
            position.id,
            remaining_pct=actual_remaining_pct,
        )
        position.remaining_pct = actual_remaining_pct
        logger.info(
            "doppler_position_reconciled_external_partial_sell",
            extra={
                "position_id": position.id,
                "mint": position.mint,
                "wallet": wallet_pubkey,
                "source": "doppler",
                "actual_remaining_pct": actual_remaining_pct,
            },
        )
        return True

    PositionManager._reconcile_live_position = _reconcile_live_position
    PositionManager._doppler_position_patch_installed = True
    logger.info("doppler_position_patch_installed")
    return True


def install() -> bool:
    """Install the Doppler-only reconciliation patch when PositionManager is ready."""
    return _install()


# Keep the original import-time behavior for normal startup. The dedicated
# bootstrap module retries this installer if an import-order race occurs.
try:
    _install()
except Exception:
    logger.exception("doppler_position_patch_install_deferred")
