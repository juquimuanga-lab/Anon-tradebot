"""Robinhood Chain (4663) cross-DEX arbitrage discovery.

This module is deliberately isolated from the Solana/Jupiter arbitrage path.
It uses DEX Screener only to find multi-DEX candidates, then uses 1inch's
Robinhood Chain quote API with individual liquidity-source restrictions to
measure executable buy/sell spreads. No transaction is submitted here.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

ROBINHOOD_CHAIN_ID = 4663
ROBINHOOD_DEXSCREENER_CHAIN = "robinhood"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
ONEINCH_NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
DEXSCREENER_BASE = "https://api.dexscreener.com"
ONEINCH_BASE = "https://api.1inch.com/swap/v6.1/4663"

logger = logging.getLogger("app.arbitrage.robinhood")

DEFAULT_MIN_LIQUIDITY_USD = 20_000.0
DEFAULT_MIN_VOLUME_24H_USD = 20_000.0
DEFAULT_MIN_PAIR_SPREAD_BPS = 20.0
DEFAULT_MAX_CANDIDATES = 8
DEFAULT_AMOUNT_ETH = 0.05
DEFAULT_SOURCE_LIMIT = 6
DEFAULT_TIMEOUT = 8.0


@dataclass(frozen=True)
class RobinhoodCandidate:
    token: str
    symbol: str
    name: str
    dexes: tuple[str, ...]
    liquidity_usd: float
    volume_24h_usd: float
    pair_spread_bps: float


@dataclass(frozen=True)
class RobinhoodQuote:
    source: str
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    gas: int = 0


@dataclass(frozen=True)
class RobinhoodOpportunity:
    token: str
    symbol: str
    amount_eth: float
    buy_source: str
    sell_source: str
    token_amount: int
    final_eth_amount: int
    gross_profit_wei: int
    gross_profit_bps: float
    gas_wei: int
    net_profit_wei: int
    net_profit_bps: float
    executable: bool
    reason: str


class RobinhoodArbitrage:
    """Discover executable cross-DEX spreads on Robinhood Chain."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers={"Accept": "application/json", "User-Agent": "AnonTradeBot-RobinhoodArb/1.0"})
        self._owns_client = client is None
        self._source_cache: tuple[str, ...] = ()
        self._source_cache_at = 0.0

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _config_float(self, name: str, default: float) -> float:
        try:
            return max(float(os.getenv(name, str(default))), 0.0)
        except ValueError:
            return default

    def _config_int(self, name: str, default: int) -> int:
        try:
            return max(int(os.getenv(name, str(default))), 1)
        except ValueError:
            return default

    async def _json(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        response = await self._client.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

    async def _candidate_pairs(self) -> list[dict[str, Any]]:
        # Search by the two canonical quote assets so the candidate pool stays
        # focused on liquid Robinhood markets rather than the entire chain.
        results = await asyncio.gather(
            self._json(f"{DEXSCREENER_BASE}/latest/dex/search", params={"q": "WETH"},),
            self._json(f"{DEXSCREENER_BASE}/latest/dex/search", params={"q": "USDG"},),
            return_exceptions=True,
        )
        pairs: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, dict):
                pairs.extend(item for item in result.get("pairs", []) if isinstance(item, dict) and item.get("chainId") == ROBINHOOD_DEXSCREENER_CHAIN)
        return pairs

    async def discover_candidates(self, limit: int | None = None) -> tuple[RobinhoodCandidate, ...]:
        min_liquidity = self._config_float("ROBINHOOD_ARB_MIN_LIQUIDITY_USD", DEFAULT_MIN_LIQUIDITY_USD)
        min_volume = self._config_float("ROBINHOOD_ARB_MIN_VOLUME_24H_USD", DEFAULT_MIN_VOLUME_24H_USD)
        min_spread = self._config_float("ROBINHOOD_ARB_MIN_PAIR_SPREAD_BPS", DEFAULT_MIN_PAIR_SPREAD_BPS)
        max_candidates = limit or self._config_int("ROBINHOOD_ARB_MAX_CANDIDATES", DEFAULT_MAX_CANDIDATES)

        pairs = await self._candidate_pairs()
        grouped: dict[str, dict[str, Any]] = {}
        for pair in pairs:
            base = pair.get("baseToken") or {}
            quote = pair.get("quoteToken") or {}
            token = str(base.get("address") or "")
            quote_address = str(quote.get("address") or "")
            if not token or quote_address.lower() not in {WETH.lower(), USDG.lower()}:
                continue
            liquidity = self._float((pair.get("liquidity") or {}).get("usd"))
            volume = self._float((pair.get("volume") or {}).get("h24"))
            price = self._float(pair.get("priceUsd"))
            if liquidity < min_liquidity or volume < min_volume or price <= 0:
                continue
            entry = grouped.setdefault(token, {"symbol": str(base.get("symbol") or token[:8]), "name": str(base.get("name") or base.get("symbol") or token[:8]), "dexes": {}, "liquidity": 0.0, "volume": 0.0})
            dex = str(pair.get("dexId") or "unknown").lower()
            entry["dexes"][dex] = max(float(entry["dexes"].get(dex, 0.0)), price)
            entry["liquidity"] = max(float(entry["liquidity"]), liquidity)
            entry["volume"] += volume

        candidates: list[RobinhoodCandidate] = []
        for token, entry in grouped.items():
            if len(entry["dexes"]) < 2:
                continue
            prices = list(entry["dexes"].values())
            spread_bps = (max(prices) / min(prices) - 1.0) * 10_000 if min(prices) > 0 else 0.0
            if spread_bps < min_spread:
                continue
            candidates.append(RobinhoodCandidate(token, entry["symbol"], entry["name"], tuple(sorted(entry["dexes"])), float(entry["liquidity"]), float(entry["volume"]), spread_bps))

        candidates.sort(key=lambda item: (item.pair_spread_bps, item.volume_24h_usd, item.liquidity_usd), reverse=True)
        return tuple(candidates[:max_candidates])

    async def liquidity_sources(self) -> tuple[str, ...]:
        now = asyncio.get_running_loop().time()
        if self._source_cache and now - self._source_cache_at < 300:
            return self._source_cache
        api_key = os.getenv("ONEINCH_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ONEINCH_API_KEY is not configured")
        payload = await self._json(f"{ONEINCH_BASE}/liquidity-sources", headers={"Authorization": f"Bearer {api_key}"})
        sources = [str(item.get("id")) for item in payload.get("protocols", []) if isinstance(item, dict) and item.get("id")]
        # Keep the source set bounded. Explicitly prioritize the venues known
        # to be active on Robinhood Chain; additional sources fill remaining slots.
        preferred = ("UNISWAP_V4", "UNISWAP_V3", "UNISWAP_V2", "RIALTO")
        ordered = [source for source in preferred if source in sources]
        ordered.extend(source for source in sources if source not in ordered)
        self._source_cache = tuple(ordered[: self._config_int("ROBINHOOD_ARB_SOURCE_LIMIT", DEFAULT_SOURCE_LIMIT)])
        self._source_cache_at = now
        return self._source_cache

    async def _quote(self, source: str, token_in: str, token_out: str, amount: int) -> RobinhoodQuote | None:
        api_key = os.getenv("ONEINCH_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ONEINCH_API_KEY is not configured")
        try:
            payload = await self._json(
                f"{ONEINCH_BASE}/quote",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"src": token_in, "dst": token_out, "amount": str(amount), "protocols": source, "includeGas": "true", "includeProtocols": "true"},
            )
            out = int(payload.get("dstAmount") or 0)
            if out <= 0:
                return None
            return RobinhoodQuote(source, token_in, token_out, amount, out, int(payload.get("gas") or 0))
        except httpx.HTTPStatusError as exc:
            logger.warning("robinhood_1inch_quote_failed", extra={"source": source, "status": exc.response.status_code, "token": token_in})
            return None

    async def scan(self, token: str, amount_eth: float = DEFAULT_AMOUNT_ETH) -> tuple[RobinhoodOpportunity, ...]:
        if not token or not token.startswith("0x"):
            raise ValueError("token must be a Robinhood Chain EVM address")
        amount = max(1, int(float(amount_eth) * 10**18))
        sources = await self.liquidity_sources()
        buy_quotes = await asyncio.gather(*(self._quote(source, WETH, token, amount) for source in sources))
        valid_buys = [quote for quote in buy_quotes if quote is not None]
        opportunities: list[RobinhoodOpportunity] = []
        for buy in valid_buys:
            sell_quotes = await asyncio.gather(*(self._quote(source, token, WETH, buy.amount_out) for source in sources if source != buy.source))
            for sell in (quote for quote in sell_quotes if quote is not None):
                gross = sell.amount_out - amount
                gross_bps = gross / amount * 10_000
                # 1inch quote gas is expressed in gas units. Use live gas price
                # from the chain RPC in execution; discovery uses a conservative
                # configurable ETH gas estimate so it never treats gas as free.
                gas_price_gwei = self._config_float("ROBINHOOD_ARB_GAS_PRICE_GWEI", 0.1)
                gas_wei = int((buy.gas + sell.gas) * gas_price_gwei * 1_000_000_000)
                net = gross - gas_wei
                net_bps = net / amount * 10_000
                opportunities.append(RobinhoodOpportunity(token, token[:8], amount_eth, buy.source, sell.source, buy.amount_out, sell.amount_out, gross, gross_bps, gas_wei, net, net_bps, net > 0, "profit_threshold_met" if net > 0 else "profit_threshold_not_met"))
        return tuple(sorted(opportunities, key=lambda item: (item.executable, item.net_profit_wei, item.net_profit_bps), reverse=True))

    async def hunt(self, limit: int | None = None) -> tuple[RobinhoodOpportunity, ...]:
        candidates = await self.discover_candidates(limit)
        results: list[RobinhoodOpportunity] = []
        for candidate in candidates:
            try:
                results.extend(await self.scan(candidate.token, self._config_float("ROBINHOOD_ARB_AMOUNT_ETH", DEFAULT_AMOUNT_ETH)))
            except Exception as exc:
                logger.warning("robinhood_arb_candidate_failed", extra={"token": candidate.token, "error": str(exc)})
        return tuple(sorted(results, key=lambda item: (item.executable, item.net_profit_wei, item.net_profit_bps), reverse=True))
