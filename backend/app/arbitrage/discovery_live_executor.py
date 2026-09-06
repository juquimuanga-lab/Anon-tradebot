"""Live executor adapter that preserves fresh discovery routes through execution."""
from __future__ import annotations

import contextvars
import os
import time
from typing import Any

from app.arbitrage.live_executor import ArbitrageLiveExecutor, LiveExecutionResult
from app.arbitrage.models import Quote
from app.arbitrage.jupiter_quotes import VenueConfig
from app.arbitrage.telemetry import telemetry


class DiscoveryAwareLiveExecutor(ArbitrageLiveExecutor):
    """Reuse a fresh discovery quote, falling back to Jupiter only when stale."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        try:
            max_age = float(os.getenv("ARBITRAGE_LIVE_DISCOVERY_QUOTE_MAX_AGE_SECONDS", "1.5"))
        except ValueError:
            max_age = 1.5
        self._discovery_quote_max_age_seconds = max(0.1, min(max_age, 10.0))
        self._discovery_quotes: contextvars.ContextVar[tuple[Quote, Quote] | None] = contextvars.ContextVar(
            "arbitrage_discovery_quotes", default=None
        )
        self._pending_discoveries: dict[tuple[str, float], tuple[Quote, Quote, float]] = {}

    def remember_discovery(
        self,
        *,
        token_mint: str,
        amount_sol: float,
        buy_quote: Quote,
        sell_quote: Quote,
    ) -> None:
        """Make a just-discovered route available to the existing Telegram executor."""
        if buy_quote.raw_response is None or sell_quote.raw_response is None:
            return
        self._pending_discoveries[(token_mint, float(amount_sol))] = (
            buy_quote,
            sell_quote,
            time.monotonic(),
        )

    async def execute_unrestricted(
        self,
        owner_user_id: int,
        token_mint: str,
        amount_sol: float,
    ) -> LiveExecutionResult:
        """Prefer the matching discovery route before falling back to fresh quotes."""
        key = (token_mint, float(amount_sol))
        pending = self._pending_discoveries.get(key)
        if pending:
            buy_quote, sell_quote, stored_at = pending
            if time.monotonic() - stored_at <= self._discovery_quote_max_age_seconds:
                self._pending_discoveries.pop(key, None)
                return await self.execute_discovery(
                    owner_user_id=owner_user_id,
                    token_mint=token_mint,
                    amount_sol=amount_sol,
                    buy_quote=buy_quote,
                    sell_quote=sell_quote,
                )
            self._pending_discoveries.pop(key, None)
            telemetry.increment("live_discovery_quote_cache_expired")
        return await super().execute_unrestricted(owner_user_id, token_mint, amount_sol)

    async def execute_discovery(
        self,
        *,
        owner_user_id: int,
        token_mint: str,
        amount_sol: float,
        buy_quote: Quote,
        sell_quote: Quote,
    ) -> LiveExecutionResult:
        """Execute directly from discovery quotes when they are still fresh."""
        if buy_quote.raw_response is None or sell_quote.raw_response is None:
            telemetry.increment("live_discovery_quote_missing_payload")
            return await super().execute_unrestricted(owner_user_id, token_mint, amount_sol)

        token = self._discovery_quotes.set((buy_quote, sell_quote))
        try:
            telemetry.observe(
                "live_discovery_buy_quote_age_ms",
                max(0.0, time.monotonic() - buy_quote.quoted_at_monotonic) * 1000.0,
            )
            telemetry.observe(
                "live_discovery_sell_quote_age_ms",
                max(0.0, time.monotonic() - sell_quote.quoted_at_monotonic) * 1000.0,
            )
            return await super().execute_unrestricted(owner_user_id, token_mint, amount_sol)
        finally:
            self._discovery_quotes.reset(token)

    async def _quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        venue: VenueConfig,
    ) -> dict[str, Any]:
        cached = self._discovery_quotes.get()
        if cached:
            # Buy is SOL -> token; sell is token -> SOL. Match on exact request
            # parameters so a stale/re-quoted buy cannot accidentally reuse the
            # discovery sell response for a different token amount.
            quote = cached[0] if input_mint == cached[0].input_mint else cached[1]
            age = time.monotonic() - quote.quoted_at_monotonic
            payload = quote.raw_response
            payload_amount = int(payload.get("inAmount") or 0) if payload else 0
            payload_input = str(payload.get("inputMint") or "") if payload else ""
            payload_output = str(payload.get("outputMint") or "") if payload else ""
            fresh = 0.0 <= age <= self._discovery_quote_max_age_seconds
            exact = (
                payload is not None
                and payload_input == input_mint
                and payload_output == output_mint
                and payload_amount == amount
            )
            if fresh and exact:
                telemetry.increment("live_discovery_quote_reuses")
                telemetry.observe("live_discovery_quote_age_ms", age * 1000.0)
                return payload
            telemetry.increment("live_discovery_quote_rejections")
            if not fresh:
                telemetry.increment("live_discovery_quote_stale")
            elif not exact:
                telemetry.increment("live_discovery_quote_mismatch")

        # Safety fallback: if the discovery route is stale or no longer matches
        # the exact execution input, obtain a new live quote and keep the final
        # profitability gate below the caller intact.
        return await super()._quote(input_mint, output_mint, amount, venue)
