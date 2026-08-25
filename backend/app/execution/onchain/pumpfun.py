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
import time
from collections import defaultdict
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

# Avoid duplicate bonding-curve reads during rapid qualification passes.
PUMPFUN_POOL_CACHE_SECONDS = 1.25
PUMPFUN_DECIMALS_CACHE_SECONDS = 30.0
PUMPFUN_TOKEN_DECIMALS = 6
_pumpfun_pool_cache: dict[str, tuple[float, dict]] = {}
_pumpfun_decimals_cache: dict[str, tuple[float, int]] = {}

# ---------------------------------------------------------------------------
# Early-launch organic-flow safety gate
# ---------------------------------------------------------------------------
# These are internal safety defaults. They are deliberately independent of
# Telegram RuleParams so admins can keep their existing multi-ruleset UI.
#
# The gate is designed to reject strong coordination signals without requiring
# a large holder count or a long observation window.
PUMPFUN_LAUNCH_SAFETY_CACHE_SECONDS = 2.0
PUMPFUN_LAUNCH_SAFETY_SIGNATURE_LIMIT = 30
PUMPFUN_LAUNCH_SAFETY_MAX_AGE_SECONDS = 20.0

PUMPFUN_SAFETY_TOP_BUYER_SHARE = 0.50
PUMPFUN_SAFETY_TOP3_BUYER_SHARE = 0.85
PUMPFUN_SAFETY_SAME_SLOT_SHARE = 0.80
PUMPFUN_SAFETY_SAME_SIZE_SHARE = 0.80
PUMPFUN_SAFETY_SHARED_FUNDER_MAX_BUYERS = 3
PUMPFUN_SAFETY_SHARED_FUNDER_VOLUME_SHARE = 0.45
PUMPFUN_SAFETY_CREATOR_BUY_SHARE = 0.20
PUMPFUN_SAFETY_MIN_BUY_PRESSURE = 0.55
PUMPFUN_SAFETY_MIN_BUY_EVENTS_FOR_PRESSURE = 4
PUMPFUN_SAFETY_PATCH_VERSION = "V7_SOL_FLOW_FIX"

_pumpfun_launch_safety_cache: dict[str, tuple[float, dict]] = {}


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
# Token decimals (legacy helper retained for compatibility)
# ---------------------------------------------------------------------------

async def _get_token_decimals(
    client: AsyncClient,
    mint_pubkey: Pubkey,
) -> int:
    """Read SPL token decimals if explicitly needed elsewhere.

    The hot-path get_pool_info() no longer calls this RPC; Pump.fun launch
    tokens use the fixed 6-decimal convention.
    """
    mint = str(mint_pubkey)
    now = asyncio.get_running_loop().time()
    cached = _pumpfun_decimals_cache.get(mint)
    if cached and (now - cached[0]) < PUMPFUN_DECIMALS_CACHE_SECONDS:
        return cached[1]

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

    _pumpfun_decimals_cache[mint] = (now, decimals)
    return decimals


