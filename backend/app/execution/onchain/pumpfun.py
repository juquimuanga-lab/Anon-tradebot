"""Pump.fun bonding-curve market data and transaction building.

Responsibilities:

- Derive Pump.fun bonding-curve PDA.
- Read bonding-curve state directly through Solana RPC.
- Decode current bonding-curve reserves.
- Read token decimals/supply.
- Calculate live Pump.fun token price.
- Calculate market cap and curve liquidity.
- Detect completed/migrated bonding curves.
- Build an UNSIGNED Pump.fun BUY transaction through the Node/Pump SDK
  transaction builder.

Security:

- This module never receives a private key.
- This module never signs a transaction.
- This module never submits a transaction.
- Signing remains in Python's existing wallet/RPC pipeline.

Pump.fun transaction construction is delegated to:

    backend/app/execution/onchain/dbc_builder/pumpfun_build_tx.js

That JavaScript builder uses the official Pump.fun SDK and returns an
unsigned transaction to this module.
"""

import asyncio
import base64
import json
import logging
import os
import struct
from pathlib import Path
from typing import Optional

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey


logger = logging.getLogger(
    "app.execution.onchain.pumpfun"
)


# ---------------------------------------------------------------------------
# Program constants
# ---------------------------------------------------------------------------

PUMPFUN_PROGRAM_ID = Pubkey.from_string(
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
)

BONDING_CURVE_SEED = b"bonding-curve"

SOL_LAMPORTS_PER_SOL = 1_000_000_000


# ---------------------------------------------------------------------------
# Builder configuration
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve()

_DBC_BUILDER_DIR = (
    _THIS_DIR.parent
    / "dbc_builder"
)

PUMPFUN_BUILDER_PATH = (
    _DBC_BUILDER_DIR
    / "pumpfun_build_tx.js"
)

