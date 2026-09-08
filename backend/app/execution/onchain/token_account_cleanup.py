"""Automatic SPL token-account dust burn/close helper.

This module is deliberately independent from trade construction. It only
runs after a position is fully closed and recovers rent from the owner's
empty/dust token account.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from app.execution.onchain.solana_rpc import _rpc_request, send_and_confirm

logger = logging.getLogger("app.execution.onchain.token_account_cleanup")

TOKEN_PROGRAM_ID = Pubkey.from_string(
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
)
TOKEN_2022_PROGRAM_ID = Pubkey.from_string(
    "TokenzQdBNbLqP5VEh6kW1mM8q3fQnYv8w1q6p7s8t9u0"
)


async def cleanup_token_accounts(
    rpc_url: str,
    keypair: Keypair,
    mint: str,
    dust_threshold_tokens: float = 10.0,
) -> dict:
    """Burn dust and close every eligible token account for ``mint``.

    The RPC query uses jsonParsed token-account data so the helper works for
    both the legacy SPL Token program and Token-2022. A token account must be
    at or below the configured dust threshold before we touch it. Any non-dust
    balance is left alone.

    Returns counts rather than raising so cleanup can never turn a successful
    trade into a failed trade.
    """
    owner = keypair.pubkey()
    mint_pubkey = Pubkey.from_string(mint)
    threshold = max(0.0, float(dust_threshold_tokens))

    result = await _rpc_request(
        rpc_url,
        "getTokenAccountsByOwner",
        [
            str(owner),
            {"mint": str(mint_pubkey)},
            {"commitment": "confirmed", "encoding": "jsonParsed"},
        ],
    )

    accounts = list((result or {}).get("value") or [])
    closed = 0
    burned = 0
    skipped = 0

    for entry in accounts:
        try:
            account_pubkey = Pubkey.from_string(entry["pubkey"])
            account = entry.get("account") or {}
            program_id = Pubkey.from_string(str(account.get("owner")))

            if program_id not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
                skipped += 1
                continue

            parsed = ((account.get("data") or {}).get("parsed") or {})
            info = parsed.get("info") or {}
            token_amount = info.get("tokenAmount") or {}
            raw = int(token_amount.get("amount") or 0)
            decimals = int(token_amount.get("decimals") or 0)
            ui_amount = float(Decimal(raw) / (Decimal(10) ** decimals))

            # Native/wrapped SOL token accounts are not normal dust accounts
            # for this cleanup path.
            if bool(info.get("isNative")):
                skipped += 1
                continue

            if ui_amount > threshold:
                skipped += 1
                logger.info(
                    "token_account_cleanup_kept",
                    extra={
                        "mint": str(mint_pubkey),
                        "account": str(account_pubkey),
                        "balance_tokens": ui_amount,
                        "threshold_tokens": threshold,
                    },
                )
                continue

            instructions = []

            if raw > 0:
                # SPL Token / Token-2022 Burn instruction: tag 8 + u64 amount.
                instructions.append(
                    Instruction(
                        program_id,
                        bytes([8]) + raw.to_bytes(8, "little"),
                        [
                            AccountMeta(account_pubkey, False, True),
                            AccountMeta(mint_pubkey, False, False),
                            AccountMeta(owner, True, False),
                        ],
                    )
                )

            # CloseAccount instruction: tag 9. The reclaimed lamports go
            # directly back to the trading wallet.
            instructions.append(
                Instruction(
                    program_id,
                    bytes([9]),
                    [
                        AccountMeta(account_pubkey, False, True),
                        AccountMeta(owner, False, True),
                        AccountMeta(owner, True, False),
                    ],
                )
            )

            blockhash_result = await _rpc_request(
                rpc_url,
                "getLatestBlockhash",
                [{"commitment": "confirmed"}],
            )
            blockhash = Hash.from_string(
                blockhash_result["value"]["blockhash"]
            )
            last_valid_block_height = blockhash_result["value"].get(
                "lastValidBlockHeight"
            )

            tx = Transaction.new_signed_with_payer(
                instructions,
                owner,
                [keypair],
                blockhash,
            )

            signature = await send_and_confirm(
                rpc_url,
                bytes(tx),
                last_valid_block_height,
            )

            closed += 1
            if raw > 0:
                burned += 1

            logger.info(
                "token_account_burn_close_completed",
                extra={
                    "mint": str(mint_pubkey),
                    "account": str(account_pubkey),
                    "balance_tokens": ui_amount,
                    "burned": raw > 0,
                    "tx_signature": signature,
                },
            )

        except Exception as exc:
            logger.warning(
                "token_account_burn_close_failed",
                extra={
                    "mint": str(mint_pubkey),
                    "account": entry.get("pubkey"),
                    "error": str(exc),
                },
            )

    return {
        "accounts": len(accounts),
        "closed": closed,
        "burned": burned,
        "skipped": skipped,
    }
