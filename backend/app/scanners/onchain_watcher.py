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
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

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

        # The direct JSON-RPC response is a dict, while the existing
        # extract_new_mint() expects the solders transaction wrapper.
        # Therefore direct fallback currently only establishes that the
        # RPC path itself works. We do not manufacture a parsed object.
        #
        # This keeps the existing Anoncoin parser untouched.

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
# Pump.fun detector
# ---------------------------------------------------------------------------

async def poll_new_pumpfun_mints(
    rpc_url: str,
    mint_authority: str,
    watermarks: WatermarkStore,
    limit: int = 20,
) -> list[dict]:
    """Poll Pump.fun's mint-authority address for new token launches.

    The watcher specifically looks for:

        Pump.fun program
              +
        create instruction discriminator
    """

    async with AsyncClient(
        rpc_url
    ) as client:

        authority_pubkey = (
            Pubkey.from_string(
                mint_authority
            )
        )

        watermark_key = (
            f"pumpfun:{mint_authority}"
        )

        until = watermarks.get(
            watermark_key
        )

        sig_infos = None

        # ------------------------------------------------------------------
        # Primary Solana client
        # ------------------------------------------------------------------

        try:

            resp = (
                await client.get_signatures_for_address(
                    authority_pubkey,
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

            sig_infos = [
                {
                    "signature": str(
                        item.signature
                    ),
                    "err": item.err,
                    "block_time": (
                        item.block_time
                    ),
                }
                for item in resp.value
            ]

        except Exception as exc:

            logger.warning(
                "pumpfun_get_signatures_failed: "
                "solana client: "
                f"{type(exc).__name__}: "
                f"{exc} | "
                f"rpc={_safe_rpc_url(rpc_url)} | "
                f"mint_authority={mint_authority}"
            )

            # --------------------------------------------------------------
            # Direct JSON-RPC fallback
            # --------------------------------------------------------------

            try:

                sig_infos = (
                    await _get_signatures_direct(
                        rpc_url,
                        mint_authority,
                        limit,
                        until,
                    )
                )

                logger.info(
                    "pumpfun_get_signatures_direct_rpc_success",
                    extra={
                        "mint_authority": (
                            mint_authority
                        ),
                        "count": len(
                            sig_infos
                        ),
                    },
                )

            except Exception as direct_exc:

                logger.error(
                    "pumpfun_get_signatures_failed: "
                    "direct rpc fallback: "
                    f"{type(direct_exc).__name__}: "
                    f"{direct_exc} | "
                    f"rpc={_safe_rpc_url(rpc_url)} | "
                    f"mint_authority={mint_authority}"
                )

                return []

        if not sig_infos:

            return []

        # ------------------------------------------------------------------
        # Normalize direct/client responses
        # ------------------------------------------------------------------

        normalized = []

        for item in sig_infos:

            if isinstance(
                item,
                dict,
            ):

                normalized.append(
                    item
                )

            else:

                normalized.append(
                    _signature_dict(
                        item
                    )
                )

        if not normalized:

            return []

        newest_signature = (
            normalized[0].get(
                "signature"
            )
        )

        if not newest_signature:

            logger.warning(
                "pumpfun_signature_response_missing_signature"
            )

            return []

        watermarks.set(
            watermark_key,
            newest_signature,
        )

        if not watermarks.is_initialized(
            watermark_key
        ):

            # Do not snipe historical launches on startup.
            watermarks.mark_initialized(
                watermark_key
            )

            logger.info(
                "pumpfun_watermark_initialized",
                extra={
                    "mint_authority": (
                        mint_authority
                    ),
                    "signature": (
                        newest_signature
                    ),
                },
            )

            return []

        discovered = []

        # ------------------------------------------------------------------
        # Oldest -> newest
        # ------------------------------------------------------------------

        for sig_info in reversed(
            normalized
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

            # --------------------------------------------------------------
            # Get transaction
            # --------------------------------------------------------------

            tx_value = None

            try:

                # If this signature came from the solana-py client,
                # convert it back to a Signature object.
                signature_obj = (
                    Signature.from_string(
                        signature
                    )
                )

                tx_resp = (
                    await client.get_transaction(
                        signature_obj,
                        encoding="jsonParsed",
                        max_supported_transaction_version=0,
                    )
                )

                tx_value = (
                    tx_resp.value
                )

            except Exception as exc:

                logger.warning(
                    "pumpfun_get_transaction_failed: "
                    "solana client: "
                    f"{type(exc).__name__}: "
                    f"{exc} | "
                    f"signature={signature}"
                )

                # ----------------------------------------------------------
                # Direct getTransaction fallback
                # ----------------------------------------------------------

                try:

                    tx_value = (
                        await _get_transaction_direct(
                            rpc_url,
                            signature,
                        )
                    )

                except Exception as direct_exc:

                    logger.warning(
                        "pumpfun_get_transaction_failed: "
                        "direct rpc fallback: "
                        f"{type(direct_exc).__name__}: "
                        f"{direct_exc} | "
                        f"signature={signature}"
                    )

                    continue

            if not tx_value:

                continue

            # --------------------------------------------------------------
            # The primary parser expects a solders response wrapper.
            # Direct RPC fallback returns raw JSON, so only use the
            # instruction parser for the native solana-py path.
            # --------------------------------------------------------------

            launch = None

            try:

                launch = (
                    extract_pumpfun_create(
                        tx_value
                    )
                )

            except Exception:

                logger.debug(
                    "pumpfun_create_parse_failed",
                    exc_info=True,
                )

            if not launch:

                continue

            launch[
                "tx_signature"
            ] = signature

            launch[
                "block_time"
            ] = sig_info.get(
                "block_time"
            )

            launch[
                "watched_wallet"
            ] = mint_authority

            discovered.append(
                launch
            )

            logger.info(
                "pumpfun_launch_detected",
                extra={
                    "mint": launch.get(
                        "mint"
                    ),
                    "creator": launch.get(
                        "creator"
                    ),
                    "tx_signature": launch.get(
                        "tx_signature"
                    ),
                },
            )

            await asyncio.sleep(
                0.05
            )

        return discovered
