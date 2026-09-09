"""Direct Robinhood Chain Doppler/Long launch lane.

This module bypasses the Pons launch detector. It attaches the Doppler watcher
 directly to ScannerService so launch discovery remains independent of the
existing Pons scanner.

Discovery is intentionally independent from the runtime sniper switch:
we must continue seeing/logging launches even when live trading is OFF.
Only SPCX + the observed Anoncoin vanity fingerprint are admitted to the
live sniper queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
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
_INSTALL_RETRY_SECONDS = 2.0


def _suffix_matches(mint: str) -> bool:
    return str(mint or "").lower().endswith(
        doppler_control.ANONCOIN_ADDRESS_SUFFIX.lower()
    )


async def _watch_doppler_for_new_mints(scanner) -> None:
    """Poll the independent Doppler detector and feed qualified launches."""
    poll_started = datetime.now(timezone.utc)
    try:
        discovered = await doppler_client.poll_new_launches()
    except Exception as exc:
        logger.exception(
            "doppler_direct_launch_watch_failed",
            extra={"error": str(exc)},
        )
        return

    logger.info(
        "doppler_direct_polling",
        extra={
            "discovered": len(discovered or []),
            "deployment_enabled": doppler_control.deployment_enabled(),
            "sniper_enabled": doppler_control.is_enabled(),
            "pending_before": len(scanner._pending_watch),
            "poll_started": poll_started.isoformat(),
        },
    )

    if not discovered:
        logger.info(
            "doppler_direct_no_launches",
            extra={"pending_total": len(scanner._pending_watch)},
        )
        return

    accepted = 0
    spcx_candidates = 0
    fingerprint_matches = 0

    for item in discovered:
        mint = item.get("mint")
        logger.info(
            "doppler_launch_candidate_seen",
            extra={
                "mint": mint,
                "numeraire": item.get("numeraire"),
                "initializer": item.get("initializer"),
                "pool_or_hook": item.get("pool_or_hook"),
                "launcher": item.get("launcher"),
                "creator": item.get("creator"),
                "tx_hash": item.get("tx_hash"),
                "block_number": item.get("block_number"),
                "source": item.get("source"),
                "venue": item.get("venue"),
            },
        )

        if not mint:
            logger.warning(
                "doppler_launch_rejected_missing_mint",
                extra={"item": item},
            )
            continue

        numeraire = str(item.get("numeraire", "")).lower()
        if numeraire != doppler_control.SPCX_TOKEN.lower():
            logger.info(
                "doppler_launch_rejected_non_spcx",
                extra={
                    "mint": mint,
                    "numeraire": numeraire,
                    "required_numeraire": doppler_control.SPCX_TOKEN,
                    "stage": "numeraire_filter",
                },
            )
            continue

        spcx_candidates += 1
        logger.info(
            "doppler_spcx_candidate_passed",
            extra={
                "mint": mint,
                "numeraire": numeraire,
                "spcx_candidates": spcx_candidates,
            },
        )

        suffix_match = _suffix_matches(mint)
        logger.info(
            "doppler_fingerprint_evaluated",
            extra={
                "mint": mint,
                "required_suffix": doppler_control.ANONCOIN_ADDRESS_SUFFIX,
                "suffix_match": suffix_match,
            },
        )
        if not suffix_match:
            logger.info(
                "doppler_spcx_launch_rejected_fingerprint",
                extra={
                    "mint": mint,
                    "required_suffix": doppler_control.ANONCOIN_ADDRESS_SUFFIX,
                    "stage": "fingerprint_filter",
                },
            )
            continue

        fingerprint_matches += 1
        deployment_enabled = doppler_control.deployment_enabled()
        sniper_enabled = doppler_control.is_enabled()
        logger.info(
            "doppler_qualified_launch_gate_evaluated",
            extra={
                "mint": mint,
                "deployment_enabled": deployment_enabled,
                "sniper_enabled": sniper_enabled,
                "stage": "trading_gate",
            },
        )

        if not deployment_enabled or not sniper_enabled:
            logger.info(
                "doppler_qualified_launch_trading_disabled",
                extra={
                    "mint": mint,
                    "deployment_enabled": deployment_enabled,
                    "trading_enabled": sniper_enabled,
                    "stage": "trading_gate_rejected",
                },
            )
            continue

        if mint in scanner._pending_watch:
            logger.info(
                "doppler_launch_already_pending",
                extra={
                    "mint": mint,
                    "stage": "pending_dedup",
                },
            )
            continue

        try:
            already_seen = await scanner._pending_repo_token_seen(mint)
        except Exception as exc:
            logger.exception(
                "doppler_repo_duplicate_check_failed",
                extra={"mint": mint, "error": str(exc)},
            )
            continue

        if already_seen:
            logger.info(
                "doppler_launch_rejected_already_seen",
                extra={
                    "mint": mint,
                    "stage": "repository_dedup",
                },
            )
            continue

        scanner._pending_watch[mint] = {
            "first_seen": item.get("created_on") or datetime.now(timezone.utc),
            "source": SOURCE_DOPPLER,
            "metadata": item,
        }
        accepted += 1

        logger.info(
            "doppler_launch_queued",
            extra={
                "mint": mint,
                "source": SOURCE_DOPPLER,
                "accepted": accepted,
                "pending_total": len(scanner._pending_watch),
                "tx_hash": item.get("tx_hash"),
                "block_number": item.get("block_number"),
            },
        )

    logger.info(
        "doppler_direct_launch_batch",
        extra={
            "discovered": len(discovered),
            "spcx_candidates": spcx_candidates,
            "fingerprint_matches": fingerprint_matches,
            "accepted": accepted,
            "deployment_enabled": doppler_control.deployment_enabled(),
            "sniper_enabled": doppler_control.is_enabled(),
            "pending_after": len(scanner._pending_watch),
        },
    )


async def _snapshot_doppler(mint: str, metadata: dict, first_seen: datetime):
    from app.scoring.rules import TokenSnapshot

    logger.info(
        "doppler_snapshot_started",
        extra={
            "mint": mint,
            "tx_hash": metadata.get("tx_hash"),
            "block_number": metadata.get("block_number"),
            "numeraire": metadata.get("numeraire"),
            "initializer": metadata.get("initializer"),
            "pool_or_hook": metadata.get("pool_or_hook"),
        },
    )

    try:
        market = await doppler_client.market_snapshot(mint, metadata)
    except Exception as exc:
        logger.exception(
            "doppler_direct_snapshot_not_ready",
            extra={"mint": mint, "error": str(exc)},
        )
        return None

    price_usd = float(market.get("price_usd", 0.0) or 0.0)
    logger.info(
        "doppler_snapshot_result",
        extra={
            "mint": mint,
            "price_usd": price_usd,
            "price_quote": market.get("price_quote"),
            "quote_usd": market.get("quote_usd"),
            "market_cap_usd": market.get("market_cap_usd"),
            "liquidity_usd": market.get("liquidity_usd"),
            "holders": market.get("holders"),
            "holders_ready": market.get("holders_ready"),
            "status": market.get("status"),
            "tokens_on_curve": market.get("tokens_on_curve"),
            "quote_symbol": market.get("quote_symbol"),
            "pool_key": market.get("pool_key"),
        },
    )
    if price_usd <= 0:
        logger.warning(
            "doppler_snapshot_rejected_invalid_price",
            extra={
                "mint": mint,
                "price_usd": price_usd,
                "market_cap_usd": market.get("market_cap_usd"),
                "liquidity_usd": market.get("liquidity_usd"),
            },
        )
        return None

    token = TokenSnapshot(
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
    logger.info(
        "doppler_snapshot_built",
        extra={
            "mint": mint,
            "ticker": token.ticker_symbol,
            "price_usd": token.price_usd,
            "market_cap_usd": token.market_cap_usd,
            "liquidity_usd": token.liquidity_usd,
            "holders": token.holders,
            "age_seconds": token.age_seconds,
            "source": token.source,
        },
    )
    return token


def _install() -> bool:
    try:
        from app.scanners.scanner import ScannerService
        from app.storage import repository as repo
        from app.execution.router import ExecutionRouter, NoWalletConnectedAdapter
        from app.execution.doppler_live import DopplerExecutionAdapter
        from app.config.settings import settings

        if getattr(ScannerService, "_doppler_direct_lane_installed", False):
            return True

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
                logger.info(
                    "doppler_snapshot_dispatch",
                    extra={
                        "mint": mint,
                        "source": source,
                        "pending_metadata_keys": sorted(metadata.keys()),
                    },
                )
                return await _snapshot_doppler(mint, metadata, first_seen)
            return await original_snapshot(self, mint, source, metadata, first_seen)

        async def _enrich(self, token):
            if getattr(token, "source", "") == SOURCE_DOPPLER:
                logger.info(
                    "doppler_holder_enrichment_skipped",
                    extra={
                        "mint": token.mint,
                        "holders": getattr(token, "holders", None),
                        "reason": "direct_doppler_lane_does_not_use_solana_helius_enrichment",
                    },
                )
                return token
            return await original_enrich(self, token)

        async def _get_adapter(self, mode, owner_user_id, source="anoncoin_onchain"):
            if source != SOURCE_DOPPLER:
                return await original_get_adapter(
                    self, mode, owner_user_id, source=source
                )

            logger.info(
                "doppler_execution_adapter_requested",
                extra={
                    "mode": mode,
                    "owner_user_id": owner_user_id,
                    "source": source,
                    "deployment_enabled": doppler_control.deployment_enabled(),
                },
            )

            if mode == "paper":
                logger.info(
                    "doppler_execution_adapter_paper",
                    extra={"owner_user_id": owner_user_id},
                )
                return self._paper_adapter

            if owner_user_id is None:
                logger.warning(
                    "doppler_execution_adapter_rejected_no_owner",
                    extra={"source": source},
                )
                return NoWalletConnectedAdapter(
                    "No wallet owner is associated with this trade."
                )

            if not doppler_control.deployment_enabled():
                logger.warning(
                    "doppler_execution_adapter_rejected_deployment_disabled",
                    extra={"owner_user_id": owner_user_id},
                )
                return NoWalletConnectedAdapter(
                    "Doppler live trading is disabled; set "
                    "ROBINHOOD_DOPPLER_TRADING_ENABLED=true."
                )

            raw_key = await secrets_manager.get_robinhood_wallet_private_key(
                owner_user_id
            )
            if not raw_key:
                logger.warning(
                    "doppler_execution_adapter_rejected_no_wallet_key",
                    extra={"owner_user_id": owner_user_id},
                )
                return NoWalletConnectedAdapter(
                    "No Robinhood Chain wallet connected. "
                    "Use /connectrobinhoodwallet first."
                )

            try:
                account = load_robinhood_account(raw_key)
                rpc_url = resolve_robinhood_rpc_url(settings)
            except (InvalidRobinhoodWalletKeyError, RuntimeError, ValueError) as exc:
                logger.exception(
                    "doppler_execution_adapter_wallet_error",
                    extra={"owner_user_id": owner_user_id, "error": str(exc)},
                )
                return NoWalletConnectedAdapter(str(exc))

            slippage = int(
                os.getenv("DOPPLER_BUY_SLIPPAGE_BPS", "1000") or 1000
            )
            logger.info(
                "doppler_direct_execution_adapter_selected",
                extra={
                    "owner_user_id": owner_user_id,
                    "wallet": account.address,
                    "slippage_bps": slippage,
                    "rpc_url_configured": bool(rpc_url),
                },
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
                "discovery_independent_of_trading_gate": True,
            },
        )
        return True
    except Exception as exc:
        logger.warning(
            "doppler_direct_lane_install_retry",
            extra={"error": str(exc)},
        )
        return False


def _schedule_install_retry() -> None:
    """Install from a timer so import-time event-loop state cannot block it."""
    logger.info("doppler_direct_lane_bootstrap_scheduled")

    def _attempt() -> None:
        if _install():
            logger.info("doppler_direct_lane_bootstrap_complete")
            return
        timer = threading.Timer(_INSTALL_RETRY_SECONDS, _attempt)
        timer.daemon = True
        timer.start()

    timer = threading.Timer(0.0, _attempt)
    timer.daemon = True
    timer.start()


_schedule_install_retry()
