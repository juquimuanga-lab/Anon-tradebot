"""Live execution adapter for Anoncoin.

Anoncoin's public API docs do not expose a buy/sell trade endpoint yet (only
coin discovery/profile/create-coin, all marked "Coming Soon"). This adapter is
deliberately isolated: it will call ANONCOIN_TRADE_ENDPOINT if an operator
configures one once Anoncoin ships it, and otherwise fails loudly and safely
instead of ever faking a fill.
"""
import logging

from app.config.settings import settings
from app.connectors.anoncoin import AnoncoinClient
from app.execution.base import ExecutionAdapter, ExecutionNotAvailableError, OrderResult
from app.scoring.rules import TokenSnapshot

logger = logging.getLogger("app.execution.live")


class AnoncoinLiveExecutionAdapter(ExecutionAdapter):
    mode = "live"

    def __init__(self, client: AnoncoinClient):
        self._client = client

    async def buy(self, token: TokenSnapshot, amount_sol: float) -> OrderResult:
        if not settings.anoncoin_trade_endpoint:
            logger.warning("live_execution_unavailable", extra={"mint": token.mint, "side": "buy"})
            return OrderResult(
                success=False,
                status="failed",
                error_message="Live trade execution endpoint is not published by Anoncoin yet.",
            )
        raise ExecutionNotAvailableError("Configured trade endpoint call not implemented")

    async def sell(self, token: TokenSnapshot, amount_tokens: float, sell_pct: float) -> OrderResult:
        if not settings.anoncoin_trade_endpoint:
            logger.warning("live_execution_unavailable", extra={"mint": token.mint, "side": "sell"})
            return OrderResult(
                success=False,
                status="failed",
                error_message="Live trade execution endpoint is not published by Anoncoin yet.",
            )
        raise ExecutionNotAvailableError("Configured trade endpoint call not implemented")
