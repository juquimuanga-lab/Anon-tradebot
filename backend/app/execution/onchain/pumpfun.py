"""Pump.fun bonding-curve market data.

This module is READ-ONLY.

Responsibilities:

- Derive a Pump.fun bonding-curve PDA from a token mint.
- Read the bonding-curve account directly through Solana RPC.
- Decode the current bonding-curve reserves.
- Read token decimals/supply.
- Calculate the live Pump.fun token price in SOL/USD.
- Calculate market cap and available curve liquidity.
- Detect whether the bonding curve has completed/migrated.

It intentionally does NOT:

- sign transactions
- submit transactions
- buy tokens
- sell tokens
- use Meteora
- use Jupiter for launch-price discovery

The trading adapter will be added separately.

Pump.fun bonding curve:

    PDA = ["bonding-curve", mint]

Price:

    SOL/token =
        virtual_sol_reserves / virtual_token_reserves

adjusted for the token's base-unit decimals and SOL lamports.

Market cap:

    price_usd * token_total_supply

The Pump.fun program documentation describes the bonding curve as using
virtual token and SOL reserves for pricing and real reserves for actual
curve inventory. A curve becomes complete when real token reserves reach
zero. 
"""

import logging
import struct
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


# Pump.fun bonding-curve PDA seed.
BONDING_CURVE_SEED = (
    b"bonding-curve"
)


# Native SOL.
SOL_LAMPORTS_PER_SOL = 1_000_000_000


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
    """Raised when the bonding curve account cannot be decoded."""


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


def decode_bonding_curve(
    address: str,
    data: bytes,
) -> PumpFunBondingCurve:
    """Decode a Pump.fun BondingCurve account.

    Anchor account data starts with an 8-byte discriminator.

    Current core fields:

        virtual_token_reserves  u64
        virtual_sol_reserves    u64
        real_token_reserves     u64
        real_sol_reserves       u64
        token_total_supply      u64
        complete                 bool

    Newer Pump.fun deployments append creator and feature flags.

    We decode the stable prefix first and then conditionally decode the
    appended fields when present.

    This means a newer 150-byte bonding curve account remains compatible
    with the price reader.
    """

    # 8-byte Anchor account discriminator.
    MIN_PREFIX_LENGTH = 8 + (
        5 * 8
    ) + 1

    if len(data) < MIN_PREFIX_LENGTH:

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
    # Newer creator field
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
    # Newer feature flags
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
# Pool state
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

        raw_data = account.data

        # Solana RPC normally returns:
        #
        #     (base64_string, encoding)
        #
        # for base64 account data.
        if isinstance(
            raw_data,
            tuple,
        ):

            encoded = raw_data[0]

            import base64

            try:

                data = base64.b64decode(
                    encoded
                )

            except Exception as exc:

                raise PumpFunInvalidAccount(
                    "failed to decode Pump.fun "
                    "bonding curve account"
                ) from exc

        elif isinstance(
            raw_data,
            bytes,
        ):

            data = raw_data

        else:

            raise PumpFunInvalidAccount(
                "unexpected Solana account-data format"
            )


        curve = (
            decode_bonding_curve(
                str(curve_address),
                data,
            )
        )

        return curve


# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------

async def get_pool_info(
    mint: str,
    rpc_url: str,
    sol_usd: Optional[float] = None,
    commitment: str = "processed",
) -> dict:
    """Return Pump.fun bonding-curve market information.

    Returned fields intentionally mirror the Meteora information shape
    where possible so the scanner can use a common TokenSnapshot model.

    Price is derived from the live virtual reserves:

        SOL/token =
            virtual_sol_reserves / 1e9
            /
            virtual_token_reserves / 10^decimals

    Market cap:

        price_usd * token_total_supply

    Liquidity:

        real_sol_reserves converted to USD

    NOTE:

    `real_sol_reserves` is the SOL accumulated by the bonding curve.
    It is not the same concept as a traditional AMM pool TVL.
    """

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

        # ---------------------------------------------------------------
        # Read bonding curve and token decimals concurrently.
        # ---------------------------------------------------------------

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


        raw_data = account.data


        if isinstance(
            raw_data,
            tuple,
        ):

            encoded = raw_data[0]

            import base64

            try:

                data = base64.b64decode(
                    encoded
                )

            except Exception as exc:

                raise PumpFunInvalidAccount(
                    "failed to decode Pump.fun "
                    "bonding curve account"
                ) from exc

        elif isinstance(
            raw_data,
            bytes,
        ):

            data = raw_data

        else:

            raise PumpFunInvalidAccount(
                "unexpected Solana account-data format"
            )


        curve = (
            decode_bonding_curve(
                str(curve_address),
                data,
            )
        )


    # -----------------------------------------------------------------------
    # Validate curve
    # -----------------------------------------------------------------------

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


    # -----------------------------------------------------------------------
    # Base-unit conversions
    # -----------------------------------------------------------------------

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


    # -----------------------------------------------------------------------
    # Live price
    # -----------------------------------------------------------------------

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


    # -----------------------------------------------------------------------
    # SOL/USD
    # -----------------------------------------------------------------------

    if sol_usd is None:

        # Import locally to avoid making the Pump.fun module depend on the
        # scanner price feed at import time.
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


    # -----------------------------------------------------------------------
    # Return normalized data
    # -----------------------------------------------------------------------

    result = {
        "success": True,

        "source": "pumpfun",

        "pool_address": str(
            curve_address
        ),

        "creator": (
            curve.creator
            or ""
        ),

        "token_decimals": (
            decimals
        ),

        "price_sol_per_token": (
            price_sol_per_token
        ),

        "price_usd": (
            price_usd
        ),

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


    logger.debug(
        "pumpfun_pool_info",
        extra={
            "mint": mint,
            "bonding_curve": str(
                curve_address
            ),
            "price_sol": (
                price_sol_per_token
            ),
            "price_usd": price_usd,
            "market_cap_usd": (
                market_cap_usd
            ),
            "complete": (
                curve.complete
            ),
        },
    )


    return result
