ANON_TRADEBOT_WATCHER_BUILD = 'V7_LAUNCH_VERIFY_SMART_MONEY_FALLBACK'
SMART_MONEY_FIX_V3_ACTIVE = True

"""On-chain launch detection.

This module contains two independent launch detectors:

1. Anoncoin/Meteora
   Watches the configured Anoncoin creator address and detects new SPL
   mints from token-balance changes.

2. Pump.fun
   Watches the configured Pump.fun mint-authority address and identifies
   actual Pump.fun creation instructions. Both legacy `create` and
   Token-2022 `create_v2` launches are forwarded to the trading pipeline.

The two paths intentionally remain separate because a Pump.fun launch
starts on Pump.fun's bonding curve and must not be routed through the
Anoncoin/Meteora launch path.
"""

import asyncio
import base64
import logging
import json
import struct
import time
from collections import deque
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solders.signature import Signature

from app.config.settings import settings


logger = logging.getLogger(
    "app.scanners.onchain_watcher"
)


# ---------------------------------------------------------------------------
# Common constants
# ---------------------------------------------------------------------------

SOL_MINT = (
    "So11111111111111111111111111111111111111112"
)


# ---------------------------------------------------------------------------
# Pump.fun constants
# ---------------------------------------------------------------------------

# Official Pump.fun program.
PUMPFUN_PROGRAM_ID = (
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
)


# Pump.fun mint authority used to create bonding-curve tokens.
#
# This is the address supplied for this bot's Pump.fun launch detection.
PUMPFUN_MINT_AUTHORITY = (
    "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
)


# Anchor discriminators for Pump.fun token creation instructions.
#
#     global:create
#     global:create_v2
#
# create_v2 is the current Token-2022 creation instruction.
PUMPFUN_CREATE_DISCRIMINATOR = bytes(
    [
        24,
        30,
        200,
        40,
        5,
        28,
        7,
        119,
    ]
)

PUMPFUN_CREATE_V2_DISCRIMINATOR = bytes(
    [
        214,
        144,
        76,
        236,
        95,
        139,
        49,
        180,
    ]
)

# Keep both discriminators for transaction parsing/identification.
# Both legacy `create` and Token-2022 `create_v2` are supported by the
# trading pipeline.
PUMPFUN_CREATE_DISCRIMINATORS = (
    PUMPFUN_CREATE_DISCRIMINATOR,
    PUMPFUN_CREATE_V2_DISCRIMINATOR,
)

# Pump.fun emits the same CreateEvent for both legacy create and create_v2.
# The event contains the mint directly, so discovery does not need a
# getTransaction RPC call for every launch.
PUMPFUN_CREATE_EVENT_DISCRIMINATOR = bytes(
    [27, 114, 169, 77, 222, 235, 99, 118]
)

PUMPFUN_STREAM_RECONNECT_SECONDS = 2.0
PUMPFUN_STREAM_MAX_BACKOFF_SECONDS = 30.0
PUMPFUN_FALLBACK_POLL_SECONDS = 120.0
PUMPFUN_DEGRADED_POLL_SECONDS = 5.0
ALCHEMY_HEALTHCHECK_TIMEOUT_SECONDS = 8.0
PUMPFUN_FALLBACK_SIGNATURE_LIMIT = 10
PUMPFUN_EVENT_QUEUE_MAXSIZE = 500

# Smart-money wallet monitoring. This uses the existing Helius Solana
# WebSocket transport instead of Solana Tracker REST polling.
SMART_MONEY_EVENT_QUEUE_MAXSIZE = 200
SMART_MONEY_STREAM_RECONNECT_SECONDS = 1.0
SMART_MONEY_STREAM_MAX_BACKOFF_SECONDS = 15.0

# One stream task per watched mint-authority address.
_pumpfun_streams: dict[str, dict] = {}

# One low-latency Helius stream for the configured smart-money wallet.
_smart_money_streams: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# RPC constants
# ---------------------------------------------------------------------------

RPC_TIMEOUT_SECONDS = 8.0

