"""Regression tests for per-admin rule isolation.

Verifies the core guarantee: one admin creating, activating, or trading on
their own rule never touches another admin's active rule, positions, or
rate limits. Each test uses its own unique admin IDs / mints so tests don't
interfere with each other in the shared on-disk test database.
"""
import datetime as dt

import pytest

from app.scoring.rules import RuleParams, TokenSnapshot
from app.storage import repository as repo
from app.storage.database import init_db


@pytest.fixture(autouse=True)
async def _init_database():
    await init_db()
    yield


async def test_creating_a_rule_does_not_deactivate_another_admins_rule():
    admin_a, admin_b = 91001, 91002

    rule_a = await repo.create_rule(RuleParams(name="rule-a"), admin_a, activate=True)
    rule_b = await repo.create_rule(RuleParams(name="rule-b"), admin_b, activate=True)

    active_a = await repo.get_active_rule_for(admin_a)
    active_b = await repo.get_active_rule_for(admin_b)

    assert active_a is not None and active_a.id == rule_a.id
    assert active_b is not None and active_b.id == rule_b.id


async def test_get_all_active_rules_includes_every_admin_with_one():
    admin_a, admin_b = 91011, 91012
    await repo.create_rule(RuleParams(name="rule-a"), admin_a, activate=True)
    await repo.create_rule(RuleParams(name="rule-b"), admin_b, activate=True)

    active_ids = {r.created_by for r in await repo.get_all_active_rules()}
    assert admin_a in active_ids
    assert admin_b in active_ids


async def test_activate_rule_for_admin_rejects_another_admins_rule_id():
    owner, intruder = 91021, 91022
    owned_rule = await repo.create_rule(RuleParams(name="owned"), owner, activate=False)

    result = await repo.activate_rule_for_admin(owned_rule.id, intruder)

    assert result is None
    # confirm it's genuinely untouched, not just that the call returned None
    still = await repo.get_active_rule_for(owner)
    assert still is None  # was never activated in the first place


async def test_activate_rule_for_admin_switches_between_own_rules_only():
    admin_a, admin_b = 91031, 91032
    rule_a1 = await repo.create_rule(RuleParams(name="a1"), admin_a, activate=True)
    rule_a2 = await repo.create_rule(RuleParams(name="a2"), admin_a, activate=False)
    rule_b1 = await repo.create_rule(RuleParams(name="b1"), admin_b, activate=True)

    switched = await repo.activate_rule_for_admin(rule_a2.id, admin_a)
    assert switched is not None and switched.id == rule_a2.id

    active_a = await repo.get_active_rule_for(admin_a)
    active_b = await repo.get_active_rule_for(admin_b)
    assert active_a.id == rule_a2.id  # switched
    assert active_b.id == rule_b1.id  # admin B completely untouched


async def test_has_open_or_pending_position_scoped_by_owner():
    mint = "IsolationTestMint1"
    admin_a, admin_b = 91041, 91042
    await repo.save_token(TokenSnapshot(mint=mint, ticker_symbol="ISO1", source="mock_simulated",
                                         created_on=dt.datetime.now(dt.timezone.utc)))
    await repo.create_position(mint, rule_id=None, mode="paper", entry_price_usd=1.0,
                                amount_tokens=10.0, amount_sol_invested=1.0, owner_user_id=admin_a)

    assert await repo.has_open_or_pending_position(mint, admin_a) is True
    assert await repo.has_open_or_pending_position(mint, admin_b) is False


async def test_recent_buy_count_and_cooldown_scoped_by_owner():
    mint = "IsolationTestMint2"
    admin_a, admin_b = 91051, 91052
    await repo.create_order(mint, "buy", "paper", "filled", 0.5, 1.0,
                             rule_id=None, owner_user_id=admin_a)

    assert await repo.recent_buy_count(hours=1.0, owner_user_id=admin_a) == 1
    assert await repo.recent_buy_count(hours=1.0, owner_user_id=admin_b) == 0

    assert await repo.seconds_since_last_buy(owner_user_id=admin_a) is not None
    assert await repo.seconds_since_last_buy(owner_user_id=admin_b) is None
