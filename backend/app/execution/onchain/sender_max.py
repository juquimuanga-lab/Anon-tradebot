"""Helius Sender Max delivery for latency-sensitive Solana trades.

This module is intentionally isolated from the existing RPC implementation.
The existing send/confirm state machine remains the source of truth for
success/failure; Sender is only the first submission path.
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

# Sender Max currently requires a 0.001 SOL minimum tip for the priority
# tip buffer. Keep it configurable so the deployment can tune cost vs landing.
DEFAULT_TIP_LAMPORTS = 1_000_000
MIN_TIP_LAMPORTS = 1_000_000

try:
    _configured_tip_lamports = int(
        os.getenv("HELIUS_SENDER_TIP_LAMPORTS", DEFAULT_TIP_LAMPORTS)
    )
except (TypeError, ValueError):
    _configured_tip_lamports = DEFAULT_TIP_LAMPORTS

SENDER_TIP_LAMPORTS = max(
    MIN_TIP_LAMPORTS,
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


async def send_transaction(signed_tx_bytes: bytes) -> str:
    """Submit a fully signed transaction through Helius Sender Max.

    The transaction must already contain a Sender tip instruction and a
    ComputeBudget priority-fee instruction. Sender performs the low-latency
    routing; confirmation is deliberately handled by the existing RPC state
    machine after this function returns.
    """
    if not signed_tx_bytes:
        raise RuntimeError("sender_max_empty_transaction")

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

    endpoint = SENDER_ENDPOINT
    sender_api_key = os.getenv("HELIUS_SENDER_API_KEY", "").strip()
    if sender_api_key and "api-key=" not in endpoint:
        separator = "&" if "?" in endpoint else "?"
        endpoint = f"{endpoint}{separator}api-key={sender_api_key}"

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
            "tip_lamports": SENDER_TIP_LAMPORTS,
            "skip_preflight": True,
            "max_retries": 0,
        },
    )

    return str(signature)
