"""Shared Solana RPC helpers.

Responsibilities:
- Sign legacy and versioned Solana transactions.
- Broadcast signed transactions through raw JSON-RPC.
- Use explicit Solana send options without relying on TxOpts.
- Keep retrying while the transaction's blockhash is valid.
- Detect actual on-chain execution failures.
- Detect blockhash expiration.
- Provide SOL balance lookups.

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
from solders.signature import Signature
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

# How often we check whether the transaction has landed.
STATUS_POLL_INTERVAL_SECONDS = 0.25

# How often we resend a still-valid transaction.
RESEND_INTERVAL_SECONDS = 0.50

# HTTP timeout for an individual RPC request.
RPC_REQUEST_TIMEOUT_SECONDS = 8.0

# We control retries ourselves so we can continue until blockhash expiry.
RPC_MAX_RETRIES = 0

# Keep preflight enabled for now.
#
# This is intentional while we are diagnosing the sniper. If preflight
# passes, we know the RPC was able to verify the signature and simulate the
# transaction before forwarding it.
SKIP_PREFLIGHT = False


class SolanaTxError(Exception):
    """Raised when a Solana transaction cannot safely be considered successful."""


# ---------------------------------------------------------------------------
# Generic JSON-RPC helper
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
        error = body["error"]

        raise SolanaTxError(
            redact_text(
                f"RPC {method} error: {error}"
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
    """Decode and sign a legacy Solana transaction.

    Used by the Meteora DBC path.

    build_tx.js has already constructed the transaction, including the
    Compute Budget priority-fee instruction and final blockhash.

    Signing preserves those instructions.
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
    """Decode and sign a versioned Solana transaction.

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
    """Get the current confirmed Solana block height."""

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
    """Get the transaction status from the RPC.

    searchTransactionHistory=True is intentional. Solana documents that
    getSignatureStatuses otherwise only searches the recent status cache.
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
    """Fetch confirmed transaction details for diagnostics."""

    try:
        result = await _rpc_request(
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

        return result

    except SolanaTxError:
        return None


def _format_transaction_error(
    status: Optional[dict],
    transaction: Optional[dict],
) -> str:
    """Create a useful on-chain failure message."""

    pieces = []

    if status:
        status_error = status.get("err")

        if status_error is not None:
            pieces.append(
                f"status.err={status_error}"
            )

    if transaction:
        meta = transaction.get("meta") or {}

        transaction_error = meta.get("err")

        if transaction_error is not None:
            pieces.append(
                f"transaction.meta.err={transaction_error}"
            )

        logs = meta.get("logMessages")

        if logs:
            useful_logs = [
                str(line)
                for line in logs[-20:]
            ]

            pieces.append(
                "logs:\n" +
                "\n".join(useful_logs)
            )

    if not pieces:
        return "unknown on-chain transaction error"

    return "\n".join(pieces)


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------

async def _send_transaction(
    rpc_url: str,
    signed_tx_bytes: bytes,
) -> str:
    """Send a signed transaction using explicit Solana RPC options.

    We use:
        encoding = base64
        skipPreflight = False
        preflightCommitment = confirmed
        maxRetries = 0

    maxRetries=0 is deliberate because this module controls resubmission
    itself and can continue doing so until lastValidBlockHeight expires.
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
    """Broadcast and confirm a signed Solana transaction.

    The transaction is repeatedly checked and resent while its original
    blockhash remains valid.

    We NEVER declare success merely because sendTransaction returned a
    signature.

    Success requires:
        confirmationStatus = confirmed/finalized
        AND
        err = null

    Failure requires:
        an actual on-chain error
        OR
        blockhash expiration
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
            "last_valid_block_height": last_valid_block_height,
        },
    )

    last_send_time = 0.0
    send_count = 0
    started_at = time.monotonic()

    while True:
        # ---------------------------------------------------------------
        # 1. Check whether transaction has already landed.
        # ---------------------------------------------------------------

        status = await _get_signature_status(
            rpc_url,
            signature,
        )

        if status is not None:

            transaction_error = status.get("err")

            if transaction_error is not None:
                transaction = await _get_transaction_details(
                    rpc_url,
                    signature,
                )

                error_details = _format_transaction_error(
                    status,
                    transaction,
                )

                raise SolanaTxError(
                    "transaction landed but failed on-chain:\n"
                    f"{error_details}\n"
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
        # 2. Check blockhash expiration.
        # ---------------------------------------------------------------

        current_height = await _get_block_height(
            rpc_url
        )

        if (
            last_valid_block_height is not None
            and current_height is not None
            and current_height > last_valid_block_height
        ):
            # One final status lookup after expiry. It is possible for the
            # transaction to have landed at the boundary.
            final_status = await _get_signature_status(
                rpc_url,
                signature,
            )

            if final_status is not None:
                final_error = final_status.get("err")

                if final_error is not None:
                    transaction = (
                        await _get_transaction_details(
                            rpc_url,
                            signature,
                        )
                    )

                    error_details = _format_transaction_error(
                        final_status,
                        transaction,
                    )

                    raise SolanaTxError(
                        "transaction landed but failed on-chain:\n"
                        f"{error_details}\n"
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
                f"confirmation. Last valid block height: "
                f"{last_valid_block_height}; current: "
                f"{current_height}. "
                f"{solscan_link}"
            )

        # ---------------------------------------------------------------
        # 3. Broadcast/rebroadcast.
        # ---------------------------------------------------------------

        now = time.monotonic()

        if (
            send_count == 0
            or now - last_send_time
            >= RESEND_INTERVAL_SECONDS
        ):
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
                            "rpc_signature": returned_signature,
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

                # If the RPC explicitly rejected the transaction during
                # preflight, don't blindly resend it forever. The error is
                # generally deterministic and is exactly what we need for
                # diagnosing the transaction.
                lower_error = error_text.lower()

                if any(
                    marker in lower_error
                    for marker in (
                        "simulation",
                        "instruction error",
                        "account not found",
                        "insufficient funds",
                        "blockhash not found",
                        "transaction simulation failed",
                    )
                ):
                    raise

                # Otherwise, this may be a transient RPC/network error.
                await asyncio.sleep(
                    RESEND_INTERVAL_SECONDS
                )

        # ---------------------------------------------------------------
        # 4. Safety timeout only when the builder didn't give us an expiry.
        # ---------------------------------------------------------------

        if (
            last_valid_block_height is None
            and (
                time.monotonic() - started_at
            ) >= 60.0
        ):
            raise SolanaTxError(
                "transaction could not be confirmed within "
                f"60 seconds and no last_valid_block_height "
                f"was supplied: {solscan_link}"
            )

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
    """Return the wallet SOL balance."""

    async with AsyncClient(
        rpc_url,
        commitment=Confirmed,
    ) as client:

        try:
            response = await client.get_balance(
                Pubkey.from_string(pubkey_str),
                commitment=Confirmed,
            )

            return response.value / 1_000_000_000

        except Exception as exc:
            raise SolanaTxError(
                redact_text(
                    f"SOL balance lookup failed: {exc}"
                )
            ) from exc
