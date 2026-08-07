"""Regression tests for PositionManager exit rules: stop loss, trailing stop,
time-based exit and the newly-wired sell-on-volume-drop rule."""
import datetime as dt

import pytest

from app.execution.onchain.jupiter import JupiterClient
from app.execution.router import ExecutionRouter
from app.positions.manager import PositionManager
from app.storage import repository as repo
from app.storage.database import init_db


class FakeNotifier:
    def __getattr__(self, name):
        async def _noop(*args, **kwargs):
            return None

        return _noop


@pytest.fixture(autouse=True)
async def _init_database():
    await init_db()
    yield


@pytest.fixture
def manager():
    return PositionManager(FakeNotifier(), anoncoin=None, execution_router=ExecutionRouter(JupiterClient("https://quote-api.jup.ag/v6")))


async def _make_token(mint: str, source: str = "mock_simulated"):
    from app.scoring.rules import TokenSnapshot

    await repo.save_token(
        TokenSnapshot(mint=mint, ticker_symbol=mint[:6], ticker_name=mint, source=source, created_on=dt.datetime.now(dt.timezone.utc))
    )


async def _make_rule(**overrides):
    from app.scoring.rules import RuleParams

    overrides.setdefault("stop_loss_pct", 0)
    params = RuleParams(**overrides)
    return await repo.create_rule(params, created_by=1, activate=True)


async def _closed_for(mint: str):
    return [p for p in await repo.get_closed_positions() if p.mint == mint]


async def _open_for(mint: str):
    return [p for p in await repo.get_open_positions() if p.mint == mint]


@pytest.mark.asyncio
async def test_stop_loss_closes_position(manager, monkeypatch):
    mint = "StopLossMint111"
    await _make_token(mint)
    rule = await _make_rule(stop_loss_pct=20.0)
    position = await repo.create_position(mint, rule.id, "paper", entry_price_usd=1.0, amount_tokens=100, amount_sol_invested=1.0)

    monkeypatch.setattr("app.positions.manager.get_current_price_usd", _fixed_price(0.75))
    monkeypatch.setattr("app.positions.manager.get_current_volume_usd", _fixed_volume(0.0))

    await manager.check_position(position)

    closed = await _closed_for(mint)
    assert len(closed) == 1
    assert closed[0].close_reason == "stop loss hit"


@pytest.mark.asyncio
async def test_trailing_stop_closes_position_after_peak_drop(manager, monkeypatch):
    mint = "TrailingStopMint222"
    await _make_token(mint)
    rule = await _make_rule(trailing_stop_pct=10.0)
    position = await repo.create_position(mint, rule.id, "paper", entry_price_usd=1.0, amount_tokens=100, amount_sol_invested=1.0)

    monkeypatch.setattr("app.positions.manager.get_current_price_usd", _fixed_price(2.0))
    monkeypatch.setattr("app.positions.manager.get_current_volume_usd", _fixed_volume(0.0))
    await manager.check_position(position)
    position = (await _open_for(mint))[0]
    assert position.peak_price_usd == 2.0

    monkeypatch.setattr("app.positions.manager.get_current_price_usd", _fixed_price(1.7))
    await manager.check_position(position)

    closed = await _closed_for(mint)
    assert len(closed) == 1
    assert closed[0].close_reason == "trailing stop hit"


@pytest.mark.asyncio
async def test_time_based_exit_closes_position_after_max_age(manager, monkeypatch):
    mint = "TimeExitMint333"
    await _make_token(mint)
    rule = await _make_rule(time_based_exit_seconds=1)
    position = await repo.create_position(mint, rule.id, "paper", entry_price_usd=1.0, amount_tokens=100, amount_sol_invested=1.0)
    await repo.update_position(position.id, opened_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5))
    position = (await _open_for(mint))[0]

    monkeypatch.setattr("app.positions.manager.get_current_price_usd", _fixed_price(1.0))
    monkeypatch.setattr("app.positions.manager.get_current_volume_usd", _fixed_volume(0.0))

    await manager.check_position(position)

    closed = await _closed_for(mint)
    assert len(closed) == 1
    assert closed[0].close_reason == "time-based exit"


@pytest.mark.asyncio
async def test_volume_drop_closes_position_after_peak_volume_falls(manager, monkeypatch):
    mint = "VolumeDropMint444"
    await _make_token(mint)
    rule = await _make_rule(sell_on_volume_drop_pct=50.0)
    position = await repo.create_position(
        mint, rule.id, "paper", entry_price_usd=1.0, amount_tokens=100, amount_sol_invested=1.0,
        entry_volume_24h_usd=10000.0,
    )

    monkeypatch.setattr("app.positions.manager.get_current_price_usd", _fixed_price(1.0))
    monkeypatch.setattr("app.positions.manager.get_current_volume_usd", _fixed_volume(15000.0))
    await manager.check_position(position)
    position = (await _open_for(mint))[0]
    assert position.peak_volume_24h_usd == 15000.0

    monkeypatch.setattr("app.positions.manager.get_current_volume_usd", _fixed_volume(6000.0))
    await manager.check_position(position)

    closed = await _closed_for(mint)
    assert len(closed) == 1
    assert closed[0].close_reason == "volume drop exit"


@pytest.mark.asyncio
async def test_healthy_position_with_no_triggers_stays_open(manager, monkeypatch):
    mint = "HealthyMint555"
    await _make_token(mint)
    rule = await _make_rule(sell_on_volume_drop_pct=50.0, trailing_stop_pct=30.0)
    position = await repo.create_position(
        mint, rule.id, "paper", entry_price_usd=1.0, amount_tokens=100, amount_sol_invested=1.0,
        entry_volume_24h_usd=10000.0,
    )

    monkeypatch.setattr("app.positions.manager.get_current_price_usd", _fixed_price(1.1))
    monkeypatch.setattr("app.positions.manager.get_current_volume_usd", _fixed_volume(9500.0))

    await manager.check_position(position)

    assert len(await _open_for(mint)) == 1
    assert len(await _closed_for(mint)) == 0


async def test_failed_sell_leaves_position_open_instead_of_closing_it(manager, monkeypatch):
    """Regression test: a sell that fails on-chain (adapter returns
    success=False) used to still reduce remaining_pct and close the
    position anyway, permanently losing track of tokens that were never
    actually sold. It should now leave the position untouched so it gets
    retried on the next check_position cycle."""
    from app.execution.base import OrderResult
    from app.execution.paper import PaperExecutionAdapter

    mint = "FailedSellMint666"
    await _make_token(mint)
    rule = await _make_rule(stop_loss_pct=20.0)
    position = await repo.create_position(mint, rule.id, "paper", entry_price_usd=1.0, amount_tokens=100, amount_sol_invested=1.0)

    monkeypatch.setattr("app.positions.manager.get_current_price_usd", _fixed_price(0.75))
    monkeypatch.setattr("app.positions.manager.get_current_volume_usd", _fixed_volume(0.0))

    async def _failing_sell(self, token, amount_tokens, sell_pct):
        return OrderResult(success=False, status="failed", error_message="slippage tolerance exceeded")

    monkeypatch.setattr(PaperExecutionAdapter, "sell", _failing_sell)

    await manager.check_position(position)

    still_open = await _open_for(mint)
    assert len(still_open) == 1
    assert still_open[0].remaining_pct == 100.0
    assert len(await _closed_for(mint)) == 0


def _fixed_price(price: float):
    async def _fn(client, token, tick=0):
        return price, False

    return _fn


def _fixed_volume(volume: float):
    async def _fn(client, token, tick=0):
        return volume, False

    return _fn
