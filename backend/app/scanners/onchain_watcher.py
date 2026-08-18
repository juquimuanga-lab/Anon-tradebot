"""On-chain launch detection.

This module contains two independent launch detectors:

1. Anoncoin/Meteora
   Watches the configured Anoncoin creator address and detects new SPL
   mints from token-balance changes.

2. Pump.fun
   Watches the configured Pump.fun mint-authority address and identifies
   actual Pump.fun creation instructions. Only legacy `create` launches
   are forwarded to the trading pipeline; Token-2022 `create_v2` launches
   are detected but skipped.

The two paths intentionally remain separate because a Pump.fun launch
starts on Pump.fun's bonding curve and must not be routed through the
Anoncoin/Meteora launch path.
"""

import asyncio
import base64
import logging
import json
import struct
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
# The trading pipeline below intentionally accepts ONLY legacy `create`.
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

# One stream task per watched mint-authority address.
_pumpfun_streams: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# RPC constants
# ---------------------------------------------------------------------------

RPC_TIMEOUT_SECONDS = 8.0

RPC_RETRIES = 2

RPC_RETRY_DELAY_SECONDS = 0.35


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

            # If the active endpoint was primary, retry the transaction on Alchemy.
            if active_transport == "primary":
                fallback = getattr(settings, "alchemy_solana_rpc_url", None)
                if fallback and fallback != active_rpc:
                    try:
                        async with AsyncClient(fallback) as fallback_client:
                            tx_resp = await fallback_client.get_transaction(
                                sig_info.signature,
                                encoding="jsonParsed",
                                max_supported_transaction_version=0,
                            )
                        tx_value = tx_resp.value
                        logger.warning(
                            "onchain_rpc_fallback_to_alchemy",
                            extra={
                                "method": "getTransaction",
                                "signature": str(sig_info.signature),
                            },
                        )
                    except Exception:
                        continue

        if not tx_value:
            continue

        mint = extract_new_mint(tx_value)
        if mint:
            discovered.append(
                {
                    "mint": mint,
                    "tx_signature": str(sig_info.signature),
                    "block_time": sig_info.block_time,
                    "watched_wallet": wallet,
                    "source": "anoncoin_onchain",
                    "rpc_transport": active_transport,
                }
            )

        await asyncio.sleep(0.15)

    return discovered


def _extract_new_mint_from_raw_tx(
    tx: dict,
) -> Optional[str]:
    """Extract a newly-created SPL mint from raw JSON-RPC getTransaction data."""

    try:
        meta = tx.get("meta") or {}
        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []

        pre_mints = {
            str(item.get("mint"))
            for item in pre
            if item.get("mint")
        }
        post_mints = {
            str(item.get("mint"))
            for item in post
            if item.get("mint")
        }

        for mint in post_mints - pre_mints:
            if mint and mint != SOL_MINT:
                return mint
    except Exception:
        logger.debug(
            "raw_mint_extraction_failed",
            exc_info=True,
        )

    return None


async def _process_direct_anoncoin_signatures(
    rpc_url: str,
    wallet: str,
    watermarks: WatermarkStore,
    sig_infos: list[dict],
) -> list[dict]:
    """Process Anoncoin signatures returned by direct JSON-RPC."""

    if not sig_infos:

        return []

    newest_signature = (
        sig_infos[0].get(
            "signature"
        )
    )

    if not newest_signature:

        return []

    watermarks.set(
        wallet,
        newest_signature,
    )

    if not watermarks.is_initialized(
        wallet
    ):

        watermarks.mark_initialized(
            wallet
        )

        return []

    discovered = []

    for sig_info in reversed(
        sig_infos
    ):

        if sig_info.get(
            "err"
        ) is not None:

            continue

        signature = sig_info.get(
            "signature"
        )

        if not signature:

            continue

        try:

            tx = await _get_transaction_direct(
                rpc_url,
                signature,
            )

        except Exception as exc:

            logger.warning(
                "get_transaction_failed: "
                "direct rpc: "
                f"{type(exc).__name__}: "
                f"{exc} | "
                f"signature={signature}"
            )

            continue

        if not tx:

            continue

        # Direct JSON-RPC returns a raw dict. Parse its token balances
        # instead of treating a successful fallback as discovery unavailable.
        mint = _extract_new_mint_from_raw_tx(tx)
        if mint:
            discovered.append(
                {
                    "mint": mint,
                    "tx_signature": signature,
                    "block_time": sig_info.get("blockTime"),
                    "watched_wallet": wallet,
                    "source": "anoncoin_onchain",
                }
            )

        await asyncio.sleep(
            0.15
        )

    return discovered


# ---------------------------------------------------------------------------
# Pump.fun instruction helpers
# ---------------------------------------------------------------------------