# ---------------------------------------------------------------------------
# Bonding curve
# ---------------------------------------------------------------------------
async def _get_curve_account_data(
    curve_address: Pubkey,
    rpc_url: str,
    commitment: str,
) -> bytes:
    """Read a Pump.fun curve account with provider fallback."""
    candidates = _rpc_candidate_urls(rpc_url)
    last_exc: Exception | None = None
    for index, candidate in enumerate(candidates):
        try:
            async with AsyncClient(candidate) as client:
                response = await client.get_account_info(
                    curve_address,
                    commitment=commitment,
                    encoding="base64",
                )
                account = response.value
                if account is not None:
                    return _decode_account_data(account.data)
                last_exc = PumpFunPoolNotFound(
                    f"no Pump.fun bonding curve exists for {curve_address}"
                )
        except Exception as exc:
            last_exc = exc
            if index < len(candidates) - 1:
                logger.warning(
                    "pumpfun_curve_account_provider_fallback",
                    extra={"curve": str(curve_address), "provider_index": index},
                )
                continue
            raise
    if last_exc:
        raise last_exc
    raise PumpFunPoolNotFound(f"no Pump.fun bonding curve exists for {curve_address}")



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

    data = await _get_curve_account_data(
        curve_address,
        rpc_url,
        commitment,
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

    now = asyncio.get_running_loop().time()
    cached = _pumpfun_pool_cache.get(mint)
    if cached and (now - cached[0]) < PUMPFUN_POOL_CACHE_SECONDS:
        return dict(cached[1])

    mint_pubkey = Pubkey.from_string(
        mint
    )

    curve_address, _ = (
        get_bonding_curve_address(
            mint
        )
    )

    # Pump.fun launch tokens use 6 decimals. The bonding-curve account
    # already contains the token supply/reserve values needed for pricing,
    # so do NOT make a second GetTokenSupply RPC call here.
    decimals = PUMPFUN_TOKEN_DECIMALS

    data = await _get_curve_account_data(
        curve_address,
        rpc_url,
        commitment,
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

    _pumpfun_pool_cache[mint] = (now, result)
    return dict(result)



# ---------------------------------------------------------------------------
# Early-launch organic-flow analysis
# ---------------------------------------------------------------------------

def _rpc_json_value(body: dict, *, method: str = ""):
    """Return a JSON-RPC result and preserve the real provider error."""
    if not isinstance(body, dict):
        raise RuntimeError(
            f"{method or 'RPC'} returned non-object JSON"
        )

    error = body.get("error")
    if error is not None:
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or "unknown RPC error"
            data = error.get("data")
            detail = f"code={code} message={message}"
            if data is not None:
                detail += f" data={str(data)[:500]}"
        else:
            detail = str(error)[:700]
        raise RuntimeError(
            f"{method or 'RPC'} returned JSON-RPC error: {detail}"
        )

    if "result" not in body:
        raise RuntimeError(
            f"{method or 'RPC'} response missing result"
        )

    return body.get("result")


def _rpc_candidate_urls(rpc_url: str) -> list[str]:
    """Return the primary Solana RPC plus optional Alchemy fallback."""
    urls: list[str] = []
    if rpc_url:
        urls.append(str(rpc_url))
    try:
        from app.config.settings import settings
        fallback = getattr(settings, "alchemy_solana_rpc_url", None)
        if fallback and str(fallback) not in urls:
            urls.append(str(fallback))
    except Exception:
        pass
    return urls


async def _raw_rpc_call(
    rpc_url: str,
    method: str,
    params: list,
    *,
    retries: int = 3,
):
    """Call Solana JSON-RPC with bounded retries and provider fallback.

    Helius remains primary. If it is throttled/unavailable, the optional
    Alchemy Solana RPC is tried before the caller gives up. Deterministic
    account/parameter errors are surfaced rather than hidden.
    """
    import httpx

    candidates = _rpc_candidate_urls(rpc_url)
    if not candidates:
        raise RuntimeError(f"invalid Solana RPC URL for {method}: {rpc_url!r}")

    payload = {
        "jsonrpc": "2.0",
        "id": f"pumpfun-safety-{method}",
        "method": method,
        "params": params,
    }

    delays = (0.0, 0.20, 0.50, 1.00)[: max(1, retries + 1)]
    last_exc: Exception | None = None

    timeout = httpx.Timeout(connect=2.5, read=5.0, write=5.0, pool=2.5)

    for provider_index, candidate in enumerate(candidates):
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"Content-Type": "application/json"},
            follow_redirects=True,
        ) as client:
            for attempt, delay in enumerate(delays, start=1):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    response = await client.post(candidate, json=payload)
                    body_text = response.text[:700]

                    if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                        raise RuntimeError(
                            f"{method} HTTP {response.status_code} "
                            f"(attempt {attempt}/{len(delays)}): {body_text}"
                        )

                    response.raise_for_status()
                    try:
                        body = response.json()
                    except Exception as exc:
                        raise RuntimeError(
                            f"{method} returned invalid JSON "
                            f"(HTTP {response.status_code}): {body_text}"
                        ) from exc

                    return _rpc_json_value(body, method=method)

                except Exception as exc:
                    last_exc = exc
                    message = str(exc)
                    transient = (
                        isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))
                        or any(
                            f" HTTP {status} " in message
                            for status in (408, 425, 429, 500, 502, 503, 504)
                        )
                    )
                    if not transient:
                        # Deterministic errors should not be hidden by a
                        # second provider returning a different error.
                        raise RuntimeError(
                            f"{method} RPC failed on provider {provider_index + 1}: {exc}"
                        ) from exc

                    if attempt < len(delays):
                        logger.warning(
                            "pumpfun_launch_safety_rpc_retry",
                            extra={
                                "method": method,
                                "attempt": attempt,
                                "max_attempts": len(delays),
                                "provider_index": provider_index,
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                        )

        if provider_index < len(candidates) - 1:
            logger.warning(
                "pumpfun_launch_safety_rpc_provider_fallback",
                extra={"method": method, "provider_index": provider_index, "fallback": "alchemy"},
            )

    raise RuntimeError(
        f"{method} RPC failed on all configured providers: {last_exc}"
    ) from last_exc


async def _get_launch_transactions(
    rpc_url: str,
    curve_address: str,
    *,
    limit: int,
) -> list[dict]:
    """Fetch recent Pump.fun curve transactions reliably.

    Helius may expose the signature before the full transaction is queryable.
    We therefore:
      1. use confirmed signatures (required by this RPC endpoint),
      2. retry getTransaction when it returns null,
      3. retry transient RPC failures,
      4. keep the successful transactions even if one signature is temporarily
         unavailable,
      5. log the exact failure instead of hiding it behind RuntimeError.
    """
    signatures = await _raw_rpc_call(
        rpc_url,
        "getSignaturesForAddress",
        [
            curve_address,
            {
                "limit": max(1, min(int(limit), 100)),
                "commitment": "confirmed",
            },
        ],
        retries=3,
    )

    if not isinstance(signatures, list):
        raise RuntimeError(
            "getSignaturesForAddress returned a non-list result"
        )

    transactions: list[dict] = []
    fetch_failures = 0

    for item in signatures:
        if not isinstance(item, dict) or item.get("err") is not None:
            continue

        signature = item.get("signature")
        if not signature:
            continue

        tx = None

        # A signature can become visible before its full transaction record.
        # Retry null responses as well as transient RPC errors.
        for attempt in range(1, 5):
            try:
                tx = await _raw_rpc_call(
                    rpc_url,
                    "getTransaction",
                    [
                        signature,
                        {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0,
                            "commitment": "confirmed",
                        },
                    ],
                    retries=2,
                )
            except Exception as exc:
                fetch_failures += 1
                logger.warning(
                    "pumpfun_launch_safety_transaction_fetch_failed "
                    f"signature={signature} attempt={attempt}/4 "
                    f"error={type(exc).__name__}: {exc}"
                )
                tx = None

            if isinstance(tx, dict):
                break

            if attempt < 4:
                await asyncio.sleep(0.15 * attempt)

        if isinstance(tx, dict):
            transactions.append(tx)
        else:
            fetch_failures += 1
            logger.warning(
                "pumpfun_launch_safety_transaction_unavailable "
                f"signature={signature} attempts=4"
            )

        if len(transactions) >= min(8, max(1, int(limit))):
            break

    logger.info(
        "pumpfun_launch_safety_transaction_fetch_summary "
        f"signatures={len(signatures)} "
        f"transactions={len(transactions)} "
        f"failures={fetch_failures}"
    )

    return transactions

def _account_key_list(tx: dict) -> list[str]:
    message = ((tx or {}).get("transaction") or {}).get("message") or {}
    keys = message.get("accountKeys") or []
    result = []
    for item in keys:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            result.append(str(item.get("pubkey") or item.get("address") or ""))
        else:
            result.append(str(item))
    return result


def _token_balance_map(entries: list, mint: str) -> dict[int, tuple[str, int]]:
    result = {}
    for item in entries or []:
        if not isinstance(item, dict) or item.get("mint") != mint:
            continue
        try:
            index = int(item.get("accountIndex"))
            amount = int(
                ((item.get("uiTokenAmount") or {}).get("amount"))
                or 0
            )
        except (TypeError, ValueError):
            continue
        owner = str(item.get("owner") or "")
        result[index] = (owner, amount)
    return result


def _extract_direct_funder(tx: dict, buyer: str) -> str | None:
    """Best-effort direct SOL funder detection from parsed System transfers."""
    if not buyer:
        return None

    def inspect_instruction(ix):
        if not isinstance(ix, dict):
            return None
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            return None
        if parsed.get("type") != "transfer":
            return None
        if ix.get("program") != "system" and ix.get("programId") != "11111111111111111111111111111111":
            return None
        info = parsed.get("info") or {}
        destination = str(info.get("destination") or "")
        source = str(info.get("source") or "")
        if destination == buyer and source and source != buyer:
            return source
        return None

    message = ((tx or {}).get("transaction") or {}).get("message") or {}
    for ix in message.get("instructions") or []:
        funder = inspect_instruction(ix)
        if funder:
            return funder

    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in group.get("instructions") or []:
            funder = inspect_instruction(ix)
            if funder:
                return funder
    return None


def _round_trade_size(sol_spent: float) -> float:
    if sol_spent <= 0:
        return 0.0
    # 0.001 SOL buckets catch synchronized fixed-size buys while tolerating
    # normal curve-price variation.
    return round(sol_spent, 3)


def _parsed_system_transfers(tx: dict, curve_address: str) -> tuple[dict[str, int], dict[str, int]]:
    """Extract native SOL transfers involving the Pump.fun curve.

    Preferred source:
      * source=buyer, destination=curve -> buy SOL
      * source=curve, destination=seller -> sell SOL

    Some RPC responses / newer Pump.fun instruction paths do not expose the
    economically relevant System transfer as a parsed inner instruction.
    In that case the caller can use the curve account's pre/post lamport delta
    as a transaction-level fallback.

    Returns:
        (buy_sol_by_wallet_lamports, sell_sol_by_wallet_lamports)
    """
    buys: dict[str, int] = defaultdict(int)
    sells: dict[str, int] = defaultdict(int)

    if not curve_address:
        return dict(buys), dict(sells)

    def inspect(ix):
        if not isinstance(ix, dict):
            return
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") != "transfer":
            return
        program = ix.get("program")
        program_id = ix.get("programId")
        if program != "system" and program_id != "11111111111111111111111111111111":
            return

        info = parsed.get("info") or {}
        source = str(info.get("source") or "")
        destination = str(info.get("destination") or "")
        try:
            lamports = int(info.get("lamports") or 0)
        except (TypeError, ValueError):
            lamports = 0
        if lamports <= 0:
            return

        if destination == curve_address and source and source != curve_address:
            buys[source] += lamports
        elif source == curve_address and destination and destination != curve_address:
            sells[destination] += lamports

    message = ((tx or {}).get("transaction") or {}).get("message") or {}
    for ix in message.get("instructions") or []:
        inspect(ix)

    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in group.get("instructions") or []:
            inspect(ix)

    return dict(buys), dict(sells)


def _curve_lamport_delta(tx: dict, curve_address: str) -> int | None:
    """Return the curve PDA's post-pre native SOL lamport delta.

    ``preBalances``/``postBalances`` are indexed by the transaction message's
    account keys. This is a transaction-level fallback for providers that omit
    the parsed System transfer used by Pump.fun's buy/sell instruction.
    """
    if not curve_address:
        return None

    meta = (tx or {}).get("meta") or {}
    pre_balances = meta.get("preBalances") or []
    post_balances = meta.get("postBalances") or []
    if not pre_balances or not post_balances:
        return None

    message = ((tx or {}).get("transaction") or {}).get("message") or {}
    keys = []
    for item in message.get("accountKeys") or []:
        if isinstance(item, str):
            keys.append(item)
        elif isinstance(item, dict):
            keys.append(str(item.get("pubkey") or item.get("address") or ""))
        else:
            keys.append(str(item))

    loaded = meta.get("loadedAddresses") or {}
    keys.extend(str(x) for x in (loaded.get("writable") or []))
    keys.extend(str(x) for x in (loaded.get("readonly") or []))

    try:
        index = keys.index(curve_address)
        pre = int(pre_balances[index])
        post = int(post_balances[index])
    except (ValueError, IndexError, TypeError):
        return None

    return post - pre


def _allocate_curve_delta_to_traders(
    delta_lamports: int | None,
    buyers: dict[str, int],
    sellers: dict[str, int],
) -> tuple[dict[str, int], dict[str, int]]:
    """Attribute a curve balance delta to token-side traders.

    Normally a Pump.fun launch transaction has one economically active buyer
    or seller. If a transaction contains multiple token owners, distribute
    the absolute curve delta proportionally to token volume.
    """
    buys: dict[str, int] = defaultdict(int)
    sells: dict[str, int] = defaultdict(int)

    if delta_lamports is None or delta_lamports == 0:
        return dict(buys), dict(sells)

    if delta_lamports > 0 and buyers:
        total = sum(max(0, int(v)) for v in buyers.values())
        if total > 0:
            remaining = delta_lamports
            items = list(buyers.items())
            for pos, (wallet, amount) in enumerate(items):
                if pos == len(items) - 1:
                    allocated = remaining
                else:
                    allocated = int(delta_lamports * int(amount) / total)
                    allocated = max(0, min(allocated, remaining))
                if allocated > 0:
                    buys[wallet] += allocated
                    remaining -= allocated

    elif delta_lamports < 0 and sellers:
        absolute = abs(delta_lamports)
        total = sum(max(0, int(v)) for v in sellers.values())
        if total > 0:
            remaining = absolute
            items = list(sellers.items())
            for pos, (wallet, amount) in enumerate(items):
                if pos == len(items) - 1:
                    allocated = remaining
                else:
                    allocated = int(absolute * int(amount) / total)
                    allocated = max(0, min(allocated, remaining))
                if allocated > 0:
                    sells[wallet] += allocated
                    remaining -= allocated

    return dict(buys), dict(sells)

def _extract_buy_sell_event(
    tx: dict,
    mint: str,
    curve_address: str = "",
) -> dict | None:
    meta = (tx or {}).get("meta") or {}
    pre = _token_balance_map(meta.get("preTokenBalances") or [], mint)
    post = _token_balance_map(meta.get("postTokenBalances") or [], mint)
    if not post and not pre:
        return None

    buyers = defaultdict(int)
    sellers = defaultdict(int)

    indices = set(pre) | set(post)
    for index in indices:
        owner, post_amount = post.get(index, ("", 0))
        pre_owner, pre_amount = pre.get(index, (owner, 0))
        owner = owner or pre_owner
        delta = post_amount - pre_amount
        if not owner or delta == 0:
            continue

        if delta > 0:
            buyers[owner] += delta
        else:
            sellers[owner] += abs(delta)

    if not buyers and not sellers:
        return None

    buy_sol_lamports, sell_sol_lamports = _parsed_system_transfers(
        tx,
        curve_address,
    )

    # Fallback for RPC responses/instruction paths where the parsed System
    # transfer is missing: use the curve PDA's native lamport delta and
    # attribute it to the token-side trader(s).
    curve_delta = _curve_lamport_delta(tx, curve_address)
    if curve_delta is not None:
        fallback_buys, fallback_sells = _allocate_curve_delta_to_traders(
            curve_delta,
            buyers,
            sellers,
        )
        if not buy_sol_lamports and fallback_buys:
            buy_sol_lamports = fallback_buys
        if not sell_sol_lamports and fallback_sells:
            sell_sol_lamports = fallback_sells

    buyer_sol = {
        buyer: amount / SOL_LAMPORTS_PER_SOL
        for buyer, amount in buy_sol_lamports.items()
    }
    seller_sol = {
        seller: amount / SOL_LAMPORTS_PER_SOL
        for seller, amount in sell_sol_lamports.items()
    }

    # Never manufacture a zero when the provider exposed neither parsed
    # transfers nor a usable curve balance delta.
    sol_flow_available = bool(
        buy_sol_lamports or sell_sol_lamports
    )

    slot = (tx or {}).get("slot")
    return {
        "buyers": dict(buyers),
        "sellers": dict(sellers),
        "buyer_sol": buyer_sol,
        "seller_sol": seller_sol,
        "sol_flow_available": sol_flow_available,
        "slot": int(slot) if slot is not None else None,
    }


async def analyze_launch_safety(
    mint: str,
    rpc_url: str,
    creator: str = "",
    created_at: float | None = None,
) -> dict:
    """Inspect the first Pump.fun curve transactions for coordination signals.

    This is intentionally a safety layer, not another admin ruleset. It uses
    multiple weak signals together so a normal launch is not rejected merely
    because two snipers happened to buy in the same slot.
    """
    now = time.monotonic()
    cached = _pumpfun_launch_safety_cache.get(mint)
    if cached and (now - cached[0]) < PUMPFUN_LAUNCH_SAFETY_CACHE_SECONDS:
        return dict(cached[1])

    result = {
        "status": "degraded",
        "safe": True,
        "mint": mint,
        "reason": "",
        "risk_score": 0,
        "signals": {},
    }

    try:
        curve_address, _ = get_bonding_curve_address(mint)
        transactions = await _get_launch_transactions(
            rpc_url,
            str(curve_address),
            limit=PUMPFUN_LAUNCH_SAFETY_SIGNATURE_LIMIT,
        )

        if not transactions:
            raise RuntimeError(
                "no launch transactions could be retrieved from Pump.fun curve"
            )

        events = []
        for tx in transactions:
            event = _extract_buy_sell_event(tx, mint, str(curve_address))
            if not event:
                continue

            # A newly-created curve has no reason to have an old transaction
            # stream. If block time exists, retain only a short launch window.
            block_time = tx.get("blockTime")
            if block_time is not None and created_at:
                if float(block_time) < float(created_at) - 2.0:
                    continue

            signatures = ((tx.get("transaction") or {}).get("signatures") or [])
            event["signature"] = signatures[0] if signatures else ""
            event["block_time"] = block_time
            event["funders"] = {
                buyer: _extract_direct_funder(tx, buyer)
                for buyer in event["buyers"]
            }
            events.append(event)

            if len(events) >= 25:
                break

        buy_events = [e for e in events if e["buyers"]]
        sell_events = [e for e in events if e["sellers"]]

        buyer_volume = defaultdict(int)
        seller_volume = defaultdict(int)
        buyer_sol = defaultdict(float)
        seller_sol = defaultdict(float)
        slot_volume = defaultdict(int)
        size_volume = defaultdict(int)
        funder_volume = defaultdict(int)
        funder_buyers = defaultdict(set)
        creator_volume = 0

        total_buy_tokens = 0
        total_sell_tokens = 0
        total_buy_sol = 0.0
        total_sell_sol = 0.0
        sol_flow_events = 0
        sol_flow_buy_events = 0
        sol_flow_sell_events = 0

        for event in events:
            if event.get("sol_flow_available"):
                sol_flow_events += 1

        sol_flow_buy_events = sum(
            1 for event in buy_events if event.get("buyer_sol")
        )
        sol_flow_sell_events = sum(
            1 for event in sell_events if event.get("seller_sol")
        )

        for event in buy_events:
            for buyer, amount in event["buyers"].items():
                buyer_volume[buyer] += int(amount)
                total_buy_tokens += int(amount)

                # Only count SOL when the transaction contained an
                # authoritative curve System transfer for this wallet.
                sol_amount = event["buyer_sol"].get(buyer)
                if sol_amount is not None and sol_amount > 0:
                    buyer_sol[buyer] += float(sol_amount)
                    total_buy_sol += float(sol_amount)
                    size_bucket = _round_trade_size(float(sol_amount))
                    if size_bucket > 0:
                        size_volume[size_bucket] += int(amount)

                if event.get("slot") is not None:
                    slot_volume[event["slot"]] += int(amount)

                funder = event["funders"].get(buyer)
                if funder:
                    funder_volume[funder] += int(amount)
                    funder_buyers[funder].add(buyer)
                if creator and buyer == creator:
                    creator_volume += int(amount)

        for event in sell_events:
            for seller, amount in event["sellers"].items():
                seller_volume[seller] += int(amount)
                total_sell_tokens += int(amount)
                sol_amount = event["seller_sol"].get(seller)
                if sol_amount is not None and sol_amount > 0:
                    seller_sol[seller] += float(sol_amount)
                    total_sell_sol += float(sol_amount)

        unique_buyers = len(buyer_volume)
        buy_count = sum(len(e["buyers"]) for e in buy_events)
        sell_count = sum(len(e["sellers"]) for e in sell_events)

        def share(value, total):
            return (float(value) / float(total)) if total > 0 else 0.0

        sorted_buyer = sorted(buyer_volume.values(), reverse=True)
        top1_share = share(sorted_buyer[0] if sorted_buyer else 0, total_buy_tokens)
        top3_share = share(sum(sorted_buyer[:3]), total_buy_tokens)

        # Wallet concentration is meaningful only when actual SOL flow was
        # observed. Never turn "unknown" into a fake 100% concentration.
        sorted_buyer_sol = sorted(buyer_sol.values(), reverse=True)
        top10_sol_share = (
            share(sum(sorted_buyer_sol[:10]), total_buy_sol)
            if total_buy_sol > 0
            else None
        )

        creator_sell_volume = 0
        if creator:
            for event in sell_events:
                creator_sell_volume += int(event["sellers"].get(creator, 0))
        creator_sell_share = share(creator_sell_volume, total_sell_tokens)

        event_times = [
            float(e["block_time"])
            for e in events
            if e.get("block_time") is not None
        ]
        if len(event_times) >= 2:
            flow_span_seconds = max(1.0, max(event_times) - min(event_times))
        elif created_at and event_times:
            flow_span_seconds = max(1.0, max(event_times) - float(created_at))
        else:
            flow_span_seconds = max(1.0, float(len(events)))

        buy_velocity_sol = (
            total_buy_sol / flow_span_seconds
            if total_buy_sol > 0
            else None
        )

        if total_buy_sol > 0 or total_sell_sol > 0:
            buy_pressure = share(
                total_buy_sol,
                total_buy_sol + total_sell_sol,
            )
            buy_sell_ratio = (
                total_buy_sol / total_sell_sol
                if total_sell_sol > 0
                else None
            )
        else:
            buy_pressure = None
            buy_sell_ratio = None

        # Preserve the original buyer-event diversity metric. It is based on
        # token-account ownership/events and remains useful independently of
        # whether SOL attribution was available for every wallet.
        buyer_diversity = unique_buyers / max(1, buy_count)

        max_slot_share = 0.0
        if slot_volume:
            max_slot_share = share(max(slot_volume.values()), total_buy_tokens)

        max_size_share = 0.0
        if size_volume:
            max_size_share = share(max(size_volume.values()), total_buy_tokens)

        max_shared_funder_buyers = 0
        max_shared_funder_volume_share = 0.0
        for funder, buyers in funder_buyers.items():
            max_shared_funder_buyers = max(max_shared_funder_buyers, len(buyers))
            max_shared_funder_volume_share = max(
                max_shared_funder_volume_share,
                share(funder_volume[funder], total_buy_tokens),
            )

        creator_buy_share = share(creator_volume, total_buy_tokens)

        risk = 0
        reasons = []

        if top1_share >= PUMPFUN_SAFETY_TOP_BUYER_SHARE:
            risk += 30
            reasons.append(f"top buyer controls {top1_share:.0%} of early buy flow")
        elif top1_share >= 0.40:
            risk += 15

        if top3_share >= PUMPFUN_SAFETY_TOP3_BUYER_SHARE:
            risk += 25
            reasons.append(f"top 3 buyers control {top3_share:.0%} of early buy flow")
        elif top3_share >= 0.70:
            risk += 10

        if max_slot_share >= PUMPFUN_SAFETY_SAME_SLOT_SHARE:
            risk += 25
            reasons.append(f"{max_slot_share:.0%} of early buy flow landed in one slot")

        if max_size_share >= PUMPFUN_SAFETY_SAME_SIZE_SHARE and buy_count >= 4:
            risk += 20
            reasons.append(f"{max_size_share:.0%} of buy flow uses the same SOL-size bucket")

        if (
            max_shared_funder_buyers >= PUMPFUN_SAFETY_SHARED_FUNDER_MAX_BUYERS
            and max_shared_funder_volume_share >= PUMPFUN_SAFETY_SHARED_FUNDER_VOLUME_SHARE
        ):
            risk += 40
            reasons.append(
                f"{max_shared_funder_buyers} early buyers share one direct funder "
                f"({max_shared_funder_volume_share:.0%} of flow)"
            )

        if creator_buy_share >= PUMPFUN_SAFETY_CREATOR_BUY_SHARE:
            risk += 30
            reasons.append(f"creator controls {creator_buy_share:.0%} of early buy flow")

        if (
            buy_count >= PUMPFUN_SAFETY_MIN_BUY_EVENTS_FOR_PRESSURE
            and buy_pressure is not None
            and buy_pressure < PUMPFUN_SAFETY_MIN_BUY_PRESSURE
        ):
            risk += 25
            reasons.append(f"early buy pressure is only {buy_pressure:.0%}")

        # Hard red flags require strong evidence. Moderate early-wallet
        # concentration is deferred to the Graduation Hunter when there is
        # genuine buying momentum and no creator/funder/same-slot red flag.
        strong_momentum = (
            buy_pressure is not None
            and buy_pressure >= 0.60
            and buy_sell_ratio is not None
            and buy_sell_ratio >= 1.50
            and unique_buyers >= 3
        )
        moderate_concentration = (
            strong_momentum
            and top1_share < 0.80
            and top3_share < 0.98
            and max_slot_share < 0.70
            and max_size_share < 0.80
            and max_shared_funder_buyers < 3
            and max_shared_funder_volume_share < 0.45
            and creator_buy_share < 0.20
        )

        reject = (
            (max_shared_funder_buyers >= 3 and max_shared_funder_volume_share >= 0.45)
            or creator_buy_share >= 0.35
            or max_slot_share >= 0.90
            or (top1_share >= 0.80 and not moderate_concentration)
            or (top3_share >= 0.98 and not moderate_concentration)
            or (risk >= 70 and not moderate_concentration)
            or (
                top1_share >= 0.60
                and not moderate_concentration
                and (
                    buy_pressure is None
                    or buy_pressure < 0.60
                    or buy_sell_ratio is None
                    or buy_sell_ratio < 1.50
                    or unique_buyers < 3
                )
            )
        )

        result.update(
            {
                "status": "ready",
                "safe": not reject,
                "reason": "; ".join(reasons[:4]) if reject else "",
                "risk_score": min(100, risk),
                "moderate_concentration_deferred_to_hunter": bool(
                    moderate_concentration and not reject
                ),
                "signals": {
                    "patch_version": PUMPFUN_SAFETY_PATCH_VERSION,
                    "transactions_examined": len(events),
                    "transaction_records_retrieved": len(transactions),
                    "transaction_data_complete": bool(transactions),
                    "buy_transactions": buy_count,
                    "sell_transactions": sell_count,
                    "unique_buyers": unique_buyers,
                    "top_buyer_share": round(top1_share, 4),
                    "top3_buyer_share": round(top3_share, 4),
                    "same_slot_share": round(max_slot_share, 4),
                    "same_size_share": round(max_size_share, 4),
                    "shared_funder_buyers": max_shared_funder_buyers,
                    "shared_funder_volume_share": round(max_shared_funder_volume_share, 4),
                    "creator_buy_share": round(creator_buy_share, 4),
                    "buy_pressure": (
                        round(buy_pressure, 4)
                        if buy_pressure is not None
                        else None
                    ),
                    "total_buy_sol": round(total_buy_sol, 6),
                    "total_sell_sol": round(total_sell_sol, 6),
                    "buy_sell_ratio": (
                        round(buy_sell_ratio, 4)
                        if buy_sell_ratio is not None
                        else None
                    ),
                    "buy_sell_ratio_basis": "curve_system_transfers",
                    "buy_velocity_sol_per_sec": (
                        round(buy_velocity_sol, 6)
                        if buy_velocity_sol is not None
                        else None
                    ),
                    "flow_span_seconds": round(flow_span_seconds, 3),
                    "buyer_diversity": (
                        round(buyer_diversity, 4)
                        if buyer_diversity is not None
                        else None
                    ),
                    "top10_buyer_sol_share": (
                        round(top10_sol_share, 4)
                        if top10_sol_share is not None
                        else None
                    ),
                    "sol_flow_events": sol_flow_events,
                    "sol_flow_buy_events": sol_flow_buy_events,
                    "sol_flow_sell_events": sol_flow_sell_events,
                    "sol_flow_available": bool(total_buy_sol or total_sell_sol),
                    "creator_sell_share": round(creator_sell_share, 4),
                },
            }
        )
    except Exception as exc:
        # Keep the safety gate fail-closed when its analysis cannot be
        # completed. The scanner treats safe=False as a hard rejection.
        result.update(
            {
                "status": "degraded",
                "safe": False,
                "reason": f"launch safety RPC unavailable: {type(exc).__name__}",
                "risk_score": 100,
                "signals": {
                    "analysis_unavailable": True,
                },
            }
        )
        logger.warning(
            "pumpfun_launch_safety_degraded "
            f"mint={mint} error={type(exc).__name__}: {exc}"
        )

    _pumpfun_launch_safety_cache[mint] = (now, dict(result))
    logger.info(
        "pumpfun_launch_safety",
        extra=result,
    )
    return result

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
