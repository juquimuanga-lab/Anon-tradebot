"""Solana Tracker Data API connector for Phase 1 smart-money observation.

This module is deliberately read-only. It never places trades and it does not
change the bot's qualification or execution decisions.

The token-trades endpoint is queried for a specific mint and matched against
the configured SMART_MONEY_WALLETS list.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config.settings import settings

logger = logging.getLogger("app.connectors.solana_tracker")


class SolanaTrackerAPIError(Exception):
    """Raised when Solana Tracker cannot satisfy a request."""


@dataclass(frozen=True)
class SmartMoneyTrade:
    wallet: str
    tx: str
    trade_type: str
    volume_usd: float
    volume_sol: float
    price_usd: float
    timestamp_ms: int
    program: str

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - (self.timestamp_ms / 1000.0))


@dataclass(frozen=True)
class SmartMoneySignal:
    mint: str
    matched_trades: tuple[SmartMoneyTrade, ...]
    score: float
    strongest_buy_usd: float

    @property
    def matched_wallets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(t.wallet for t in self.matched_trades))

    @property
    def has_signal(self) -> bool:
        return bool(self.matched_trades)


class SolanaTrackerClient:
    """Small async client for the Solana Tracker Data API."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str],
        timeout_seconds: float = 8.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_seconds,
            headers={
                "x-api-key": api_key or "",
                "accept": "application/json",
            },
        )
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = asyncio.Lock()
        self._cache_ttl_seconds = 10.0

    async def aclose(self) -> None:
        await self._client.aclose()

    def _enabled(self) -> bool:
        return bool(
            settings.smart_money_enabled
            and self._api_key
            and settings.smart_money_wallets
        )

    @staticmethod
    def _safe_float(value) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _safe_int(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0


    async def get_wallet_profiles(self) -> dict[str, dict]:
        """Fetch and cache basic wallet quality metrics.

        PnL V2 exposes total PnL, ROI, win rate, and trade counts for a
        wallet. Profiles are cached for one hour because they change much
        more slowly than token trades.
        """
        if not self._enabled():
            return {}

        profiles: dict[str, dict] = {}
        for wallet in settings.smart_money_wallets:
            cache_key = f"wallet:{wallet}"
            cached = self._cache.get(cache_key)
            if (
                cached
                and time.monotonic() - cached[0] < 3600
                and isinstance(cached[1], dict)
            ):
                profiles[wallet] = cached[1]
                continue

            try:
                response = await self._client.get(
                    f"/v2/pnl/wallets/{wallet}",
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "solana_tracker_wallet_request_failed",
                    extra={"wallet": wallet, "error": type(exc).__name__},
                )
                continue

            if response.status_code >= 400:
                logger.warning(
                    "solana_tracker_wallet_http_error",
                    extra={"wallet": wallet, "status": response.status_code},
                )
                continue

            try:
                body = response.json()
            except ValueError:
                continue

            summary = body.get("summary") or {}
            analysis = body.get("analysis") or {}
            counts = summary.get("counts") or {}

            pnl_total = self._safe_float(
                (summary.get("pnl") or {}).get("total")
            )
            roi = self._safe_float(summary.get("roi"))
            win_rate = self._safe_float(
                analysis.get("winRate")
            )
            tokens_traded = self._safe_int(
                counts.get("tokensTraded")
            )
            trades = self._safe_int(
                counts.get("trades")
            )

            quality_score = min(
                100.0,
                (max(0.0, min(win_rate, 100.0)) * 0.50)
                + (max(0.0, min(roi, 100.0)) * 0.30)
                + (min(tokens_traded, 100) * 0.10)
                + (min(trades, 1000) / 1000.0 * 10.0),
            )

            profile = {
                "wallet": wallet,
                "pnl_total": pnl_total,
                "roi": roi,
                "win_rate": win_rate,
                "tokens_traded": tokens_traded,
                "trades": trades,
                "quality_score": quality_score,
            }

            # Reuse the same cache container; the value type is intentionally
            # broad because token signals and wallet profiles are independent.
            async with self._lock:
                self._cache[cache_key] = (time.monotonic(), profile)  # type: ignore[arg-type]

            profiles[wallet] = profile

        return profiles

    async def get_token_smart_money(
        self,
        mint: str,
        *,
        max_age_seconds: Optional[int] = None,
        force_refresh: bool = False,
    ) -> SmartMoneySignal:
        """Return recent qualifying buys from configured wallets.

        Phase 1 is observational: this method never raises for a normal
        disabled configuration and never causes a trade decision by itself.
        """
        empty = SmartMoneySignal(
            mint=mint,
            matched_trades=(),
            score=0.0,
            strongest_buy_usd=0.0,
        )

        if not self._enabled():
            return empty

        now = time.monotonic()
        cached = self._cache.get(mint)
        if (
            cached
            and not force_refresh
            and now - cached[0] < self._cache_ttl_seconds
            and isinstance(cached[1], SmartMoneySignal)
        ):
            return cached[1]

        lookback = (
            max_age_seconds
            if max_age_seconds is not None
            else settings.smart_money_trade_lookback_seconds
        )
        lookback = max(1, int(lookback))
        cutoff_ms = int((time.time() - lookback) * 1000)

        try:
            response = await self._client.get(
                f"/trades/{mint}",
                params={
                    "hideArb": "true",
                    "sortDirection": "DESC",
                },
            )
        except httpx.HTTPError as exc:
            raise SolanaTrackerAPIError(
                f"Solana Tracker request failed: {type(exc).__name__}"
            ) from exc

        if response.status_code == 401:
            raise SolanaTrackerAPIError(
                "Solana Tracker authentication failed"
            )
        if response.status_code == 429:
            raise SolanaTrackerAPIError(
                "Solana Tracker rate limit exceeded"
            )
        if response.status_code >= 400:
            raise SolanaTrackerAPIError(
                f"Solana Tracker HTTP {response.status_code}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise SolanaTrackerAPIError(
                "Solana Tracker returned invalid JSON"
            ) from exc

        if not isinstance(body, dict):
            raise SolanaTrackerAPIError(
                "Solana Tracker returned an unexpected response"
            )

        configured = set(settings.smart_money_wallets)
        matched: list[SmartMoneyTrade] = []

        for row in body.get("trades") or []:
            if not isinstance(row, dict):
                continue

            wallet = str(row.get("wallet") or "")
            if wallet not in configured:
                continue

            trade_type = str(row.get("type") or "").lower()
            if trade_type != "buy":
                continue

            # Phase 1 is focused on Pump.fun launches.
            # Solana Tracker identifies the venue/program on each trade.
            if "pumpfun" not in str(row.get("program") or "").lower():
                continue

            timestamp_ms = self._safe_int(row.get("time"))
            if timestamp_ms and timestamp_ms < cutoff_ms:
                continue

            volume_usd = self._safe_float(row.get("volume"))
            if volume_usd < settings.smart_money_min_buy_usd:
                continue

            matched.append(
                SmartMoneyTrade(
                    wallet=wallet,
                    tx=str(row.get("tx") or ""),
                    trade_type=trade_type,
                    volume_usd=volume_usd,
                    volume_sol=self._safe_float(row.get("volumeSol")),
                    price_usd=self._safe_float(row.get("priceUsd")),
                    timestamp_ms=timestamp_ms,
                    program=str(row.get("program") or ""),
                )
            )

            if len(matched) >= settings.smart_money_max_trades_per_token:
                break

        # Phase 1 score is intentionally descriptive, not a gate.
        # Size contributes up to 60 points, recency up to 25, and wallet
        # breadth up to 15.
        strongest = max(
            (trade.volume_usd for trade in matched),
            default=0.0,
        )

        size_score = min(
            60.0,
            60.0 * (strongest / max(settings.smart_money_min_buy_usd * 10.0, 1.0)),
        )

        recency_score = 0.0
        if matched:
            freshest_age = min(trade.age_seconds for trade in matched)
            recency_score = max(
                0.0,
                25.0 * (
                    1.0 - min(
                        freshest_age / max(lookback, 1),
                        1.0,
                    )
                ),
            )

        wallet_count = len({trade.wallet for trade in matched})
        breadth_score = min(15.0, wallet_count * 7.5)

        score = min(
            100.0,
            size_score + recency_score + breadth_score,
        )

        signal = SmartMoneySignal(
            mint=mint,
            matched_trades=tuple(matched),
            score=score,
            strongest_buy_usd=strongest,
        )

        async with self._lock:
            self._cache[mint] = (time.monotonic(), signal)

        return signal


solana_tracker = SolanaTrackerClient(
    settings.solana_tracker_base_url,
    settings.solana_tracker_api_key,
)