def _pubkey_string(
    value,
) -> Optional[str]:
    """Convert a possible Solana pubkey representation to a string."""

    if value is None:
        return None

    if isinstance(
        value,
        dict,
    ):
        # jsonParsed / raw RPC account-key representations.
        for key in (
            "pubkey",
            "publicKey",
            "address",
        ):
            if key in value:
                return _pubkey_string(
                    value[key]
                )

        return None

    try:
        return str(
            value
        )

    except Exception:
        return None


def _instruction_program_id(
    instruction,
    account_keys=None,
) -> Optional[str]:
    """Get program ID from native or raw JSON-RPC instruction."""

    program_id = getattr(
        instruction,
        "program_id",
        None,
    )

    if program_id is not None:
        return _pubkey_string(
            program_id
        )

    program_id = getattr(
        instruction,
        "programId",
        None,
    )

    if program_id is not None:
        return _pubkey_string(
            program_id
        )

    if isinstance(
        instruction,
        dict,
    ):
        program_id = instruction.get(
            "programId"
        )

        if program_id is not None:
            return _pubkey_string(
                program_id
            )

        # Partially-decoded JSON-RPC instructions may expose the
        # program as an account-key index.
        program_index = instruction.get(
            "programIdIndex"
        )

        if (
            program_index is not None
            and account_keys
        ):
            try:
                program_index = int(
                    program_index
                )

                if (
                    0 <= program_index
                    < len(account_keys)
                ):
                    return _pubkey_string(
                        account_keys[
                            program_index
                        ]
                    )

            except Exception:
                pass

    return None


def _instruction_data_bytes(
    instruction,
) -> Optional[bytes]:
    """Decode instruction data from a Solana instruction."""

    if isinstance(
        instruction,
        dict,
    ):
        data = instruction.get(
            "data"
        )

    else:
        data = getattr(
            instruction,
            "data",
            None,
        )

    if data is None:
        return None

    if isinstance(
        data,
        bytes,
    ):
        return data

    if isinstance(
        data,
        bytearray,
    ):
        return bytes(
            data
        )

    if isinstance(
        data,
        list,
    ):
        try:
            return bytes(
                data
            )
        except Exception:
            return None

    if isinstance(
        data,
        str,
    ):
        try:
            import base58

            return base58.b58decode(
                data
            )

        except Exception:

            try:
                return base64.b64decode(
                    data
                )

            except Exception:
                return None

    return None


def _instruction_accounts(
    instruction,
    account_keys=None,
) -> list[str]:
    """Return account addresses from native or raw RPC instruction."""

    if isinstance(
        instruction,
        dict,
    ):
        accounts = instruction.get(
            "accounts"
        )

    else:
        accounts = getattr(
            instruction,
            "accounts",
            None,
        )

    if not accounts:
        return []

    result = []

    for account in accounts:

        # Native solders instructions contain Pubkey objects.
        if not isinstance(
            account,
            int,
        ):
            value = _pubkey_string(
                account
            )

            if value:
                result.append(
                    value
                )

            continue

        # Raw JSON-RPC instructions may contain account-key indexes.
        if (
            account_keys
            and 0 <= int(account)
            < len(account_keys)
        ):
            value = _pubkey_string(
                account_keys[
                    int(account)
                ]
            )

            if value:
                result.append(
                    value
                )

    return result


def _read_borsh_string(
    data: bytes,
    offset: int,
) -> tuple[Optional[str], int]:
    """Read a Borsh UTF-8 string from instruction arguments."""

    if (
        offset + 4
        > len(data)
    ):
        return None, offset

    length = int.from_bytes(
        data[
            offset:offset + 4
        ],
        byteorder="little",
        signed=False,
    )

    offset += 4

    if (
        length < 0
        or offset + length
        > len(data)
    ):
        return None, offset

    raw = data[
        offset:offset + length
    ]

    offset += length

    try:
        return (
            raw.decode(
                "utf-8"
            ),
            offset,
        )

    except UnicodeDecodeError:
        return None, offset


def _extract_pumpfun_creator_from_data(
    data: Optional[bytes],
) -> Optional[str]:
    """Extract creator pubkey from Pump.fun create/create_v2 args.

    Both creation instructions encode:
        name: string
        symbol: string
        uri: string
        creator: pubkey

    The remaining boolean arguments differ by instruction version,
    so only the stable prefix is decoded here.
    """

    if not data:
        return None

    if data.startswith(
        PUMPFUN_CREATE_V2_DISCRIMINATOR
    ):
        offset = len(
            PUMPFUN_CREATE_V2_DISCRIMINATOR
        )

    elif data.startswith(
        PUMPFUN_CREATE_DISCRIMINATOR
    ):
        offset = len(
            PUMPFUN_CREATE_DISCRIMINATOR
        )

    else:
        return None

    for _ in range(3):
        _, offset = _read_borsh_string(
            data,
            offset,
        )

        if offset > len(data):
            return None

    if (
        offset + 32
        > len(data)
    ):
        return None

    creator_bytes = data[
        offset:offset + 32
    ]

    try:
        return str(
            Pubkey.from_bytes(
                creator_bytes
            )
        )

    except Exception:
        return None


