"""Regression tests for send_and_confirm.

The bug: a transaction that broadcast without an exception was always
reported as a successful, filled trade - even if it later failed on-chain
(e.g. slippage exceeded, extremely common on a token this fresh/thin) or
never confirmed at all. confirm_transaction's response was never actually
checked for an on-chain error, and a confirmation timeout was silently
swallowed and treated as success.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.execution.onchain.solana_rpc import SolanaTxError, send_and_confirm


def _mock_client(status_err=None, confirm_side_effect=None, send_side_effect=None):
    client = AsyncMock()
    if send_side_effect is not None:
        client.send_raw_transaction = AsyncMock(side_effect=send_side_effect)
    else:
        client.send_raw_transaction = AsyncMock(return_value=SimpleNamespace(value="sig123"))

    if confirm_side_effect is not None:
        client.confirm_transaction = AsyncMock(side_effect=confirm_side_effect)
    else:
        status = SimpleNamespace(err=status_err, confirmation_status="confirmed")
        client.confirm_transaction = AsyncMock(return_value=SimpleNamespace(value=[status]))

    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def test_successful_confirmation_returns_the_signature():
    client = _mock_client(status_err=None)
    with patch("app.execution.onchain.solana_rpc.AsyncClient", return_value=client):
        sig = await send_and_confirm("https://fake-rpc", b"fake-tx-bytes")
    assert sig == "sig123"


async def test_onchain_failure_raises_instead_of_reporting_success():
    """This is the exact bug that shipped: a tx that lands but fails
    on-chain must never be reported as a successful trade."""
    client = _mock_client(status_err={"InstructionError": [0, "slippage tolerance exceeded"]})
    with patch("app.execution.onchain.solana_rpc.AsyncClient", return_value=client):
        with pytest.raises(SolanaTxError, match="failed on-chain"):
            await send_and_confirm("https://fake-rpc", b"fake-tx-bytes")


async def test_confirmation_timeout_raises_instead_of_reporting_success():
    client = _mock_client(confirm_side_effect=asyncio.TimeoutError)
    with patch("app.execution.onchain.solana_rpc.AsyncClient", return_value=client):
        with pytest.raises(SolanaTxError, match="timed out"):
            await send_and_confirm("https://fake-rpc", b"fake-tx-bytes")


async def test_missing_confirmation_status_raises():
    client = _mock_client()
    client.confirm_transaction = AsyncMock(return_value=SimpleNamespace(value=[]))
    with patch("app.execution.onchain.solana_rpc.AsyncClient", return_value=client):
        with pytest.raises(SolanaTxError, match="unknown outcome"):
            await send_and_confirm("https://fake-rpc", b"fake-tx-bytes")


async def test_broadcast_failure_raises():
    client = _mock_client(send_side_effect=Exception("connection refused"))
    with patch("app.execution.onchain.solana_rpc.AsyncClient", return_value=client):
        with pytest.raises(SolanaTxError, match="broadcast failed"):
            await send_and_confirm("https://fake-rpc", b"fake-tx-bytes")
