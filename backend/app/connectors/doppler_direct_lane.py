"""Direct Robinhood Chain Doppler/Long launch lane.

This module bypasses the Pons launch detector. It attaches the Doppler watcher
directly to ScannerService at runtime so the existing screening pipeline can
be reused without copying the large scanner module.

Only canonical SPCX-quoted launches with the observed Anoncoin vanity suffix
are admitted to the sniper queue.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from app.connectors import doppler_control
from app.connectors.doppler import doppler_client
from app.execution.onchain.robinhood_wallet import (
    InvalidRobinhoodWalletKeyError,
    load_robinhood_account,
    resolve_robinhood_rpc_url,
)
from app.security.secrets_manager import secrets_manager

logger = logging.getLogger("app.connectors.doppler_direct_lane")
SOURCE_DOPPLER = "doppler"


def _suffix_matches(mint: str) -> bool:
    return str(mint or "").lower().endswith(
        doppler_control.ANONCOIN_ADDRESS_SUFFIX.lower()
    )


async def _watch_doppler_for_new_mints(scanner) -> None:
    if not doppler_control.deployment_enabled() or not doppler_control.is_enabled():
        return

    try:
        discovered = await doppler_client.poll_new_launches()
    except Exception as exc:
        logger.exception(
            "doppler_direct_launch_watch_failed",
            extra={"error": str(exc)},
        )
        return

    accepted = 0
    for item in discovered:
        mint = item.get("mint")
        if not mint:
            continue

        numeraire = str(item.get("numeraire", "")).lower()
        if numeraire != doppler_control.SPCX_TOKEN.lower():
            logger.debug(
                "doppler_launch_rejected_non_spcx",
                extra={"mint": mint, "numeraire": numeraire},
            )
            continue

        if not _suffix_matches(mint):
            logger.debug(
                "doppler_launch_rejected_non_anoncoin_fingerprint",
                extra={
                    "mint": mint,
                    "required_suffix": doppler_control.ANONCOIN_ADDRESS_SUFFIX,
                },
            )
            continue

        if mint in scanner._pending_watch:
            continue
        if await scanner._pending_repo_token_seen(mint):
            continue

        scanner._pending_watch[mint] = {
            "first_seen": item.get("created_on") or datetime.now(timezone.utc),
            "source": SOURCE_DOPPLER,
            "metadata": item,
        }
        accepted += 1

    if discovered or accepted:
        logger.info(
            "doppler_direct_launch_batch",
            extra={"discovered": len(discovered), "accepted": accepted},
        )


async def _snapshot_doppler(mint: str, metadata: dict, first_seen: datetime):
    from app.scoring.rules import TokenSnapshot

    try:
        market = await doppler_client.market_snapshot(mint, metadata)
    except Exception as exc:
        logger.warning(
            "doppler_direct_snapshot_not_ready",
            extra={"mint": mint, "error": str(exc)},
        )
        return None

    price_usd = float(market.get("price_usd", 0.0) or 0.0)
    if price_usd <= 0:
        return None

    return TokenSnapshot(
        mint=mint,
        ticker_name=market.get("name", metadata.get("symbol", "")),
        ticker_symbol=market.get("symbol", metadata.get("symbol", "")),
        creator_wallet=metadata.get("creator", ""),
        created_on=first_seen,
        price_usd=price_usd,
        market_cap_usd=float(market.get("market_cap_usd", 0.0) or 0.0),
        liquidity_usd=float(market.get("liquidity_usd", 0.0) or 0.0),
        holders=int(market.get("holders", 0) or 0),
        volume_24h_usd=float(market.get("volume_24h_usd", 0.0) or 0.0),
        is_migrated=bool(market.get("is_migrated", False)),
        decimals=int(market.get("decimals", 18)),
        source=SOURCE_DOPPLER,
        raw_enrichment={
            "doppler": market,
            "tx_hash": metadata.get("tx_hash"),
            "launcher": metadata.get("launcher"),
            "numeraire": metadata.get("numeraire"),
        },
    )


def _install() -> None:
    try:
        from app.scanners.scanner import ScannerService
        from app.storage import repository as repo
        from app.execution.router import ExecutionRouter, NoWalletConnectedAdapter
        from app.execution.doppler_live import DopplerExecutionAdapter
        from app.config.settings import settings

        if getattr(ScannerService, "_doppler_direct_lane_installed", False):
            return

        original_watch_all = ScannerService._watch_wallets_for_new_mints
        original_snapshot = ScannerService._build_onchain_snapshot
        original_enrich = ScannerService._enrich_holders
        original_get_adapter = ExecutionRouter.get_adapter

        async def _pending_repo_token_seen(self, mint: str) -> bool:
            return await repo.token_already_seen(mint)

        async def _noop_pons_watch(self):
            return

        async def _watch_all(self):
            await original_watch_all(self)
            await _watch_doppler_for_new_mints(self)

        async def _build_snapshot(self, mint, source, metadata, first_seen):
            if source == SOURCE_DOPPLER:
                return await _snapshot_doppler(mint, metadata, first_seen)
            return await original_snapshot(self, mint, source, metadata, first_seen)

        async def _enrich(self, token):
            if getattr(token, "source", "") == SOURCE_DOPPLER:
                return token
            return await original_enrich(self, token)

        async def _get_adapter(self, mode, owner_user_id, source="anoncoin_onchain"):
            if source != SOURCE_DOPPLER:
                return await original_get_adapter(
                    self, mode, owner_user_id, source=source
                )

            if mode == "paper":
                return self._paper_adapter

            if owner_user_id is None:
                return NoWalletConnectedAdapter(
                    "No wallet owner is associated with this trade."
                )

            if not doppler_control.deployment_enabled():
                return NoWalletConnectedAdapter(
                    "Doppler live trading is disabled; set "
                    "ROBINHOOD_DOPPLER_TRADING_ENABLED=true."
                )

            raw_key = await secrets_manager.get_robinhood_wallet_private_key(
                owner_user_id
            )
            if not raw_key:
                return NoWalletConnectedAdapter(
                    "No Robinhood Chain wallet connected. "
                    "Use /connectrobinhoodwallet first."
                )

            try:
                account = load_robinhood_account(raw_key)
                rpc_url = resolve_robinhood_rpc_url(settings)
            except (InvalidRobinhoodWalletKeyError, RuntimeError, ValueError) as exc:
                return NoWalletConnectedAdapter(str(exc))

            slippage = int(
                os.getenv("DOPPLER_BUY_SLIPPAGE_BPS", "1000") or 1000
            )
            logger.info(
                "doppler_direct_execution_adapter_selected",
                extra={"owner_user_id": owner_user_id, "wallet": account.address},
            )
            return DopplerExecutionAdapter(
                account=account,
                rpc_url=rpc_url,
                buy_slippage_bps=slippage,
            )

        ScannerService._watch_pons_for_new_mints = _noop_pons_watch
        ScannerService._watch_wallets_for_new_mints = _watch_all
        ScannerService._build_onchain_snapshot = _build_snapshot
        ScannerService._enrich_holders = _enrich
        ScannerService._pending_repo_token_seen = _pending_repo_token_seen
        ExecutionRouter.get_adapter = _get_adapter
        ScannerService._doppler_direct_lane_installed = True

        logger.info(
            "doppler_direct_lane_installed",
            extra={
                "quote": doppler_control.SPCX_TOKEN,
                "address_suffix": doppler_control.ANONCOIN_ADDRESS_SUFFIX,
                "pons_dependency": False,
            },
        )
    except Exception:
        logger.exception("doppler_direct_lane_install_failed")


try:
    loop = asyncio.get_running_loop()
    loop.call_soon(_install)
except RuntimeError:
    try:
        asyncio.get_event_loop().call_soon(_install)
    except Exception:
        logger.exception("doppler_direct_lane_schedule_failed")
