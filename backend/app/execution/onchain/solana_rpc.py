"""Shared Solana RPC helpers.

Responsibilities:
- Sign legacy and versioned Solana transactions.
- Broadcast signed transactions.
- Confirm transactions using last_valid_block_height.
- Detect transactions that landed but failed on-chain.
- Retry transaction submission when appropriate.
- Provide SOL balance lookups.

This module intentionally avoids TxOpts so it remains compatible with the
Solana Python package version currently installed on Railway.
"""

import asyncio
import base64
import logging
import time
from typing import Optional

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

# build_tx.js gets the blockhash using "confirmed".
# Keep the Python RPC client on the same commitment.
RPC_COMMITMENT = Confirmed

# Controlled application-level resend attempts.
MAX_SEND_ATTEMPTS = 3

# Delay between resend attempts.
SEND_RETRY_DELAY_SECONDS = 0.25

# How frequently to check transaction status.
STATUS_POLL_INTERVAL_SECONDS = 0.25

# Fallback timeout when the caller does not provide
# last_valid_block_height.
DEFAULT_CONFIRMATION_TIMEOUT_SECONDS = 45.0


class SolanaTxError(Exception):
    """Raised when a Solana transaction cannot safely be considered successful."""


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

    The transaction has already been constructed by build_tx.js, including
    any Compute Budget priority-fee instruction. Signing preserves the
    transaction instructions.
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

    # First try legacy transaction.
    try:
        tx = LegacyTransaction.from_bytes(
            signed_tx_bytes
        )

        if tx.signatures:
            return str(tx.signatures[0])

    except Exception:
        pass

    # Then try versioned transaction.
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
# Signature status
# ---------------------------------------------------------------------------

async def _get_signature_status(
    client: AsyncClient,
    signature: str,
):
    """Get the current RPC status for a transaction signature."""

    try:
        parsed_signature = Signature.from_string(
            signature
        )

        response = await client.get_signature_statuses(
            [parsed_signature]
        )

        if not response.value:
            return None

        return response.value[0]

    except Exception as exc:
        logger.debug(
            "signature_status_check_failed",
            extra={
                "error": redact_text(str(exc)),
            },
        )

        return None


# ---------------------------------------------------------------------------
# Block height
# ---------------------------------------------------------------------------

async def _get_block_height(
    client: AsyncClient,
) -> Optional[int]:
    """Return the current confirmed Solana block height."""

    try:
        response = await client.get_block_height(
            commitment=RPC_COMMITMENT
        )

        return int(response.value)

    except Exception as exc:
        logger.debug(
            "block_height_check_failed",
            extra={
                "error": redact_text(str(exc)),
            },
        )

        return None


# ---------------------------------------------------------------------------
# Transaction outcome
# ---------------------------------------------------------------------------

async def _check_transaction_outcome(
    client: AsyncClient,
    signature: str,
    last_valid_block_height: Optional[int],
) -> Optional[bool]:
    """Check the current transaction outcome.

    Returns:

        True  -> transaction confirmed successfully
        False -> transaction landed but failed on-chain
        None  -> transaction not confirmed yet
    """

    status = await _get_signature_status(
        client,
        signature,
    )

    if status is not None:
        transaction_error = getattr(
            status,
            "err",
            None,
        )

        # Transaction actually reached the network and the program returned
        # an error.
        if transaction_error is not None:
            return False

        confirmation_status = getattr(
            status,
            "confirmation_status",
            None,
        )

        if confirmation_status in (
            "confirmed",
            "finalized",
        ):
            return True

    # If the transaction isn't confirmed yet, check whether its original
    # blockhash has expired.
    if last_valid_block_height is not None:
        current_height = await _get_block_height(
            client
        )

        if (
            current_height is not None
            and current_height > last_valid_block_height
        ):
            raise SolanaTxError(
                "transaction blockhash expired before confirmation"
            )

    return None


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------

async def _broadcast_transaction(
    client: AsyncClient,
    signed_tx_bytes: bytes,
) -> str:
    """Broadcast a signed transaction.

    No TxOpts are used because the Solana package installed on Railway does
    not expose TxOpts from solana.rpc.types.

    The RPC's normal send_raw_transaction behavior is therefore used.
    """

    response = await client.send_raw_transaction(
        signed_tx_bytes
    )

    return str(response.value)


# ---------------------------------------------------------------------------
# Send and confirm
# ---------------------------------------------------------------------------

