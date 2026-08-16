"""Admin-only Solana token-account rent recovery.

This module accepts one confirmed BUY or SELL transaction signature and scans that trade for token
accounts that are still open, owned by the sniper wallet, and have an exact
zero token balance. Eligible accounts are closed with the standard SPL Token
CloseAccount instruction so their account lamports are returned to the wallet.

No private key is accepted from Telegram. The recovery path reuses a Keypair
already held by the application/secrets layer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from app.execution.onchain.solana_rpc import (
    LAMPORTS_PER_SOL,
    SolanaTxError,
    _rpc_request,
    send_and_confirm,
)
from app.security.redact import redact_text

try:
    from app.security.secrets_manager import secrets_manager
except Exception:  # pragma: no cover - defensive import for isolated tests
    secrets_manager = None


logger = logging.getLogger("app.execution.onchain.rent_recovery")

MAX_INPUT_SIGNATURES = 20
MAX_CANDIDATE_ACCOUNTS = 100
ACCOUNTS_PER_RECOVERY_TX = 8

TOKEN_PROGRAM_ID = Pubkey.from_string(
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
)
TOKEN_2022_PROGRAM_ID = Pubkey.from_string(
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
)


@dataclass(frozen=True)
class RecoverableAccount:
    address: str
    token_program: str
    lamports: int
    source_signature: str
    mint: Optional[str]

    @property
    def sol(self) -> float:
        return self.lamports / LAMPORTS_PER_SOL


@dataclass(frozen=True)
class BurnCloseAccount:
    address: str
    token_program: str
    mint: str
    amount: int
    decimals: int
    ui_amount: Optional[float]
    lamports: int
    close_authority: str

    @property
    def rent_sol(self) -> float:
        return self.lamports / LAMPORTS_PER_SOL


@dataclass
class RecoveryScan:
    signatures: list[str]
    wallet: str
    eligible: list[RecoverableAccount]
    skipped: list[str]

    @property
    def gross_lamports(self) -> int:
        return sum(item.lamports for item in self.eligible)

    @property
    def gross_sol(self) -> float:
        return self.gross_lamports / LAMPORTS_PER_SOL


class RentRecoveryError(Exception):
    """Raised when rent recovery cannot safely proceed."""


def _normalize_signatures(text: str) -> list[str]:
    values = []
    for part in text.replace("\n", ",").split(","):
        value = part.strip()
        if not value:
            continue
        if value not in values:
            values.append(value)
    if not values:
        raise RentRecoveryError(
            "Send at least one BUY or SELL transaction signature."
        )
    if len(values) > MAX_INPUT_SIGNATURES:
        raise RentRecoveryError(
            f"Send no more than {MAX_INPUT_SIGNATURES} BUY/SELL transaction signatures at a time."
        )
    return values


def _decode_keypair(value: Any) -> Optional[Keypair]:
    if value is None:
        return None
    if isinstance(value, Keypair):
        return value

    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if len(raw) == 64:
            return Keypair.from_bytes(raw)
        if len(raw) == 32:
            return Keypair.from_seed(raw)
        try:
            return Keypair.from_base58_string(raw.decode("utf-8"))
        except Exception:
            return None

    if isinstance(value, (list, tuple)):
        try:
            raw = bytes(int(x) for x in value)
            if len(raw) == 64:
                return Keypair.from_bytes(raw)
            if len(raw) == 32:
                return Keypair.from_seed(raw)
        except Exception:
            return None
        return None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return _decode_keypair(parsed)
        except Exception:
            pass
        try:
            return Keypair.from_base58_string(text)
        except Exception:
            pass
        try:
            raw = bytes.fromhex(text.removeprefix("0x"))
            return _decode_keypair(raw)
        except Exception:
            return None

    return None


async def _maybe_call(method: Any, user_id: str) -> Any:
    try:
        result = method(user_id)
        if hasattr(result, "__await__"):
            return await result
        return result
    except TypeError:
        try:
            result = method()
            if hasattr(result, "__await__"):
                return await result
            return result
        except Exception:
            return None
    except Exception:
        return None


async def resolve_wallet_keypair(application: Any, user_id: str) -> Keypair:
    """Resolve the already-connected wallet without accepting a key from chat."""

    # Preferred: an explicit getter on the existing encrypted secrets layer.
    if secrets_manager is not None:
        for method_name in (
            "get_wallet_private_key",
            "get_wallet_key",
            "get_private_key",
            "load_wallet_private_key",
            "load_wallet_key",
        ):
            method = getattr(secrets_manager, method_name, None)
            if method is None:
                continue
            value = await _maybe_call(method, user_id)
            keypair = _decode_keypair(value)
            if keypair is not None:
                return keypair

    # Fallback: reuse a Keypair already held by the running execution layer.
    bot_data = getattr(application, "bot_data", {}) or {}
    direct_names = (
        "wallet_keypair",
        "keypair",
        "sniper_keypair",
        "trading_keypair",
    )
    for name in direct_names:
        keypair = _decode_keypair(bot_data.get(name))
        if keypair is not None:
            return keypair

    manager_names = (
        "wallet_manager",
        "wallet_service",
        "execution_manager",
        "position_manager",
        "wallet",
    )
    method_names = (
        "get_keypair",
        "get_wallet_keypair",
        "get_wallet_private_key",
        "get_private_key",
        "load_keypair",
        "keypair_for_user",
    )
    for manager_name in manager_names:
        manager = bot_data.get(manager_name)
        if manager is None:
            continue
        for method_name in method_names:
            method = getattr(manager, method_name, None)
            if method is None:
                continue
            value = await _maybe_call(method, user_id)
            keypair = _decode_keypair(value)
            if keypair is not None:
                return keypair
        for attr_name in direct_names:
            keypair = _decode_keypair(getattr(manager, attr_name, None))
            if keypair is not None:
                return keypair

    raise RentRecoveryError(
        "The connected sniper wallet could not be accessed by the rent-recovery module. "
        "No private key was requested from Telegram."
    )


async def _get_transaction(rpc_url: str, signature: str) -> Optional[dict]:
    try:
        result = await _rpc_request(
            rpc_url,
            "getTransaction",
            [
                signature,
                {
                    "commitment": "confirmed",
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        return result
    except SolanaTxError:
        raise
    except Exception as exc:
        raise RentRecoveryError(
            f"Transaction lookup failed: {redact_text(str(exc))}"
        ) from exc


async def _get_accounts(rpc_url: str, addresses: list[str]) -> list[Optional[dict]]:
    if not addresses:
        return []
    result = await _rpc_request(
        rpc_url,
        "getMultipleAccounts",
        [
            addresses,
            {
                "encoding": "jsonParsed",
                "commitment": "confirmed",
            },
        ],
    )
    if not result:
        return [None] * len(addresses)
    return result.get("value", [])


def _token_balance_candidates(
    transaction: dict,
    wallet: str,
) -> list[tuple[str, Optional[str]]]:
    """Find token accounts touched by the supplied trade.

    A BUY is an important special case: immediately after the BUY, the token
    account normally has a NON-zero balance.  The old implementation only
    considered zero balances in the historical transaction, so BUY signatures
    could never discover the account that later became empty after a SELL.

    We therefore collect wallet-owned token accounts from both token-balance
    records (regardless of historical amount) and explicit ATA create/createIdempotent
    instructions in the transaction.  We then verify the account's *current*
    on-chain balance before allowing closure.
    """
    message = (transaction.get("transaction") or {}).get("message") or {}
    account_keys = list(message.get("accountKeys") or [])
    meta = transaction.get("meta") or {}
    loaded = meta.get("loadedAddresses") or {}
    account_keys.extend(loaded.get("writable") or [])
    account_keys.extend(loaded.get("readonly") or [])

    candidates: dict[str, Optional[str]] = {}

    def key_at(index: Any) -> Optional[str]:
        try:
            key = account_keys[int(index)]
        except (TypeError, ValueError, IndexError):
            return None
        return str(key.get("pubkey")) if isinstance(key, dict) else str(key)

    # 1. Token balances: include BOTH pre and post accounts, regardless of
    # historical amount. The current account state is checked later.
    for bucket_name in ("preTokenBalances", "postTokenBalances"):
        for item in meta.get(bucket_name) or []:
            if str(item.get("owner") or "") != wallet:
                continue
            address = key_at(item.get("accountIndex"))
            if address:
                candidates[address] = (
                    str(item.get("mint")) if item.get("mint") else None
                )

    # 2. Explicit Associated Token Account creation. This is the key path for
    # a BUY that created the ATA during the supplied transaction.
    instructions: list[dict] = []
    instructions.extend(message.get("instructions") or [])
    for group in meta.get("innerInstructions") or []:
        instructions.extend(group.get("instructions") or [])

    for instruction in instructions:
        if instruction.get("program") != "spl-associated-token-account":
            continue
        parsed = instruction.get("parsed") or {}
        if parsed.get("type") not in ("create", "createIdempotent"):
            continue
        info = parsed.get("info") or {}
        account = info.get("account")
        owner = info.get("wallet") or info.get("owner") or info.get("source")
        mint = info.get("mint")
        if not account or str(owner or "") != wallet:
            continue
        candidates[str(account)] = str(mint) if mint else candidates.get(str(account))

    return list(candidates.items())


def _account_is_token_program(owner: str) -> bool:
    return owner in {
        str(TOKEN_PROGRAM_ID),
        str(TOKEN_2022_PROGRAM_ID),
    }


def _parsed_token_info(
    account: dict,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    data = account.get("data") or {}
    parsed = data.get("parsed") or {}
    info = parsed.get("info") or {}
    token_amount = info.get("tokenAmount") or {}
    lamports = account.get("lamports")
    try:
        rent = int(lamports) if lamports is not None else None
    except (TypeError, ValueError):
        rent = None
    return (
        str(info.get("owner")) if info.get("owner") else None,
        str(info.get("mint")) if info.get("mint") else None,
        str(info.get("closeAuthority")) if info.get("closeAuthority") else None,
        rent,
    )


def _is_buy_or_sell_transaction(transaction: dict, wallet: str) -> bool:
    """Return True when the supplied transaction looks like a wallet buy/sell.

    We require both a wallet-owned SPL token balance change and a native SOL
    balance change. This accepts either direction (buy or sell) while rejecting
    unrelated signatures such as token-only transfers or recovery transactions.
    """
    meta = transaction.get("meta") or {}
    pre_tokens = meta.get("preTokenBalances") or []
    post_tokens = meta.get("postTokenBalances") or []

    token_changed = False
    by_key = {}
    for item in pre_tokens + post_tokens:
        if str(item.get("owner") or "") != wallet:
            continue
        key = (item.get("accountIndex"), item.get("mint"))
        amount = str((item.get("uiTokenAmount") or {}).get("amount") or "0")
        by_key.setdefault(key, {})["prepost"] = by_key.get(key, {}).get("prepost", []) + [amount]

    # Compare aggregate wallet-owned token amounts by mint.
    pre_by_mint = {}
    post_by_mint = {}
    for item in pre_tokens:
        if str(item.get("owner") or "") == wallet:
            mint = str(item.get("mint") or "")
            pre_by_mint[mint] = pre_by_mint.get(mint, 0) + int((item.get("uiTokenAmount") or {}).get("amount") or 0)
    for item in post_tokens:
        if str(item.get("owner") or "") == wallet:
            mint = str(item.get("mint") or "")
            post_by_mint[mint] = post_by_mint.get(mint, 0) + int((item.get("uiTokenAmount") or {}).get("amount") or 0)
    for mint in set(pre_by_mint) | set(post_by_mint):
        if pre_by_mint.get(mint, 0) != post_by_mint.get(mint, 0):
            token_changed = True
            break

    # Find the wallet account index and compare SOL/lamport balance.
    message = (transaction.get("transaction") or {}).get("message") or {}
    keys = list(message.get("accountKeys") or [])
    wallet_index = None
    for i, key in enumerate(keys):
        address = key.get("pubkey") if isinstance(key, dict) else key
        if str(address) == wallet:
            wallet_index = i
            break
    sol_changed = False
    if wallet_index is not None:
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        if wallet_index < len(pre) and wallet_index < len(post):
            sol_changed = int(pre[wallet_index]) != int(post[wallet_index])

    return token_changed and sol_changed


async def scan_rent_recovery(
    rpc_url: str,
    wallet_pubkey: str,
    signature_text: str,
) -> RecoveryScan:
    signatures = _normalize_signatures(signature_text)
    wallet = str(Pubkey.from_string(wallet_pubkey))
    candidate_map: dict[str, tuple[Optional[str], str]] = {}
    skipped: list[str] = []

    for signature in signatures:
        transaction = await _get_transaction(rpc_url, signature)
        if not transaction:
            skipped.append(f"{signature}: transaction not found")
            continue
        if (transaction.get("meta") or {}).get("err") is not None:
            skipped.append(f"{signature}: transaction failed on-chain")
            continue
        if not _is_buy_or_sell_transaction(transaction, wallet):
            raise RentRecoveryError(
                "The supplied signature is not recognized as a BUY or SELL transaction for the connected sniper wallet. "
                "Send the actual buy signature or sell signature from the trade."
            )
        for address, mint in _token_balance_candidates(transaction, wallet):
            if len(candidate_map) >= MAX_CANDIDATE_ACCOUNTS:
                skipped.append("candidate limit reached")
                break
            candidate_map.setdefault(address, (mint, signature))

    addresses = list(candidate_map)
    accounts = await _get_accounts(rpc_url, addresses)
    eligible: list[RecoverableAccount] = []

    for address, account in zip(addresses, accounts):
        mint, source_signature = candidate_map[address]
        if not account:
            skipped.append(f"{address}: account is already closed or unavailable")
            continue

        program_owner = str(account.get("owner") or "")
        if not _account_is_token_program(program_owner):
            skipped.append(f"{address}: not an SPL/Token-2022 account")
            continue

        info_owner, parsed_mint, close_authority, rent_lamports = _parsed_token_info(account)
        effective_close_authority = close_authority or info_owner
        if effective_close_authority != wallet:
            skipped.append(f"{address}: close authority is not the sniper wallet")
            continue

        data = ((account.get("data") or {}).get("parsed") or {}).get("info") or {}
        raw_amount = ((data.get("tokenAmount") or {}).get("amount"))
        if raw_amount is None:
            skipped.append(f"{address}: token amount could not be verified")
            continue
        try:
            raw_amount_int = int(raw_amount)
        except (TypeError, ValueError):
            skipped.append(f"{address}: invalid token amount")
            continue
        if raw_amount_int != 0:
            skipped.append(f"{address}: token balance is not zero")
            continue
        if rent_lamports is None or rent_lamports <= 0:
            skipped.append(f"{address}: no recoverable lamports")
            continue

        eligible.append(
            RecoverableAccount(
                address=address,
                token_program=program_owner,
                lamports=rent_lamports,
                source_signature=source_signature,
                mint=parsed_mint or mint,
            )
        )

    return RecoveryScan(
        signatures=signatures,
        wallet=wallet,
        eligible=eligible,
        skipped=skipped,
    )


def _normalize_account_addresses(text: str) -> list[str]:
    values: list[str] = []
    for part in text.replace("\n", ",").split(","):
        value = part.strip()
        if not value:
            continue
        try:
            value = str(Pubkey.from_string(value))
        except Exception as exc:
            raise RentRecoveryError(f"Invalid token account address: {value}") from exc
        if value not in values:
            values.append(value)
    if not values:
        raise RentRecoveryError("Send at least one token-account address.")
    if len(values) > MAX_INPUT_SIGNATURES:
        raise RentRecoveryError(
            f"Send no more than {MAX_INPUT_SIGNATURES} token-account addresses at a time."
        )
    return values


async def scan_burn_close(
    rpc_url: str,
    wallet_pubkey: str,
    account_text: str,
) -> list[BurnCloseAccount]:
    """Validate explicit token accounts for a full-balance burn followed by close."""
    wallet = str(Pubkey.from_string(wallet_pubkey))
    addresses = _normalize_account_addresses(account_text)
    accounts = await _get_accounts(rpc_url, addresses)
    eligible: list[BurnCloseAccount] = []

    for address, account in zip(addresses, accounts):
        if not account:
            raise RentRecoveryError(f"{address}: token account is closed or unavailable.")

        program_owner = str(account.get("owner") or "")
        if not _account_is_token_program(program_owner):
            raise RentRecoveryError(f"{address}: not an SPL/Token-2022 token account.")

        info_owner, mint, close_authority, rent_lamports = _parsed_token_info(account)
        if not info_owner or info_owner != wallet:
            raise RentRecoveryError(f"{address}: token account owner is not the connected wallet.")

        effective_close_authority = close_authority or info_owner
        if effective_close_authority != wallet:
            raise RentRecoveryError(f"{address}: close authority is not the connected wallet.")

        data = ((account.get("data") or {}).get("parsed") or {}).get("info") or {}
        token_amount = data.get("tokenAmount") or {}
        if token_amount.get("isNative") is not None:
            raise RentRecoveryError(
                f"{address}: wrapped/native SOL token accounts are not handled by /burnclose; use /recoverent or close them separately."
            )
        raw_amount = token_amount.get("amount")
        decimals = token_amount.get("decimals")
        if raw_amount is None or decimals is None or not mint:
            raise RentRecoveryError(f"{address}: token balance/mint metadata could not be verified.")

        try:
            amount = int(raw_amount)
            decimals_int = int(decimals)
        except (TypeError, ValueError) as exc:
            raise RentRecoveryError(f"{address}: invalid token amount metadata.") from exc

        if amount < 0:
            raise RentRecoveryError(f"{address}: invalid negative token balance.")
        if rent_lamports is None or rent_lamports <= 0:
            raise RentRecoveryError(f"{address}: no recoverable account rent.")

        ui_amount = token_amount.get("uiAmount")
        try:
            ui_amount = float(ui_amount) if ui_amount is not None else None
        except (TypeError, ValueError):
            ui_amount = None

        eligible.append(
            BurnCloseAccount(
                address=address,
                token_program=program_owner,
                mint=str(mint),
                amount=amount,
                decimals=decimals_int,
                ui_amount=ui_amount,
                lamports=rent_lamports,
                close_authority=effective_close_authority,
            )
        )

    return eligible


def _burn_checked_instruction(account: BurnCloseAccount, wallet: Pubkey) -> Instruction:
    """Create SPL Token/Token-2022 BurnChecked with the mint's exact decimals."""
    program_id = Pubkey.from_string(account.token_program)
    data = bytes([15]) + int(account.amount).to_bytes(8, "little") + bytes([account.decimals])
    return Instruction(
        program_id,
        data,
        [
            AccountMeta(Pubkey.from_string(account.address), False, True),
            AccountMeta(Pubkey.from_string(account.mint), False, True),
            AccountMeta(wallet, True, False),
        ],
    )


