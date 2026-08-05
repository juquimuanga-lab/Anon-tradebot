"""Regression test for a bug where `_solscan_backoff_until` / `_solscan_failure_count`
were referenced in `ScannerService._enrich_with_solscan` without ever being
initialized in `__init__`, crashing every single scan cycle with
`AttributeError: 'ScannerService' object has no attribute '_solscan_backoff_until'`.
"""
import pytest

from app.connectors.solscan import SolscanClient
from app.execution.onchain.jupiter import JupiterClient
from app.execution.router import ExecutionRouter
from app.scanners.scanner import ScannerService
from app.scoring.rules import TokenSnapshot
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
def scanner():
    return ScannerService(
        notifier=FakeNotifier(),
        position_manager=None,
        anoncoin=None,
        solscan=SolscanClient("https://pro-api.solscan.io/v2.0", None),
        execution_router=ExecutionRouter(JupiterClient("https://quote-api.jup.ag/v6")),
    )


def test_backoff_state_is_initialized_on_construction(scanner):
    assert scanner._solscan_failure_count == 0
    assert scanner._solscan_backoff_until is None


@pytest.mark.asyncio
async def test_enrich_with_solscan_never_raises_attribute_error(scanner):
    token = TokenSnapshot(mint="RegressionMint1", source="mock_simulated")

    # No Solscan key configured -> both calls fail every time; this used to
    # raise AttributeError instead of degrading gracefully.
    enriched = await scanner._enrich_with_solscan(token)
    assert enriched.mint == token.mint


@pytest.mark.asyncio
async def test_repeated_solscan_failures_trigger_backoff_without_crashing(scanner):
    token = TokenSnapshot(mint="RegressionMint2", source="mock_simulated")

    for _ in range(3):
        await scanner._enrich_with_solscan(token)

    assert scanner._solscan_failure_count >= 3
    assert scanner._solscan_backoff_until is not None

    # While backed off, enrichment should short-circuit and return the token untouched.
    result = await scanner._enrich_with_solscan(token)
    assert result is token