def _pumpfun_create_version(
    instruction,
) -> Optional[str]:
    data = _instruction_data_bytes(
        instruction
    )

    if not data:
        return None

    if data.startswith(
        PUMPFUN_CREATE_V2_DISCRIMINATOR
    ):
        return "create_v2"

    if data.startswith(
        PUMPFUN_CREATE_DISCRIMINATOR
    ):
        return "create"

    return None


def _is_pumpfun_create_instruction(
    instruction,
    account_keys=None,
) -> bool:
    """Return True for Pump.fun create or create_v2."""

    program_id = (
        _instruction_program_id(
            instruction,
            account_keys,
        )
    )

    if (
        program_id
        != PUMPFUN_PROGRAM_ID
    ):
        return False

    data = (
        _instruction_data_bytes(
            instruction
        )
    )

    if not data:
        return False

    return any(
        data.startswith(
            discriminator
        )
        for discriminator in (
            PUMPFUN_CREATE_DISCRIMINATORS
        )
    )


def _raw_transaction_account_keys(
    tx: dict,
) -> list:
    """Return raw RPC account keys from getTransaction."""

    message = (
        tx.get(
            "transaction",
            {},
        )
        .get(
            "message",
            {},
        )
    )

    return message.get(
        "accountKeys",
        [],
    )


def _raw_transaction_instructions(
    tx: dict,
) -> list:
    """Return raw outer instructions from getTransaction."""

    message = (
        tx.get(
            "transaction",
            {},
        )
        .get(
            "message",
            {},
        )
    )

    return message.get(
        "instructions",
        []
    )


def _extract_pumpfun_create_from_instructions(
    instructions,
    account_keys=None,
) -> Optional[dict]:
    """Extract a Pump.fun launch from a list of instructions."""

    if not instructions:
        return None

    for instruction in instructions:

        if not _is_pumpfun_create_instruction(
            instruction,
            account_keys,
        ):
            continue

        accounts = (
            _instruction_accounts(
                instruction,
                account_keys,
            )
        )

        if not accounts:
            continue

        # Pump.fun create/create_v2 account 0 is the new mint.
        mint = accounts[0]

        if (
            not mint
            or mint == SOL_MINT
        ):
            continue

        data = (
            _instruction_data_bytes(
                instruction
            )
        )

        creator = (
            _extract_pumpfun_creator_from_data(
                data
            )
        )

        # Legacy create has the user/creator at account index 7.
        # Keep this fallback for older transactions if Borsh decoding
        # is unavailable.
        if (
            creator is None
            and len(accounts) > 7
        ):
            creator = accounts[7]

        return {
            "mint": mint,
            "creator": creator,
            "source": "pumpfun",
            "instruction": (
                _pumpfun_create_version(
                    instruction
                )
            ),
        }

    return None


def _extract_pumpfun_create_from_native_tx(
    tx,
) -> Optional[dict]:
    """Extract a Pump.fun launch from a solders transaction response."""

    try:
        message = (
            tx.transaction.transaction.message
        )

    except Exception:

        try:
            message = (
                tx.transaction.message
            )

        except Exception:

            logger.debug(
                "pumpfun_message_extraction_failed",
                exc_info=True,
            )

            return None

    instructions = getattr(
        message,
        "instructions",
        None,
    )

    return (
        _extract_pumpfun_create_from_instructions(
            instructions
        )
    )


def _extract_pumpfun_create_from_raw_tx(
    tx: dict,
) -> Optional[dict]:
    """Extract a Pump.fun launch from raw JSON-RPC getTransaction data."""

    if not isinstance(
        tx,
        dict,
    ):
        return None

    account_keys = (
        _raw_transaction_account_keys(
            tx
        )
    )

    instructions = (
        _raw_transaction_instructions(
            tx
        )
    )

    return (
        _extract_pumpfun_create_from_instructions(
            instructions,
            account_keys,
        )
    )


def extract_pumpfun_create(
    tx,
) -> Optional[dict]:
    """Extract a Pump.fun create/create_v2 launch.

    Supports both the native solana-py/solders response and the raw
    JSON-RPC fallback response.
    """

    if isinstance(
        tx,
        dict,
    ):
        return _extract_pumpfun_create_from_raw_tx(
            tx
        )

    return _extract_pumpfun_create_from_native_tx(
        tx
    )


def _is_legacy_pumpfun_launch(
    launch: Optional[dict],
) -> bool:
    """Return True only for the legacy Pump.fun `create` instruction.

    Token-2022 launches use `create_v2`. They are intentionally excluded
    from buying until Token-2022 execution is re-enabled.
    """

    return bool(
        launch
        and launch.get("instruction") == "create"
    )


# ---------------------------------------------------------------------------
# Pump.fun streaming discovery
# ---------------------------------------------------------------------------