PUMPFUN_SELL_BUILDER_PATH = (
    _DBC_BUILDER_DIR
    / "pumpfun_sell_build_tx.js"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PumpFunError(Exception):
    """Base Pump.fun adapter error."""


class PumpFunPoolNotFound(
    PumpFunError
):
    """Raised when a token has no Pump.fun bonding curve."""


class PumpFunInvalidAccount(
    PumpFunError
):
    """Raised when a Pump.fun account cannot be decoded."""


class PumpFunTransactionBuildError(
    PumpFunError
):
    """Raised when the Pump.fun transaction builder fails."""


# ---------------------------------------------------------------------------
# Bonding curve representation
# ---------------------------------------------------------------------------

class PumpFunBondingCurve:
    """Decoded Pump.fun bonding-curve state."""

    def __init__(
        self,
        *,
        address: str,
        virtual_token_reserves: int,
        virtual_sol_reserves: int,
        real_token_reserves: int,
        real_sol_reserves: int,
        token_total_supply: int,
        complete: bool,
        creator: Optional[str] = None,
        is_mayhem_mode: Optional[bool] = None,
        is_cashback_coin: Optional[bool] = None,
    ):
        self.address = address

        self.virtual_token_reserves = (
            virtual_token_reserves
        )

        self.virtual_sol_reserves = (
            virtual_sol_reserves
        )

        self.real_token_reserves = (
            real_token_reserves
        )

        self.real_sol_reserves = (
            real_sol_reserves
        )

        self.token_total_supply = (
            token_total_supply
        )

        self.complete = complete

        self.creator = creator

        self.is_mayhem_mode = (
            is_mayhem_mode
        )

        self.is_cashback_coin = (
            is_cashback_coin
        )


# ---------------------------------------------------------------------------
# PDA
# ---------------------------------------------------------------------------

def get_bonding_curve_address(
    mint: str,
) -> tuple[Pubkey, int]:
    """Derive Pump.fun bonding-curve PDA."""

    try:

        mint_pubkey = (
            Pubkey.from_string(
                mint
            )
        )

    except Exception as exc:

        raise PumpFunError(
            f"invalid Pump.fun mint: {mint}"
        ) from exc

    address, bump = (
        Pubkey.find_program_address(
            [
                BONDING_CURVE_SEED,
                bytes(
                    mint_pubkey
                ),
            ],
            PUMPFUN_PROGRAM_ID,
        )
    )

    return address, bump


# ---------------------------------------------------------------------------
# Account decoding
# ---------------------------------------------------------------------------

def _read_u64(
    data: bytes,
    offset: int,
) -> int:
    """Read little-endian u64."""

    end = offset + 8

    if end > len(data):

        raise PumpFunInvalidAccount(
            "bonding curve account is too short "
            f"for u64 at offset {offset}"
        )

    return struct.unpack_from(
        "<Q",
        data,
        offset,
    )[0]


def _decode_account_data(
    raw_data,
) -> bytes:
    """Normalize Solana RPC account data into bytes."""

    if isinstance(
        raw_data,
        tuple,
    ):

        encoded = raw_data[0]

        try:

            return base64.b64decode(
                encoded
            )

        except Exception as exc:

            raise PumpFunInvalidAccount(
                "failed to decode Pump.fun "
                "bonding curve account"
            ) from exc

    if isinstance(
        raw_data,
        bytes,
    ):

        return raw_data

    raise PumpFunInvalidAccount(
        "unexpected Solana account-data format"
    )


def decode_bonding_curve(
    address: str,
    data: bytes,
) -> PumpFunBondingCurve:
    """Decode a Pump.fun BondingCurve account."""

    # 8-byte discriminator +
    # 5 x u64 +
    # 1-byte bool.
    min_prefix_length = (
        8
        + (5 * 8)
        + 1
    )

    if len(data) < min_prefix_length:

        raise PumpFunInvalidAccount(
            "bonding curve account data is too short: "
            f"{len(data)} bytes"
        )

    offset = 8

    virtual_token_reserves = (
        _read_u64(
            data,
            offset,
        )
    )

    offset += 8

    virtual_sol_reserves = (
        _read_u64(
            data,
            offset,
        )
    )

    offset += 8

    real_token_reserves = (
        _read_u64(
            data,
            offset,
        )
    )

    offset += 8

    real_sol_reserves = (
        _read_u64(
            data,
            offset,
        )
    )

    offset += 8

    token_total_supply = (
        _read_u64(
            data,
            offset,
        )
    )

    offset += 8

    complete = (
        data[offset] != 0
    )

    offset += 1

    # -----------------------------------------------------------------------
    # Optional newer creator field
    # -----------------------------------------------------------------------

    creator = None

    if len(data) >= offset + 32:

        try:

            creator = str(
                Pubkey.from_bytes(
                    data[
                        offset:
                        offset + 32
                    ]
                )
            )

            offset += 32

        except Exception:

            logger.debug(
                "pumpfun_creator_decode_failed",
                exc_info=True,
            )

    # -----------------------------------------------------------------------
    # Optional feature flags
    # -----------------------------------------------------------------------

    is_mayhem_mode = None

    if len(data) > offset:

        is_mayhem_mode = (
            data[offset] != 0
        )

        offset += 1

    is_cashback_coin = None

    if len(data) > offset:

        is_cashback_coin = (
            data[offset] != 0
        )

    return PumpFunBondingCurve(
        address=address,

        virtual_token_reserves=(
            virtual_token_reserves
        ),

        virtual_sol_reserves=(
            virtual_sol_reserves
        ),

        real_token_reserves=(
            real_token_reserves
        ),

        real_sol_reserves=(
            real_sol_reserves
        ),

        token_total_supply=(
            token_total_supply
        ),

        complete=complete,

        creator=creator,

        is_mayhem_mode=(
            is_mayhem_mode
        ),

        is_cashback_coin=(
            is_cashback_coin
        ),
    )


# ---------------------------------------------------------------------------
# Token decimals
# ---------------------------------------------------------------------------

async def _get_token_decimals(
    client: AsyncClient,
    mint_pubkey: Pubkey,
) -> int:
    """Read SPL token decimals."""

    response = (
        await client.get_token_supply(
            mint_pubkey
        )
    )

    value = response.value

    if value is None:

        raise PumpFunInvalidAccount(
            "token supply response is empty"
        )

    decimals = int(
        value.decimals
    )

    if decimals < 0 or decimals > 18:

        raise PumpFunInvalidAccount(
            f"invalid token decimals: {decimals}"
        )

    return decimals


# ---------------------------------------------------------------------------
# Bonding curve
# ---------------------------------------------------------------------------

async def get_bonding_curve(
    mint: str,
    rpc_url: str,
    commitment: str = "processed",
) -> PumpFunBondingCurve:
    """Read and decode a Pump.fun bonding curve."""

    mint_pubkey = Pubkey.from_string(
        mint
    )

    curve_address, _ = (
        get_bonding_curve_address(
            mint
        )
    )

    async with AsyncClient(
        rpc_url
    ) as client:

        response = (
            await client.get_account_info(
                curve_address,
                commitment=commitment,
                encoding="base64",
            )
        )

        account = response.value

        if account is None:

            raise PumpFunPoolNotFound(
                "no Pump.fun bonding curve "
                f"exists for {mint}"
            )

        data = _decode_account_data(
            account.data
        )

    return decode_bonding_curve(
        str(curve_address),
        data,
    )


# ---------------------------------------------------------------------------
# Pool information
# ---------------------------------------------------------------------------

async def get_pool_info(
    mint: str,
    rpc_url: str,
    sol_usd: Optional[float] = None,
    commitment: str = "processed",
) -> dict:
    """Return normalized Pump.fun bonding-curve market information."""

    mint_pubkey = Pubkey.from_string(
        mint
    )

    curve_address, _ = (
        get_bonding_curve_address(
            mint
        )
    )

    async with AsyncClient(
        rpc_url
    ) as client:

        curve_task = asyncio.create_task(
            client.get_account_info(
                curve_address,
                commitment=commitment,
                encoding="base64",
            )
        )

        decimals_task = asyncio.create_task(
            _get_token_decimals(
                client,
                mint_pubkey,
            )
        )

        try:

            account_response, decimals = (
                await asyncio.gather(
                    curve_task,
                    decimals_task,
                )
            )

        except Exception:

            for task in (
                curve_task,
                decimals_task,
            ):

                if not task.done():
                    task.cancel()

            await asyncio.gather(
                curve_task,
                decimals_task,
                return_exceptions=True,
            )

            raise

        account = (
            account_response.value
        )

        if account is None:

            raise PumpFunPoolNotFound(
                "no Pump.fun bonding curve "
                f"exists for {mint}"
            )

        data = _decode_account_data(
            account.data
        )

    curve = decode_bonding_curve(
        str(curve_address),
        data,
    )

    if (
        curve.virtual_token_reserves
        <= 0
    ):

        raise PumpFunInvalidAccount(
            "Pump.fun virtual token reserves "
            "are zero"
        )

    if (
        curve.virtual_sol_reserves
        <= 0
    ):

        raise PumpFunInvalidAccount(
            "Pump.fun virtual SOL reserves "
            "are zero"
        )

    token_unit = (
        10 ** decimals
    )

    virtual_tokens = (
        curve.virtual_token_reserves
        / token_unit
    )

    virtual_sol = (
        curve.virtual_sol_reserves
        / SOL_LAMPORTS_PER_SOL
    )

    real_tokens = (
        curve.real_token_reserves
        / token_unit
    )

    real_sol = (
        curve.real_sol_reserves
        / SOL_LAMPORTS_PER_SOL
    )

    total_supply = (
        curve.token_total_supply
        / token_unit
    )

    if virtual_tokens <= 0:

        raise PumpFunInvalidAccount(
            "Pump.fun virtual token supply "
            "is zero"
        )

    price_sol_per_token = (
        virtual_sol
        / virtual_tokens
    )

    if (
        price_sol_per_token
        <= 0
    ):

        raise PumpFunInvalidAccount(
            "Pump.fun calculated token price "
            "is non-positive"
        )

    if sol_usd is None:

        from app.scanners import price_feed

        sol_usd = (
            await price_feed.get_sol_usd_price(
                "https://lite-api.jup.ag/price/v3"
            )
        )

    sol_usd = float(
        sol_usd
    )

    if sol_usd <= 0:

        raise PumpFunError(
            "invalid SOL/USD price"
        )

    price_usd = (
        price_sol_per_token
        * sol_usd
    )

    market_cap_sol = (
        price_sol_per_token
        * total_supply
    )

    market_cap_usd = (
        market_cap_sol
        * sol_usd
    )

    liquidity_usd = (
        real_sol
        * sol_usd
    )

    return {
        "success": True,

        "source": "pumpfun",

        "pool_address": str(
            curve_address
        ),

        "creator": (
            curve.creator
            or ""
        ),

        "token_decimals": decimals,

        "price_sol_per_token": (
            price_sol_per_token
        ),

        "price_usd": price_usd,

        "supply_tokens": (
            total_supply
        ),

        "market_cap_sol": (
            market_cap_sol
        ),

        "market_cap_usd": (
            market_cap_usd
        ),

        "quote_reserve_sol": (
            real_sol
        ),

        "liquidity_usd": (
            liquidity_usd
        ),

        "real_token_reserves": (
            real_tokens
        ),

        "real_sol_reserves": (
            real_sol
        ),

        "virtual_token_reserves": (
            virtual_tokens
        ),

        "virtual_sol_reserves": (
            virtual_sol
        ),

        "token_total_supply": (
            total_supply
        ),

        "is_migrated": (
            curve.complete
        ),

        "complete": (
            curve.complete
        ),

        "commitment": (
            commitment
        ),

        "is_mayhem_mode": (
            curve.is_mayhem_mode
        ),

        "is_cashback_coin": (
            curve.is_cashback_coin
        ),
    }


# ---------------------------------------------------------------------------
# Pump.fun transaction builder
# ---------------------------------------------------------------------------

async def build_unsigned_buy_transaction(
    *,
    mint: str,
    owner_pubkey: str,
    amount_lamports: int,
    slippage_bps: int,
    rpc_url: str,
) -> dict:
    """Build an unsigned Pump.fun BUY transaction.

    The transaction is constructed by the Node/Pump.fun SDK builder.

    Returns:

        {
            "transaction_b64": "...",
            "blockhash": "...",
            "last_valid_block_height": ...,
            ...
        }

    The transaction is NOT signed and NOT submitted here.
    """

    if not mint:
        raise PumpFunTransactionBuildError(
            "mint_missing"
        )

    if not owner_pubkey:
        raise PumpFunTransactionBuildError(
            "owner_pubkey_missing"
        )

    if not rpc_url:
        raise PumpFunTransactionBuildError(
            "rpc_url_missing"
        )

    try:

        amount_lamports_int = int(
            amount_lamports
        )

    except (
        TypeError,
        ValueError,
    ) as exc:

        raise PumpFunTransactionBuildError(
            "amount_lamports_invalid"
        ) from exc

    if amount_lamports_int <= 0:

        raise PumpFunTransactionBuildError(
            "amount_lamports_must_be_positive"
        )

    try:

        slippage_bps_int = int(
            slippage_bps
        )

    except (
        TypeError,
        ValueError,
    ) as exc:

        raise PumpFunTransactionBuildError(
            "slippage_bps_invalid"
        ) from exc

    if (
        slippage_bps_int < 0
        or slippage_bps_int > 10_000
    ):

        raise PumpFunTransactionBuildError(
            "slippage_bps_out_of_range"
        )

    if not PUMPFUN_BUILDER_PATH.exists():

        raise PumpFunTransactionBuildError(
            "pumpfun_builder_not_found: "
            f"{PUMPFUN_BUILDER_PATH}"
        )

    payload = {
        "action": "buy",

        "baseMint": mint,

        "ownerPubkey": owner_pubkey,

        "amountLamports": (
            str(
                amount_lamports_int
            )
        ),

        "slippageBps": (
            slippage_bps_int
        ),

        "rpcUrl": rpc_url,
    }

    # -----------------------------------------------------------------------
    # Run Node builder.
    #
    # cwd is the existing dbc_builder directory so Node resolves:
    #
    #     @pump-fun/pump-sdk
    #
    # from its installed node_modules.
    # -----------------------------------------------------------------------

    process = (
        await asyncio.create_subprocess_exec(
            "node",
            str(
                PUMPFUN_BUILDER_PATH
            ),
            cwd=str(
                _DBC_BUILDER_DIR
            ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    )

    stdin_data = (
        json.dumps(
            payload
        ).encode("utf-8")
    )

    try:

        stdout, stderr = (
            await asyncio.wait_for(
                process.communicate(
                    stdin_data
                ),
                timeout=20,
            )
        )

    except asyncio.TimeoutError:

        try:
            process.kill()
        except ProcessLookupError:
            pass

        await process.communicate()

        raise PumpFunTransactionBuildError(
            "pumpfun_builder_timeout"
        )

    stdout_text = (
        stdout
        .decode(
            "utf-8",
            errors="replace",
        )
        .strip()
    )

    stderr_text = (
        stderr
        .decode(
            "utf-8",
            errors="replace",
        )
        .strip()
    )

    if not stdout_text:

        raise PumpFunTransactionBuildError(
            "pumpfun_builder_empty_response"
            + (
                f": {stderr_text}"
                if stderr_text
                else ""
            )
        )

    try:

        result = json.loads(
            stdout_text.splitlines()[-1]
        )

    except json.JSONDecodeError as exc:

        raise PumpFunTransactionBuildError(
            "pumpfun_builder_invalid_json: "
            f"{stdout_text[-1000:]}"
        ) from exc

    if not result.get(
        "success",
        False,
    ):

        error = result.get(
            "error",
            "unknown builder error",
        )

        raise PumpFunTransactionBuildError(
            str(error)
        )

    transaction_b64 = (
        result.get(
            "transaction_b64"
        )
    )

    blockhash = (
        result.get(
            "blockhash"
        )
    )

    last_valid_block_height = (
        result.get(
            "last_valid_block_height"
        )
    )

    if not transaction_b64:

        raise PumpFunTransactionBuildError(
            "pumpfun_builder_missing_transaction"
        )

    if not blockhash:

        raise PumpFunTransactionBuildError(
            "pumpfun_builder_missing_blockhash"
        )

    if (
        last_valid_block_height
        is None
    ):

        raise PumpFunTransactionBuildError(
            "pumpfun_builder_missing_last_valid_block_height"
        )

    # Keep stderr visible in debug logs but don't treat normal diagnostic
    # output as a transaction-builder failure when JSON succeeded.

    if stderr_text:

        logger.debug(
            "pumpfun_builder_stderr",
            extra={
                "mint": mint,
                "stderr": stderr_text[
                    -2000:
                ],
            },
        )

    logger.info(
        "pumpfun_unsigned_transaction_built",
        extra={
            "mint": mint,
            "owner": owner_pubkey,
            "amount_lamports": (
                amount_lamports_int
            ),
            "slippage_bps": (
                slippage_bps_int
            ),
            "blockhash": blockhash,
            "last_valid_block_height": (
                last_valid_block_height
            ),
            "priority_fee_micro_lamports": (
                result.get(
                    "priority_fee_micro_lamports"
                )
            ),
            "priority_fee_source": (
                result.get(
                    "priority_fee_source"
                )
            ),
        },
    )

    return result


# ---------------------------------------------------------------------------
# Pump.fun SELL transaction builder
# ---------------------------------------------------------------------------

async def build_unsigned_sell_transaction(
    *,
    mint: str,
    owner_pubkey: str,
    amount_tokens_raw: int,
    slippage_bps: int,
    rpc_url: str,
) -> dict:
    """Build an unsigned Pump.fun SELL transaction.

    The transaction is constructed by the dedicated Node/Pump.fun SDK
    SELL builder.

    This function never receives a private key, never signs and never
    submits a transaction.
    """

    if not mint:
        raise PumpFunTransactionBuildError(
            "mint_missing"
        )

    if not owner_pubkey:
        raise PumpFunTransactionBuildError(
            "owner_pubkey_missing"
        )

    if not rpc_url:
        raise PumpFunTransactionBuildError(
            "rpc_url_missing"
        )

    try:
        amount_tokens_raw_int = int(
            amount_tokens_raw
        )
    except (
        TypeError,
        ValueError,
    ) as exc:
        raise PumpFunTransactionBuildError(
            "amount_tokens_raw_invalid"
        ) from exc

    if amount_tokens_raw_int <= 0:
        raise PumpFunTransactionBuildError(
            "amount_tokens_raw_must_be_positive"
        )

    try:
        slippage_bps_int = int(
            slippage_bps
        )
    except (
        TypeError,
        ValueError,
    ) as exc:
        raise PumpFunTransactionBuildError(
            "slippage_bps_invalid"
        ) from exc

    if (
        slippage_bps_int < 0
        or slippage_bps_int > 10_000
    ):
        raise PumpFunTransactionBuildError(
            "slippage_bps_out_of_range"
        )

    if not PUMPFUN_SELL_BUILDER_PATH.exists():
        raise PumpFunTransactionBuildError(
            "pumpfun_sell_builder_not_found: "
            f"{PUMPFUN_SELL_BUILDER_PATH}"
        )

    payload = {
        "action": "sell",
        "baseMint": mint,
        "ownerPubkey": owner_pubkey,
        "amountTokensRaw": str(
            amount_tokens_raw_int
        ),
        "slippageBps": slippage_bps_int,
        "rpcUrl": rpc_url,
    }

    process = (
        await asyncio.create_subprocess_exec(
            "node",
            str(
                PUMPFUN_SELL_BUILDER_PATH
            ),
            cwd=str(
                _DBC_BUILDER_DIR
            ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    )

    stdin_data = (
        json.dumps(
            payload
        ).encode("utf-8")
    )

    try:
        stdout, stderr = (
            await asyncio.wait_for(
                process.communicate(
                    stdin_data
                ),
                timeout=20,
            )
        )
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass

        await process.communicate()

        raise PumpFunTransactionBuildError(
            "pumpfun_sell_builder_timeout"
        )

    stdout_text = (
        stdout
        .decode(
            "utf-8",
            errors="replace",
        )
        .strip()
    )

    stderr_text = (
        stderr
        .decode(
            "utf-8",
            errors="replace",
        )
        .strip()
    )

    if not stdout_text:
        raise PumpFunTransactionBuildError(
            "pumpfun_sell_builder_empty_response"
            + (
                f": {stderr_text}"
                if stderr_text
                else ""
            )
        )

    try:
        result = json.loads(
            stdout_text.splitlines()[-1]
        )
    except json.JSONDecodeError as exc:
        raise PumpFunTransactionBuildError(
            "pumpfun_sell_builder_invalid_json: "
            f"{stdout_text[-1000:]}"
        ) from exc

    if not result.get(
        "success",
        False,
    ):
        error = result.get(
            "error",
            "unknown Pump.fun SELL builder error",
        )

        raise PumpFunTransactionBuildError(
            str(error)
        )

    transaction_b64 = result.get(
        "transaction_b64"
    )

    blockhash = result.get(
        "blockhash"
    )

    last_valid_block_height = result.get(
        "last_valid_block_height"
    )

    if not transaction_b64:
        raise PumpFunTransactionBuildError(
            "pumpfun_sell_builder_missing_transaction"
        )

    if not blockhash:
        raise PumpFunTransactionBuildError(
            "pumpfun_sell_builder_missing_blockhash"
        )

    if (
        last_valid_block_height
        is None
    ):
        raise PumpFunTransactionBuildError(
            "pumpfun_sell_builder_missing_last_valid_block_height"
        )

    if stderr_text:
        logger.debug(
            "pumpfun_sell_builder_stderr",
            extra={
                "mint": mint,
                "stderr": stderr_text[-2000:],
            },
        )

    logger.info(
        "pumpfun_unsigned_sell_transaction_built",
        extra={
            "mint": mint,
            "owner": owner_pubkey,
            "amount_tokens_raw": (
                amount_tokens_raw_int
            ),
            "slippage_bps": (
                slippage_bps_int
            ),
            "blockhash": blockhash,
            "last_valid_block_height": (
                last_valid_block_height
            ),
            "priority_fee_micro_lamports": (
                result.get(
                    "priority_fee_micro_lamports"
                )
            ),
            "priority_fee_source": (
                result.get(
                    "priority_fee_source"
                )
            ),
        },
    )

    return result


# ---------------------------------------------------------------------------
# Convenience alias
# ---------------------------------------------------------------------------

build_buy_transaction = (
    build_unsigned_buy_transaction
)