async def send_and_confirm(
    rpc_url: str,
    signed_tx_bytes: bytes,
    last_valid_block_height: int | None = None,
) -> str:
    """Broadcast a signed transaction and verify that it landed.

    A successful send_raw_transaction response alone does NOT mean the
    transaction executed successfully.

    This function therefore:

    1. Extracts the local transaction signature.
    2. Broadcasts the signed transaction.
    3. Checks the actual signature status.
    4. Detects on-chain execution errors.
    5. Detects blockhash expiration.
    6. Retries delivery when appropriate.
    7. Returns only after confirmed/finalized success.
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

    async with AsyncClient(
        rpc_url,
        commitment=RPC_COMMITMENT,
    ) as client:

        started_at = time.monotonic()
        send_attempt = 0

        while True:
            # -----------------------------------------------------------
            # Check whether a previous submission already landed.
            # -----------------------------------------------------------

            outcome = await _check_transaction_outcome(
                client,
                signature,
                last_valid_block_height,
            )

            if outcome is True:
                logger.info(
                    "transaction_confirmed",
                    extra={
                        "signature": signature,
                    },
                )

                return signature

            if outcome is False:
                status = await _get_signature_status(
                    client,
                    signature,
                )

                error = (
                    getattr(
                        status,
                        "err",
                        None,
                    )
                    if status is not None
                    else "unknown"
                )

                raise SolanaTxError(
                    "transaction landed but failed on-chain: "
                    f"{error} - {solscan_link}"
                )

            # -----------------------------------------------------------
            # Fallback timeout if no block-height expiry was provided.
            # -----------------------------------------------------------

            if (
                last_valid_block_height is None
                and (
                    time.monotonic() - started_at
                ) >= DEFAULT_CONFIRMATION_TIMEOUT_SECONDS
            ):
                raise SolanaTxError(
                    "transaction confirmation timed out "
                    f"with unknown outcome: {solscan_link}"
                )

            # -----------------------------------------------------------
            # Broadcast
            # -----------------------------------------------------------

            send_attempt += 1

            try:
                returned_signature = (
                    await _broadcast_transaction(
                        client,
                        signed_tx_bytes,
                    )
                )

                if returned_signature != signature:
                    logger.warning(
                        "rpc_signature_mismatch",
                        extra={
                            "local_signature": signature,
                            "rpc_signature": returned_signature,
                        },
                    )

                logger.info(
                    "transaction_broadcast",
                    extra={
                        "signature": signature,
                        "attempt": send_attempt,
                    },
                )

            except Exception as exc:
                logger.warning(
                    "transaction_broadcast_error",
                    extra={
                        "signature": signature,
                        "attempt": send_attempt,
                        "error": redact_text(str(exc)),
                    },
                )

                # An RPC error does not necessarily prove the transaction
                # wasn't received. Check the signature before retrying.
                outcome = await _check_transaction_outcome(
                    client,
                    signature,
                    last_valid_block_height,
                )

                if outcome is True:
                    return signature

                if outcome is False:
                    status = await _get_signature_status(
                        client,
                        signature,
                    )

                    error = (
                        getattr(
                            status,
                            "err",
                            None,
                        )
                        if status is not None
                        else "unknown"
                    )

                    raise SolanaTxError(
                        "transaction landed but failed on-chain: "
                        f"{error} - {solscan_link}"
                    )

            # -----------------------------------------------------------
            # Wait for confirmation after a successful broadcast.
            # -----------------------------------------------------------

            status_deadline = (
                time.monotonic() + 2.0
            )

            while (
                time.monotonic()
                < status_deadline
            ):
                outcome = (
                    await _check_transaction_outcome(
                        client,
                        signature,
                        last_valid_block_height,
                    )
                )

                if outcome is True:
                    logger.info(
                        "transaction_confirmed",
                        extra={
                            "signature": signature,
                        },
                    )

                    return signature

                if outcome is False:
                    status = await _get_signature_status(
                        client,
                        signature,
                    )

                    error = (
                        getattr(
                            status,
                            "err",
                            None,
                        )
                        if status is not None
                        else "unknown"
                    )

                    raise SolanaTxError(
                        "transaction landed but failed on-chain: "
                        f"{error} - {solscan_link}"
                    )

                await asyncio.sleep(
                    STATUS_POLL_INTERVAL_SECONDS
                )

            # -----------------------------------------------------------
            # Controlled retry limit.
            # -----------------------------------------------------------

            if send_attempt >= MAX_SEND_ATTEMPTS:
                # Final status check before declaring failure.
                outcome = await _check_transaction_outcome(
                    client,
                    signature,
                    last_valid_block_height,
                )

                if outcome is True:
                    return signature

                if outcome is False:
                    status = await _get_signature_status(
                        client,
                        signature,
                    )

                    error = (
                        getattr(
                            status,
                            "err",
                            None,
                        )
                        if status is not None
                        else "unknown"
                    )

                    raise SolanaTxError(
                        "transaction landed but failed on-chain: "
                        f"{error} - {solscan_link}"
                    )

                raise SolanaTxError(
                    "transaction was submitted but was not "
                    "confirmed within the controlled retry "
                    f"window: {solscan_link}"
                )

            # -----------------------------------------------------------
            # Resend the SAME signed transaction.
            #
            # We do not create a new transaction here. A new transaction
            # would require a new blockhash and signature.
            # -----------------------------------------------------------

            await asyncio.sleep(
                SEND_RETRY_DELAY_SECONDS
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
        commitment=RPC_COMMITMENT,
    ) as client:

        try:
            response = await client.get_balance(
                Pubkey.from_string(pubkey_str),
                commitment=RPC_COMMITMENT,
            )

            return response.value / 1_000_000_000

        except Exception as exc:
            raise SolanaTxError(
                redact_text(
                    f"SOL balance lookup failed: {exc}"
                )
            ) from exc
