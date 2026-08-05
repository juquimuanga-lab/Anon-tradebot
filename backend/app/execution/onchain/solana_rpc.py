"""Shared Solana RPC helpers: sign (legacy or versioned tx), broadcast,
confirm, and balance lookups. The wallet's Keypair only ever exists in this
process's memory - it is never passed to the Node.js DBC builder."""
import asyncio
import base64
import logging

from solana.rpc.async_api import AsyncClient
from solders.hash import Hash as SoldersHash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction as LegacyTransaction
from solders.transaction import VersionedTransaction

from app.security.redact import redact_text

logger = logging.getLogger("app.execution.onchain.solana_rpc")


class SolanaTxError(Exception):
    pass


def sign_legacy_transaction(tx_b64: str, blockhash_str: str, keypair: Keypair) -> bytes:
    raw = base64.b64decode(tx_b64)
    tx = LegacyTransaction.from_bytes(raw)
    tx.sign([keypair], SoldersHash.from_string(blockhash_str))
    return bytes(tx)


def sign_versioned_transaction(tx_b64: str, keypair: Keypair) -> bytes:
    raw = base64.b64decode(tx_b64)
    unsigned = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(unsigned.message, [keypair])
    return bytes(signed)


async def send_and_confirm(rpc_url: str, signed_tx_bytes: bytes, last_valid_block_height: int | None = None) -> str:
    async with AsyncClient(rpc_url) as client:
        try:
            resp = await client.send_raw_transaction(signed_tx_bytes)
        except Exception as exc:
            raise SolanaTxError(redact_text(f"broadcast failed: {exc}"))

        signature = resp.value
        try:
            await asyncio.wait_for(
                client.confirm_transaction(signature, last_valid_block_height=last_valid_block_height),
                timeout=45,
            )
        except asyncio.TimeoutError:
            logger.warning("confirmation_timed_out", extra={"signature": str(signature)})
        return str(signature)


async def get_sol_balance(rpc_url: str, pubkey_str: str) -> float:
    async with AsyncClient(rpc_url) as client:
        resp = await client.get_balance(Pubkey.from_string(pubkey_str))
        return resp.value / 1_000_000_000