def _burn_close_instructions(account: BurnCloseAccount, wallet: Pubkey) -> list[Instruction]:
    instructions: list[Instruction] = []
    if account.amount > 0:
        instructions.append(_burn_checked_instruction(account, wallet))
    instructions.append(
        Instruction(
            Pubkey.from_string(account.token_program),
            bytes([9]),
            [
                AccountMeta(Pubkey.from_string(account.address), False, True),
                AccountMeta(wallet, False, True),
                AccountMeta(wallet, True, False),
            ],
        )
    )
    return instructions


async def burn_and_close(
    rpc_url: str,
    keypair: Keypair,
    accounts: list[BurnCloseAccount],
) -> dict[str, Any]:
    """Burn full balances and close the same token accounts in confirmed batches."""
    if not accounts:
        return {
            "burned_accounts": 0,
            "closed": 0,
            "recovered_lamports": 0,
            "recovered_sol": 0.0,
            "transactions": [],
            "failed": [],
        }

    wallet = keypair.pubkey()
    results: list[str] = []
    failed: list[str] = []
    burned_accounts = 0
    closed_accounts = 0
    recovered_lamports = 0

    for start in range(0, len(accounts), ACCOUNTS_PER_RECOVERY_TX):
        batch = accounts[start : start + ACCOUNTS_PER_RECOVERY_TX]
        try:
            blockhash, last_valid = await _latest_blockhash(rpc_url)
            instructions: list[Instruction] = []
            for account in batch:
                instructions.extend(_burn_close_instructions(account, wallet))

            tx = Transaction.new_signed_with_payer(
                instructions, wallet, [keypair], Hash.from_string(blockhash)
            )
            signature = await send_and_confirm(
                rpc_url, bytes(tx), last_valid_block_height=last_valid
            )
            results.append(signature)
            burned_accounts += sum(1 for item in batch if item.amount > 0)
            closed_accounts += len(batch)
            recovered_lamports += sum(item.lamports for item in batch)
            logger.info(
                "burn_close_batch_confirmed",
                extra={
                    "signature": signature,
                    "accounts": len(batch),
                    "recovered_lamports": sum(item.lamports for item in batch),
                },
            )
        except Exception as exc:
            message = redact_text(str(exc))
            failed.append(f"{len(batch)} account(s): {message}")
            logger.exception("burn_close_batch_failed")

    return {
        "burned_accounts": burned_accounts,
        "closed": closed_accounts,
        "recovered_lamports": recovered_lamports,
        "recovered_sol": recovered_lamports / LAMPORTS_PER_SOL,
        "transactions": results,
        "failed": failed,
    }


