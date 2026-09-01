"""Observe-only candidate discovery for Solana arbitrage.

DEX Screener is used only as a broad candidate source. Candidates are then
priced through the existing unrestricted Jupiter discovery engine. This
module never constructs or submits a transaction and never touches the
sniper path.
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

DEFAULT_MAX_CANDIDATES = 8
DEFAULT_MIN_LIQUIDITY_USD = 100_000.0
DEFAULT_MIN_VOLUME_24H_USD = 250_000.0
DEFAULT_MIN_DEXES = 1
DEFAULT_REQUEST_TIMEOUT_SECONDS = 8.0
DEFAULT_SEARCH_TERMS = (
    "SOL", "USDC", "USDT", "JUP", "RAY", "ORCA", "MET", "PUMP", "FARTCOIN"
)
# A hunt is a screening pass, so use a compact ladder to reduce API pressure.
# Full /arbdiscover keeps the normal six-size ladder.
DEFAULT_HUNT_DISCOVERY_SIZES_SOL = (0.02, 0.10, 0.50)
DEFAULT_JUPITER_MIN_INTERVAL_SECONDS = 0.40

EXCLUDED_MINTS = {
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
    tier: str = "B"


@dataclass(frozen=True)
class HuntStats:
    profile_addresses: int = 0
    search_pairs: int = 0
    unique_tokens: int = 0
    liquidity_qualified: int = 0
    volume_qualified: int = 0
    venue_qualified: int = 0
    final_candidates: int = 0
    jupiter_round_trips: int = 0
    jupiter_429s: int = 0


@dataclass(frozen=True)
class HuntResult:
    candidates: tuple[HuntCandidate, ...]
    discoveries: tuple[tuple[HuntCandidate, DiscoveryResult], ...]
    errors: tuple[str, ...] = ()
    stats: HuntStats = HuntStats()


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


def _env_terms() -> tuple[str, ...]:
    raw = os.getenv("ARBITRAGE_HUNT_SEARCH_TERMS", "").strip()
    if not raw:
        return DEFAULT_SEARCH_TERMS
    values = tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    return values or DEFAULT_SEARCH_TERMS


def _hunt_sizes() -> tuple[float, ...]:
    raw = os.getenv("ARBITRAGE_HUNT_DISCOVERY_SIZES_SOL", "").strip()
    if not raw:
        return DEFAULT_HUNT_DISCOVERY_SIZES_SOL
    values: list[float] = []
    for item in raw.split(","):
        try:
            value = float(item.strip())
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return tuple(dict.fromkeys(values)) or DEFAULT_HUNT_DISCOVERY_SIZES_SOL


class DexScreenerCandidateSource:
    """Build a broad Solana candidate pool from profiles, boosts and pair search."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=DEXSCREENER_BASE,
            timeout=_env_float("ARBITRAGE_HUNT_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS),
            headers={"Accept": "application/json", "User-Agent": "AnonTradeBot-ArbHunt/2.1"},
        )
        self._owns_client = client is None
        self.last_stats = HuntStats()

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

        profiles, boosts, searches = await asyncio.gather(
            self._get_json("/token-profiles/latest/v1"),
            self._get_json("/token-boosts/top/v1"),
            self._search_pairs(),
            return_exceptions=True,
        )

        addresses: list[str] = []
        if isinstance(profiles, list):
            addresses.extend(
                str(item.get("tokenAddress", "")) for item in profiles
                if isinstance(item, dict) and item.get("chainId") == SOLANA
            )
        if isinstance(boosts, list):
            addresses.extend(
                str(item.get("tokenAddress", "")) for item in boosts
                if isinstance(item, dict) and item.get("chainId") == SOLANA
            )
        profile_addresses = len(set(address for address in addresses if address))

        pair_payloads: list[dict[str, Any]] = []
        if isinstance(searches, list):
            pair_payloads.extend(item for item in searches if isinstance(item, dict))

        unique_addresses = [
            address for address in dict.fromkeys(addresses)
            if address and address not in EXCLUDED_MINTS
        ][:30]
        sem = asyncio.Semaphore(5)

        async def token_pairs(address: str):
            async with sem:
                try:
                    payload = await self._get_json(f"/token-pairs/v1/{SOLANA}/{address}")
                    return payload if isinstance(payload, list) else []
                except Exception:
                    return []

        if unique_addresses:
            fetched = await asyncio.gather(*(token_pairs(address) for address in unique_addresses))
            for payload in fetched:
                pair_payloads.extend(item for item in payload if isinstance(item, dict))

        aggregates: dict[str, dict[str, Any]] = {}
        for pair in pair_payloads:
            if pair.get("chainId") != SOLANA:
                continue
            base = pair.get("baseToken") or {}
            quote = pair.get("quoteToken") or {}
            base_address = str(base.get("address") or "")
            quote_address = str(quote.get("address") or "")
            if not base_address or not quote_address:
                continue

            if base_address not in EXCLUDED_MINTS:
                token = base
            elif quote_address not in EXCLUDED_MINTS:
                token = quote
            else:
                continue

            address = str(token.get("address") or "")
            if not address or address in EXCLUDED_MINTS:
                continue

            entry = aggregates.setdefault(
                address,
                {"symbol": str(token.get("symbol") or address[:8]),
                 "name": str(token.get("name") or token.get("symbol") or address[:8]),
                 "liquidity": 0.0, "volume": 0.0, "dexes": set()},
            )
            entry["liquidity"] = max(
                float(entry["liquidity"]), _float_value((pair.get("liquidity") or {}).get("usd"))
            )
            entry["volume"] += _float_value((pair.get("volume") or {}).get("h24"))
            dex_id = str(pair.get("dexId") or "").lower().strip()
            if dex_id:
                entry["dexes"].add(dex_id)

        unique_tokens = len(aggregates)
        liquidity_qualified = sum(1 for value in aggregates.values() if value["liquidity"] >= min_liquidity)
        volume_qualified = sum(1 for value in aggregates.values() if value["volume"] >= min_volume)
        venue_qualified = sum(1 for value in aggregates.values() if len(value["dexes"]) >= min_dexes)

        candidates: list[HuntCandidate] = []
        for address, value in aggregates.items():
            liquidity = float(value["liquidity"])
            volume = float(value["volume"])
            dex_count = len(value["dexes"])
            if liquidity < min_liquidity or volume < min_volume or dex_count < min_dexes:
                continue
            tier = "A" if liquidity >= 250_000 and volume >= 1_000_000 and dex_count >= 2 else "B"
            score = volume * max(liquidity, 1.0) ** 0.25 * max(dex_count, 1) ** 0.75
            candidates.append(HuntCandidate(
                address, str(value["symbol"]), str(value["name"]), liquidity,
                volume, dex_count, score, tier,
            ))

        candidates.sort(key=lambda candidate: (
            candidate.tier == "A", candidate.score, candidate.dex_count
        ), reverse=True)
        final = tuple(candidates[:max_candidates])
        self.last_stats = HuntStats(
            profile_addresses=profile_addresses,
            search_pairs=len(pair_payloads),
            unique_tokens=unique_tokens,
            liquidity_qualified=liquidity_qualified,
            volume_qualified=volume_qualified,
            venue_qualified=venue_qualified,
            final_candidates=len(final),
        )
        return final

    async def _search_pairs(self) -> list[dict[str, Any]]:
        sem = asyncio.Semaphore(4)

        async def search(term: str) -> list[dict[str, Any]]:
            async with sem:
                try:
                    payload = await self._get_json(f"/latest/dex/search?q={term}")
                    pairs = payload.get("pairs") if isinstance(payload, dict) else None
                    return [pair for pair in (pairs or []) if isinstance(pair, dict) and pair.get("chainId") == SOLANA]
                except Exception:
                    return []

        results = await asyncio.gather(*(search(term) for term in _env_terms()))
        return [pair for result in results for pair in result]


