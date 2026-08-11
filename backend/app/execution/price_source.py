"""Shared live price/volume sources.

IMPORTANT:
- Live positions must NEVER use simulated prices for exit decisions.
- Paper/simulated positions may use the random-walk fallback.
- Meteora DBC positions use the live on-chain pool price.
- If a live price cannot be obtained, the caller receives the last known
  price together with is_simulated=True and should NOT trigger an automated
  exit from that value.
"""

import logging
import random

from app.config.settings import settings
from app.connectors.anoncoin import (
    AnoncoinClient,
    AnoncoinUnavailable,
)
from app.execution.onchain import meteora_dbc
from app.execution.onchain.meteora_dbc import DbcBuildError
from app.scanners import price_feed
from app.scoring.rules import TokenSnapshot


logger = logging.getLogger(
    "app.execution.price_source"
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
        base_price * (1 + drift),
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
        base_volume * decay * noise,
    )


# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------

async def get_current_price_usd(
    client: AnoncoinClient,
    token: TokenSnapshot,
    tick: int = 0,
) -> tuple[float, bool]:
    """Return (price_usd, is_simulated).

    For LIVE positions:
        A real market price is required.

        If the live source is temporarily unavailable, the function returns
        the last known price with is_simulated=True.

        The caller MUST NOT use that price to trigger an automated exit.

    For simulated/paper positions:
        The deterministic random-walk fallback remains available.
    """

    # -----------------------------------------------------------------------
    # Anoncoin API price
    # -----------------------------------------------------------------------

    if token.source == "anoncoin":
        try:
            details = await client.get_coin_details(
                token.mint
            )

            raw_price = details.get(
                "priceUsd"
            )

            if raw_price is not None:
                price = float(raw_price)

                if price > 0:
                    return price, False

            logger.warning(
                "anoncoin_price_missing",
                extra={
                    "mint": token.mint,
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
            logger.exception(
                "anoncoin_price_lookup_error",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        # IMPORTANT:
        #
        # Do NOT generate a simulated price here for a live token.
        return (
            float(token.price_usd or 0.0),
            True,
        )

    # -----------------------------------------------------------------------
    # Meteora DBC on-chain price
    # -----------------------------------------------------------------------

    if token.source == "anoncoin_onchain":
        try:
            info = await meteora_dbc.get_pool_info(
                token.mint,
                settings.solana_rpc_url,
            )

            price_sol = float(
                info["price_sol_per_token"]
            )

            if price_sol <= 0:
                raise DbcBuildError(
                    "Meteora returned non-positive token price"
                )

            sol_price = await price_feed.get_sol_usd_price(
                settings.jupiter_price_url
            )

            sol_price = float(sol_price)

            if sol_price <= 0:
                raise DbcBuildError(
                    "SOL/USD price is unavailable or non-positive"
                )

            price_usd = (
                price_sol * sol_price
            )

            if price_usd <= 0:
                raise DbcBuildError(
                    "calculated USD price is non-positive"
                )

            logger.debug(
                "live_meteora_price",
                extra={
                    "mint": token.mint,
                    "price_sol": price_sol,
                    "sol_usd": sol_price,
                    "price_usd": price_usd,
                },
            )

            return price_usd, False

        except DbcBuildError as exc:
            logger.warning(
                "live_pool_price_lookup_failed",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        except Exception as exc:
            logger.exception(
                "live_pool_price_lookup_error",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        # IMPORTANT:
        #
        # The position manager must see is_simulated=True and therefore
        # refuse to trigger TP/SL based on this stale value.
        return (
            float(token.price_usd or 0.0),
            True,
        )

    # -----------------------------------------------------------------------
    # Simulated/paper source
    # -----------------------------------------------------------------------

    return (
        _walk_price(
            token.mint,
            token.price_usd or 0.000001,
            tick,
        ),
        True,
    )


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

async def get_current_volume_usd(
    client: AnoncoinClient,
    token: TokenSnapshot,
    tick: int = 0,
) -> tuple[float, bool]:
    """Return (volume_24h_usd, is_simulated).

    Anoncoin API:
        Live volume when available.

    Meteora:
        Meteora pool reads do not provide the same 24h volume metric, so
        volume is marked simulated/unavailable.

    Paper/simulated:
        Deterministic simulated volume remains available.
    """

    # -----------------------------------------------------------------------
    # Anoncoin live volume
    # -----------------------------------------------------------------------

    if token.source == "anoncoin":
        try:
            details = await client.get_coin_details(
                token.mint
            )

            volume = details.get(
                "volume24HrsUsd"
            )

            if volume is not None:
                volume = float(volume)

                if volume >= 0:
                    return volume, False

        except AnoncoinUnavailable as exc:
            logger.warning(
                "anoncoin_volume_unavailable",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        except Exception as exc:
            logger.exception(
                "anoncoin_volume_lookup_error",
                extra={
                    "mint": token.mint,
                    "error": str(exc),
                },
            )

        # Live volume unavailable.
        #
        # Return the last known value as simulated/unverified rather than
        # pretending it is current.
        return (
            float(token.volume_24h_usd or 0.0),
            True,
        )

    # -----------------------------------------------------------------------
    # Meteora
    # -----------------------------------------------------------------------

    if token.source == "anoncoin_onchain":
        # Meteora DBC pool info does not provide the same 24h volume metric
        # used by the Anoncoin API.
        #
        # Do NOT fabricate live volume for an automated volume-drop exit.
        return (
            float(token.volume_24h_usd or 0.0),
            True,
        )

    # -----------------------------------------------------------------------
    # Paper/simulated
    # -----------------------------------------------------------------------

    return (
        _walk_volume(
            token.mint,
            token.volume_24h_usd or 0.0,
            tick,
        ),
        True,
    )
