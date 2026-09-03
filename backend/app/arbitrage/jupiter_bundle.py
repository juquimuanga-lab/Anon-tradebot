"""Build Jupiter swap transactions from instructions with optional Jito tips."""
from __future__ import annotations

import base64
import os
from typing import Any

import httpx
from solders.address_lookup_table_account import AddressLookupTable, AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from app.execution.onchain.solana_rpc import _rpc_request


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


async def _load_lookup_tables(
    rpc_url: str,
    addresses: list[str],
) -> list[AddressLookupTableAccount]:
    """Load Jupiter address lookup tables without relying on solana-py helpers."""
    lookup_tables: list[AddressLookupTableAccount] = []
    for address in addresses:
        try:
            result = await _rpc_request(
                rpc_url,
                "getAccountInfo",
                [address, {"encoding": "base64", "commitment": "processed"}],
            )
            value = (result or {}).get("value")
            if not value:
                raise JupiterInstructionBuildError(
                    f"missing address lookup table: {address}"
                )
            data = value.get("data")
            if not isinstance(data, list) or len(data) < 1:
                raise JupiterInstructionBuildError(
                    f"address lookup table has no binary data: {address}"
                )
            raw_data = base64.b64decode(data[0])
            table = AddressLookupTable.deserialize(raw_data)
            lookup_tables.append(
                AddressLookupTableAccount(
                    key=Pubkey.from_string(address),
                    addresses=table.addresses,
                )
            )
        except JupiterInstructionBuildError:
            raise
        except Exception as exc:
            raise JupiterInstructionBuildError(
                f"failed to load address lookup table {address}: {exc}"
            ) from exc
    return lookup_tables


def _priority_fee_config() -> dict[str, Any]:
    """Return an economical Jupiter priority-fee policy for small arb trades.

    Jito bundle selection is driven by the Jito tip, while Solana priority fees
    are still paid by the transactions. A veryHigh 1 SOL cap per leg can make
    small positive spreads uneconomic, so use a lower default and expose both
    controls as environment variables.
    """
    level = os.getenv("ARBITRAGE_LIVE_PRIORITY_LEVEL", "low").strip()
    if level not in {"low", "medium", "high", "veryHigh"}:
        level = "low"
    try:
        max_lamports = int(
            os.getenv("ARBITRAGE_LIVE_MAX_PRIORITY_FEE_LAMPORTS", "100000")
        )
    except ValueError:
        max_lamports = 100000
    max_lamports = max(1000, min(max_lamports, 1_000_000))
    return {
        "priorityLevelWithMaxLamports": {
            "priorityLevel": level,
            "maxLamports": max_lamports,
        }
    }


async def _build(
    *,
    base_url: str,
    quote_response: dict[str, Any],
    user_pubkey: str,
    keypair: Keypair,
    rpc_url: str,
    api_key: str | None,
    tip_account: str | None,
    tip_lamports: int,
    timeout_seconds: float,
) -> tuple[bytes, int]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    body = {
        "userPublicKey": user_pubkey,
        "quoteResponse": quote_response,
        "wrapAndUnwrapSol": True,
        "useSharedAccounts": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": _priority_fee_config(),
    }
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_seconds) as client:
        response = await client.post("/swap-instructions", json=body, headers=headers)
    if response.status_code != 200:
        raise JupiterInstructionBuildError(
            f"Jupiter swap-instructions failed: HTTP {response.status_code}"
        )
    payload = response.json()
    if payload.get("error"):
        raise JupiterInstructionBuildError(
            f"Jupiter instruction build failed: {payload['error']}"
        )

    instructions: list[Instruction] = []
    for key in ("computeBudgetInstructions", "setupInstructions", "otherInstructions"):
        instructions.extend(_instruction(item) for item in (payload.get(key) or []))
    swap_instruction = payload.get("swapInstruction")
    if not swap_instruction:
        raise JupiterInstructionBuildError("Jupiter response is missing swapInstruction")
    instructions.append(_instruction(swap_instruction))
    cleanup = payload.get("cleanupInstruction")
    if cleanup:
        instructions.append(_instruction(cleanup))

    if tip_account is not None:
        if tip_lamports < 1000:
            raise JupiterInstructionBuildError("Jito tip must be at least 1000 lamports")
        instructions.append(
            transfer(
                TransferParams(
                    from_pubkey=keypair.pubkey(),
                    to_pubkey=Pubkey.from_string(tip_account),
                    lamports=tip_lamports,
                )
            )
        )

    lookup_tables = await _load_lookup_tables(
        rpc_url,
        [str(address) for address in (payload.get("addressLookupTableAddresses") or [])],
    )

    try:
        latest = await _rpc_request(
            rpc_url,
            "getLatestBlockhash",
            [{"commitment": "processed"}],
        )
        blockhash = ((latest or {}).get("value") or {}).get("blockhash")
        if not blockhash:
            raise JupiterInstructionBuildError("RPC returned no recent blockhash")
        message = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=instructions,
            address_lookup_table_accounts=lookup_tables,
            recent_blockhash=Hash.from_string(str(blockhash)),
        )
        signed = VersionedTransaction(message, [keypair])
    except JupiterInstructionBuildError:
        raise
    except Exception as exc:
        raise JupiterInstructionBuildError(f"failed to compile Jupiter transaction: {exc}") from exc

    return bytes(signed), int(payload.get("prioritizationFeeLamports") or 0)


async def build_signed_swap_without_tip(
    *,
    base_url: str,
    quote_response: dict[str, Any],
    user_pubkey: str,
    keypair: Keypair,
    rpc_url: str,
    api_key: str | None = None,
    timeout_seconds: float = 10.0,
) -> tuple[bytes, int]:
    return await _build(
        base_url=base_url,
        quote_response=quote_response,
        user_pubkey=user_pubkey,
        keypair=keypair,
        rpc_url=rpc_url,
        api_key=api_key,
        tip_account=None,
        tip_lamports=0,
        timeout_seconds=timeout_seconds,
    )


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
    if tip_lamports < 1000:
        raise JupiterInstructionBuildError("Jito tip must be at least 1000 lamports")
    return await _build(
        base_url=base_url,
        quote_response=quote_response,
        user_pubkey=user_pubkey,
        keypair=keypair,
        rpc_url=rpc_url,
        api_key=api_key,
        tip_account=tip_account,
        tip_lamports=tip_lamports,
        timeout_seconds=timeout_seconds,
    )
