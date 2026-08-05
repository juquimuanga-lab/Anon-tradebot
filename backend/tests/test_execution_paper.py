import pytest

from app.execution.paper import PaperExecutionAdapter
from app.scoring.rules import TokenSnapshot
from app.storage.database import init_db
from app.storage.repository import get_or_create_bot_state, update_bot_state


@pytest.fixture(autouse=True)
async def _init_database():
    await init_db()
    await update_bot_state(paper_balance_sol=10.0)
    yield


def make_token(price_usd: float = 0.001) -> TokenSnapshot:
    return TokenSnapshot(mint="MintPaperTest", ticker_symbol="PPR", price_usd=price_usd)


@pytest.mark.asyncio
async def test_paper_buy_deducts_balance_and_fills():
    adapter = PaperExecutionAdapter()
    state_before = await get_or_create_bot_state()

    result = await adapter.buy(make_token(), amount_sol=1.0)

    state_after = await get_or_create_bot_state()
    assert result.success is True
    assert result.status == "filled"
    assert state_after.paper_balance_sol == pytest.approx(state_before.paper_balance_sol - 1.0)


@pytest.mark.asyncio
async def test_paper_buy_rejected_when_balance_insufficient():
    await update_bot_state(paper_balance_sol=0.01)
    adapter = PaperExecutionAdapter()

    result = await adapter.buy(make_token(), amount_sol=5.0)

    assert result.success is False
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_paper_sell_applies_simulated_slippage():
    adapter = PaperExecutionAdapter()
    token = make_token(price_usd=0.002)

    result = await adapter.sell(token, amount_tokens=100, sell_pct=100)

    assert result.success is True
    assert result.price_usd < token.price_usd


@pytest.mark.asyncio
async def test_paper_credit_balance_increases_wallet():
    adapter = PaperExecutionAdapter()
    before = await get_or_create_bot_state()

    await adapter.credit_balance(2.5)

    after = await get_or_create_bot_state()
    assert after.paper_balance_sol == pytest.approx(before.paper_balance_sol + 2.5)
