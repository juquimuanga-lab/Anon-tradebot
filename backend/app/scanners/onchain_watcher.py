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


# Anchor discriminator for:
#
#     global:create
#
# Pump.fun's official IDL defines:
#
#     [24, 30, 200, 40, 5, 28, 7, 119]
#
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
    """Poll an Anoncoin creator address for newly-created tokens.

    This is intentionally unchanged in behavior from the existing
    Anoncoin/Meteora watcher.
    """

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
                "get_signatures_failed",
                extra={
                    "wallet": wallet,
                    "error": str(exc),
                },
            )

            return []

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

            # Establish the initial watermark.
            #
            # We intentionally do not process historical launches.
            watermarks.mark_initialized(
                wallet
            )

            return []

        discovered = []

        # Oldest -> newest.
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
                    "get_transaction_failed",
                    extra={
                        "error": str(exc),
                    },
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

            # Be gentle with public RPC.
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

    try:

        return str(value)

    except Exception:

        return None


def _instruction_program_id(
    instruction,
) -> Optional[str]:
    """Get program ID from a partially-decoded Solana instruction."""

    # solders PartiallyDecodedInstruction
    program_id = getattr(
        instruction,
        "program_id",
        None,
    )

    if program_id is not None:

        return _pubkey_string(
            program_id
        )

    # Some parsed representations expose `programId`.
    program_id = getattr(
        instruction,
        "programId",
        None,
    )

    if program_id is not None:

        return _pubkey_string(
            program_id
        )

    return None


def _instruction_data_bytes(
    instruction,
) -> Optional[bytes]:
    """Decode instruction data from a Solana instruction.

    jsonParsed transactions can still expose a partially decoded instruction
    with base58 instruction data.

    We intentionally avoid adding another base58 dependency here because
    solders already provides the transaction object. If the SDK returns raw
    bytes, those are used directly.
    """

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

    # solders may expose raw instruction data as a string.
    if isinstance(
        data,
        str,
    ):

        try:

            # Import lazily so this module keeps its existing lightweight
            # dependency footprint.
            import base58

            return base58.b58decode(
                data
            )

        except Exception:

            # Some RPC representations can expose base64.
            try:

                return base64.b64decode(
                    data
                )

            except Exception:

                return None

    return None


def _instruction_accounts(
    instruction,
) -> list[str]:
    """Return account addresses from a partially-decoded instruction."""

    accounts = getattr(
        instruction,
        "accounts",
        None,
    )

    if not accounts:
        return []

    result = []

    for account in accounts:

        value = _pubkey_string(
            account
        )

        if value:
            result.append(
                value
            )

    return result


def _is_pumpfun_create_instruction(
    instruction,
) -> bool:
    """Return True only for Pump.fun's actual create instruction."""

    program_id = (
        _instruction_program_id(
            instruction
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

    return data[
        : len(
            PUMPFUN_CREATE_DISCRIMINATOR
        )
    ] == PUMPFUN_CREATE_DISCRIMINATOR


def extract_pumpfun_create(
    tx,
) -> Optional[dict]:
    """Extract a Pump.fun launch from a transaction.

    We only accept a transaction containing the Pump.fun `create`
    instruction.

    According to the Pump.fun IDL, the first account of `create` is:

        account[0] = mint

    This prevents ordinary Pump.fun buys/sells/transfers from being
    interpreted as launches.
    """

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

    if not instructions:
        return None


    for instruction in instructions:

        if not _is_pumpfun_create_instruction(
            instruction
        ):
            continue


        accounts = (
            _instruction_accounts(
                instruction
            )
        )

        if not accounts:
            continue


        # Pump.fun create account 0 = mint.
        mint = accounts[0]


        # Sanity check: never accept SOL as a token mint.
        if mint == SOL_MINT:
            continue


        # Pump.fun create also includes the creator/user account.
        #
        # The exact account index can evolve with instruction variants, so
        # we only use the mint here. The scanner can retrieve creator
        # metadata separately if required.
        creator = (
            accounts[7]
            if len(accounts) > 7
            else None
        )


        return {
            "mint": mint,
            "creator": creator,
            "source": "pumpfun",
        }


    return None


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

    Unlike the Anoncoin watcher, this does NOT identify launches by token
    balance differences.

    It specifically looks for:

        Pump.fun program
              +
        create instruction discriminator

    This prevents normal Pump.fun buys/sells from being treated as new
    launches.

    The mint-authority address is used as the watched address because the
    Pump.fun create instruction references the global mint-authority PDA.
    """

    async with AsyncClient(
        rpc_url
    ) as client:

        authority_pubkey = (
            Pubkey.from_string(
                mint_authority
            )
        )

        # Keep Pump.fun's watermark namespace separate from Anoncoin.
        watermark_key = (
            f"pumpfun:{mint_authority}"
        )

        until = watermarks.get(
            watermark_key
        )

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

        except Exception as exc:

            logger.warning(
                "pumpfun_get_signatures_failed",
                extra={
                    "mint_authority": (
                        mint_authority
                    ),
                    "error": str(exc),
                },
            )

            return []


        sig_infos = resp.value

        if not sig_infos:
            return []


        # Establish newest watermark immediately.
        watermarks.set(
            watermark_key,
            str(
                sig_infos[0].signature
            ),
        )


        if not watermarks.is_initialized(
            watermark_key
        ):

            # Do not snipe historical Pump.fun launches when the bot first
            # starts.
            watermarks.mark_initialized(
                watermark_key
            )

            return []


        discovered = []


        # Oldest -> newest.
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
                    "pumpfun_get_transaction_failed",
                    extra={
                        "signature": str(
                            sig_info.signature
                        ),
                        "error": str(exc),
                    },
                )

                continue


            if not tx_resp.value:
                continue


            launch = (
                extract_pumpfun_create(
                    tx_resp.value
                )
            )


            if not launch:
                continue


            launch[
                "tx_signature"
            ] = str(
                sig_info.signature
            )

            launch[
                "block_time"
            ] = sig_info.block_time

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


            # Avoid hammering the RPC.
            await asyncio.sleep(
                0.05
            )


        return discovered