def _rpc_http_to_ws_url(rpc_url: str) -> str:
    """Convert a Solana/Helius HTTP RPC URL to its WebSocket equivalent."""

    if rpc_url.startswith("https://"):
        return "wss://" + rpc_url[len("https://"):]

    if rpc_url.startswith("http://"):
        return "ws://" + rpc_url[len("http://"):]

    return rpc_url


def _read_borsh_string(
    data: bytes,
    offset: int,
) -> tuple[Optional[str], int]:
    """Read a Borsh length-prefixed UTF-8 string."""

    if offset + 4 > len(data):
        return None, offset

    length = struct.unpack_from("<I", data, offset)[0]
    offset += 4

    if length > 1_000_000 or offset + length > len(data):
        return None, offset

    raw = data[offset:offset + length]
    offset += length

    try:
        return raw.decode("utf-8", errors="replace"), offset
    except Exception:
        return None, offset


def _parse_pumpfun_create_event(
    encoded_data: str,
) -> Optional[dict]:
    """Parse Pump.fun's CreateEvent from a `Program data:` log line.

    CreateEvent is emitted by both `create` and `create_v2` and contains the
    mint and creator directly. This lets the watcher discover launches without
    calling getTransaction for every signature.
    """

    try:
        data = base64.b64decode(encoded_data)
    except Exception:
        return None

    if not data.startswith(PUMPFUN_CREATE_EVENT_DISCRIMINATOR):
        return None

    offset = len(PUMPFUN_CREATE_EVENT_DISCRIMINATOR)

    name, offset = _read_borsh_string(data, offset)
    if name is None:
        return None

    symbol, offset = _read_borsh_string(data, offset)
    if symbol is None:
        return None

    uri, offset = _read_borsh_string(data, offset)
    if uri is None:
        return None

    # CreateEvent layout:
    # name, symbol, uri, mint, bonding_curve, user, creator, ...
    pubkey_size = 32
    required = pubkey_size * 4
    if offset + required > len(data):
        return None

    mint_bytes = data[offset:offset + 32]
    offset += 32

    bonding_curve_bytes = data[offset:offset + 32]
    offset += 32

    user_bytes = data[offset:offset + 32]
    offset += 32

    creator_bytes = data[offset:offset + 32]

    try:
        mint = str(Pubkey.from_bytes(mint_bytes))
        bonding_curve = str(Pubkey.from_bytes(bonding_curve_bytes))
        user = str(Pubkey.from_bytes(user_bytes))
        creator = str(Pubkey.from_bytes(creator_bytes))
    except Exception:
        return None

    if not mint or mint == SOL_MINT:
        return None

    return {
        "mint": mint,
        "creator": creator,
        "user": user,
        "bonding_curve": bonding_curve,
        "name": name,
        "symbol": symbol,
        "uri": uri,
        "source": "pumpfun",
    }


def _extract_pumpfun_event_from_logs(
    logs: list[str],
) -> Optional[dict]:
    """Find a Pump.fun CreateEvent in transaction logs."""

    for line in logs:
        if not isinstance(line, str):
            continue

        prefix = "Program data:"
        if not line.startswith(prefix):
            continue

        encoded = line[len(prefix):].strip()
        event = _parse_pumpfun_create_event(encoded)
        if event:
            return event

    return None



