"""Shared live price/volume sources.

IMPORTANT:

- Live positions must NEVER use simulated prices for exit decisions.
- Paper/simulated positions may use the random-walk fallback.
- Meteora DBC positions use the live on-chain pool price.
- Pump.fun positions use the live Pump.fun bonding-curve price.
- If a live price cannot be obtained quickly, the caller receives the
  last known price together with is_simulated=True and must NOT trigger
  an automated price exit from that value.

Latency design:

- Live price lookups have short bounded timeouts.
- Meteora pool price and SOL/USD price are fetched concurrently.
- Pump.fun price and SOL/USD price are handled efficiently.
- SOL/USD is very briefly cached because it does not need to be fetched
  independently for every token on every monitoring cycle.
- Pump.fun prices have a very short cache to prevent duplicate RPC reads
  from overlapping monitoring/discovery requests.
- A slow external service must never block the position manager for
  the entire RPC/API timeout window.
"""

import asyncio
import logging
import random
import time
from typing import Optional

from app.config.settings import settings
from app.connectors.anoncoin import (
    AnoncoinClient,
    AnoncoinUnavailable,
)
from app.execution.onchain import meteora_dbc
from app.execution.onchain import pumpfun
from app.execution.onchain.meteora_dbc import DbcBuildError
from app.execution.onchain.pumpfun import (
    PumpFunError,
    PumpFunPoolNotFound,
)
from app.scanners import price_feed
from app.connectors.fourmeme import fourmeme_client
from app.scoring.rules import TokenSnapshot


logger = logging.getLogger(
    "app.execution.price_source"
)


# ---------------------------------------------------------------------------
# Latency controls
# ---------------------------------------------------------------------------

# Maximum time we allow a live Anoncoin price lookup to occupy one
# monitoring cycle.
ANONCOIN_PRICE_TIMEOUT_SECONDS = 2.0

# Same principle for volume.
ANONCOIN_VOLUME_TIMEOUT_SECONDS = 2.0

# Meteora pool_info currently invokes a Node process. Do not allow a
# temporary RPC/Node problem to block the exit monitor for too long.
METEORA_PRICE_TIMEOUT_SECONDS = 2.5

# Pump.fun price reads are performed directly through Solana RPC.
#
# Keep this short because the position manager should never wait several
# seconds for a single price read when a stop-loss could be approaching.
PUMPFUN_PRICE_TIMEOUT_SECONDS = 4.0

# SOL/USD is not nearly as volatile as a newly launched token price.
# A very short cache avoids repeatedly paying the external price-feed
# latency while still keeping the conversion fresh.
SOL_USD_CACHE_TTL_SECONDS = 2.0

# Pump.fun price cache.
#
# This is intentionally very short. We are NOT using this as a substitute
# for live market data. It only prevents duplicate RPC requests when two
# parts of the application request the exact same token price almost
# simultaneously.
PUMPFUN_PRICE_CACHE_TTL_SECONDS = 0.0


# ---------------------------------------------------------------------------
# SOL/USD short-lived cache
# ---------------------------------------------------------------------------

_sol_usd_cache_price: Optional[float] = None
_sol_usd_cache_timestamp: float = 0.0

_sol_usd_cache_lock = asyncio.Lock()


async def _get_sol_usd_price_fast() -> float:
    """Get SOL/USD with a very short process-local cache.

    This cache is intentionally tiny.

    We are NOT caching token prices. Only the SOL/USD conversion is cached,
    because the newly launched token price itself must remain live.
    """

    global _sol_usd_cache_price
    global _sol_usd_cache_timestamp

    now = time.monotonic()

    cached_price = _sol_usd_cache_price

    if (
        cached_price is not None
        and now - _sol_usd_cache_timestamp
        < SOL_USD_CACHE_TTL_SECONDS
    ):
        return cached_price

    async with _sol_usd_cache_lock:

        now = time.monotonic()

        cached_price = _sol_usd_cache_price

        if (
            cached_price is not None
            and now - _sol_usd_cache_timestamp
            < SOL_USD_CACHE_TTL_SECONDS
        ):
            return cached_price

        price = await asyncio.wait_for(
            price_feed.get_sol_usd_price(
                settings.jupiter_price_url
            ),
            timeout=2.0,
        )

        price = float(price)

        if price <= 0:
            raise DbcBuildError(
                "SOL/USD price is unavailable "
                "or non-positive"
            )

        _sol_usd_cache_price = price
        _sol_usd_cache_timestamp = (
            time.monotonic()
        )

        return price


