"""Shared Solana RPC helpers.

Responsibilities:
- Sign legacy and versioned Solana transactions.
- Broadcast signed transactions.
- Keep checking a submitted transaction until it confirms or expires.
- Detect transactions that landed but failed on-chain.
- Provide useful transaction diagnostics.
- Provide SOL balance lookups.
- Provide SPL-token balance lookups for position reconciliation.

The wallet Keypair only exists in this Python process.
It is never passed to the Node.js DBC builder.
"""

import asyncio
import base64
import logging
import time
from typing import Any, Optional

import httpx
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.hash import Hash as SoldersHash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction as LegacyTransaction
from solders.transaction import VersionedTransaction

from app.security.redact import redact_text


logger = logging.getLogger(
    "app.execution.onchain.solana_rpc"
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RPC_COMMITMENT = "confirmed"

# Token-balance reconciliation can use processed data because we want to
# detect an external/manual sale as quickly as practical.
TOKEN_BALANCE_COMMITMENT = "processed"

# How frequently we check transaction status.
STATUS_POLL_INTERVAL_SECONDS = 0.25

# Minimum time between rebroadcasts.
RESEND_INTERVAL_SECONDS = 0.50

# Timeout for an individual JSON-RPC request.
RPC_REQUEST_TIMEOUT_SECONDS = 8.0

# We control rebroadcasting ourselves.
RPC_MAX_RETRIES = 0

# Keep preflight enabled while we establish reliable execution.
SKIP_PREFLIGHT = False

LAMPORTS_PER_SOL = 1_000_000_000


class SolanaTxError(Exception):
    """Raised when a Solana transaction cannot safely be considered successful."""


# ---------------------------------------------------------------------------
# Generic JSON-RPC
# ---------------------------------------------------------------------------

async def _rpc_request(
    rpc_url: str,
    method: str,
    params: list[Any],
) -> Any:
    """Execute a raw Solana JSON-RPC request."""

    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000),
        "method": method,
        "params": params,
    }

    try:
        async with httpx.AsyncClient(
            timeout=RPC_REQUEST_TIMEOUT_SECONDS
        ) as client:

            response = await client.post(
                rpc_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                },
            )

            response.raise_for_status()

            body = response.json()

    except Exception as exc:
        raise SolanaTxError(
            redact_text(
                f"RPC request failed ({method}): {exc}"
            )
        ) from exc

    if "error" in body:
        raise SolanaTxError(
            redact_text(
                f"RPC {method} error: {body['error']}"
            )
        )

    return body.get("result")


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def sign_legacy_transaction(
    tx_b64: str,
    blockhash_str: str,
    keypair: Keypair,
) -> bytes:
    """Decode and sign a legacy transaction.

    Used by the Meteora DBC path.

    The transaction has already been constructed by build_tx.js, including
    its Compute Budget priority-fee instruction and recent blockhash.
    """

    try:
        raw = base64.b64decode(tx_b64)

        tx = LegacyTransaction.from_bytes(raw)

        tx.sign(
            [keypair],
            SoldersHash.from_string(blockhash_str),
        )

        return bytes(tx)

    except Exception as exc:
        raise SolanaTxError(
            redact_text(
                f"legacy transaction signing failed: {exc}"
            )
        ) from exc


def sign_versioned_transaction(
    tx_b64: str,
    keypair: Keypair,
) -> bytes:
    """Decode and sign a versioned transaction.

    Used by the Jupiter execution path.
    """

    try:
        raw = base64.b64decode(tx_b64)

        unsigned = VersionedTransaction.from_bytes(raw)

        signed = VersionedTransaction(
            unsigned.message,
            [keypair],
        )

        return bytes(signed)

    except Exception as exc:
        raise SolanaTxError(
            redact_text(
                f"versioned transaction signing failed: {exc}"
            )
        ) from exc


# ---------------------------------------------------------------------------
# Signature extraction
# ---------------------------------------------------------------------------

def _extract_signature(
    signed_tx_bytes: bytes,
) -> str:
    """Extract the first signature from a signed transaction."""

    if not signed_tx_bytes:
        raise SolanaTxError(
            "signed transaction is empty"
        )

    # Meteora legacy transaction.
    try:
        tx = LegacyTransaction.from_bytes(
            signed_tx_bytes
        )

        if tx.signatures:
            return str(tx.signatures[0])

    except Exception:
        pass

    # Jupiter versioned transaction.
    try:
        tx = VersionedTransaction.from_bytes(
            signed_tx_bytes
        )

        if tx.signatures:
            return str(tx.signatures[0])

    except Exception:
        pass

    raise SolanaTxError(
        "unable to extract transaction signature"
    )


# ---------------------------------------------------------------------------
# Block height
# ---------------------------------------------------------------------------