RPC_RETRIES = 2

RPC_RETRY_DELAY_SECONDS = 0.35


# ---------------------------------------------------------------------------
# Pons / Robinhood Chain
# ---------------------------------------------------------------------------

async def poll_new_pons_launches():
    """Detect new Pons v2 TokenLaunched events on Robinhood Chain."""
    from app.connectors.pons import pons_client
    return await pons_client.poll_new_launches(
        from_block=getattr(settings, "pons_factory_start_block", 0),
    )


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

def _rpc_candidates(rpc_url: str) -> list[tuple[str, str]]:
    """Return primary RPC followed by the configured Alchemy fallback."""

    candidates: list[tuple[str, str]] = [(rpc_url, "primary")]
    fallback = getattr(
        settings,
        "alchemy_solana_rpc_url",
        None,
    )
    if fallback and fallback != rpc_url:
        candidates.append((fallback, "alchemy"))
    return candidates


def _safe_rpc_url(
    rpc_url: str,
) -> str:
    """Return an RPC URL with sensitive query parameters redacted."""

    try:

        parsed = urlsplit(
            rpc_url
        )

        if parsed.query:

            return urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    "REDACTED",
                    "",
                )
            )

        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                "",
                "",
            )
        )

    except Exception:

        return "<invalid-rpc-url>"


async def _direct_rpc_request(
    rpc_url: str,
    method: str,
    params: list,
) -> dict:
    """Make a direct JSON-RPC request.

    This bypasses solana-py's AsyncClient and is used as a diagnostic/
    fallback path when the library-level RPC call fails.
    """

    payload = {
        "jsonrpc": "2.0",
        "id": "anon-tradebot",
        "method": method,
        "params": params,
    }

    last_error = None

    for attempt in range(
        RPC_RETRIES + 1
    ):

        try:

            async with httpx.AsyncClient(
                timeout=RPC_TIMEOUT_SECONDS
            ) as http_client:

                response = await http_client.post(
                    rpc_url,
                    json=payload,
                    headers={
                        "Content-Type": (
                            "application/json"
                        ),
                    },
                )

                response_text = (
                    response.text
                )

                if response.status_code >= 400:

                    raise RuntimeError(
                        "HTTP "
                        f"{response.status_code}: "
                        f"{response_text[:500]}"
                    )

                try:

                    body = response.json()

                except Exception as exc:

                    raise RuntimeError(
                        "RPC returned non-JSON response: "
                        f"{response_text[:500]}"
                    ) from exc

                if "error" in body:

                    error = body.get(
                        "error"
                    )

                    raise RuntimeError(
                        "RPC error: "
                        f"{error}"
                    )

                if "result" not in body:

                    raise RuntimeError(
                        "RPC response missing result: "
                        f"{body}"
                    )

                return body

        except Exception as exc:

            last_error = exc

            if attempt < RPC_RETRIES:

                await asyncio.sleep(
                    RPC_RETRY_DELAY_SECONDS
                    * (attempt + 1)
                )

                continue

            raise RuntimeError(
                f"{method} failed after "
                f"{RPC_RETRIES + 1} attempts: "
                f"{exc}"
            ) from exc

    raise RuntimeError(
        str(last_error)
    )


async def _get_signatures_direct(
    rpc_url: str,
    address: str,
    limit: int,
    until: Optional[str],
) -> list[dict]:
    """Direct JSON-RPC implementation of getSignaturesForAddress."""

    params = [
        address,
        {
            "limit": int(limit),
            "commitment": "confirmed",
        },
    ]

    if until:

        params[1][
            "until"
        ] = until

    body = await _direct_rpc_request(
        rpc_url,
        "getSignaturesForAddress",
        params,
    )

    result = body.get(
        "result"
    )

    if not isinstance(
        result,
        list,
    ):

        raise RuntimeError(
            "getSignaturesForAddress returned "
            "an invalid result"
        )

    return result