# ---------------------------------------------------------------------------
# Pump.fun price cache
# ---------------------------------------------------------------------------

_pumpfun_price_cache: dict[
    str,
    tuple[float, float],
] = {}

_pumpfun_price_cache_lock = asyncio.Lock()


def _get_cached_pumpfun_price(
    mint: str,
) -> Optional[float]:
    """Return a very recent Pump.fun price if available."""

    cached = _pumpfun_price_cache.get(
        mint
    )

    if cached is None:
        return None

    price, timestamp = cached

    if (
        time.monotonic()
        - timestamp
        >= PUMPFUN_PRICE_CACHE_TTL_SECONDS
    ):
        _pumpfun_price_cache.pop(
            mint,
            None,
        )
        return None

    return price


def _cache_pumpfun_price(
    mint: str,
    price: float,
) -> None:
    """Store a very recent Pump.fun live price."""

    _pumpfun_price_cache[mint] = (
        float(price),
        time.monotonic(),
    )

    # Keep the cache bounded.
    #
    # The position monitor may encounter many newly launched tokens.
    # Remove stale entries opportunistically.
    if len(_pumpfun_price_cache) > 500:

        now = time.monotonic()

        stale_mints = [
            cached_mint
            for cached_mint, (
                _price,
                timestamp,
            ) in _pumpfun_price_cache.items()
            if (
                now - timestamp
                >= PUMPFUN_PRICE_CACHE_TTL_SECONDS
            )
        ]

        for stale_mint in stale_mints:
            _pumpfun_price_cache.pop(
                stale_mint,
                None,
            )


# ---------------------------------------------------------------------------
# Simulation helpers
# ---------------------------------------------------------------------------


def _walk_price(
    mint: str,
    base_price: float,
    tick: int,
) -> float:
    """Generate a deterministic simulated price.

    ONLY intended for paper/simulated positions.
    """

    rng = random.Random(
        f"{mint}:{tick}"
    )

    drift = rng.uniform(
        -0.06,
        0.07,
    )

    return max(
        base_price * 1e-6,
        base_price * (
            1 + drift
        ),
    )


def _walk_volume(
    mint: str,
    base_volume: float,
    tick: int,
) -> float:
    """Generate simulated 24h volume.

    ONLY intended for paper/simulated positions.
    """

    rng = random.Random(
        f"{mint}:vol:{tick}"
    )

    decay = max(
        0.15,
        1 - tick * 0.03,
    )

    noise = rng.uniform(
        0.85,
        1.15,
    )

    return max(
        0.0,
        base_volume
        * decay
        * noise,
    )


# ---------------------------------------------------------------------------
# Anoncoin price
# ---------------------------------------------------------------------------


async def _get_anoncoin_price(
    client: AnoncoinClient,
    mint: str,
) -> float:
    """Fetch Anoncoin price with a bounded timeout."""

    details = await asyncio.wait_for(
        client.get_coin_details(
            mint
        ),
        timeout=ANONCOIN_PRICE_TIMEOUT_SECONDS,
    )

    raw_price = details.get(
        "priceUsd"
    )

    if raw_price is None:
        raise DbcBuildError(
            "Anoncoin returned no priceUsd"
        )

    price = float(raw_price)

    if price <= 0:
        raise DbcBuildError(
            "Anoncoin returned non-positive price"
        )

    return price


# ---------------------------------------------------------------------------
# Meteora price
# ---------------------------------------------------------------------------