async def _get_block_height(
    rpc_url: str,
) -> Optional[int]:
    """Get the current confirmed block height."""

    try:
        result = await _rpc_request(
            rpc_url,
            "getBlockHeight",
            [
                {
                    "commitment": RPC_COMMITMENT,
                }
            ],
        )

        if result is None:
            return None

        return int(result)

    except SolanaTxError as exc:
        logger.debug(
            "block_height_check_failed",
            extra={
                "error": redact_text(str(exc)),
            },
        )

        return None


# ---------------------------------------------------------------------------
# Signature status
# ---------------------------------------------------------------------------

async def _get_signature_status(
    rpc_url: str,
    signature: str,
) -> Optional[dict]:
    """Get the current status for a transaction signature.

    searchTransactionHistory=True allows the RPC to look beyond its recent
    status cache.
    """

    try:
        result = await _rpc_request(
            rpc_url,
            "getSignatureStatuses",
            [
                [signature],
                {
                    "searchTransactionHistory": True,
                },
            ],
        )

        if not result:
            return None

        values = result.get("value", [])

        if not values:
            return None

        return values[0]

    except SolanaTxError as exc:
        logger.debug(
            "signature_status_check_failed",
            extra={
                "signature": signature,
                "error": redact_text(str(exc)),
            },
        )

        return None


# ---------------------------------------------------------------------------
# Transaction details
# ---------------------------------------------------------------------------