async def _get_transaction_direct(
    rpc_url: str,
    signature: str,
) -> Optional[dict]:
    """Direct JSON-RPC implementation of getTransaction."""

    params = [
        signature,
        {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
            "commitment": "confirmed",
        },
    ]

    body = await _direct_rpc_request(
        rpc_url,
        "getTransaction",
        params,
    )

    return body.get(
        "result"
    )


def _signature_dict(
    value,
) -> dict:
    """Normalize a direct RPC signature result."""

    if isinstance(
        value,
        dict,
    ):
        return value

    return {}


# ---------------------------------------------------------------------------
# Watermarks
# ---------------------------------------------------------------------------

class WatermarkStore:
    """Per-source/address signature watermark.

    Each watched source has its own initialization state so adding Pump.fun
    does not interfere with the existing Anoncoin watcher.
    """

    def __init__(self):

        self._last_seen: dict[
            str,
            str,
        ] = {}

        self._initialized: set[
            str
        ] = set()


    def get(
        self,
        wallet: str,
    ) -> Optional[str]:

        return self._last_seen.get(
            wallet
        )


    def set(
        self,
        wallet: str,
        signature: str,
    ) -> None:

        self._last_seen[
            wallet
        ] = signature


    def is_initialized(
        self,
        wallet: str,
    ) -> bool:

        return (
            wallet
            in self._initialized
        )


    def mark_initialized(
        self,
        wallet: str,
    ) -> None:

        self._initialized.add(
            wallet
        )


# ---------------------------------------------------------------------------
# Anoncoin detector
# ---------------------------------------------------------------------------

def extract_new_mint(
    tx,
) -> Optional[str]:
    """Extract a newly-created SPL mint from a parsed transaction.

    This is the existing Anoncoin/Meteora detection mechanism.

    It compares:

        preTokenBalances
        vs
        postTokenBalances

    and ignores wrapped SOL.
    """

    try:

        meta = tx.transaction.meta

        pre_mints = {
            str(b.mint)
            for b in (
                meta.pre_token_balances
                or []
            )
        }

        post_mints = {
            str(b.mint)
            for b in (
                meta.post_token_balances
                or []
            )
        }

        new_mints = [
            mint
            for mint in (
                post_mints
                - pre_mints
            )
            if mint != SOL_MINT
        ]

        if new_mints:

            return new_mints[0]

    except Exception:

        logger.debug(
            "mint_extraction_failed",
            exc_info=True,
        )

    return None