async def _get_meteora_price_usd(
    mint: str,
) -> float:
    """Fetch Meteora token price in USD.

    Pool price and SOL/USD are requested concurrently.

    This is important because previously the path was:

        pool_info
            ↓
        wait
            ↓
        SOL/USD
            ↓
        calculate price

    Now it is:

        pool_info ──────────┐
                            ├── calculate
        SOL/USD ────────────┘
    """

    pool_task = asyncio.create_task(
        meteora_dbc.get_pool_info(
            mint,
            settings.solana_rpc_url,
        )
    )

    sol_price_task = asyncio.create_task(
        _get_sol_usd_price_fast()
    )

    try:

        pool_info, sol_price = (
            await asyncio.wait_for(
                asyncio.gather(
                    pool_task,
                    sol_price_task,
                ),
                timeout=(
                    METEORA_PRICE_TIMEOUT_SECONDS
                ),
            )
        )

    except asyncio.TimeoutError:

        for task in (
            pool_task,
            sol_price_task,
        ):
            if not task.done():
                task.cancel()

        await asyncio.gather(
            pool_task,
            sol_price_task,
            return_exceptions=True,
        )

        raise DbcBuildError(
            "Meteora live price lookup exceeded "
            f"{METEORA_PRICE_TIMEOUT_SECONDS}s"
        )

    except Exception:

        for task in (
            pool_task,
            sol_price_task,
        ):
            if not task.done():
                task.cancel()

        await asyncio.gather(
            pool_task,
            sol_price_task,
            return_exceptions=True,
        )

        raise

    price_sol = float(
        pool_info[
            "price_sol_per_token"
        ]
    )

    if price_sol <= 0:
        raise DbcBuildError(
            "Meteora returned non-positive "
            "token price"
        )

    sol_price = float(
        sol_price
    )

    if sol_price <= 0:
        raise DbcBuildError(
            "SOL/USD price is unavailable "
            "or non-positive"
        )

    price_usd = (
        price_sol
        * sol_price
    )

    if price_usd <= 0:
        raise DbcBuildError(
            "calculated USD price is non-positive"
        )

    logger.debug(
        "live_meteora_price",
        extra={
            "mint": mint,
            "price_sol": price_sol,
            "sol_usd": sol_price,
            "price_usd": price_usd,
        },
    )

    return price_usd


# ---------------------------------------------------------------------------
# Pump.fun price
# ---------------------------------------------------------------------------


async def _get_pumpfun_price_usd(
    mint: str,
) -> float:
    """Fetch the live Pump.fun bonding-curve price.

    Pump.fun positions MUST use this path rather than the simulated
    random-walk fallback.

    The Pump.fun module calculates the token's SOL price from the live
    bonding-curve reserves and then converts it to USD.

    We supply our already cached SOL/USD price so Pump.fun does not make
    another external SOL price request.
    """

    cached_price = _get_cached_pumpfun_price(
        mint
    )

    if cached_price is not None:

        logger.debug(
            "pumpfun_price_cache_hit",
            extra={
                "mint": mint,
                "price_usd": cached_price,
            },
        )

        return cached_price

    # Only one identical Pump.fun lookup should be allowed to populate
    # the cache at a time.
    #
    # The lock is intentionally held only around the actual lookup.
    async with _pumpfun_price_cache_lock:

        cached_price = (
            _get_cached_pumpfun_price(
                mint
            )
        )

        if cached_price is not None:
            return cached_price

        sol_usd = (
            await _get_sol_usd_price_fast()
        )

        pool_info = await asyncio.wait_for(
            pumpfun.get_pool_info(
                mint,
                settings.solana_rpc_url,
                sol_usd=sol_usd,
                commitment="processed",
            ),
            timeout=(
                PUMPFUN_PRICE_TIMEOUT_SECONDS
            ),
        )

        if not pool_info:
            raise PumpFunError(
                "Pump.fun returned empty pool "
                "information"
            )

        if not pool_info.get(
            "success",
            False,
        ):
            raise PumpFunError(
                "Pump.fun price lookup was "
                "not successful"
            )

        price_usd = float(
            pool_info.get(
                "price_usd",
                0.0,
            )
        )

        if price_usd <= 0:
            raise PumpFunError(
                "Pump.fun returned a non-positive "
                "price"
            )

        _cache_pumpfun_price(
            mint,
            price_usd,
        )

        logger.debug(
            "live_pumpfun_price",
            extra={
                "mint": mint,
                "price_usd": price_usd,
                "price_sol_per_token": (
                    pool_info.get(
                        "price_sol_per_token"
                    )
                ),
                "market_cap_usd": (
                    pool_info.get(
                        "market_cap_usd"
                    )
                ),
                "liquidity_usd": (
                    pool_info.get(
                        "liquidity_usd"
                    )
                ),
                "complete": (
                    pool_info.get(
                        "complete"
                    )
                ),
            },
        )

        return price_usd


# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------


async def get_current_price_usd(
    client: AnoncoinClient,
    token: TokenSnapshot,
    tick: int = 0,
) -> tuple[float, bool]:
    """Return (price_usd, is_simulated).

    LIVE positions:

        A real market price is required.

        If the live source cannot respond quickly, return the last known
        price with is_simulated=True.

        The position manager must NOT trigger TP/SL from that value.

    PAPER positions:

        The deterministic random-walk fallback remains available.
    """

    # -----------------------------------------------------------------------
    # Anoncoin API
    # -----------------------------------------------------------------------

    if token.source == "anoncoin":

        try:

            price = await _get_anoncoin_price(
                client,
                token.mint,
            )

            return (
                price,
                False,
            )

        except asyncio.TimeoutError:

            logger.warning(
                "anoncoin_price_timeout",
                extra={
                    "mint": token.mint,
                    "timeout_seconds": (
                        ANONCOIN_PRICE_TIMEOUT_SECONDS
                    ),
                },
            )

        except AnoncoinUnavailable as exc:

            logger.warning(
                "anoncoin_price_unavailable",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        except Exception as exc:

            logger.warning(
                "anoncoin_price_lookup_error",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        # IMPORTANT:
        #
        # Never generate a simulated live price.
        return (
            float(
                token.price_usd
                or 0.0
            ),
            True,
        )

    # -----------------------------------------------------------------------
    # Meteora DBC on-chain price
    # -----------------------------------------------------------------------

    if token.source == "anoncoin_onchain":

        try:

            price_usd = (
                await _get_meteora_price_usd(
                    token.mint
                )
            )

            return (
                price_usd,
                False,
            )

        except asyncio.TimeoutError:

            logger.warning(
                "live_pool_price_timeout",
                extra={
                    "mint": token.mint,
                    "timeout_seconds": (
                        METEORA_PRICE_TIMEOUT_SECONDS
                    ),
                },
            )

        except DbcBuildError as exc:

            logger.warning(
                "live_pool_price_lookup_failed",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        except Exception as exc:

            logger.warning(
                "live_pool_price_lookup_error",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        # IMPORTANT:
        #
        # The position manager receives is_simulated=True and therefore
        # refuses to trigger TP/SL from this stale value.
        return (
            float(
                token.price_usd
                or 0.0
            ),
            True,
        )

    # -----------------------------------------------------------------------
    # Pump.fun on-chain price
    # -----------------------------------------------------------------------

    if token.source == "pumpfun":

        try:

            price_usd = (
                await _get_pumpfun_price_usd(
                    token.mint
                )
            )

            # CRITICAL:
            #
            # This is a REAL Pump.fun bonding-curve price.
            #
            # Returning False tells PositionManager that it is safe to
            # evaluate stop-loss / take-profit against this price.
            return (
                price_usd,
                False,
            )

        except asyncio.TimeoutError:

            logger.warning(
                "pumpfun_price_timeout",
                extra={
                    "mint": token.mint,
                    "timeout_seconds": (
                        PUMPFUN_PRICE_TIMEOUT_SECONDS
                    ),
                },
            )

        except PumpFunPoolNotFound as exc:

            logger.warning(
                "pumpfun_price_pool_not_found",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        except PumpFunError as exc:

            logger.warning(
                "pumpfun_price_lookup_failed",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        except Exception as exc:

            logger.warning(
                "pumpfun_price_lookup_error",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        # IMPORTANT:
        #
        # Never fabricate a Pump.fun price.
        #
        # Returning True prevents PositionManager from triggering a
        # stop-loss/take-profit using stale data.
        return (
            float(
                token.price_usd
                or 0.0
            ),
            True,
        )

    # -----------------------------------------------------------------------
    # Four.meme / BSC live price
    # -----------------------------------------------------------------------

    if token.source == "fourmeme":
        try:
            market = await asyncio.wait_for(
                fourmeme_client.market_snapshot(token.mint),
                timeout=1.5,
            )
            if market.get("price_usd", 0) > 0:
                return float(market["price_usd"]), False
        except Exception as exc:
            logger.warning("fourmeme_price_lookup_failed", extra={"mint":token.mint,"error":str(exc)})
        return float(token.price_usd or 0.0), True

    # -----------------------------------------------------------------------
    # Simulated / paper source
    # -----------------------------------------------------------------------

    return (
        _walk_price(
            token.mint,
            token.price_usd
            or 0.000001,
            tick,
        ),
        True,
    )


# ---------------------------------------------------------------------------
# Anoncoin volume
# ---------------------------------------------------------------------------


async def _get_anoncoin_volume(
    client: AnoncoinClient,
    mint: str,
) -> float:
    """Fetch Anoncoin volume with a bounded timeout."""

    details = await asyncio.wait_for(
        client.get_coin_details(
            mint
        ),
        timeout=ANONCOIN_VOLUME_TIMEOUT_SECONDS,
    )

    volume = details.get(
        "volume24HrsUsd"
    )

    if volume is None:
        raise DbcBuildError(
            "Anoncoin returned no "
            "volume24HrsUsd"
        )

    volume = float(volume)

    if volume < 0:
        raise DbcBuildError(
            "Anoncoin returned negative volume"
        )

    return volume


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


async def get_current_volume_usd(
    client: AnoncoinClient,
    token: TokenSnapshot,
    tick: int = 0,
) -> tuple[float, bool]:
    """Return (volume_24h_usd, is_simulated).

    Anoncoin:
        Live volume when available.

    Meteora:
        Meteora pool reads do not provide the same 24h volume metric.

    Pump.fun:
        Pump.fun bonding-curve reads do not provide the same 24h volume
        metric used by the Anoncoin API.

    Paper/simulated:
        Deterministic simulated volume remains available.
    """

    # -----------------------------------------------------------------------
    # Anoncoin live volume
    # -----------------------------------------------------------------------

    if token.source == "anoncoin":

        try:

            volume = await _get_anoncoin_volume(
                client,
                token.mint,
            )

            return (
                volume,
                False,
            )

        except asyncio.TimeoutError:

            logger.warning(
                "anoncoin_volume_timeout",
                extra={
                    "mint": token.mint,
                    "timeout_seconds": (
                        ANONCOIN_VOLUME_TIMEOUT_SECONDS
                    ),
                },
            )

        except AnoncoinUnavailable as exc:

            logger.warning(
                "anoncoin_volume_unavailable",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        except Exception as exc:

            logger.warning(
                "anoncoin_volume_lookup_error",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        # Return last known volume but mark it unverified.
        return (
            float(
                token.volume_24h_usd
                or 0.0
            ),
            True,
        )

    # -----------------------------------------------------------------------
    # Meteora
    # -----------------------------------------------------------------------

    if token.source == "anoncoin_onchain":

        # Meteora DBC pool info does not provide the same 24h volume metric
        # used by the Anoncoin API.
        #
        # Never fabricate live volume.
        return (
            float(
                token.volume_24h_usd
                or 0.0
            ),
            True,
        )

    # -----------------------------------------------------------------------
    # Pump.fun
    # -----------------------------------------------------------------------

    if token.source == "pumpfun":

        # Pump.fun bonding curve data does not provide the same 24h volume
        # metric used by the Anoncoin API.
        #
        # Never fabricate live volume.
        return (
            float(
                token.volume_24h_usd
                or 0.0
            ),
            True,
        )

    # -----------------------------------------------------------------------
    # Paper / simulated
    # -----------------------------------------------------------------------

    return (
        _walk_volume(
            token.mint,
            token.volume_24h_usd
            or 0.0,
            tick,
        ),
        True,
)
