"""Python wrapper around the Node.js Meteora DBC transaction builder.

Responsibilities:

- Build unsigned Meteora DBC transactions through the official SDK.
- Read Meteora DBC pool state for live price monitoring.
- Never pass private keys into Node.js.
- Keep transaction signing inside Python.
- Keep read-only price lookups tightly time-bounded so a slow RPC cannot
  block the position monitor for a long time.

The live price path is intentionally optimized for monitoring:

    Python
       ↓
    Node pool_info.js
       ↓
    Solana RPC
       ↓
    pool state
       ↓
    Python

Transaction building remains on the existing safer path and keeps the
longer timeout because transaction construction can legitimately take
longer than a read-only price lookup.
"""

import asyncio
import json
import logging
import os
from typing import Optional


logger = logging.getLogger(
    "app.execution.onchain.meteora_dbc"
)


_BUILDER_DIR = os.path.join(
    os.path.dirname(__file__),
    "dbc_builder",
)


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------

# Transaction construction can take longer because the SDK may need several
# RPC reads and transaction assembly.
TRANSACTION_BUILDER_TIMEOUT_SECONDS = 30.0

# Price monitoring must be fast. The caller normally has its own ~2.5 second
# timeout, so this subprocess timeout is deliberately only slightly larger.
POOL_INFO_TIMEOUT_SECONDS = 3.0


class DbcBuildError(Exception):
    """Raised when Meteora DBC construction/read fails."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _terminate_process(
    proc: asyncio.subprocess.Process,
) -> None:
    """Safely terminate a child process."""

    if proc.returncode is not None:
        return

    try:
        proc.kill()
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(
            proc.wait(),
            timeout=1.0,
        )
    except asyncio.TimeoutError:

        logger.warning(
            "meteora_child_process_did_not_exit_after_kill"
        )


def _parse_builder_output(
    stdout: bytes,
    stderr: bytes,
    operation: str,
) -> dict:
    """Parse the final JSON line returned by a Node builder."""

    if not stdout.strip():

        stderr_text = (
            stderr.decode(
                errors="replace"
            )[:500]
        )

        raise DbcBuildError(
            f"{operation} produced no output "
            f"(stderr: {stderr_text})"
        )

    try:

        output = stdout.decode(
            errors="replace"
        ).strip()

        last_line = (
            output
            .splitlines()[-1]
        )

        result = json.loads(
            last_line
        )

    except Exception as exc:

        stderr_text = (
            stderr.decode(
                errors="replace"
            )[:500]
        )

        raise DbcBuildError(
            f"{operation} returned invalid JSON: "
            f"{exc}; stderr: {stderr_text}"
        ) from exc

    if not isinstance(
        result,
        dict,
    ):
        raise DbcBuildError(
            f"{operation} returned an invalid response"
        )

    if not result.get(
        "success"
    ):

        raise DbcBuildError(
            result.get(
                "error",
                f"unknown Meteora DBC {operation} error",
            )
        )

    return result


# ---------------------------------------------------------------------------
# Transaction builder
# ---------------------------------------------------------------------------

async def build_unsigned_swap(
    action: str,
    base_mint: str,
    owner_pubkey: str,
    amount_lamports: int,
    slippage_bps: int,
    rpc_url: str,
) -> dict:
    """Build an unsigned Meteora DBC transaction.

    The private key never crosses into Node.js.

    Node receives only:

        action
        token mint
        owner public key
        amount
        slippage
        RPC URL

    The resulting unsigned transaction is signed later by Python.
    """

    payload = {
        "action": action,
        "baseMint": base_mint,
        "ownerPubkey": owner_pubkey,
        "amountLamports": str(
            amount_lamports
        ),
        "slippageBps": int(
            slippage_bps
        ),
        "rpcUrl": rpc_url,
    }

    proc = await asyncio.create_subprocess_exec(
        "node",
        "build_tx.js",
        cwd=_BUILDER_DIR,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:

        stdout, stderr = (
            await asyncio.wait_for(
                proc.communicate(
                    json.dumps(
                        payload
                    ).encode()
                ),
                timeout=(
                    TRANSACTION_BUILDER_TIMEOUT_SECONDS
                ),
            )
        )

    except asyncio.TimeoutError:

        logger.error(
            "meteora_transaction_builder_timeout",
            extra={
                "action": action,
                "base_mint": base_mint,
                "timeout_seconds": (
                    TRANSACTION_BUILDER_TIMEOUT_SECONDS
                ),
            },
        )

        await _terminate_process(
            proc
        )

        raise DbcBuildError(
            "dbc builder timed out after "
            f"{TRANSACTION_BUILDER_TIMEOUT_SECONDS}s "
            "(node process killed)"
        )

    except asyncio.CancelledError:

        await _terminate_process(
            proc
        )

        raise

    result = _parse_builder_output(
        stdout,
        stderr,
        "dbc builder",
    )

    logger.debug(
        "meteora_unsigned_transaction_built",
        extra={
            "action": action,
            "base_mint": base_mint,
        },
    )

    return result


# ---------------------------------------------------------------------------
# Fast pool-state reader
# ---------------------------------------------------------------------------

async def get_pool_info(
    base_mint: str,
    rpc_url: str,
    commitment: str = "processed",
) -> dict:
    """Read Meteora DBC pool state for live price monitoring.

    This is intentionally optimized for the position manager.

    Default commitment is `processed` because this is a market-price
    observation, not transaction confirmation.

    IMPORTANT:

    This function does NOT decide whether a trade succeeded.

    Transaction confirmation continues to use the separate confirmation
    path in solana_rpc.py.
    """

    payload = {
        "baseMint": base_mint,
        "rpcUrl": rpc_url,
        "commitment": commitment,
    }

    proc = await asyncio.create_subprocess_exec(
        "node",
        "pool_info.js",
        cwd=_BUILDER_DIR,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:

        stdout, stderr = (
            await asyncio.wait_for(
                proc.communicate(
                    json.dumps(
                        payload
                    ).encode()
                ),
                timeout=(
                    POOL_INFO_TIMEOUT_SECONDS
                ),
            )
        )

    except asyncio.TimeoutError:

        logger.warning(
            "meteora_pool_info_timeout",
            extra={
                "base_mint": base_mint,
                "timeout_seconds": (
                    POOL_INFO_TIMEOUT_SECONDS
                ),
            },
        )

        await _terminate_process(
            proc
        )

        raise DbcBuildError(
            "pool_info timed out after "
            f"{POOL_INFO_TIMEOUT_SECONDS}s "
            "(node process killed)"
        )

    except asyncio.CancelledError:

        await _terminate_process(
            proc
        )

        raise

    result = _parse_builder_output(
        stdout,
        stderr,
        "pool_info",
    )

    logger.debug(
        "meteora_pool_info_read",
        extra={
            "base_mint": base_mint,
            "commitment": commitment,
            "pool_address": result.get(
                "pool_address"
            ),
            "price_sol_per_token": result.get(
                "price_sol_per_token"
            ),
        },
    )

    return result