async def _alchemy_http_health_check() -> bool:
    """Verify the configured Alchemy Solana HTTP endpoint independently."""
    rpc_url = getattr(settings, "alchemy_solana_rpc_url", None)
    if not rpc_url:
        logger.error("alchemy_http_health_failed", extra={"error": "missing_alchemy_solana_rpc_url"})
        return False

    try:
        response = await _direct_rpc_request(
            rpc_url,
            "getSlot",
            [{"commitment": "confirmed"}],
        )
        if "error" in response:
            raise RuntimeError(str(response["error"]))
        slot = response.get("result")
        if slot is None:
            raise RuntimeError(f"getSlot returned no result: {response!r}")
        logger.info(
            "alchemy_http_health_ok",
            extra={"slot": slot, "rpc_url": _safe_rpc_url(rpc_url)},
        )
        return True
    except Exception as exc:
        logger.error(
            "alchemy_http_health_failed",
            extra={
                "rpc_url": _safe_rpc_url(rpc_url),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return False


async def _alchemy_ws_health_check() -> bool:
    """
    Alchemy WSS is intentionally not used as the Pump.fun fallback.

    Production diagnostics showed:
      slotSubscribe  -> -32601 Method not found
      logsSubscribe  -> -32601 Method not found

    Alchemy remains enabled for HTTP RPC recovery through _rpc_candidates().
    """
    ws_url = getattr(settings, "alchemy_solana_ws_url", None)

    if not ws_url:
        logger.warning(
            "alchemy_ws_unavailable reason=missing_alchemy_solana_ws_url"
        )
        return False

    logger.warning(
        f"alchemy_ws_disabled_for_pumpfun "
        f"reason=solana_pubsub_method_not_found "
        f"ws_url={_safe_rpc_url(ws_url)}"
    )
    return False


async def _pumpfun_stream_worker(
    rpc_url: str,
    mint_authority: str,
    queue: asyncio.Queue,
    stop_event: asyncio.Event,
    state: dict,
) -> None:
    """
    Pump.fun real-time stream.

    Helius WSS remains the real-time transport. Alchemy is deliberately used
    only by the existing HTTP recovery path because the deployed Alchemy
    Solana WSS endpoint returned JSON-RPC -32601 "Method not found" for
    slotSubscribe/logsSubscribe.

    Writes state["connected"] on every transition so poll_new_pumpfun_mints
    can tell a genuinely live stream apart from one that's stuck retrying.
    This worker's own while-loop swallows every connection error and keeps
    looping rather than returning, by design - so the asyncio task itself
    never reports done() while stuck in reconnect/backoff, even though no
    data is flowing. state["connected"] is the signal that actually tracks
    that: poll_new_pumpfun_mints was reading task.done() to decide whether
    to fall back to the fast HTTP recovery poll, which meant a stream stuck
    endlessly retrying (exactly what repeated 429s produce) never tripped
    it and got stuck on the slow 120s catch-up cadence instead of the 5s
    degraded one - a real gap during the exact outage window it exists to
    cover.
    """

    backoff = PUMPFUN_STREAM_RECONNECT_SECONDS

    while not stop_event.is_set():
        ws_url = _rpc_http_to_ws_url(rpc_url)

        if not ws_url:
            state["connected"] = False

            logger.warning(
                f"pumpfun_stream_unavailable "
                f"reason=missing_primary_ws_url mint_authority={mint_authority}"
            )
        else:
            try:
                logger.info(
                    f"pumpfun_stream_connect_attempt transport=primary "
                    f"mint_authority={mint_authority} "
                    f"ws_url={_safe_rpc_url(ws_url)}"
                )

                async with websockets.connect(
                    ws_url,
                    open_timeout=15,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    request = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [mint_authority]},
                            {"commitment": "confirmed"},
                        ],
                    }

                    await ws.send(json.dumps(request))

                    try:
                        subscription_raw = await asyncio.wait_for(
                            ws.recv(),
                            timeout=15,
                        )
                    except asyncio.TimeoutError as exc:
                        raise RuntimeError(
                            "logsSubscribe response timeout"
                        ) from exc

                    try:
                        subscription_response = json.loads(subscription_raw)
                    except Exception as exc:
                        raise RuntimeError(
                            f"invalid logsSubscribe response: "
                            f"{subscription_raw!r}"
                        ) from exc

                    if "error" in subscription_response:
                        raise RuntimeError(
                            "Pump.fun logsSubscribe failed on primary: "
                            f"{subscription_response['error']}"
                        )

                    if "result" not in subscription_response:
                        raise RuntimeError(
                            "Pump.fun logsSubscribe returned no subscription "
                            f"id on primary: {subscription_response!r}"
                        )

                    logger.info(
                        f"pumpfun_stream_subscription_confirmed "
                        f"transport=primary mint_authority={mint_authority} "
                        f"subscription_id={subscription_response.get('result')}"
                    )
                    logger.info(
                        f"pumpfun_stream_connected transport=primary "
                        f"mint_authority={mint_authority}"
                    )

                    state["connected"] = True
                    state["last_summary_at"] = asyncio.get_running_loop().time()

                    backoff = PUMPFUN_STREAM_RECONNECT_SECONDS

                    while not stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(),
                                timeout=60,
                            )
                        except asyncio.TimeoutError:
                            pong = await ws.ping()
                            await asyncio.wait_for(pong, timeout=10)
                            continue

                        try:
                            message = json.loads(raw)
                        except Exception:
                            continue

                        params = message.get("params") or {}
                        result = params.get("result") or {}
                        value = result.get("value") or {}

                        if value.get("err") is not None:
                            continue

                        logs = value.get("logs") or []
                        signature = value.get("signature")

                        if not signature:
                            continue

                        # A provider can occasionally deliver the same notification more
                        # than once. Deduplicate before doing any downstream work.
                        seen = state.setdefault("seen_signatures", {})
                        if signature in seen:
                            state["duplicates_skipped"] = int(
                                state.get("duplicates_skipped", 0)
                            ) + 1
                            continue
                        seen[signature] = asyncio.get_running_loop().time()
                        if len(seen) > 5000:
                            # Keep memory bounded; old signatures are only a short-lived
                            # duplicate guard and do not affect recovery watermarks.
                            oldest = sorted(seen.items(), key=lambda item: item[1])[:1000]
                            for old_signature, _ in oldest:
                                seen.pop(old_signature, None)

                        # Preserve the existing Pump.fun event parser.
                        launch = _extract_pumpfun_event_from_logs(logs)
                        if not launch:
                            continue

                        # CreateEvent is identical for legacy `create` and
                        # Token-2022 `create_v2`, so the event alone cannot
                        # safely tell us which token program was used.
                        # Verify the actual transaction before allowing the
                        # launch into the trading pipeline. Fail closed:
                        # if verification cannot prove legacy `create`, skip it.
                        verified_launch = None
                        try:
                            async with AsyncClient(rpc_url) as verify_client:
                                verify_resp = await verify_client.get_transaction(
                                    Signature.from_string(signature),
                                    encoding="jsonParsed",
                                    max_supported_transaction_version=0,
                                )
                            verify_tx = verify_resp.value
                            if verify_tx:
                                verified_launch = extract_pumpfun_create(verify_tx)
                        except Exception as exc:
                            logger.warning(
                                "pumpfun_launch_version_verification_failed",
                                extra={
                                    "signature": signature,
                                    "error": f"{type(exc).__name__}: {exc}",
                                },
                            )

                        if not _is_legacy_pumpfun_launch(verified_launch):
                            logger.info(
                                "pumpfun_token2022_launch_skipped",
                                extra={
                                    "signature": signature,
                                    "mint": launch.get("mint"),
                                    "instruction": (
                                        verified_launch.get("instruction")
                                        if verified_launch
                                        else None
                                    ),
                                },
                            )
                            continue

                        # Use the transaction-derived launch metadata because
                        # it includes the authoritative instruction version.
                        launch = verified_launch

                        now = asyncio.get_running_loop().time()
                        state["events_seen"] = int(state.get("events_seen", 0)) + 1
                        state["last_event_signature"] = signature
                        state["last_event_mint"] = launch.get("mint")
                        state["last_event_at"] = now

                        # Do not flood production logs with the entire Pump.fun firehose.
                        # The event remains available at DEBUG level, while a periodic
                        # INFO summary below proves the stream is alive.
                        logger.debug(
                            "pumpfun_create_event_received",
                            extra={
                                "signature": signature,
                                "mint": launch.get("mint"),
                            },
                        )

                        last_summary = float(state.get("last_summary_at", 0.0))
                        if now - last_summary >= 60.0:
                            state["last_summary_at"] = now
                            logger.info(
                                "pumpfun_stream_heartbeat",
                                extra={
                                    "events_seen": state.get("events_seen", 0),
                                    "duplicates_skipped": state.get("duplicates_skipped", 0),
                                    "queue_drops": state.get("queue_drops", 0),
                                    "queue_size": queue.qsize(),
                                    "last_event_signature": state.get("last_event_signature"),
                                    "last_event_mint": state.get("last_event_mint"),
                                },
                            )

                        launch["tx_signature"] = signature
                        launch["block_time"] = None
                        launch["watched_wallet"] = mint_authority
                        launch["discovery"] = "websocket_create_event"
                        launch["rpc_transport"] = "primary"

                        try:
                            queue.put_nowait(launch)
                        except asyncio.QueueFull:
                            state["queue_drops"] = int(state.get("queue_drops", 0)) + 1
                            logger.warning(
                                "pumpfun_stream_queue_full",
                                extra={
                                    "queue_size": queue.qsize(),
                                    "queue_drops": state.get("queue_drops", 0),
                                },
                            )
                            try:
                                queue.get_nowait()
                            except asyncio.QueueEmpty:
                                pass
                            try:
                                queue.put_nowait(launch)
                            except asyncio.QueueFull:
                                pass

            except asyncio.CancelledError:
                raise

            except ConnectionClosed as exc:
                state["connected"] = False

                logger.warning(
                    f"pumpfun_stream_disconnected transport=primary "
                    f"mint_authority={mint_authority} "
                    f"close_code={exc.code} close_reason={exc.reason!r} "
                    f"retry_seconds={backoff}"
                )
                logger.warning(
                    f"pumpfun_stream_using_http_recovery "
                    f"mint_authority={mint_authority} fallback=alchemy_http"
                )

            except Exception as exc:
                state["connected"] = False

                logger.warning(
                    f"pumpfun_stream_disconnected transport=primary "
                    f"mint_authority={mint_authority} "
                    f"error_type={type(exc).__name__} error={exc!r} "
                    f"retry_seconds={backoff}"
                )
                logger.warning(
                    f"pumpfun_stream_using_http_recovery "
                    f"mint_authority={mint_authority} fallback=alchemy_http"
                )

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=backoff,
            )
        except asyncio.TimeoutError:
            pass

        backoff = min(
            backoff * 2,
            PUMPFUN_STREAM_MAX_BACKOFF_SECONDS,
        )