class ArbitrageHunter:
    """Find candidate tokens, then run the existing observe-only Jupiter scan."""

    def __init__(self, source: DexScreenerCandidateSource | None = None) -> None:
        self.source = source or DexScreenerCandidateSource()
        self.discovery = ArbitrageDiscovery()
        self._owns_source = source is None
        self._jupiter_lock = asyncio.Lock()
        self._last_jupiter_request = 0.0

    async def close(self) -> None:
        await self.discovery.close()
        if self._owns_source:
            await self.source.close()

    async def _discover_candidate(self, candidate: HuntCandidate) -> DiscoveryResult:
        async with self._jupiter_lock:
            now = asyncio.get_running_loop().time()
            wait = _env_float("ARBITRAGE_HUNT_JUPITER_MIN_INTERVAL_SECONDS", DEFAULT_JUPITER_MIN_INTERVAL_SECONDS)
            delay = wait - (now - self._last_jupiter_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_jupiter_request = asyncio.get_running_loop().time()
            return await self.discovery.discover(candidate.token_mint, sizes_sol=_hunt_sizes())

    async def hunt(self, limit: int | None = None) -> HuntResult:
        candidates = await self.source.discover_candidates(limit)
        stats = self.source.last_stats
        if not candidates:
            return HuntResult(
                (), (), ("No candidates met the configured liquidity, volume, and venue screening thresholds.",), stats
            )

        errors: list[str] = []
        discoveries: list[tuple[HuntCandidate, DiscoveryResult]] = []
        for candidate in candidates:
            try:
                result = await self._discover_candidate(candidate)
                discoveries.append((candidate, result))
            except Exception as exc:
                errors.append(f"{candidate.symbol}: {exc}")

        round_trips = sum(
            1 for _, discovery in discoveries if discovery.opportunity is not None
        )
        rate_limits = sum(
            1 for _, discovery in discoveries
            if discovery.error and "HTTP 429" in discovery.error
        )
        stats = HuntStats(
            **{**stats.__dict__, "jupiter_round_trips": round_trips, "jupiter_429s": rate_limits}
        )
        discoveries.sort(key=lambda item: (
            bool(item[1].opportunity and item[1].opportunity.executable),
            item[1].opportunity.net_profit_atomic if item[1].opportunity else -1,
            item[1].opportunity.net_profit_bps if item[1].opportunity else float("-inf"),
        ), reverse=True)
        return HuntResult(candidates, tuple(discoveries), tuple(errors), stats)
