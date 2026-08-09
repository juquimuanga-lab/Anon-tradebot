"""Launch detection + screening + trade decision loop.

Detection has up to three layers, tried in this priority order per cycle:
1. On-chain watcher: polls wallets in CREATOR_WATCHLIST directly via the free
   public Solana RPC for brand-new SPL mint creations (real data, no paid API
   needed) - this is the primary real signal today, and the only one needed
   to trigger real paper/live trades against the Anoncoin.it creator wallet.
2. Anoncoin's own coin-discovery API, once it's live.
3. A clearly-labelled simulated feed - OFF by default (see
   settings.enable_mock_feed). It used to run unconditionally every cycle
   whenever step 2 was unavailable, which drowned real on-chain detections
   in constant [SIMULATED] alerts since Anoncoin's discovery API has no
   live endpoint yet. Now it only runs if explicitly enabled, purely for
   demoing the pipeline; mock tokens are still always skipped in live mode.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config.settings import settings
from app.connectors.anoncoin import AnoncoinAPIError, AnoncoinClient
from app.connectors.helius import HeliusAPIError, HeliusClient
from app.execution.base import OrderResult
from app.execution.onchain import meteora_dbc
from app.execution.onchain.meteora_dbc import DbcBuildError
from app.execution.router import ExecutionRouter
from app.metrics import metrics
from app.scanners import mock_feed, onchain_watcher, price_feed
from app.scanners.normalize import apply_holder_enrichment, from_anoncoin_coin
from app.scoring.rules import TokenSnapshot, evaluate_hard_filters
from app.scoring.scorer import compute_score
from app.storage import repository as repo

logger = logging.getLogger("app.scanners.scanner")


class ScannerService:
    def __init__(self, notifier, position_manager, anoncoin: AnoncoinClient, holders_client: HeliusClient,
                 execution_router: ExecutionRouter):
        self._notifier = notifier
        self._position_manager = position_manager
        self._anoncoin = anoncoin
        self._holders_client = holders_client
        self._execution_router = execution_router
        self._watermarks = onchain_watcher.WatermarkStore()
        self._pending_watch: dict[str, datetime] = {}
        self._notified_fail: set[tuple[str, int]] = set()
        self._holders_failure_count = 0
        self._holders_backoff_until: datetime | None = None

    async def _fetch_new_tokens(self):
        try:
            raw_coins = await self._anoncoin.get_coins(sort_by="new", limit=20)
            return [from_anoncoin_coin(c) for c in raw_coins]
        except AnoncoinAPIError as exc:
            # Covers both AnoncoinUnavailable (endpoint not live / network
            # error) and a missing/invalid API key - either way, Anoncoin's
            # discovery API isn't usable right now. Previously this always
            # fell back to the simulated feed, which fired 0-2 fake
            # [SIMULATED] tokens every scan_interval_seconds (~every 30s)
            # regardless of trading mode - completely drowning out the real,
            # comparatively rare on-chain detections from CREATOR_WATCHLIST.
            # Real detection doesn't depend on this at all (see
            # _watch_wallets_for_new_mints), so the safe default is to
            # contribute nothing here rather than synthetic noise.
            logger.info("anoncoin_discovery_unavailable", extra={"detail": str(exc)})
            if settings.enable_mock_feed:
                return mock_feed.generate()
            return []

    async def _watch_wallets_for_new_mints(self):
        for wallet in settings.creator_watchlist:
            try:
                discovered = await onchain_watcher.poll_new_mints(settings.solana_rpc_url, wallet, self._watermarks)
            except Exception:
                logger.exception("onchain_watch_poll_failed", extra={"wallet": wallet})
                continue
            for item in discovered:
                mint = item["mint"]
                if mint in self._pending_watch or await repo.token_already_seen(mint):
                    continue
                self._pending_watch[mint] = datetime.now(timezone.utc)
                metrics.tokens_scanned += 1
                logger.info("onchain_new_mint_detected", extra={"mint": mint, "watched_wallet": wallet})

    async def _build_onchain_snapshot(self, mint: str) -> TokenSnapshot | None:
        try:
            info = await meteora_dbc.get_pool_info(mint, settings.solana_rpc_url)
        except DbcBuildError as exc:
            logger.warning("pool_info_failed", extra={"mint": mint, "error": str(exc)})
            return None
        sol_price = await price_feed.get_sol_usd_price(settings.jupiter_price_url)
        return TokenSnapshot(
            mint=mint,
            ticker_name=f"anon-{mint[:6]}",
            ticker_symbol=mint[:6],
            creator_wallet=info.get("creator", ""),
            created_on=self._pending_watch.get(mint, datetime.now(timezone.utc)),
            price_usd=info["price_sol_per_token"] * sol_price,
            market_cap_usd=info["market_cap_sol"] * sol_price,
            liquidity_usd=info["quote_reserve_sol"] * sol_price,
            holders=0,
            volume_24h_usd=0.0,
            is_migrated=bool(info.get("is_migrated", False)),
            decimals=int(info.get("token_decimals", 6)),
            source="anoncoin_onchain",
        )

    async def _enrich_holders(self, token):
        now = datetime.now(timezone.utc)
        if self._holders_backoff_until and now < self._holders_backoff_until:
            return token

        try:
            holder_count = await self._holders_client.get_token_holder_count(token.mint)
            self._holders_failure_count = 0
        except HeliusAPIError as exc:
            metrics.degraded_count += 1
            self._holders_failure_count += 1
            logger.warning("holders_enrichment_failed", extra={"mint": token.mint, "error": str(exc)})
            holder_count = None

        if self._holders_failure_count >= 3 and not self._holders_backoff_until:
            self._holders_backoff_until = now + timedelta(minutes=10)
            logger.warning("holders_enrichment_disabled_temporarily", extra={"minutes": 10})
        elif self._holders_failure_count == 0:
            self._holders_backoff_until = None

        return apply_holder_enrichment(token, holder_count)

    async def _maybe_trade(self, token, rule_row, score_result) -> bool:
        state = await repo.get_or_create_bot_state()
        if not state.trading_enabled:
            return False

        if state.mode == "live" and token.source == "mock_simulated":
            await repo.save_trade_decision(
                token.mint, rule_row.id, "skip",
                "simulated token has no real on-chain mint - skipped in live mode", score_result.score,
            )
            return True  # nothing more to do with this one, ever

        if await repo.has_open_or_pending_position(token.mint, rule_row.created_by):
            return True

        recent_buys = await repo.recent_buy_count(hours=1.0, owner_user_id=rule_row.created_by)
        if recent_buys >= rule_row.max_trades_per_hour:
            await repo.save_trade_decision(token.mint, rule_row.id, "skip", "max trades per hour reached", score_result.score)
            return False

        seconds_since = await repo.seconds_since_last_buy(owner_user_id=rule_row.created_by)
        if seconds_since is not None and seconds_since < rule_row.cooldown_seconds:
            await repo.save_trade_decision(token.mint, rule_row.id, "skip", "cooldown active", score_result.score)
            return False

        amount_sol = min(rule_row.max_buy_size_sol, state.paper_balance_sol if state.mode == "paper" else rule_row.max_buy_size_sol)
        adapter = await self._execution_router.get_adapter(state.mode, rule_row.created_by)
        order = await repo.create_order(
            token.mint, "buy", state.mode, "pending", amount_sol, token.price_usd,
            rule_id=rule_row.id, owner_user_id=rule_row.created_by,
        )
        logger.info("buy_placed", extra={"mint": token.mint, "rule_id": rule_row.id, "amount_sol": amount_sol, "mode": state.mode})
        await self._notifier.buy_placed(rule_row.created_by, token.ticker_symbol or token.mint[:8], amount_sol, state.mode)

        try:
            result = await asyncio.wait_for(adapter.buy(token, amount_sol), timeout=settings.execution_timeout_seconds)
        except asyncio.TimeoutError:
            # Belt-and-suspenders: every await inside adapter.buy() is already
            # either try/excepted or bounded by its own timeout, but this
            # guarantees the order can never sit in 'pending' forever (visible
            # via /history) with no Telegram follow-up if something inside
            # still manages to hang - e.g. a cancelled task on redeploy.
            logger.error("buy_execution_timeout", extra={"mint": token.mint, "rule_id": rule_row.id})
            result = OrderResult(
                success=False, status="failed",
                error_message=(
                    f"execution did not resolve within {settings.execution_timeout_seconds}s - "
                    "outcome unknown, verify wallet balance / Solscan manually"
                ),
            )
        if result.success:
            fill_price = result.price_usd or token.price_usd
            amount_tokens = amount_sol / max(fill_price, 1e-12)
            await repo.create_position(
                token.mint, rule_row.id, state.mode, fill_price, amount_tokens, amount_sol,
                owner_user_id=rule_row.created_by, entry_volume_24h_usd=token.volume_24h_usd,
            )
            await repo.update_order(order.id, "filled", tx_signature=result.tx_signature)
            await repo.save_trade_decision(token.mint, rule_row.id, "buy", "passed rules", score_result.score)
            metrics.trades_placed += 1
            logger.info("buy_filled", extra={"mint": token.mint, "rule_id": rule_row.id, "tx_signature": result.tx_signature})
            await self._notifier.buy_filled(
                rule_row.created_by, token.ticker_symbol or token.mint[:8], fill_price, state.mode, result.tx_signature
            )
            return True

        await repo.update_order(order.id, "failed", error_message=result.error_message)
        await repo.save_trade_decision(token.mint, rule_row.id, "buy_failed", result.error_message or "unknown error", score_result.score)
        logger.warning("buy_failed", extra={"mint": token.mint, "rule_id": rule_row.id, "error": result.error_message})
        await self._notifier.buy_failed(rule_row.created_by, token.ticker_symbol or token.mint[:8], result.error_message or "unknown error")
        return False

    async def _screen_and_maybe_trade(self, token, rule, notify_on_fail: bool) -> bool:
        if not rule:
            return False

        from app.storage.repository import rule_row_to_params

        rule_params = rule_row_to_params(rule)
        passed, reasons = evaluate_hard_filters(token, rule_params)
        score_result = compute_score(token, rule_params, settings.creator_watchlist)

        await repo.save_screening_result(
            token.mint, passed, score_result.score, reasons, token.liquidity_usd,
            token.holders, token.market_cap_usd, score_result.creator_match,
            {"source": token.source, "breakdown": score_result.breakdown, "rule_id": rule.id},
        )

        if not passed:
            if notify_on_fail:
                await self._notifier.rule_violation(rule.created_by, token.ticker_symbol or token.mint[:8], reasons)
            return False

        if score_result.score < settings.qualify_score_threshold:
            return False

        metrics.tokens_qualified += 1
        logger.info("token_qualified", extra={"mint": token.mint, "rule_id": rule.id, "score": score_result.score})
        await self._notifier.new_qualified_token(
            rule.created_by, token.ticker_symbol or token.mint[:8], token.mint, score_result.score, token.source
        )
        return await self._maybe_trade(token, rule, score_result)

    async def _process_watched_wallet_pending(self, active_rules: list):
        """Each admin's rule ages a pending mint out independently (its own
        max_age_seconds), and each is screened/traded independently - one
        admin's rule matching or failing never affects another's. A mint
        only leaves _pending_watch once every active rule has either traded
        it or aged out on it.
        """
        now = datetime.now(timezone.utc)
        fallback_max_age = 3600  # only used if no admin has an active rule at all

        for mint in list(self._pending_watch.keys()):
            first_seen = self._pending_watch[mint]
            age_seconds = (now - first_seen).total_seconds()

            if not active_rules:
                if age_seconds > fallback_max_age:
                    del self._pending_watch[mint]
                continue

            due_rules = [r for r in active_rules if age_seconds <= r.max_age_seconds]
            if not due_rules:
                # every active rule has aged out on this mint
                del self._pending_watch[mint]
                for r in active_rules:
                    self._notified_fail.discard((mint, r.id))
                continue

            token = await self._build_onchain_snapshot(mint)
            if token is None:
                continue
            await repo.save_token(token)
            token = await self._enrich_holders(token)

            all_settled = True
            for rule in due_rules:
                key = (mint, rule.id)
                done = await self._screen_and_maybe_trade(token, rule, notify_on_fail=(key not in self._notified_fail))
                self._notified_fail.add(key)
                if done:
                    self._notified_fail.discard(key)
                else:
                    all_settled = False

            if all_settled:
                del self._pending_watch[mint]
                for rule in active_rules:
                    self._notified_fail.discard((mint, rule.id))

    async def scan_once(self):
        active_rules = await repo.get_all_active_rules()

        await self._watch_wallets_for_new_mints()
        await self._process_watched_wallet_pending(active_rules)

        for token in await self._fetch_new_tokens():
            if await repo.token_already_seen(token.mint):
                continue
            await repo.save_token(token)
            metrics.tokens_scanned += 1
            token = await self._enrich_holders(token)
            for rule in active_rules:
                await self._screen_and_maybe_trade(token, rule, notify_on_fail=True)

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
