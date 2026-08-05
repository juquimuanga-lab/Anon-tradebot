"""Launch detection + screening + trade decision loop."""
import asyncio
import logging
from datetime import datetime, timezone

from app.config.settings import settings
from app.connectors.anoncoin import AnoncoinClient, AnoncoinUnavailable
from app.connectors.solscan import SolscanAPIError, SolscanClient
from app.execution.router import ExecutionRouter
from app.metrics import metrics
from app.scanners import mock_feed
from app.scanners.normalize import apply_solscan_enrichment, from_anoncoin_coin
from app.scoring.rules import evaluate_hard_filters
from app.scoring.scorer import compute_score
from app.storage import repository as repo

logger = logging.getLogger("app.scanners.scanner")


class ScannerService:
    def __init__(self, notifier, position_manager, anoncoin: AnoncoinClient, solscan: SolscanClient,
                 execution_router: ExecutionRouter):
        self._notifier = notifier
        self._position_manager = position_manager
        self._anoncoin = anoncoin
        self._solscan = solscan
        self._execution_router = execution_router

    async def _fetch_new_tokens(self):
        try:
            raw_coins = await self._anoncoin.get_coins(sort_by="new", limit=20)
            return [from_anoncoin_coin(c) for c in raw_coins]
        except AnoncoinUnavailable as exc:
            logger.info("anoncoin_discovery_unavailable_using_mock_feed", extra={"detail": str(exc)})
            return mock_feed.generate()

    async def _enrich_with_solscan(self, token):
        try:
            meta = await self._solscan.get_token_meta(token.mint)
        except SolscanAPIError as exc:
            metrics.degraded_count += 1
            logger.warning("solscan_meta_failed", extra={"mint": token.mint, "error": str(exc)})
            meta = None
        try:
            holders = await self._solscan.get_token_holders(token.mint)
        except SolscanAPIError as exc:
            metrics.degraded_count += 1
            logger.warning("solscan_holders_failed", extra={"mint": token.mint, "error": str(exc)})
            holders = None
        return apply_solscan_enrichment(token, meta, holders)

    async def _maybe_trade(self, token, rule_row, score_result):
        state = await repo.get_or_create_bot_state()
        if not state.trading_enabled:
            return

        if await repo.has_open_or_pending_position(token.mint):
            return

        recent_buys = await repo.recent_buy_count(hours=1.0)
        if recent_buys >= rule_row.max_trades_per_hour:
            await repo.save_trade_decision(token.mint, rule_row.id, "skip", "max trades per hour reached", score_result.score)
            return

        seconds_since = await repo.seconds_since_last_buy()
        if seconds_since is not None and seconds_since < rule_row.cooldown_seconds:
            await repo.save_trade_decision(token.mint, rule_row.id, "skip", "cooldown active", score_result.score)
            return

        amount_sol = min(rule_row.max_buy_size_sol, state.paper_balance_sol if state.mode == "paper" else rule_row.max_buy_size_sol)
        adapter = await self._execution_router.get_adapter(state.mode, rule_row.created_by)
        order = await repo.create_order(token.mint, "buy", state.mode, "pending", amount_sol, token.price_usd)
        await self._notifier.buy_placed(token.ticker_symbol or token.mint[:8], amount_sol, state.mode)

        result = await adapter.buy(token, amount_sol)
        if result.success:
            fill_price = result.price_usd or token.price_usd
            amount_tokens = amount_sol / max(fill_price, 1e-12)
            await repo.create_position(
                token.mint, rule_row.id, state.mode, fill_price, amount_tokens, amount_sol,
                owner_user_id=rule_row.created_by,
            )
            await repo.save_trade_decision(token.mint, rule_row.id, "buy", "passed rules", score_result.score)
            metrics.trades_placed += 1
            await self._notifier.buy_filled(token.ticker_symbol or token.mint[:8], fill_price, state.mode)
        else:
            await repo.save_trade_decision(token.mint, rule_row.id, "buy_failed", result.error_message or "unknown error", score_result.score)
            await self._notifier.buy_failed(token.ticker_symbol or token.mint[:8], result.error_message or "unknown error")

    async def scan_once(self):
        active_rule = await repo.get_active_rule()
        tokens = await self._fetch_new_tokens()
        for token in tokens:
            if await repo.token_already_seen(token.mint):
                continue
            await repo.save_token(token)
            metrics.tokens_scanned += 1

            token = await self._enrich_with_solscan(token)

            if not active_rule:
                continue

            from app.storage.repository import rule_row_to_params

            rule_params = rule_row_to_params(active_rule)
            passed, reasons = evaluate_hard_filters(token, rule_params)
            score_result = compute_score(token, rule_params, settings.creator_watchlist)

            await repo.save_screening_result(
                token.mint, passed, score_result.score, reasons, token.liquidity_usd,
                token.holders, token.market_cap_usd, score_result.creator_match,
                {"source": token.source, "breakdown": score_result.breakdown},
            )

            if not passed:
                await self._notifier.rule_violation(token.ticker_symbol or token.mint[:8], reasons)
                continue

            if score_result.score < settings.qualify_score_threshold:
                continue

            metrics.tokens_qualified += 1
            await self._notifier.new_qualified_token(
                token.ticker_symbol or token.mint[:8], token.mint, score_result.score, token.source
            )
            await self._maybe_trade(token, active_rule, score_result)

    async def run_forever(self):
        while True:
            try:
                await self.scan_once()
            except Exception as exc:  # defensive: scanner must never die
                metrics.error_count += 1
                logger.exception("scan_cycle_failed")
                await self._notifier.api_error("scanner", str(exc))
            await asyncio.sleep(settings.scan_interval_seconds)

    async def daily_summary_loop(self):
        while True:
            await asyncio.sleep(300)
            try:
                await self._maybe_send_daily_summary()
            except Exception:
                logger.exception("daily_summary_failed")

    async def _maybe_send_daily_summary(self):
        state = await repo.get_or_create_bot_state()
        now = datetime.now(timezone.utc)
        already_sent_today = (
            state.last_daily_summary_at is not None
            and state.last_daily_summary_at.astimezone(timezone.utc).date() == now.date()
        )
        if now.hour != settings.daily_summary_hour_utc or already_sent_today:
            return

        closed = await repo.get_closed_positions()
        wins = sum(1 for p in closed if p.realized_pnl_usd > 0)
        total_pnl = sum(p.realized_pnl_usd for p in closed)
        win_rate = (wins / len(closed) * 100) if closed else 0.0
        text = (
            f"Tokens scanned: {metrics.tokens_scanned}\n"
            f"Tokens qualified: {metrics.tokens_qualified}\n"
            f"Trades placed: {metrics.trades_placed}\n"
            f"Win rate: {win_rate:.1f}%\n"
            f"Total realized PnL: ${total_pnl:.2f}\n"
            f"Errors: {metrics.error_count}"
        )
        await self._notifier.daily_summary(text)
        await repo.update_bot_state(last_daily_summary_at=now)
