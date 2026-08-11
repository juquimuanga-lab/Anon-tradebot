"""Shared Solana RPC helpers.

Responsibilities:
- Sign legacy and versioned transactions.
- Broadcast signed transactions.
- Capture useful preflight/simulation failures.
- Retry transaction delivery while the original blockhash is valid.
- Confirm the final on-chain result.
- Never treat an RPC broadcast acknowledgement as a successful trade.
- Provide SOL balance lookups.

The wallet's Keypair only ever exists in this Python process.
It is never passed to the Node.js DBC transaction builder.
"""

import asyncio
import base64
import logging
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

# We use confirmed because the transaction builder obtains its blockhash with
# the "confirmed" commitment. Solana recommends keeping the blockhash
# commitment and preflight commitment aligned.
PREFLIGHT_COMMITMENT = Confirmed

# Manual resend interval. A signed Solana transaction is safe to resend:
# the same signature identifies the same transaction and duplicate delivery
# does not execute it twice.
RESEND_INTERVAL_SECONDS = 0.35

# Maximum time we will continue retrying when the caller did not provide a
# last_valid_block_height.
DEFAULT_SEND_WINDOW_SECONDS = 20.0

# Maximum number of status polls after a broadcast attempt.
STATUS_POLL_INTERVAL_SECONDS = 0.25


class SolanaTxError(Exception):
    """Raised when a Solana transaction cannot be safely considered successful."""


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def sign_legacy_transaction(
    tx_b64: str,
    blockhash_str: str,
    keypair: Keypair,
) -> bytes:
    """Decode and sign a legacy Solana transaction."""

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
            redact_text(f"legacy transaction signing failed: {exc}")
        ) from exc


def sign_versioned_transaction(
    tx_b64: str,
    keypair: Keypair,
) -> bytes:
    """Decode and sign a versioned Solana transaction."""

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
            redact_text(f"versioned transaction signing failed: {exc}")
        ) from exc


# ---------------------------------------------------------------------------
# Transaction parsing helpers
# ---------------------------------------------------------------------------

def _parse_signed_transaction(signed_tx_bytes: bytes):
    """Return a solders transaction object suitable for simulation.

    Meteora DBC currently returns a legacy transaction while Jupiter can
    return a versioned transaction.
    """

    if not signed_tx_bytes:
        raise SolanaTxError("signed transaction is empty")

    # Solana versioned transactions have the high bit set on the first
    # serialized byte. Legacy transactions begin with the compact-u16
    # signature count and therefore normally do not have that bit set.
    first_byte = signed_tx_bytes[0]

    try:
        if first_byte & 0x80:
            return VersionedTransaction.from_bytes(signed_tx_bytes)

        return LegacyTransaction.from_bytes(signed_tx_bytes)

    except Exception as exc:
        raise SolanaTxError(
            redact_text(f"unable to parse signed transaction: {exc}")
        ) from exc


# ---------------------------------------------------------------------------
# Error/log formatting
# ---------------------------------------------------------------------------

def _format_simulation_error(simulation_value) -> str:
    """Turn simulation errors and logs into a useful bounded error message."""

    err = getattr(simulation_value, "err", None)
    logs = getattr(simulation_value, "logs", None)

    parts = []

    if err is not None:
        parts.append(f"simulation error: {err}")

    if logs:
        # Keep logs bounded so Telegram/database messages cannot become huge.
        useful_logs = [
            str(line)
            for line in logs[-25:]
        ]

        parts.append(
            "simulation logs:\n" +
            "\n".join(useful_logs)
        )

    if not parts:
        return "transaction simulation failed without an RPC error"

    return " | ".join(parts)


def _format_broadcast_error(exc: Exception) -> str:
    """Sanitize an RPC broadcast error before exposing it to the application."""

    return redact_text(
        f"broadcast failed: {exc}"
    )


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

async def _simulate_transaction(
    client: AsyncClient,
    signed_tx_bytes: bytes,
) -> None:
    """Run a signed transaction through RPC simulation.

    This is primarily a diagnostic/preflight step. If simulation reports a
    program error, we return the actual Solana program logs instead of only
    reporting a generic transaction failure.

    We deliberately keep the real transaction blockhash when simulating.
    Replacing it would hide blockhash-related problems that can occur during
    the actual send.
    """

    transaction = _parse_signed_transaction(signed_tx_bytes)

    try:
        response = await client.simulate_transaction(
            transaction,
            sig_verify=True,
            commitment=PREFLIGHT_COMMITMENT,
        )

    except Exception as exc:
        raise SolanaTxError(
            redact_text(f"transaction simulation RPC failed: {exc}")
        ) from exc

    value = response.value

    if getattr(value, "err", None) is not None:
        raise SolanaTxError(
            _format_simulation_error(value)
        )


# ---------------------------------------------------------------------------
# Signature status
# ---------------------------------------------------------------------------

async def _get_signature_status(
    client: AsyncClient,
    signature: str,
):
    """Return the current RPC signature status, if available."""

    try:
        parsed_signature = Signature.from_string(signature)

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
# Block-height helpers
# ---------------------------------------------------------------------------

