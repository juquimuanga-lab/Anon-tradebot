"""Helius Sender delivery for low-latency Solana trades.

This module is intentionally isolated from the existing RPC implementation.
The existing send/confirm state machine remains the source of truth for
success/failure; Sender is only the first submission path.

Default mode is cost-optimized SWQoS-only. Sender Max remains available via
HELIUS_SENDER_MODE=max when the higher 0.001 SOL minimum tip is justified.
"""

from __future__ import annotations

import base64
import logging
import os
import time

import httpx

from app.security.redact import redact_text

logger = logging.getLogger("app.execution.onchain.sender_max")

SENDER_ENDPOINT = os.getenv(
    "HELIUS_SENDER_ENDPOINT",
    "https://sender.helius-rpc.com/fast",
)

SENDER_MODE = os.getenv(
    "HELIUS_SENDER_MODE",
    "swqos_only",
).strip().lower()

# Helius currently accepts SWQoS-only Sender submissions from 0.000005 SOL.
# Sender Max uses a 0.001 SOL minimum tip buffer. We default to SWQoS-only
# because the bot currently trades very small positions where 0.001 SOL per
# transaction is economically disproportionate.
SWQOS_MIN_TIP_LAMPORTS = 5_000
SENDER_MAX_MIN_TIP_LAMPORTS = 1_000_000
DEFAULT_TIP_LAMPORTS = SWQOS_MIN_TIP_LAMPORTS

try:
    _configured_tip_lamports = int(
        os.getenv("HELIUS_SENDER_TIP_LAMPORTS", DEFAULT_TIP_LAMPORTS)
    )
except (TypeError, ValueError):
    _configured_tip_lamports = DEFAULT_TIP_LAMPORTS

if SENDER_MODE == "max":
    SENDER_TIP_LAMPORTS = max(
        SENDER_MAX_MIN_TIP_LAMPORTS,
        _configured_tip_lamports,
    )
else:
    SENDER_TIP_LAMPORTS = max(
        SWQOS_MIN_TIP_LAMPORTS,
        _configured_tip_lamports,
    )

try:
    SENDER_TIMEOUT_SECONDS = float(
        os.getenv("HELIUS_SENDER_TIMEOUT_SECONDS", "2.5")
    )
except (TypeError, ValueError):
    SENDER_TIMEOUT_SECONDS = 2.5


def enabled() -> bool:
    """Return whether Sender delivery is enabled."""
    return os.getenv("HELIUS_SENDER_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _sender_endpoint() -> str:
    """Build the endpoint, selecting SWQoS-only unless Sender Max is explicit."""
    endpoint = SENDER_ENDPOINT

    if SENDER_MODE != "max" and "swqos_only=true" not in endpoint.lower():
        separator = "&" if "?" in endpoint else "?"
        endpoint = f"{endpoint}{separator}swqos_only=true"

    sender_api_key = os.getenv("HELIUS_SENDER_API_KEY", "").strip()
    if sender_api_key and "api-key=" not in endpoint:
        separator = "&" if "?" in endpoint else "?"
        endpoint = f"{endpoint}{separator}api-key={sender_api_key}"

    return endpoint


async def send_transaction(signed_tx_bytes: bytes) -> str:
    """Submit a fully signed transaction through Helius Sender.

    The transaction must already contain a Sender tip instruction and a
    ComputeBudget priority-fee instruction. Confirmation remains handled by
    the existing RPC state machine after this function returns.
    """
    if not signed_tx_bytes:
        raise RuntimeError("sender_empty_transaction")

    encoded = base64.b64encode(signed_tx_bytes).decode("ascii")

    payload = {
        "jsonrpc": "2.0",
        "id": str(time.time_ns()),
        "method": "sendTransaction",
        "params": [
            encoded,
            {
                "encoding": "base64",
                "skipPreflight": True,
                "maxRetries": 0,
            },
        ],
    }

    endpoint = _sender_endpoint()

    async with httpx.AsyncClient(timeout=SENDER_TIMEOUT_SECONDS) as client:
        response = await client.post(
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
        )

    body_text = response.text[:2000]

    if response.status_code >= 400:
        raise RuntimeError(
            f"Sender HTTP {response.status_code}: {redact_text(body_text)}"
        )

    try:
        body = response.json()
    except Exception as exc:
        raise RuntimeError(
            f"Sender returned invalid JSON: {redact_text(body_text)}"
        ) from exc

    if body.get("error"):
        raise RuntimeError(
            f"Sender RPC error: {redact_text(str(body['error']))}"
        )

    signature = body.get("result")
    if not signature:
        raise RuntimeError("Sender returned no transaction signature")

    logger.info(
        "helius_sender_transaction_submitted",
        extra={
            "signature": str(signature),
            "endpoint": endpoint.split("?", 1)[0],
            "mode": SENDER_MODE,
            "tip_lamports": SENDER_TIP_LAMPORTS,
            "skip_preflight": True,
            "max_retries": 0,
        },
    )

    return str(signature)
