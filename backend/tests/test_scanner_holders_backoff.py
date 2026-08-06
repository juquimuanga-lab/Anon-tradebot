"""Regression test for a bug where `_holders_backoff_until` / `_holders_failure_count`
were referenced in `ScannerService._enrich_holders` without ever being
initialized in `__init__`, crashing every single scan cycle with
`AttributeError: 'ScannerService' object has no attribute '_holders_backoff_until'`.

Formerly test_scanner_solscan_backoff.py, testing the Solscan connector -
replaced with Helius (see app/connectors/helius.py) since Solscan's holders
endpoint required an active paid Pro API plan. Delete the old
test_scanner_solscan_backoff.py file when adding this one.
"""
import pytest

from app.connectors.helius import HeliusClient
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
        holders_client=HeliusClient("https://mainnet.helius-rpc.com", None),
        execution_router=ExecutionRouter(JupiterClient("https://quote-api.jup.ag/v6")),
    )


def test_backoff_state_is_initialized_on_construction(scanner):
    assert scanner._holders_failure_count == 0
    assert scanner._holders_backoff_until is None


@pytest.mark.asyncio
async def test_enrich_holders_never_raises_attribute_error(scanner):
    token = TokenSnapshot(mint="RegressionMint1", source="mock_simulated")

    # No Helius key configured -> the call fails every time; this used to
    # raise AttributeError instead of degrading gracefully.
    enriched = await scanner._enrich_holders(token)
    assert enriched.mint == token.mint


@pytest.mark.asyncio
async def test_repeated_holders_failures_trigger_backoff_without_crashing(scanner):
    token = TokenSnapshot(mint="RegressionMint2", source="mock_simulated")

    for _ in range(3):
        await scanner._enrich_holders(token)

    assert scanner._holders_failure_count >= 3
    assert scanner._holders_backoff_until is not None

    # While backed off, enrichment should short-circuit and return the token untouched.
    result = await scanner._enrich_holders(token)
    assert result is token