def _get_or_create_pumpfun_stream(
    rpc_url: str,
    mint_authority: str,
) -> dict:
    """Create the background Pump.fun stream once per mint authority."""

    state = _pumpfun_streams.get(mint_authority)
    if state:
        task = state.get("task")
        if task and not task.done():
            return state

    queue: asyncio.Queue = asyncio.Queue(
        maxsize=PUMPFUN_EVENT_QUEUE_MAXSIZE
    )
    stop_event = asyncio.Event()

    state = {
        "queue": queue,
        "stop_event": stop_event,
        "task": None,
        "last_fallback": 0.0,
        "connected": False,
        # Stream telemetry is kept in memory; individual CreateEvents are DEBUG-only.
        "events_seen": 0,
        "duplicates_skipped": 0,
        "queue_drops": 0,
        "last_event_signature": None,
        "last_event_mint": None,
        "last_event_at": 0.0,
        "last_summary_at": 0.0,
        "seen_signatures": {},
    }

    task = asyncio.create_task(
        _pumpfun_stream_worker(
            rpc_url,
            mint_authority,
            queue,
            stop_event,
            state,
        ),
        name=f"pumpfun-stream-{mint_authority[:8]}",
    )

    state["task"] = task
    _pumpfun_streams[mint_authority] = state
    return state


