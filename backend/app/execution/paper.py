"""Paper trading execution adapter - simulates fills, no real funds at risk."""
import logging

from app.execution.base import ExecutionAdapter, OrderResult
from app.scoring.rules import TokenSnapshot
from app.storage.repository import get_or_create_bot_state, update_bot_state

logger = logging.getLogger("app.execution.paper")

SIMULATED_SLIPPAGE_PCT = 0.5


class PaperExecutionAdapter(ExecutionAdapter):
    mode = "paper"

    async def buy(self, token: TokenSnapshot, amount_sol: float) -> OrderResult:
        state = await get_or_create_bot_state()
        if state.paper_balance_sol < amount_sol:
            return OrderResult(success=False, status="failed", error_message="Insufficient paper balance")
        fill_price = token.price_usd * (1 + SIMULATED_SLIPPAGE_PCT / 100)
        await update_bot_state(paper_balance_sol=state.paper_balance_sol - amount_sol)
        logger.info("paper_buy_filled", extra={"mint": token.mint, "amount_sol": amount_sol})
        return OrderResult(success=True, status="filled", price_usd=fill_price, tx_signature=f"paper-{token.mint[:8]}")

    async def sell(self, token: TokenSnapshot, amount_tokens: float, sell_pct: float) -> OrderResult:
        fill_price = token.price_usd * (1 - SIMULATED_SLIPPAGE_PCT / 100)
        logger.info("paper_sell_filled", extra={"mint": token.mint, "sell_pct": sell_pct})
        return OrderResult(success=True, status="filled", price_usd=fill_price, tx_signature=f"paper-sell-{token.mint[:8]}")

    async def credit_balance(self, amount_sol: float) -> None:
        state = await get_or_create_bot_state()
        await update_bot_state(paper_balance_sol=state.paper_balance_sol + amount_sol)
