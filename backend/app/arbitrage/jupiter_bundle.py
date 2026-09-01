"""Build Jupiter swap transactions from instructions and embed a Jito tip.

This intentionally avoids Jupiter's precompiled /swap transaction because the
arbitrage executor must add a conditional Jito tip to the same transaction.
Jupiter's instruction endpoint exposes the swap instruction set and lookup
addresses, which we compile into a fresh v0 transaction and then sign.
"""
from __future__ import annotations

import base64
from typing import Any

import httpx
from solana.rpc.async_api import AsyncClient
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction


class JupiterInstructionBuildError(RuntimeError):
    """Raised when Jupiter instructions cannot be safely compiled."""


def _instruction(raw: dict[str, Any]) -> Instruction:
    try:
        accounts = [
            AccountMeta(
                pubkey=Pubkey.from_string(str(account["pubkey"])),
                is_signer=bool(account.get("isSigner", False)),
                is_writable=bool(account.get("isWritable", False)),
            )
            for account in (raw.get("accounts") or [])
        ]
        return Instruction(
            program_id=Pubkey.from_string(str(raw["programId"])),
            accounts=accounts,
            data=base64.b64decode(raw.get("data") or ""),
        )
    except Exception as exc:
        raise JupiterInstructionBuildError(f"invalid Jupiter instruction: {exc}") from exc


async def build_signed_swap_with_tip(
    *,
    base_url: str,
    quote_response: dict[str, Any],
    user_pubkey: str,
    keypair: Keypair,
    rpc_url: str,
    tip_account: str,
    tip_lamports: int,
    api_key: str | None = None,
    timeout_seconds: float = 10.0,
) -> tuple[bytes, int]:
    """Compile, tip, and sign one Jupiter swap transaction.

    The Jito tip is appended to the swap's instruction list, so the tip is
    committed only if that transaction succeeds. The returned priority fee is
    taken from Jupiter's response and is used by the caller's profit gate.
    """
    if tip_lamports < 1000:
        raise JupiterInstructionBuildError("Jito tip must be at least 1000 lamports")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key

    body = {
        "userPublicKey": user_pubkey,
        "quoteResponse": quote_response,
        "wrapAndUnwrapSol": True,
        "useSharedAccounts": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "priorityLevel": "veryHigh",
                "maxLamports": 1_000_000,
            }
        },
    }

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_seconds) as client:
        response = await client.post("/swap-instructions", json=body, headers=headers)
    if response.status_code != 200:
        raise JupiterInstructionBuildError(
            f"Jupiter swap-instructions failed: HTTP {response.status_code}"
        )

    payload = response.json()
    if payload.get("error"):
        raise JupiterInstructionBuildError(f"Jupiter instruction build failed: {payload['error']}")

    all_instructions: list[Instruction] = []
    for key in ("computeBudgetInstructions", "setupInstructions", "otherInstructions"):
        all_instructions.extend(_instruction(item) for item in (payload.get(key) or []))
    swap_instruction = payload.get("swapInstruction")
    if not swap_instruction:
        raise JupiterInstructionBuildError("Jupiter response is missing swapInstruction")
    all_instructions.append(_instruction(swap_instruction))
    cleanup = payload.get("cleanupInstruction")
    if cleanup:
        all_instructions.append(_instruction(cleanup))

    async with AsyncClient(rpc_url) as rpc:
        latest = await rpc.get_latest_blockhash(commitment="processed")
        lookup_tables: list[AddressLookupTableAccount] = []
        for address in payload.get("addressLookupTableAddresses") or []:
            result = await rpc.get_address_lookup_table(Pubkey.from_string(str(address)))
            if result.value is None:
                raise JupiterInstructionBuildError(f"missing address lookup table: {address}")
            lookup_tables.append(result.value)

    tip_instruction = transfer(
        TransferParams(
            from_pubkey=keypair.pubkey(),
            to_pubkey=Pubkey.from_string(tip_account),
            lamports=tip_lamports,
        )
    )
    all_instructions.append(tip_instruction)

    try:
        message = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=all_instructions,
            address_lookup_table_accounts=lookup_tables,
            recent_blockhash=Hash.from_string(str(latest.value.blockhash)),
        )
        signed = VersionedTransaction(message, [keypair])
    except Exception as exc:
        raise JupiterInstructionBuildError(f"failed to compile tipped Jupiter transaction: {exc}") from exc

    return bytes(signed), int(payload.get("prioritizationFeeLamports") or 0)