async def _get_transaction_details(
    rpc_url: str,
    signature: str,
) -> Optional[dict]:
    """Retrieve confirmed transaction details for diagnostics."""

    try:
        return await _rpc_request(
            rpc_url,
            "getTransaction",
            [
                signature,
                {
                    "commitment": RPC_COMMITMENT,
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )

    except SolanaTxError:
        return None


def _format_transaction_error(
    status: Optional[dict],
    transaction: Optional[dict],
) -> str:
    """Format useful on-chain error information."""

    parts = []

    if status:
        status_error = status.get("err")

        if status_error is not None:
            parts.append(
                f"status.err={status_error}"
            )

    if transaction:
        meta = transaction.get("meta") or {}

        transaction_error = meta.get("err")

        if transaction_error is not None:
            parts.append(
                f"transaction.meta.err={transaction_error}"
            )

        logs = meta.get("logMessages")

        if logs:
            parts.append(
                "program logs:\n" +
                "\n".join(
                    str(line)
                    for line in logs[-20:]
                )
            )

    if not parts:
        return "unknown on-chain transaction error"

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# SPL TOKEN BALANCE
# ---------------------------------------------------------------------------

async def get_token_balance(
    rpc_url: str,
    owner_pubkey: str,
    token_mint: str,
) -> float:
    """Return the owner's current SPL-token balance.

    This is intentionally read directly from Solana RPC rather than relying
    on the bot's database.

    It is used to reconcile positions after:
        - manual Phantom sales
        - partial manual sales
        - failed/unknown sell attempts
        - other external wallet activity

    A balance of 0 means the wallet currently has no token accounts holding
    that mint.

    The lookup uses processed commitment because this is a monitoring/
    reconciliation read where low latency matters more than waiting for
    confirmation.
    """

    try:
        owner = str(
            Pubkey.from_string(owner_pubkey)
        )

        mint = str(
            Pubkey.from_string(token_mint)
        )

    except Exception as exc:
        raise SolanaTxError(
            redact_text(
                f"invalid owner or token mint: {exc}"
            )
        ) from exc

    try:
        result = await _rpc_request(
            rpc_url,
            "getTokenAccountsByOwner",
            [
                owner,
                {
                    "mint": mint,
                },
                {
                    "encoding": "jsonParsed",
                    "commitment": TOKEN_BALANCE_COMMITMENT,
                },
            ],
        )

    except SolanaTxError:
        raise

    except Exception as exc:
        raise SolanaTxError(
            redact_text(
                f"SPL token balance lookup failed: {exc}"
            )
        ) from exc

    if not result:
        return 0.0

    accounts = result.get(
        "value",
        [],
    )

    if not accounts:
        return 0.0

    total_balance = 0.0

    for account in accounts:

        try:
            parsed = (
                account
                .get("account", {})
                .get("data", {})
                .get("parsed", {})
            )

            info = parsed.get(
                "info",
                {},
            )

            token_amount = info.get(
                "tokenAmount",
                {},
            )

            ui_amount = token_amount.get(
                "uiAmount"
            )

            if ui_amount is not None:
                total_balance += float(
                    ui_amount
                )
                continue

            # Fallback for RPC responses that omit uiAmount.
            raw_amount = token_amount.get(
                "amount"
            )

            decimals = token_amount.get(
                "decimals"
            )

            if (
                raw_amount is not None
                and decimals is not None
            ):
                total_balance += (
                    int(raw_amount)
                    / (
                        10
                        ** int(decimals)
                    )
                )

        except Exception as exc:

            logger.warning(
                "token_account_parse_failed",
                extra={
                    "owner": owner_pubkey,
                    "mint": token_mint,
                    "error": redact_text(
                        str(exc)
                    ),
                },
            )

    return max(
        0.0,
        total_balance,
    )


async def get_token_balance_raw(
    rpc_url: str,
    owner_pubkey: str,
    token_mint: str,
) -> tuple[int, int]:
    """Return (raw token amount, decimals) for an SPL mint.

    This is useful when the execution layer needs exact integer token
    quantities rather than floating-point UI amounts.

    Multiple token accounts for the same mint are summed.
    """

    try:
        owner = str(
            Pubkey.from_string(owner_pubkey)
        )

        mint = str(
            Pubkey.from_string(token_mint)
        )

    except Exception as exc:
        raise SolanaTxError(
            redact_text(
                f"invalid owner or token mint: {exc}"
            )
        ) from exc

    result = await _rpc_request(
        rpc_url,
        "getTokenAccountsByOwner",
        [
            owner,
            {
                "mint": mint,
            },
            {
                "encoding": "jsonParsed",
                "commitment": TOKEN_BALANCE_COMMITMENT,
            },
        ],
    )

    if not result:
        return 0, 0

    accounts = result.get(
        "value",
        [],
    )

    if not accounts:
        return 0, 0

    total_raw = 0
    decimals = 0

    for account in accounts:

        try:

            parsed = (
                account
                .get("account", {})
                .get("data", {})
                .get("parsed", {})
            )

            info = parsed.get(
                "info",
                {},
            )

            token_amount = info.get(
                "tokenAmount",
                {},
            )

            raw_amount = token_amount.get(
                "amount"
            )

            account_decimals = token_amount.get(
                "decimals"
            )

            if raw_amount is None:
                continue

            total_raw += int(
                raw_amount
            )

            if account_decimals is not None:
                decimals = int(
                    account_decimals
                )

        except Exception as exc:

            logger.warning(
                "token_account_raw_parse_failed",
                extra={
                    "owner": owner_pubkey,
                    "mint": token_mint,
                    "error": redact_text(
                        str(exc)
                    ),
                },
            )

    return (
        max(0, total_raw),
        decimals,
    )


# ---------------------------------------------------------------------------
# Send transaction
# ---------------------------------------------------------------------------

async def _send_transaction(
    rpc_url: str,
    signed_tx_bytes: bytes,
) -> str:
    """Submit a signed transaction through Solana JSON-RPC.

    We explicitly control:
    - base64 encoding
    - preflight
    - preflight commitment
    - RPC retry behavior

    The application itself handles rebroadcasting.
    """

    encoded_transaction = base64.b64encode(
        signed_tx_bytes
    ).decode("ascii")

    result = await _rpc_request(
        rpc_url,
        "sendTransaction",
        [
            encoded_transaction,
            {
                "encoding": "base64",
                "skipPreflight": SKIP_PREFLIGHT,
                "preflightCommitment": RPC_COMMITMENT,
                "maxRetries": RPC_MAX_RETRIES,
            },
        ],
    )

    if not result:
        raise SolanaTxError(
            "sendTransaction returned no signature"
        )

    return str(result)


# ---------------------------------------------------------------------------
# Send + confirm
# ---------------------------------------------------------------------------

async def send_and_confirm(
    rpc_url: str,
    signed_tx_bytes: bytes,
    last_valid_block_height: int | None = None,
) -> str:
    """Send and track a Solana transaction until success or expiration.

    IMPORTANT:

    sendTransaction returning a signature is NOT considered a successful
    trade.

    The transaction is considered successful only when:
        confirmationStatus == confirmed/finalized
        AND
        err == None

    The transaction is considered failed when:
        - it lands with an on-chain error, OR
        - its blockhash actually expires without confirmation.
    """

    if not signed_tx_bytes:
        raise SolanaTxError(
            "cannot send empty signed transaction"
        )

    signature = _extract_signature(
        signed_tx_bytes
    )

    solscan_link = (
        f"https://solscan.io/tx/{signature}"
    )

    logger.info(
        "transaction_delivery_started",
        extra={
            "signature": signature,
            "last_valid_block_height": (
                last_valid_block_height
            ),
        },
    )

    last_send_time = 0.0
    send_count = 0

    while True:

        # ---------------------------------------------------------------
        # 1. Check transaction status FIRST
        # ---------------------------------------------------------------

        status = await _get_signature_status(
            rpc_url,
            signature,
        )

        if status is not None:

            transaction_error = status.get(
                "err"
            )

            # Transaction landed but program execution failed.
            if transaction_error is not None:

                transaction = (
                    await _get_transaction_details(
                        rpc_url,
                        signature,
                    )
                )

                details = _format_transaction_error(
                    status,
                    transaction,
                )

                raise SolanaTxError(
                    "transaction landed but failed on-chain:\n"
                    f"{details}\n"
                    f"{solscan_link}"
                )

            confirmation_status = status.get(
                "confirmationStatus"
            )

            if confirmation_status in (
                "confirmed",
                "finalized",
            ):

                logger.info(
                    "transaction_confirmed",
                    extra={
                        "signature": signature,
                        "confirmation_status": (
                            confirmation_status
                        ),
                        "send_count": send_count,
                    },
                )

                return signature

        # ---------------------------------------------------------------
        # 2. Check whether the transaction is still valid
        # ---------------------------------------------------------------

        current_height = await _get_block_height(
            rpc_url
        )

        if (
            last_valid_block_height is not None
            and current_height is not None
            and current_height > last_valid_block_height
        ):

            # One final status check at the expiry boundary.
            final_status = await _get_signature_status(
                rpc_url,
                signature,
            )

            if final_status is not None:

                final_error = final_status.get(
                    "err"
                )

                if final_error is not None:

                    transaction = (
                        await _get_transaction_details(
                            rpc_url,
                            signature,
                        )
                    )

                    details = _format_transaction_error(
                        final_status,
                        transaction,
                    )

                    raise SolanaTxError(
                        "transaction landed but failed on-chain:\n"
                        f"{details}\n"
                        f"{solscan_link}"
                    )

                final_confirmation = (
                    final_status.get(
                        "confirmationStatus"
                    )
                )

                if final_confirmation in (
                    "confirmed",
                    "finalized",
                ):
                    return signature

            raise SolanaTxError(
                "transaction blockhash expired before "
                "confirmation. "
                f"last_valid_block_height="
                f"{last_valid_block_height}, "
                f"current_block_height="
                f"{current_height}. "
                f"{solscan_link}"
            )

        # ---------------------------------------------------------------
        # 3. Rebroadcast while transaction remains valid
        # ---------------------------------------------------------------

        now = time.monotonic()

        should_send = (
            send_count == 0
            or (
                now - last_send_time
                >= RESEND_INTERVAL_SECONDS
            )
        )

        if should_send:

            try:

                returned_signature = (
                    await _send_transaction(
                        rpc_url,
                        signed_tx_bytes,
                    )
                )

                send_count += 1
                last_send_time = now

                if returned_signature != signature:

                    logger.warning(
                        "rpc_signature_mismatch",
                        extra={
                            "local_signature": signature,
                            "rpc_signature": (
                                returned_signature
                            ),
                        },
                    )

                logger.info(
                    "transaction_submitted",
                    extra={
                        "signature": signature,
                        "attempt": send_count,
                        "current_block_height": (
                            current_height
                        ),
                        "last_valid_block_height": (
                            last_valid_block_height
                        ),
                    },
                )

            except SolanaTxError as exc:

                error_text = str(exc)

                logger.warning(
                    "transaction_submission_error",
                    extra={
                        "signature": signature,
                        "attempt": send_count + 1,
                        "error": redact_text(
                            error_text
                        ),
                    },
                )

                lower_error = (
                    error_text.lower()
                )

                deterministic_errors = (
                    "simulation failed",
                    "instruction error",
                    "account not found",
                    "insufficient funds",
                    "insufficient lamports",
                    "invalid account",
                    "invalid transaction",
                    "signature verification",
                    "blockhash not found",
                )

                if any(
                    marker in lower_error
                    for marker in deterministic_errors
                ):
                    raise

        # ---------------------------------------------------------------
        # 4. Keep waiting.
        # ---------------------------------------------------------------

        await asyncio.sleep(
            STATUS_POLL_INTERVAL_SECONDS
        )


# ---------------------------------------------------------------------------
# SOL balance
# ---------------------------------------------------------------------------

async def get_sol_balance(
    rpc_url: str,
    pubkey_str: str,
) -> float:
    """Return wallet SOL balance."""

    async with AsyncClient(
        rpc_url,
        commitment=Confirmed,
    ) as client:

        try:

            response = await client.get_balance(
                Pubkey.from_string(
                    pubkey_str
                ),
                commitment=Confirmed,
            )

            return (
                response.value
                / LAMPORTS_PER_SOL
            )

        except Exception as exc:

            raise SolanaTxError(
                redact_text(
                    f"SOL balance lookup failed: {exc}"
                )
            ) from exc
