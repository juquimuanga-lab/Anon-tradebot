"""Observe-only candidate discovery for Solana arbitrage.

Uses DEX Screener only as a candidate source. Every candidate is then priced
through the existing unrestricted Jupiter discovery engine. This module never
constructs or submits a transaction and never touches the sniper path.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx

from app.arbitrage.discovery import ArbitrageDiscovery, DiscoveryResult

DEXSCREENER_BASE = "https://api.dexscreener.com"
SOLANA = "solana"

# Conservative defaults. Environment variables may override these, but are not
# required for a normal Railway deployment.
DEFAULT_MAX_CANDIDATES = 5
DEFAULT_MIN_LIQUIDITY_USD = 250_000.0
DEFAULT_MIN_VOLUME_24H_USD = 1_000_000.0
DEFAULT_MIN_DEXES = 2
DEFAULT_REQUEST_TIMEOUT_SECONDS = 8.0

EXCLUDED_QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",
    "11111111111111111111111111111111",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGgZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}


@dataclass(frozen=True)
class HuntCandidate:
    token_mint: str
    symbol: str
    name: str
    liquidity_usd: float
    volume_24h_usd: float
    dex_count: int
    score: float


@dataclass(frozen=True)
class HuntResult:
    candidates: tuple[HuntCandidate, ...]
    discoveries: tuple[tuple[HuntCandidate, DiscoveryResult], ...]
    errors: tuple[str, ...] = ()


def _float_value(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _env_int(name: str, default: int) -> int:
    try:
        return max(int(os.getenv(name, str(default))), 1)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(float(os.getenv(name, str(default))), 0.0)
    except ValueError:
        return default


class DexScreenerCandidateSource:
    """Shortlist liquid, active Solana tokens with multiple DEX venues."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=DEXSCREENER_BASE,
            timeout=_env_float("ARBITRAGE_HUNT_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS),
            headers={"Accept": "application/json", "User-Agent": "AnonTradeBot-ArbHunt/1.0"},
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get_json(self, path: str) -> Any:
        response = await self._client.get(path)
        response.raise_for_status()
        return response.json()

    async def discover_candidates(self, limit: int | None = None) -> tuple[HuntCandidate, ...]:
        max_candidates = limit or _env_int("ARBITRAGE_HUNT_MAX_CANDIDATES", DEFAULT_MAX_CANDIDATES)
        min_liquidity = _env_float("ARBITRAGE_HUNT_MIN_LIQUIDITY_USD", DEFAULT_MIN_LIQUIDITY_USD)
        min_volume = _env_float("ARBITRAGE_HUNT_MIN_VOLUME_24H_USD", DEFAULT_MIN_VOLUME_24H_USD)
        min_dexes = _env_int("ARBITRAGE_HUNT_MIN_DEXES", DEFAULT_MIN_DEXES)

        profiles, boosts = await asyncio.gather(
            self._get_json("/token-profiles/latest/v1"),
            self._get_json("/token-boosts/top/v1"),
            return_exceptions=True,
        )

        raw_addresses: list[str] = []
        if isinstance(profiles, list):
            raw_addresses.extend(
                item.get("tokenAddress", "")
                for item in profiles
                if isinstance(item, dict) and item.get("chainId") == SOLANA
            )
        if isinstance(boosts, list):
            raw_addresses.extend(
                item.get("tokenAddress", "")
                for item in boosts
                if isinstance(item, dict) and item.get("chainId") == SOLANA
            )

        addresses = list(dict.fromkeys(address for address in raw_addresses if address))
        if not addresses:
            return ()

        sem = asyncio.Semaphore(5)

        async def token_pairs(address: str):
            async with sem:
                try:
                    return address, await self._get_json(f"/token-pairs/v1/{SOLANA}/{address}")
                except Exception:
                    return address, None

        pair_results = await asyncio.gather(*(token_pairs(address) for address in addresses[:30]))
        candidates: list[HuntCandidate] = []

        for address, payload in pair_results:
            if not isinstance(payload, list):
                continue

            usable = [
                pair for pair in payload
                if isinstance(pair, dict)
                and pair.get("chainId") == SOLANA
                and pair.get("baseToken", {}).get("address") == address
                and pair.get("quoteToken", {}).get("address") not in EXCLUDED_QUOTE_MINTS
            ]
            if not usable:
                # Some pools represent the token as quoteToken. Keep them as
                # long as the pair is on Solana and has real liquidity.
                usable = [
                    pair for pair in payload
                    if isinstance(pair, dict) and pair.get("chainId") == SOLANA
                ]

            dexes = {str(pair.get("dexId", "")).lower() for pair in usable if pair.get("dexId")}
            liquidity = max((_float_value(pair.get("liquidity", {}).get("usd")) for pair in usable), default=0.0)
            volume = sum(_float_value(pair.get("volume", {}).get("h24")) for pair in usable)

            if liquidity < min_liquidity or volume < min_volume or len(dexes) < min_dexes:
                continue

            best_pair = max(
                usable,
                key=lambda pair: _float_value(pair.get("liquidity", {}).get("usd")),
            )
            base = best_pair.get("baseToken", {})
            symbol = str(base.get("symbol") or best_pair.get("quoteToken", {}).get("symbol") or address[:8])
            name = str(base.get("name") or best_pair.get("quoteToken", {}).get("name") or symbol)

            # Log-scale-ish score keeps huge volume from completely dominating
            # liquidity and venue diversity while remaining deterministic.
            score = volume * max(liquidity, 1.0) ** 0.25 * max(len(dexes), 1) ** 0.5
            candidates.append(
                HuntCandidate(address, symbol, name, liquidity, volume, len(dexes), score)
            )

        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        return tuple(candidates[:max_candidates])


class ArbitrageHunter:
    """Find candidate tokens, then run the existing observe-only Jupiter scan."""

    def __init__(self, source: DexScreenerCandidateSource | None = None) -> None:
        self.source = source or DexScreenerCandidateSource()
        self.discovery = ArbitrageDiscovery()
        self._owns_source = source is None

    async def close(self) -> None:
        await self.discovery.close()
        if self._owns_source:
            await self.source.close()

    async def hunt(self, limit: int | None = None) -> HuntResult:
        candidates = await self.source.discover_candidates(limit)
        if not candidates:
            return HuntResult((), (), ("No candidates met the liquidity, volume, and venue filters.",))

        sem = asyncio.Semaphore(2)
        errors: list[str] = []

        async def run(candidate: HuntCandidate):
            async with sem:
                try:
                    result = await self.discovery.discover(candidate.token_mint)
                    return candidate, result
                except Exception as exc:
                    errors.append(f"{candidate.symbol}: {exc}")
                    return None

        raw = await asyncio.gather(*(run(candidate) for candidate in candidates))
        discoveries = [item for item in raw if item is not None]
        discoveries.sort(
            key=lambda item: (
                bool(item[1].opportunity and item[1].opportunity.executable),
                item[1].opportunity.net_profit_atomic if item[1].opportunity else -1,
                item[1].opportunity.net_profit_bps if item[1].opportunity else float("-inf"),
            ),
            reverse=True,
        )
        return HuntResult(candidates, tuple(discoveries), tuple(errors))
