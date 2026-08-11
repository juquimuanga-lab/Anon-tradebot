"""Shared Solana RPC helpers.

Responsibilities:
- Sign legacy and versioned transactions.
- Broadcast signed transactions with controlled retry behavior.
- Confirm transactions using their last_valid_block_height.
- Detect transactions that landed but failed on-chain.
- Detect blockhash expiration.
- Provide SOL balance lookups.

The wallet Keypair only exists in this Python process.
It is never passed to the Node.js DBC builder.
"""

import asyncio
import base64
import logging
import time
from typing import Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.hash import Hash as SoldersHash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction as LegacyTransaction
from solders.transaction import VersionedTransaction

from app.security.redact import redact_text


logger = logging.getLogger("app.execution.onchain.solana_rpc")


# ---------------------------------------------------------------------------
# Transaction delivery configuration
# ---------------------------------------------------------------------------

# IMPORTANT:
# build_tx.js obtains the transaction blockhash using "confirmed".
# Keep preflight at the same commitment to avoid blockhash/preflight
# mismatches.
RPC_COMMITMENT = Confirmed

# Number of RPC submission attempts when the RPC itself rejects or fails
# before giving us a usable signature.
MAX_SEND_ATTEMPTS = 3

# Delay between controlled resend attempts.
SEND_RETRY_DELAY_SECONDS = 0.25

# How frequently we check transaction status after submission.
STATUS_POLL_INTERVAL_SECONDS = 0.25

# Safety timeout when no last_valid_block_height was supplied.
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

    This is used by the Meteora DBC path.

    The transaction has already been constructed by build_tx.js, including
    the Compute Budget priority-fee instruction. Signing does not rebuild
    the transaction or remove those instructions.
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

    This is used by the Jupiter execution path.
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
    """Extract the transaction signature from a signed transaction."""

    if not signed_tx_bytes:
        raise SolanaTxError(
            "signed transaction is empty"
        )

    # Meteora DBC path: legacy transaction.
    try:
        legacy_tx = LegacyTransaction.from_bytes(
            signed_tx_bytes
        )

        if legacy_tx.signatures:
            return str(
                legacy_tx.signatures[0]
            )
    except Exception:
        pass

    # Jupiter path: versioned transaction.
    try:
        versioned_tx = VersionedTransaction.from_bytes(
            signed_tx_bytes
        )

        if versioned_tx.signatures:
            return str(
                versioned_tx.signatures[0]
            )
    except Exception:
        pass

    raise SolanaTxError(
        "unable to extract signature from signed transaction"
    )


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

async def _get_signature_status(
    client: AsyncClient,
    signature: str,
):
    """Return the current signature status."""

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


async def _get_block_height(
    client: AsyncClient,
) -> Optional[int]:
    """Return the current confirmed block height."""

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
    """Check transaction state.

    Returns:

        True  -> landed successfully
        False -> landed but failed
        None  -> not confirmed yet
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

    # If the transaction has not appeared yet, check whether the original
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
                "transaction blockhash expired before "
                "confirmation"
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

    We deliberately keep preflight enabled.

    This means the RPC checks:
    - signature validity
    - blockhash validity
    - transaction simulation

    before accepting the transaction for forwarding.

    Once this path is proven reliable in production, we can separately
    evaluate whether a latency-sensitive path should use skip_preflight.
    """

    opts = TxOpts(
        skip_confirmation=True,

        # Keep preflight ON for now.
        #
        # This gives us useful simulation failures instead of allowing an
        # obviously invalid transaction to enter the network.
        skip_preflight=False,

        # Must match the commitment used when build_tx.js obtained the
        # transaction blockhash.
        preflight_commitment=RPC_COMMITMENT,

        # Let the application control the retry behavior.
        max_retries=0,
    )

    response = await client.send_raw_transaction(
        signed_tx_bytes,
        opts=opts,
    )

    return str(response.value)


# ---------------------------------------------------------------------------
# Send + confirm
# ---------------------------------------------------------------------------

async def send_and_confirm(
    rpc_url: str,
    signed_tx_bytes: bytes,
    last_valid_block_height: int | None = None,
) -> str:
    """Broadcast and verify a Solana transaction.

    Important:

    A successful send_raw_transaction() response does NOT mean that the
    transaction executed successfully. It only means the RPC accepted the
    submission for processing.

    We therefore:
      1. Extract the signature locally.
      2. Submit with preflight enabled.
      3. Check the actual signature status.
      4. Detect on-chain program errors.
      5. Detect blockhash expiration.
      6. Retry delivery when appropriate.
      7. Only return success after confirmed/finalized status.
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

        # ---------------------------------------------------------------
        # Submission loop
        # ---------------------------------------------------------------

        send_attempt = 0

        while True:
            send_attempt += 1

            # Before sending, check whether a previous attempt already
            # succeeded.
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
                    getattr(status, "err", None)
                    if status is not None
                    else "unknown"
                )

                raise SolanaTxError(
                    "transaction landed but failed on-chain: "
                    f"{error} - {solscan_link}"
                )

            # -----------------------------------------------------------
            # Check overall timeout when no blockhash expiry was supplied.
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

            try:
                returned_signature = (
                    await _broadcast_transaction(
                        client,
                        signed_tx_bytes,
                    )
                )

                # The signature returned by the RPC should match the first
                # signature already embedded in the signed transaction.
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

                # Once the RPC has accepted the transaction, we do not
                # immediately send another copy. Give the cluster a short
                # opportunity to process it first.
                status_deadline = (
                    time.monotonic()
                    + 2.0
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
                        status = (
                            await _get_signature_status(
                                client,
                                signature,
                            )
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
                            "transaction landed but failed "
                            f"on-chain: {error} - "
                            f"{solscan_link}"
                        )

                    await asyncio.sleep(
                        STATUS_POLL_INTERVAL_SECONDS
                    )

            except SolanaTxError:
                # These errors are already meaningful and should not be
                # hidden behind a generic broadcast error.
                raise

            except Exception as exc:
                # IMPORTANT:
                #
                # A broadcast RPC error does NOT prove that the transaction
                # never reached the network.
                #
                # Before retrying, check the signature once more.
                logger.warning(
                    "transaction_broadcast_error",
                    extra={
                        "signature": signature,
                        "attempt": send_attempt,
                        "error": redact_text(str(exc)),
                    },
                )

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
                        getattr(status, "err", None)
                        if status is not None
                        else "unknown"
                    )

                    raise SolanaTxError(
                        "transaction landed but failed "
                        f"on-chain: {error} - "
                        f"{solscan_link}"
                    )

            # -----------------------------------------------------------
            # If the transaction is still valid, perform a controlled
            # resend.
            # -----------------------------------------------------------

            if send_attempt >= MAX_SEND_ATTEMPTS:
                # We have reached our controlled submission retry limit.
                #
                # Do one final status check before reporting failure.
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
                        getattr(status, "err", None)
                        if status is not None
                        else "unknown"
                    )

                    raise SolanaTxError(
                        "transaction landed but failed "
                        f"on-chain: {error} - "
                        f"{solscan_link}"
                    )

                raise SolanaTxError(
                    "transaction was submitted but was not "
                    "confirmed within the controlled retry "
                    f"window: {solscan_link}"
                )

            # Small delay before the next submission.
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
    """Return the wallet's SOL balance."""

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