def _drain_pumpfun_queue(
    queue: asyncio.Queue,
) -> list[dict]:
    """Drain all currently buffered launch events."""

    items = []
    while True:
        try:
            items.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return items


async def poll_new_pumpfun_mints(
    rpc_url: str,
    mint_authority: str,
    watermarks: WatermarkStore,
    limit: int = 20,
) -> list[dict]:
    """Discover Pump.fun launches using WSS with HTTP recovery failover."""

    state = _get_or_create_pumpfun_stream(
        rpc_url,
        mint_authority,
    )

    discovered = []
    seen_signatures = set()

    # Fast path: drain CreateEvents already received by the WSS listener.
    for launch in _drain_pumpfun_queue(state["queue"]):
        signature = launch.get("tx_signature")
        mint = launch.get("mint")

        if not signature or not mint or signature in seen_signatures:
            continue

        seen_signatures.add(signature)
        watermark_key = f"pumpfun:{mint_authority}"
        watermarks.set(watermark_key, signature)

        if not watermarks.is_initialized(watermark_key):
            watermarks.mark_initialized(watermark_key)

        discovered.append(launch)
        logger.debug(
            "pumpfun_launch_detected",
            extra={
                "mint": launch.get("mint"),
                "creator": launch.get("creator"),
                "tx_signature": signature,
                "discovery": launch.get("discovery"),
                "rpc_transport": launch.get("rpc_transport"),
            },
        )

    loop_time = asyncio.get_running_loop().time()
    stream_task = state.get("task")
    stream_down = bool(
        (
            stream_task is not None
            and stream_task.done()
        )
        or not state.get(
            "connected",
            False,
        )
    )
    poll_interval = (
        PUMPFUN_DEGRADED_POLL_SECONDS
        if stream_down
        else PUMPFUN_FALLBACK_POLL_SECONDS
    )
    fallback_due = (
        loop_time - float(state.get("last_fallback", 0.0))
        >= poll_interval
    )

    if not stream_down and not fallback_due:
        return discovered

    state["last_fallback"] = loop_time
    watermark_key = f"pumpfun:{mint_authority}"
    until = watermarks.get(watermark_key)

    # HTTP recovery: Helius -> Alchemy.
    normalized = None
    active_rpc = rpc_url
    active_transport = "primary"
    last_exc = None

    for candidate_url, transport in _rpc_candidates(rpc_url):
        try:
            authority_pubkey = Pubkey.from_string(mint_authority)
            async with AsyncClient(candidate_url) as client:
                resp = await client.get_signatures_for_address(
                    authority_pubkey,
                    limit=min(
                        int(limit),
                        PUMPFUN_FALLBACK_SIGNATURE_LIMIT,
                    ),
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
                        "method": "pumpfun_getSignaturesForAddress",
                        "mint_authority": mint_authority,
                    },
                )

            normalized = [
                {
                    "signature": str(item.signature),
                    "err": item.err,
                    "block_time": item.block_time,
                }
                for item in resp.value
            ]
            break

        except Exception as exc:
            last_exc = exc
            logger.warning(
                "pumpfun_recovery_rpc_failed",
                extra={
                    "transport": transport,
                    "mint_authority": mint_authority,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    # If solana-py failed on both, try the direct JSON-RPC implementation on
    # both endpoints as a final recovery path.
    if normalized is None and getattr(settings, "alchemy_solana_rpc_url", None):
        await _alchemy_http_health_check()
    if normalized is None:
        direct_items = None
        direct_rpc = rpc_url
        direct_transport = "primary"
        last_direct_exc = None

        for candidate_url, transport in _rpc_candidates(rpc_url):
            try:
                direct_items = await _get_signatures_direct(
                    candidate_url,
                    mint_authority,
                    min(
                        int(limit),
                        PUMPFUN_FALLBACK_SIGNATURE_LIMIT,
                    ),
                    until,
                )
                direct_rpc = candidate_url
                direct_transport = transport
                if transport == "alchemy":
                    logger.warning(
                        "onchain_rpc_fallback_to_alchemy",
                        extra={
                            "method": "pumpfun_getSignaturesForAddress_direct",
                            "mint_authority": mint_authority,
                        },
                    )
                break
            except Exception as exc:
                last_direct_exc = exc

        if direct_items is None:
            logger.warning(
                "pumpfun_recovery_poll_failed",
                extra={
                    "mint_authority": mint_authority,
                    "error": (
                        f"all RPCs failed; primary={type(last_exc).__name__}: {last_exc}; "
                        f"direct={type(last_direct_exc).__name__}: {last_direct_exc}"
                    ),
                },
            )
            return discovered

        normalized = direct_items
        active_rpc = direct_rpc
        active_transport = direct_transport

    # On a fresh stream start, initialize from the newest signature without
    # replaying historical launches.
    if not watermarks.is_initialized(watermark_key):
        if normalized:
            watermarks.set(
                watermark_key,
                normalized[0]["signature"],
            )
        watermarks.mark_initialized(watermark_key)
        logger.info(
            "pumpfun_watermark_initialized",
            extra={
                "mint_authority": mint_authority,
                "signature": (
                    normalized[0]["signature"]
                    if normalized
                    else None
                ),
                "mode": "streaming",
                "rpc_transport": active_transport,
            },
        )
        return discovered

    # Recovery only processes signatures newer than the watermark.
    for sig_info in reversed(normalized):
        signature = sig_info.get("signature")
        if not signature or signature in seen_signatures:
            continue
        if sig_info.get("err") is not None:
            continue

        tx_value = None

        # First use the endpoint that supplied the signatures.
        try:
            async with AsyncClient(active_rpc) as client:
                tx_resp = await client.get_transaction(
                    Signature.from_string(signature),
                    encoding="jsonParsed",
                    max_supported_transaction_version=0,
                )
            tx_value = tx_resp.value
        except Exception as exc:
            logger.warning(
                "pumpfun_recovery_get_transaction_failed",
                extra={
                    "signature": signature,
                    "transport": active_transport,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

            # If the active endpoint was Helius, retry getTransaction on Alchemy.
            if active_transport == "primary":
                fallback = getattr(settings, "alchemy_solana_rpc_url", None)
                if fallback and fallback != active_rpc:
                    try:
                        async with AsyncClient(fallback) as fallback_client:
                            tx_resp = await fallback_client.get_transaction(
                                Signature.from_string(signature),
                                encoding="jsonParsed",
                                max_supported_transaction_version=0,
                            )
                        tx_value = tx_resp.value
                        active_transport = "alchemy"
                        active_rpc = fallback
                        logger.warning(
                            "onchain_rpc_fallback_to_alchemy",
                            extra={
                                "method": "pumpfun_getTransaction",
                                "signature": signature,
                            },
                        )
                    except Exception as fallback_exc:
                        try:
                            tx_value = await _get_transaction_direct(
                                fallback,
                                signature,
                            )
                            active_transport = "alchemy"
                            active_rpc = fallback
                        except Exception:
                            logger.debug(
                                "pumpfun_recovery_get_transaction_alchemy_failed",
                                extra={"error": str(fallback_exc)},
                            )
                            continue
            else:
                try:
                    tx_value = await _get_transaction_direct(
                        active_rpc,
                        signature,
                    )
                except Exception:
                    continue

        if not tx_value:
            continue

        try:
            launch = extract_pumpfun_create(tx_value)
        except Exception:
            logger.debug(
                "pumpfun_recovery_parse_failed",
                exc_info=True,
            )
            continue

        if not launch:
            continue

        # Recovery has the full transaction, so the instruction version is
        # authoritative. Only legacy Pump.fun `create` is allowed to proceed.
        if not _is_legacy_pumpfun_launch(launch):
            logger.info(
                "pumpfun_token2022_launch_skipped",
                extra={
                    "signature": signature,
                    "mint": launch.get("mint"),
                    "instruction": launch.get("instruction"),
                    "discovery": "rpc_recovery",
                },
            )
            seen_signatures.add(signature)
            watermarks.set(watermark_key, signature)
            continue

        launch["tx_signature"] = signature
        launch["block_time"] = sig_info.get("block_time")
        launch["watched_wallet"] = mint_authority
        launch["discovery"] = "rpc_recovery"
        launch["rpc_transport"] = active_transport

        discovered.append(launch)
        seen_signatures.add(signature)
        watermarks.set(watermark_key, signature)

        logger.debug(
            "pumpfun_launch_detected",
            extra={
                "mint": launch.get("mint"),
                "creator": launch.get("creator"),
                "tx_signature": signature,
                "discovery": "rpc_recovery",
                "rpc_transport": active_transport,
            },
        )

    if discovered:
        logger.info(
            "pumpfun_discovery_batch_complete",
            extra={
                "count": len(discovered),
                "mode": "streaming",
                "rpc_transport": active_transport,
            },
        )

    return discovered