async def _get_current_block_height(
    client: AsyncClient,
) -> Optional[int]:
    """Get the current confirmed block height."""

    try:
        response = await client.get_block_height(
            commitment=PREFLIGHT_COMMITMENT
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


async def _blockhash_still_valid(
    client: AsyncClient,
    last_valid_block_height: Optional[int],
) -> bool:
    """Return whether the transaction's blockhash should still be usable."""

    if last_valid_block_height is None:
        return True

    current_height = await _get_current_block_height(client)

    if current_height is None:
        # If the RPC temporarily fails to provide block height, don't
        # immediately abandon the transaction.
        return True

    return current_height <= last_valid_block_height


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

async def _wait_for_success_or_failure(
    client: AsyncClient,
    signature: str,
    last_valid_block_height: Optional[int],
    timeout_seconds: float,
) -> None:
    """Poll a signature until it succeeds, fails, expires, or times out."""

    started = asyncio.get_running_loop().time()

    while True:
        status = await _get_signature_status(
            client,
            signature,
        )

        if status is not None:
            confirmation_status = getattr(
                status,
                "confirmation_status",
                None,
            )

            transaction_error = getattr(
                status,
                "err",
                None,
            )

            # A non-null err means the transaction was actually processed
            # but the program execution failed.
            if transaction_error is not None:
                solscan_link = (
                    f"https://solscan.io/tx/{signature}"
                )

                raise SolanaTxError(
                    "transaction landed but failed on-chain: "
                    f"{transaction_error} - {solscan_link}"
                )

            # "confirmed" or "finalized" means the transaction has landed.
            if confirmation_status in ("confirmed", "finalized"):
                return

        # If the transaction's blockhash has expired, waiting longer cannot
        # make this particular signed transaction land.
        if not await _blockhash_still_valid(
            client,
            last_valid_block_height,
        ):
            solscan_link = (
                f"https://solscan.io/tx/{signature}"
            )

            raise SolanaTxError(
                "transaction blockhash expired before confirmation - "
                f"transaction may not have landed: {solscan_link}"
            )

        elapsed = (
            asyncio.get_running_loop().time() -
            started
        )

        if elapsed >= timeout_seconds:
            solscan_link = (
                f"https://solscan.io/tx/{signature}"
            )

            raise SolanaTxError(
                "transaction confirmation timed out - "
                f"outcome unknown: {solscan_link}"
            )

        await asyncio.sleep(
            STATUS_POLL_INTERVAL_SECONDS
        )


# ---------------------------------------------------------------------------
# Broadcast + confirmation
# ---------------------------------------------------------------------------

async def send_and_confirm(
    rpc_url: str,
    signed_tx_bytes: bytes,
    last_valid_block_height: int | None = None,
) -> str:
    """Broadcast a signed transaction and safely verify its outcome.

    Important behavior:

    1. Simulates the signed transaction first so program errors can be
       diagnosed before paying for a failed transaction.
    2. Sends using confirmed preflight commitment.
    3. Uses max_retries=0 because the application controls resend timing.
    4. Resends the same signed transaction while its original blockhash is
       valid.
    5. Polls the signature for an actual successful confirmation.
    6. Checks `err` so a transaction that landed but failed is never reported
       as a filled trade.
    """

    if not signed_tx_bytes:
        raise SolanaTxError(
            "cannot send empty signed transaction"
        )

    async with AsyncClient(
        rpc_url,
        commitment=PREFLIGHT_COMMITMENT,
    ) as client:

        # ---------------------------------------------------------------
        # 1. Diagnostic simulation
        # ---------------------------------------------------------------

        try:
            await _simulate_transaction(
                client,
                signed_tx_bytes,
            )

        except SolanaTxError:
            # Simulation errors are highly valuable for debugging and should
            # not be hidden behind a generic "broadcast failed" message.
            raise

        # ---------------------------------------------------------------
        # 2. Determine transaction signature
        # ---------------------------------------------------------------

        transaction = _parse_signed_transaction(
            signed_tx_bytes
        )

        try:
            if isinstance(transaction, VersionedTransaction):
                signature = str(
                    transaction.signatures[0]
                )
            else:
                signature = str(
                    transaction.signatures[0]
                )

        except Exception as exc:
            raise SolanaTxError(
                redact_text(
                    f"unable to extract transaction signature: {exc}"
                )
            ) from exc

        solscan_link = (
            f"https://solscan.io/tx/{signature}"
        )

        # ---------------------------------------------------------------
        # 3. Broadcast loop
        # ---------------------------------------------------------------

        send_started = asyncio.get_running_loop().time()

        # We keep the send window bounded. Normally the caller supplies
        # last_valid_block_height from the transaction builder, which is
        # the authoritative expiration boundary.
        fallback_deadline = (
            send_started +
            DEFAULT_SEND_WINDOW_SECONDS
        )

        first_broadcast = True

        while True:
            # -----------------------------------------------------------
            # Check whether it has already landed.
            # -----------------------------------------------------------

            existing_status = await _get_signature_status(
                client,
                signature,
            )

            if existing_status is not None:
                existing_error = getattr(
                    existing_status,
                    "err",
                    None,
                )

                if existing_error is not None:
                    raise SolanaTxError(
                        "transaction landed but failed on-chain: "
                        f"{existing_error} - {solscan_link}"
                    )

                existing_confirmation = getattr(
                    existing_status,
                    "confirmation_status",
                    None,
                )

                if existing_confirmation in (
                    "confirmed",
                    "finalized",
                ):
                    return signature

            # -----------------------------------------------------------
            # Check blockhash expiry.
            # -----------------------------------------------------------

            if last_valid_block_height is not None:
                valid = await _blockhash_still_valid(
                    client,
                    last_valid_block_height,
                )

                if not valid:
                    raise SolanaTxError(
                        "transaction blockhash expired before it "
                        f"could be confirmed - {solscan_link}"
                    )
            elif (
                asyncio.get_running_loop().time() >=
                fallback_deadline
            ):
                raise SolanaTxError(
                    "transaction delivery timed out with unknown "
                    f"outcome - {solscan_link}"
                )

            # -----------------------------------------------------------
            # Send / resend.
            # -----------------------------------------------------------

            try:
                opts = TxOpts(
                    skip_confirmation=True,

                    # We already simulated explicitly above and want the
                    # send path to minimize additional RPC latency.
                    skip_preflight=True,

                    # The transaction was built using confirmed state, so
                    # keep the send path aligned with that commitment.
                    preflight_commitment=PREFLIGHT_COMMITMENT,

                    # The application controls resubmission itself.
                    max_retries=0,
                )

                response = await client.send_raw_transaction(
                    signed_tx_bytes,
                    opts=opts,
                )

                rpc_signature = str(
                    response.value
                )

                # This should normally be identical to the signature
                # embedded in the signed transaction.
                if rpc_signature != signature:
                    logger.warning(
                        "rpc_returned_unexpected_signature",
                        extra={
                            "expected": signature,
                            "returned": rpc_signature,
                        },
                    )

                if first_broadcast:
                    logger.info(
                        "transaction_broadcast",
                        extra={
                            "signature": signature,
                            "rpc": "configured_rpc",
                        },
                    )
                    first_broadcast = False

            except Exception as exc:
                error_text = _format_broadcast_error(
                    exc
                )

                # A send failure does NOT necessarily mean the transaction
                # could not have reached the network. We therefore check the
                # signature before deciding to retry.
                status_after_error = (
                    await _get_signature_status(
                        client,
                        signature,
                    )
                )

                if status_after_error is not None:
                    status_error = getattr(
                        status_after_error,
                        "err",
                        None,
                    )

                    if status_error is not None:
                        raise SolanaTxError(
                            "transaction landed but failed on-chain: "
                            f"{status_error} - {solscan_link}"
                        )

                    status_confirmation = getattr(
                        status_after_error,
                        "confirmation_status",
                        None,
                    )

                    if status_confirmation in (
                        "confirmed",
                        "finalized",
                    ):
                        return signature

                logger.debug(
                    "transaction_send_attempt_failed",
                    extra={
                        "error": error_text,
                        "signature": signature,
                    },
                )

                # Continue the delivery loop while the blockhash remains
                # valid. This handles transient RPC/network forwarding
                # failures without creating a new transaction/signature.
                await asyncio.sleep(
                    RESEND_INTERVAL_SECONDS
                )

                continue

            # -----------------------------------------------------------
            # 4. Wait briefly for the transaction to appear.
            # -----------------------------------------------------------

            try:
                await _wait_for_success_or_failure(
                    client,
                    signature,
                    last_valid_block_height,
                    timeout_seconds=min(
                        2.0,
                        max(
                            0.5,
                            (
                                last_valid_block_height -
                                (
                                    await _get_current_block_height(client)
                                    if last_valid_block_height is not None
                                    else 0
                                )
                            ) * 0.4
                            if last_valid_block_height is not None
                            else 2.0,
                        ),
                    ),
                )

                return signature

            except SolanaTxError as exc:
                error_text = str(exc).lower()

                # If the transaction has actually landed and failed,
                # _wait_for_success_or_failure has already provided the
                # exact on-chain error. Do NOT resend it.
                if "landed but failed on-chain" in error_text:
                    raise

                # If it expired, the same signed transaction cannot become
                # valid again. The caller must rebuild it with a fresh
                # blockhash.
                if "blockhash expired" in error_text:
                    raise

                # Otherwise it may simply not have reached the RPC leader
                # yet. Continue the resend loop.
                logger.debug(
                    "transaction_not_confirmed_yet",
                    extra={
                        "signature": signature,
                        "error": str(exc),
                    },
                )

            await asyncio.sleep(
                RESEND_INTERVAL_SECONDS
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
        commitment=PREFLIGHT_COMMITMENT,
    ) as client:

        try:
            resp = await client.get_balance(
                Pubkey.from_string(pubkey_str),
                commitment=PREFLIGHT_COMMITMENT,
            )

            return resp.value / 1_000_000_000

        except Exception as exc:
            raise SolanaTxError(
                redact_text(
                    f"SOL balance lookup failed: {exc}"
                )
            ) from exc
