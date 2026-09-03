import types

import pytest

from app.arbitrage import continuous_telegram as module


class DummyState:
    def __init__(self, mode="live", trading_enabled=True):
        self.mode = mode
        self.trading_enabled = trading_enabled


class DummyExecution:
    def __init__(self, success=False, reason="test failure"):
        self.success = success
        self.reason = reason
        self.estimated_net_profit_lamports = 0
        self.bundle_id = None
        self.transaction_signatures = []
        self.input_lamports = 20_000_000


class DummyExecutor:
    def __init__(self):
        self.calls = []

    async def execute_unrestricted(self, owner_user_id, token_mint, amount_sol):
        self.calls.append((owner_user_id, token_mint, amount_sol))
        return DummyExecution()


class DummyBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


@pytest.mark.asyncio
async def test_profitable_arb_fans_out_to_each_live_admin(monkeypatch):
    monkeypatch.setattr(module.settings, "telegram_admin_ids", [101, 202], raising=False)

    async def get_state(admin_id):
        return DummyState("live", True)

    async def get_key(admin_id):
        return f"key-{admin_id}"

    monkeypatch.setattr(module.repo, "get_or_create_bot_state", get_state)
    monkeypatch.setattr(module.secrets_manager, "get_wallet_private_key", get_key)

    executor = DummyExecutor()
    monkeypatch.setattr(module, "live_executor", executor)

    candidate = types.SimpleNamespace(
        symbol="TEST",
        tier="A",
        token_mint="Mint111",
    )
    opportunity = types.SimpleNamespace(
        executable=True,
        gross_profit_bps=10.0,
        execution_cost_bps=1.0,
        required_gross_profit_bps=1.0,
        net_profit_bps=9.0,
        net_profit_atomic=1_800_000,
    )
    discovery = types.SimpleNamespace(
        opportunity=opportunity,
        amount_sol=0.02,
        buy_quote=types.SimpleNamespace(route_id="buy-route"),
        sell_quote=types.SimpleNamespace(route_id="sell-route"),
    )
    result = types.SimpleNamespace(discoveries=((candidate, discovery),))
    bot = DummyBot()

    await module._on_profitable(result, bot)

    assert executor.calls == [
        (101, "Mint111", 0.02),
        (202, "Mint111", 0.02),
    ]
    assert [m["chat_id"] for m in bot.messages] == [101, 202]


@pytest.mark.asyncio
async def test_non_live_admin_is_skipped(monkeypatch):
    monkeypatch.setattr(module.settings, "telegram_admin_ids", [101, 202], raising=False)

    async def get_state(admin_id):
        return DummyState("live" if admin_id == 101 else "paper", True)

    async def get_key(admin_id):
        return f"key-{admin_id}"

    monkeypatch.setattr(module.repo, "get_or_create_bot_state", get_state)
    monkeypatch.setattr(module.secrets_manager, "get_wallet_private_key", get_key)

    executor = DummyExecutor()
    monkeypatch.setattr(module, "live_executor", executor)

    candidate = types.SimpleNamespace(symbol="TEST", tier="A", token_mint="Mint111")
    opportunity = types.SimpleNamespace(
        executable=True,
        gross_profit_bps=10.0,
        execution_cost_bps=1.0,
        required_gross_profit_bps=1.0,
        net_profit_bps=9.0,
        net_profit_atomic=1_800_000,
    )
    discovery = types.SimpleNamespace(
        opportunity=opportunity,
        amount_sol=0.02,
        buy_quote=types.SimpleNamespace(route_id="buy-route"),
        sell_quote=types.SimpleNamespace(route_id="sell-route"),
    )
    result = types.SimpleNamespace(discoveries=((candidate, discovery),))
    bot = DummyBot()

    await module._on_profitable(result, bot)

    assert executor.calls == [(101, "Mint111", 0.02)]
    assert [m["chat_id"] for m in bot.messages] == [101]


@pytest.mark.asyncio
async def test_admin_without_wallet_is_skipped(monkeypatch):
    monkeypatch.setattr(module.settings, "telegram_admin_ids", [101, 202], raising=False)

    async def get_state(admin_id):
        return DummyState("live", True)

    async def get_key(admin_id):
        return "key-101" if admin_id == 101 else None

    monkeypatch.setattr(module.repo, "get_or_create_bot_state", get_state)
    monkeypatch.setattr(module.secrets_manager, "get_wallet_private_key", get_key)

    executor = DummyExecutor()
    monkeypatch.setattr(module, "live_executor", executor)

    candidate = types.SimpleNamespace(symbol="TEST", tier="A", token_mint="Mint111")
    opportunity = types.SimpleNamespace(
        executable=True,
        gross_profit_bps=10.0,
        execution_cost_bps=1.0,
        required_gross_profit_bps=1.0,
        net_profit_bps=9.0,
        net_profit_atomic=1_800_000,
    )
    discovery = types.SimpleNamespace(
        opportunity=opportunity,
        amount_sol=0.02,
        buy_quote=types.SimpleNamespace(route_id="buy-route"),
        sell_quote=types.SimpleNamespace(route_id="sell-route"),
    )
    result = types.SimpleNamespace(discoveries=((candidate, discovery),))
    bot = DummyBot()

    await module._on_profitable(result, bot)

    assert executor.calls == [(101, "Mint111", 0.02)]
    assert [m["chat_id"] for m in bot.messages] == [101]
