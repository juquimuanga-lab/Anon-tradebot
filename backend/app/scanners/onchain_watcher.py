"""On-chain launch detection.

This module contains two independent launch detectors:

1. Anoncoin/Meteora
   Watches the configured Anoncoin creator address and detects new SPL
   mints from token-balance changes.

2. Pump.fun
   Watches the configured Pump.fun mint-authority address and identifies
   actual Pump.fun `create` instructions.

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

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solders.signature import Signature


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
    """Poll an Anoncoin creator address for newly-created tokens."""

    async with AsyncClient(
        rpc_url
    ) as client:

        pubkey = Pubkey.from_string(
            wallet
        )

        until = watermarks.get(
            wallet
        )

        try:

            resp = (
                await client.get_signatures_for_address(
                    pubkey,
                    limit=limit,
                    until=(
                        Signature.from_string(
                            until
                        )
                        if until
                        else None
                    ),
                )
            )

        except Exception as exc:

            logger.warning(
                "get_signatures_failed: "
                "solana client error: "
                f"{type(exc).__name__}: "
                f"{exc} | "
                f"rpc={_safe_rpc_url(rpc_url)} | "
                f"wallet={wallet}"
            )

            # Try direct RPC as a fallback.
            try:

                direct_items = (
                    await _get_signatures_direct(
                        rpc_url,
                        wallet,
                        limit,
                        until,
                    )
                )

            except Exception as direct_exc:

                logger.warning(
                    "get_signatures_failed: "
                    "direct rpc fallback also failed: "
                    f"{type(direct_exc).__name__}: "
                    f"{direct_exc} | "
                    f"rpc={_safe_rpc_url(rpc_url)} | "
                    f"wallet={wallet}"
                )

                return []

            return await _process_direct_anoncoin_signatures(
                rpc_url,
                wallet,
                watermarks,
                direct_items,
            )

        sig_infos = resp.value

        if not sig_infos:

            return []

        watermarks.set(
            wallet,
            str(
                sig_infos[0].signature
            ),
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

            if sig_info.err is not None:

                continue

            try:

                tx_resp = (
                    await client.get_transaction(
                        sig_info.signature,
                        encoding="jsonParsed",
                        max_supported_transaction_version=0,
                    )
                )

            except Exception as exc:

                logger.warning(
                    "get_transaction_failed: "
                    f"{type(exc).__name__}: "
                    f"{exc} | "
                    f"signature={sig_info.signature}"
                )

                continue

            if not tx_resp.value:

                continue

            mint = extract_new_mint(
                tx_resp.value
            )

            if mint:

                discovered.append(
                    {
                        "mint": mint,
                        "tx_signature": str(
                            sig_info.signature
                        ),
                        "block_time": (
                            sig_info.block_time
                        ),
                        "watched_wallet": wallet,
                        "source": (
                            "anoncoin_onchain"
                        ),
                    }
                )

            await asyncio.sleep(
                0.15
            )

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


async def _pumpfun_stream_worker(
    rpc_url: str,
    mint_authority: str,
    queue: asyncio.Queue,
    stop_event: asyncio.Event,
) -> None:
    """Continuously stream Pump.fun CreateEvents over standard Solana WSS."""

    ws_url = _rpc_http_to_ws_url(rpc_url)
    backoff = PUMPFUN_STREAM_RECONNECT_SECONDS

    while not stop_event.is_set():
        try:
            async with websockets.connect(
                ws_url,
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
                        {"commitment": "processed"},
                    ],
                }

                await ws.send(json.dumps(request))
                subscription_response = json.loads(await ws.recv())

                if "error" in subscription_response:
                    raise RuntimeError(
                        f"Pump.fun logsSubscribe failed: {subscription_response['error']}"
                    )

                logger.info(
                    "pumpfun_stream_connected",
                    extra={
                        "mint_authority": mint_authority,
                    },
                )

                backoff = PUMPFUN_STREAM_RECONNECT_SECONDS

                while not stop_event.is_set():
                    raw = await ws.recv()
                    message = json.loads(raw)

                    params = message.get("params") or {}
                    result = params.get("result") or {}
                    value = result.get("value") or {}

                    if value.get("err") is not None:
                        continue

                    logs = value.get("logs") or []
                    signature = value.get("signature")

                    if not signature:
                        continue

                    launch = _extract_pumpfun_event_from_logs(logs)
                    if not launch:
                        continue

                    launch["tx_signature"] = signature
                    launch["block_time"] = None
                    launch["watched_wallet"] = mint_authority
                    launch["discovery"] = "websocket_create_event"

                    try:
                        queue.put_nowait(launch)
                    except asyncio.QueueFull:
                        # Never let a launch burst block the stream forever.
                        # Keep the newest signal because the scanner is more
                        # interested in current launches than stale backlog.
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

        except Exception as exc:
            logger.warning(
                "pumpfun_stream_disconnected",
                extra={
                    "mint_authority": mint_authority,
                    "error": f"{type(exc).__name__}: {exc}",
                    "retry_seconds": backoff,
                },
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
    task = asyncio.create_task(
        _pumpfun_stream_worker(
            rpc_url,
            mint_authority,
            queue,
            stop_event,
        ),
        name=f"pumpfun-stream-{mint_authority[:8]}",
    )

    state = {
        "queue": queue,
        "stop_event": stop_event,
        "task": task,
        "last_fallback": 0.0,
    }
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
    """Return newly-created Pump.fun tokens with minimal RPC credit usage.

    Primary path:
        Standard Solana logsSubscribe -> Pump.fun CreateEvent -> mint

    This avoids the old N+1 pattern of:
        getSignaturesForAddress + getTransaction for every signature.

    Recovery path:
        A low-frequency signature poll is used only when the stream has
        disconnected or every PUMPFUN_FALLBACK_POLL_SECONDS. The fallback
        preserves the old parser and is therefore able to recover missed
        events without making transaction calls during normal streaming.
    """

    state = _get_or_create_pumpfun_stream(
        rpc_url,
        mint_authority,
    )

    discovered = []
    seen_signatures = set()

    # ------------------------------------------------------------------
    # Fast path: drain CreateEvents already received by the WSS listener.
    # ------------------------------------------------------------------

    for launch in _drain_pumpfun_queue(state["queue"]):
        signature = launch.get("tx_signature")
        mint = launch.get("mint")

        if not signature or not mint:
            continue

        if signature in seen_signatures:
            continue

        seen_signatures.add(signature)
        watermarks.set(
            f"pumpfun:{mint_authority}",
            signature,
        )

        if not watermarks.is_initialized(
            f"pumpfun:{mint_authority}"
        ):
            watermarks.mark_initialized(
                f"pumpfun:{mint_authority}"
            )

        discovered.append(launch)

        logger.debug("pumpfun_launch_detected",
            extra={
                "mint": launch.get("mint"),
                "creator": launch.get("creator"),
                "tx_signature": signature,
                "discovery": launch.get("discovery"),
            },
        )

    # ------------------------------------------------------------------
    # Low-frequency recovery path.
    # ------------------------------------------------------------------
    # Only run it when enough time has elapsed. Normal operation therefore
    # uses WSS and does not call getTransaction for every launch.
    # ------------------------------------------------------------------

    loop_time = asyncio.get_running_loop().time()
    stream_task = state.get("task")
    stream_down = bool(
        stream_task is not None
        and stream_task.done()
    )

    fallback_due = (
        loop_time - float(state.get("last_fallback", 0.0))
        >= PUMPFUN_FALLBACK_POLL_SECONDS
    )

    if not stream_down and not fallback_due:
        return discovered

    state["last_fallback"] = loop_time

    watermark_key = f"pumpfun:{mint_authority}"
    until = watermarks.get(watermark_key)

    try:
        authority_pubkey = Pubkey.from_string(mint_authority)

        async with AsyncClient(rpc_url) as client:
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

            normalized = [
                {
                    "signature": str(item.signature),
                    "err": item.err,
                    "block_time": item.block_time,
                }
                for item in resp.value
            ]

            # On a fresh stream start, initialize from the newest signature
            # only if the stream has not already supplied events. This keeps
            # startup from replaying historical launches.
            if not watermarks.is_initialized(watermark_key):
                if normalized:
                    watermarks.set(
                        watermark_key,
                        normalized[0]["signature"],
                    )
                watermarks.mark_initialized(
                    watermark_key
                )
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
                    },
                )
                return discovered

            # Process only signatures newer than the watermark. The stream
            # should normally make this list empty; this is strictly recovery.
            for sig_info in reversed(normalized):
                signature = sig_info.get("signature")
                if not signature:
                    continue
                if signature in seen_signatures:
                    continue
                if sig_info.get("err") is not None:
                    continue

                tx_value = None
                try:
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
                            "error": str(exc),
                        },
                    )
                    try:
                        tx_value = await _get_transaction_direct(
                            rpc_url,
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

                launch["tx_signature"] = signature
                launch["block_time"] = sig_info.get("block_time")
                launch["watched_wallet"] = mint_authority
                launch["discovery"] = "rpc_recovery"

                discovered.append(launch)
                seen_signatures.add(signature)
                watermarks.set(
                    watermark_key,
                    signature,
                )

                logger.debug("pumpfun_launch_detected",
                    extra={
                        "mint": launch.get("mint"),
                        "creator": launch.get("creator"),
                        "tx_signature": signature,
                        "discovery": "rpc_recovery",
                    },
                )

    except Exception as exc:
        logger.warning(
            "pumpfun_recovery_poll_failed",
            extra={
                "mint_authority": mint_authority,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )

    if discovered:
        logger.info(
            "pumpfun_discovery_batch_complete",
            extra={
                "count": len(discovered),
                "mode": "streaming",
            },
        )

    return discovered

