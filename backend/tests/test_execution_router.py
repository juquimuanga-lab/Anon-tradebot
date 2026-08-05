import pytest

from app.execution.onchain.jupiter import JupiterClient
from app.execution.paper import PaperExecutionAdapter
from app.execution.router import ExecutionRouter
from app.execution.wallet_live import NoWalletConnectedAdapter, WalletExecutionAdapter
from app.security.secrets_manager import secrets_manager
from app.storage.database import init_db


@pytest.fixture(autouse=True)
async def _init_database():
    await init_db()
    yield


@pytest.fixture
def router():
    return ExecutionRouter(JupiterClient("https://quote-api.jup.ag/v6"))


@pytest.mark.asyncio
async def test_paper_mode_always_returns_paper_adapter(router):
    adapter = await router.get_adapter("paper", owner_user_id=None)
    assert isinstance(adapter, PaperExecutionAdapter)


@pytest.mark.asyncio
async def test_live_mode_without_owner_fails_safely(router):
    adapter = await router.get_adapter("live", owner_user_id=None)
    assert isinstance(adapter, NoWalletConnectedAdapter)


@pytest.mark.asyncio
async def test_live_mode_without_connected_wallet_fails_safely(router):
    adapter = await router.get_adapter("live", owner_user_id=999999)
    assert isinstance(adapter, NoWalletConnectedAdapter)


@pytest.mark.asyncio
async def test_live_mode_with_connected_wallet_returns_wallet_adapter(router):
    from solders.keypair import Keypair
    import base58

    keypair = Keypair()
    await secrets_manager.set_wallet_private_key(42, base58.b58encode(bytes(keypair)).decode())

    adapter = await router.get_adapter("live", owner_user_id=42)

    assert isinstance(adapter, WalletExecutionAdapter)


@pytest.mark.asyncio
async def test_no_wallet_adapter_buy_and_sell_fail_without_crashing():
    adapter = NoWalletConnectedAdapter("wallet not connected")

    buy_result = await adapter.buy(token=None, amount_sol=1.0)
    sell_result = await adapter.sell(token=None, amount_tokens=1.0, sell_pct=100)

    assert buy_result.success is False
    assert sell_result.success is False
