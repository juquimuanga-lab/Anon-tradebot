"""End-to-end diagnostics for the independent Robinhood Doppler lane.

Telemetry only. The wrappers below must never change scanner decisions; they
only expose why a Doppler candidate did or did not reach the next stage.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("app.connectors.doppler_diagnostics")
SOURCE_DOPPLER = "doppler"
_RETRY_SECONDS = 2.0


def _install() -> bool:
    try:
        from app.scanners.scanner import ScannerService, _rule_matches_source, _rule_strategy
        from app.storage import repository as repo
        from app.config.settings import settings

        if getattr(ScannerService, "_doppler_diagnostics_installed", False):
            return True

        original_pending = ScannerService._process_watched_wallet_pending
        original_screen = ScannerService._screen_and_maybe_trade
        original_trade = ScannerService._maybe_trade
        original_build = ScannerService._build_onchain_snapshot

        async def _process_pending(self, active_rules, smart_money_rules=None):
            doppler_before = {
                mint: dict(watch)
                for mint, watch in self._pending_watch.items()
                if watch.get("source") == SOURCE_DOPPLER
            }

            if doppler_before:
                logger.info(
                    "doppler_pending_processing_started",
                    extra={
                        "pending_mints": list(doppler_before.keys()),
                        "pending_count": len(doppler_before),
                        "active_rule_ids": [r.id for r in (active_rules or [])],
                        "active_rule_count": len(active_rules or []),
                        "smart_money_rule_ids": [r.id for r in (smart_money_rules or [])],
                    },
                )

                # Explicitly expose the rule-resolution stage. This is the
                # critical diagnostic for a candidate that is queued but never
                # reaches snapshot building.
                for mint, watch in doppler_before.items():
                    match_rows = []
                    for rule in active_rules or []:
                        try:
                            source_match = bool(_rule_matches_source(rule, SOURCE_DOPPLER))
                            strategy = _rule_strategy(rule)
                            max_age = getattr(rule, "max_age_seconds", None)
                        except Exception as exc:
                            source_match = False
                            strategy = "error"
                            max_age = None
                            logger.exception(
                                "doppler_rule_resolution_failed",
                                extra={"mint": mint, "rule_id": getattr(rule, "id", None), "error": str(exc)},
                            )
                        match_rows.append({
                            "rule_id": getattr(rule, "id", None),
                            "owner_user_id": getattr(rule, "created_by", None),
                            "source_match": source_match,
                            "strategy": strategy,
                            "max_age_seconds": max_age,
                        })

                    logger.info(
                        "doppler_rule_resolution",
                        extra={
                            "mint": mint,
                            "source": SOURCE_DOPPLER,
                            "rule_count": len(active_rules or []),
                            "rules": match_rows,
                            "smart_money_rule_count": len(smart_money_rules or []),
                            "metadata": watch.get("metadata", {}),
                        },
                    )

                    matching = [row for row in match_rows if row["source_match"]]
                    if not matching:
                        logger.warning(
                            "doppler_candidate_no_source_matching_rules",
                            extra={
                                "mint": mint,
                                "active_rule_ids": [r.id for r in (active_rules or [])],
                                "reason": "_rule_matches_source returned false for every active rule",
                            },
                        )
                    else:
                        logger.info(
                            "doppler_candidate_source_rules_found",
                            extra={
                                "mint": mint,
                                "matching_rule_ids": [row["rule_id"] for row in matching],
                            },
                        )

            result = await original_pending(self, active_rules, smart_money_rules)

            remaining = [
                mint
                for mint, watch in self._pending_watch.items()
                if watch.get("source") == SOURCE_DOPPLER
            ]
            processed = [mint for mint in doppler_before if mint not in remaining]
            if doppler_before:
                logger.info(
                    "doppler_pending_processing_finished",
                    extra={
                        "started_mints": list(doppler_before.keys()),
                        "processed_or_removed": processed,
                        "remaining_pending": remaining,
                        "remaining_count": len(remaining),
                    },
                )
            return result

        async def _build(self, mint, source, metadata, first_seen):
            if source == SOURCE_DOPPLER:
                logger.info(
                    "doppler_pipeline_snapshot_stage_entered",
                    extra={
                        "mint": mint,
                        "source": source,
                        "first_seen": first_seen,
                        "metadata": metadata,
                    },
                )
            try:
                result = await original_build(self, mint, source, metadata, first_seen)
            except Exception as exc:
                if source == SOURCE_DOPPLER:
                    logger.exception(
                        "doppler_pipeline_snapshot_stage_failed",
                        extra={"mint": mint, "error": str(exc)},
                    )
                raise
            if source == SOURCE_DOPPLER:
                logger.info(
                    "doppler_pipeline_snapshot_stage_finished",
                    extra={
                        "mint": mint,
                        "snapshot_built": result is not None,
                        "market_cap_usd": getattr(result, "market_cap_usd", None) if result else None,
                        "liquidity_usd": getattr(result, "liquidity_usd", None) if result else None,
                        "price_usd": getattr(result, "price_usd", None) if result else None,
                    },
                )
            return result

        async def _screen(self, token, rule, notify_on_fail):
            if getattr(token, "source", "") != SOURCE_DOPPLER:
                return await original_screen(self, token, rule, notify_on_fail)

            logger.info(
                "doppler_rule_screen_started",
                extra={
                    "mint": token.mint,
                    "rule_id": rule.id,
                    "owner_user_id": rule.created_by,
                    "strategy": _rule_strategy(rule),
                    "notify_on_fail": notify_on_fail,
                    "market_cap_usd": getattr(token, "market_cap_usd", 0.0),
                    "liquidity_usd": getattr(token, "liquidity_usd", 0.0),
                    "holders": getattr(token, "holders", None),
                    "price_usd": getattr(token, "price_usd", 0.0),
                    "age_seconds": getattr(token, "age_seconds", 0.0),
                },
            )
            try:
                result = await original_screen(self, token, rule, notify_on_fail)
            except Exception as exc:
                logger.exception(
                    "doppler_rule_screen_failed",
                    extra={"mint": token.mint, "rule_id": rule.id, "error": str(exc)},
                )
                raise
            logger.info(
                "doppler_rule_screen_finished",
                extra={
                    "mint": token.mint,
                    "rule_id": rule.id,
                    "result": result,
                },
            )
            return result

        async def _trade(self, token, rule_row, score_result):
            if getattr(token, "source", "") != SOURCE_DOPPLER:
                return await original_trade(self, token, rule_row, score_result)

            logger.info(
                "doppler_trade_gate_started",
                extra={
                    "mint": token.mint,
                    "rule_id": rule_row.id,
                    "owner_user_id": rule_row.created_by,
                    "mode": "unknown_until_state_read",
                    "score": getattr(score_result, "score", None),
                },
            )
            try:
                result = await original_trade(self, token, rule_row, score_result)
            except Exception as exc:
                logger.exception(
                    "doppler_trade_gate_failed",
                    extra={"mint": token.mint, "rule_id": rule_row.id, "error": str(exc)},
                )
                raise
            logger.info(
                "doppler_trade_gate_finished",
                extra={
                    "mint": token.mint,
                    "rule_id": rule_row.id,
                    "result": result,
                },
            )
            return result

        ScannerService._process_watched_wallet_pending = _process_pending
        ScannerService._build_onchain_snapshot = _build
        ScannerService._screen_and_maybe_trade = _screen
        ScannerService._maybe_trade = _trade
        ScannerService._doppler_diagnostics_installed = True

        logger.info(
            "doppler_diagnostics_installed",
            extra={
                "source": SOURCE_DOPPLER,
                "diagnostics_version": 2,
                "trading_logic_modified": False,
                "rpc_url_configured": bool(getattr(settings, "robinhood_rpc_url", None)),
            },
        )
        return True
    except Exception as exc:
        logger.warning(
            "doppler_diagnostics_install_retry",
            extra={"error": str(exc)},
        )
        return False


def _schedule_retry() -> None:
    logger.info("doppler_diagnostics_bootstrap_scheduled")

    def _attempt() -> None:
        if _install():
            logger.info("doppler_diagnostics_bootstrap_complete")
            return
        timer = threading.Timer(_RETRY_SECONDS, _attempt)
        timer.daemon = True
        timer.start()

    timer = threading.Timer(0.0, _attempt)
    timer.daemon = True
    timer.start()


_schedule_retry()
