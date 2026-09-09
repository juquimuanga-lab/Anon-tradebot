"""End-to-end diagnostics for the independent Robinhood Doppler lane.

This module is telemetry-only. It does not change qualification or execution
behavior. It wraps the scanner's pending dispatcher and trade screen so a
Doppler launch can be followed from queue -> snapshot -> rules -> execution.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("app.connectors.doppler_diagnostics")
SOURCE_DOPPLER = "doppler"
_RETRY_SECONDS = 2.0


def _install() -> bool:
    try:
        from app.scanners.scanner import ScannerService

        if getattr(ScannerService, "_doppler_diagnostics_installed", False):
            return True

        original_pending = ScannerService._process_watched_wallet_pending
        original_screen = ScannerService._screen_and_maybe_trade
        original_trade = ScannerService._maybe_trade

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
                        "smart_money_rule_ids": [r.id for r in (smart_money_rules or [])],
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

        async def _screen(self, token, rule, notify_on_fail):
            if getattr(token, "source", "") != SOURCE_DOPPLER:
                return await original_screen(self, token, rule, notify_on_fail)

            logger.info(
                "doppler_rule_screen_started",
                extra={
                    "mint": token.mint,
                    "rule_id": rule.id,
                    "owner_user_id": rule.created_by,
                    "strategy": getattr(rule, "strategy", "smart"),
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
        ScannerService._screen_and_maybe_trade = _screen
        ScannerService._maybe_trade = _trade
        ScannerService._doppler_diagnostics_installed = True
        logger.info("doppler_diagnostics_installed")
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