async def _latest_blockhash(rpc_url: str) -> tuple[str, int]:
    result = await _rpc_request(
        rpc_url,
        "getLatestBlockhash",
        [{"commitment": "confirmed"}],
    )
    value = (result or {}).get("value") or {}
    blockhash = value.get("blockhash")
    last_valid = value.get("lastValidBlockHeight")
    if not blockhash or last_valid is None:
        raise RentRecoveryError("RPC did not return a usable recent blockhash.")
    return str(blockhash), int(last_valid)


def _close_instruction(account: RecoverableAccount, wallet: Pubkey) -> Instruction:
    program_id = Pubkey.from_string(account.token_program)
    return Instruction(
        program_id,
        bytes([9]),  # SPL Token / Token-2022 CloseAccount instruction.
        [
            AccountMeta(
                Pubkey.from_string(account.address),
                False,
                True,
            ),
            AccountMeta(wallet, False, True),
            AccountMeta(wallet, True, False),
        ],
    )


async def recover_rent(
    rpc_url: str,
    keypair: Keypair,
    scan: RecoveryScan,
) -> dict[str, Any]:
    if not scan.eligible:
        return {
            "closed": 0,
            "gross_lamports": 0,
            "gross_sol": 0.0,
            "transactions": [],
            "failed": [],
        }

    wallet = keypair.pubkey()
    results: list[str] = []
    failed: list[str] = []
    closed_accounts = 0
    recovered_lamports = 0

    for start in range(0, len(scan.eligible), ACCOUNTS_PER_RECOVERY_TX):
        batch = scan.eligible[start : start + ACCOUNTS_PER_RECOVERY_TX]
        try:
            blockhash, last_valid = await _latest_blockhash(rpc_url)
            instructions = [_close_instruction(item, wallet) for item in batch]
            tx = Transaction.new_signed_with_payer(
                instructions,
                wallet,
                [keypair],
                Hash.from_string(blockhash),
            )
            signature = await send_and_confirm(
                rpc_url,
                bytes(tx),
                last_valid_block_height=last_valid,
            )
            results.append(signature)
            closed_accounts += len(batch)
            recovered_lamports += sum(item.lamports for item in batch)
            logger.info(
                "rent_recovery_batch_confirmed",
                extra={
                    "signature": signature,
                    "accounts": len(batch),
                    "gross_lamports": sum(item.lamports for item in batch),
                },
            )
        except Exception as exc:
            message = redact_text(str(exc))
            failed.append(
                f"{len(batch)} account(s): {message}"
            )
            logger.exception("rent_recovery_batch_failed")

    return {
        "closed": closed_accounts,
        "eligible": len(scan.eligible),
        "gross_lamports": scan.gross_lamports,
        "gross_sol": scan.gross_sol,
        "recovered_lamports": recovered_lamports,
        "recovered_sol": recovered_lamports / LAMPORTS_PER_SOL,
        "transactions": results,
        "failed": failed,
    }


def format_sol(lamports: int) -> str:
    return f"{lamports / LAMPORTS_PER_SOL:.9f}".rstrip("0").rstrip(".")