async def poll_new_mints(
    rpc_url: str,
    wallet: str,
    watermarks: WatermarkStore,
    limit: int = 20,
) -> list[dict]:
    """Poll an Anoncoin creator address with Helius -> Alchemy failover."""

    pubkey = Pubkey.from_string(wallet)
    until = watermarks.get(wallet)

    resp = None
    active_rpc = rpc_url
    active_transport = "primary"
    last_exc = None

    # Normal library path. Try Helius first, then Alchemy.
    for candidate_url, transport in _rpc_candidates(rpc_url):
        try:
            async with AsyncClient(candidate_url) as client:
                resp = await client.get_signatures_for_address(
                    pubkey,
                    limit=limit,
                    until=(
                        Signature.from_string(until)
                        if until
                        else None
                    ),
                )
            active_rpc = candidate_url
            active_transport = transport
            if transport == "alchemy":
                logger.warning(
                    "onchain_rpc_fallback_to_alchemy",
                    extra={
                        "method": "getSignaturesForAddress",
                        "wallet": wallet,
                    },
                )
            break
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "get_signatures_failed",
                extra={
                    "transport": transport,
                    "error": f"{type(exc).__name__}: {exc}",
                    "rpc": _safe_rpc_url(candidate_url),
                    "wallet": wallet,
                },
            )

    # Direct JSON-RPC recovery if the solana-py path failed on both endpoints.
    if resp is None:
        direct_items = None
        direct_rpc = rpc_url
        last_direct_exc = None
        for candidate_url, transport in _rpc_candidates(rpc_url):
            try:
                direct_items = await _get_signatures_direct(
                    candidate_url,
                    wallet,
                    limit,
                    until,
                )
                direct_rpc = candidate_url
                if transport == "alchemy":
                    logger.warning(
                        "onchain_rpc_fallback_to_alchemy",
                        extra={
                            "method": "getSignaturesForAddress_direct",
                            "wallet": wallet,
                        },
                    )
                break
            except Exception as exc:
                last_direct_exc = exc

        if direct_items is None:
            logger.warning(
                "get_signatures_failed",
                extra={
                    "error": (
                        f"all RPCs failed; last={type(last_exc).__name__}: {last_exc}; "
                        f"direct={type(last_direct_exc).__name__}: {last_direct_exc}"
                    ),
                    "wallet": wallet,
                },
            )
            return []

        return await _process_direct_anoncoin_signatures(
            direct_rpc,
            wallet,
            watermarks,
            direct_items,
        )

    sig_infos = resp.value

    if not sig_infos:
        return []

    watermarks.set(
        wallet,
        str(sig_infos[0].signature),
    )

    if not watermarks.is_initialized(wallet):
        watermarks.mark_initialized(wallet)
        return []

    discovered = []

    for sig_info in reversed(sig_infos):
        if sig_info.err is not None:
            continue

        tx_value = None

        # Use the same active endpoint that successfully returned signatures.
        try:
            async with AsyncClient(active_rpc) as client:
                tx_resp = await client.get_transaction(
                    sig_info.signature,
                    encoding="jsonParsed",
                    max_supported_transaction_version=0,
                )
            tx_value = tx_resp.value
        except Exception as exc:
            logger.warning(
                "get_transaction_failed",
                extra={
                    "transport": active_transport,
                    "error": f"{type(exc).__name__}: {exc}",
                    "signature": str(sig_info.signature),
                },
            )

        if tx_value is None:
            continue

        mint = extract_new_mint(tx_value)

        if not mint:
            continue

        discovered.append(
            {
                "mint": mint,
                "signature": str(sig_info.signature),
                "slot": int(sig_info.slot),
            }
        )

    return discovered


# ---------------------------------------------------------------------------
# Pump.fun launch stream
# ---------------------------------------------------------------------------

# ... existing functions remain unchanged ...

async def _process_pumpfun_stream_message(
    rpc_url: str,
    state: dict,
    message: dict,
    queue: asyncio.Queue,
) -> None:
    """Process one Pump.fun logsSubscribe event.

    CreateEvent is authoritative for launch discovery. The event already
    contains the mint and creator, so optional transaction-version verification
    is not part of the hot path. This avoids an extra getTransaction RPC for
    every launch and the resulting noisy/unhelpful verification messages.
    """
    params = message.get("params") or {}
    result = params.get("result") or {}
    value = result.get("value") or {}

    if value.get("err") is not None:
        return

    logs = value.get("logs") or []
    signature = value.get("signature")

    if not signature:
        return

    seen = state.setdefault("seen_signatures", {})
    if signature in seen:
        return
    seen[signature] = asyncio.get_running_loop().time()
    if len(seen) > 5000:
        oldest = sorted(seen.items(), key=lambda item: item[1])[:1000]
        for old_signature, _ in oldest:
            seen.pop(old_signature, None)

    launch = _extract_pumpfun_event_from_logs(logs)
    if not launch:
        return

    # CreateEvent is authoritative for launch discovery. Do not issue another
    # getTransaction RPC merely to distinguish create vs create_v2. The
    # downstream pipeline already accepts the event-derived launch metadata.
    state["events_seen"] = int(state.get("events_seen", 0)) + 1
    state["last_event_signature"] = signature
    state["last_event_mint"] = launch.get("mint")
    state["last_event_at"] = asyncio.get_running_loop().time()

    launch = dict(launch)
    launch["signature"] = signature
    launch["source"] = "pumpfun"

    try:
        queue.put_nowait(launch)
    except asyncio.QueueFull:
        logger.warning(
            "pumpfun_event_queue_full",
            extra={"mint": launch.get("mint"), "signature": signature},
        )


# existing stream worker and all remaining functions continue below unchanged
