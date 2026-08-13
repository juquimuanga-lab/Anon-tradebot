"""Read-only Solana Tracker Data API integration for Phase 1 smart money.

This module intentionally does not execute trades and does not use Helius.
It is only queried after an existing token has already passed the bot's
normal qualification rules.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.config.settings import settings

logger = logging.getLogger("app.connectors.solana_tracker")

BASE_URL = "https://data.solanatracker.io"


class SolanaTrackerError(Exception):
    """Base error for the Solana Tracker integration."""


@dataclass
class SmartMoneyTrade:
    wallet: str
    amount_usd: float
    amount_sol: float
    trade_type: str
    program: str
    timestamp_ms: int
    tx: str
    seconds_after_seen: float

    @property
    def short_wallet(self) -> str:
        return f"{self.wallet[:6]}...{self.wallet[-6:]}"


@dataclass
class SmartMoneyWalletQuality:
    wallet: str
    realized_pnl_usd: float = 0.0
    volume_usd: float = 0.0
    trades: int = 0
    positive_streak: int = 0
    negative_streak: int = 0
    drawdown_percent: float = 0.0

    @property
    def score(self) -> float:
        """Conservative quality score used only for telemetry.

        This deliberately does not treat the provider's displayed win rate
        as ground truth because the performance endpoint exposes realized
        PnL, volume, trade count, streaks and drawdown rather than a single
        universal win-rate definition.
        """
        score = 50.0

        if self.realized_pnl_usd > 0:
            score += 15.0
        elif self.realized_pnl_usd < 0:
            score -= 15.0

        if self.volume_usd > 0 and self.realized_pnl_usd > 0:
            roi = self.realized_pnl_usd / self.volume_usd
            if roi >= 0.10:
                score += 15.0
            elif roi >= 0.03:
                score += 8.0

        if self.trades >= 100:
            score += 8.0
        elif self.trades >= 30:
            score += 4.0

        if self.positive_streak >= 7:
            score += 5.0

        if self.drawdown_percent > 10:
            score -= 10.0
        elif self.drawdown_percent > 5:
            score -= 5.0

        return max(0.0, min(100.0, score))


@dataclass
class SmartMoneySignal:
    detected: bool
    score: float = 0.0
    trades: list[SmartMoneyTrade] | None = None
    qualities: dict[str, SmartMoneyWalletQuality] | None = None

    def __post_init__(self):
        if self.trades is None:
            self.trades = []
        if self.qualities is None:
            self.qualities = {}

    @property
    def wallet_count(self) -> int:
        return len({trade.wallet for trade in self.trades or []})

    def summary(self) -> str:
        if not self.detected:
            return "No tracked Pump.fun smart-money buy detected"

        lines = [
            f"Smart Money Score: {self.score:.1f}/100",
            f"Tracked wallets buying: {self.wallet_count}",
        ]

        for trade in self.trades[:5]:
            quality = self.qualities.get(trade.wallet)
            qscore = quality.score if quality else 50.0
            lines.append(
                f"- {trade.short_wallet} bought "
                f"${trade.amount_usd:,.2f} "
                f"{max(0.0, trade.seconds_after_seen):.0f}s ago "
                f"| quality {qscore:.0f}/100"
            )

        return "\n".join(lines)


class SolanaTrackerClient:
    """Small async client with aggressive caching for Phase 1."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        timeout_seconds: float = 8.0,
    ):
        self.api_key = api_key or settings.solana_tracker_api_key
        self.timeout_seconds = timeout_seconds
        self._quality_cache: dict[
            str, tuple[float, SmartMoneyWalletQuality]
        ] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(
            settings.smart_money_enabled
            and self.api_key
            and settings.smart_money_wallets
        )

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None):
        if not self.api_key:
            raise SolanaTrackerError("SOLANA_TRACKER_API_KEY is not configured")

        url = f"{BASE_URL}{path}"
        headers = {"x-api-key": self.api_key}

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds
            ) as client:
                response = await client.get(
                    url,
                    headers=headers,
                    params=params,
                )
        except Exception as exc:
            raise SolanaTrackerError(
                f"request failed: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise SolanaTrackerError(
                f"HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

        try:
            return response.json()
        except Exception as exc:
            raise SolanaTrackerError(
                "invalid JSON response"
            ) from exc

    async def get_token_trades(
        self,
        mint: str,
    ) -> list[dict[str, Any]]:
        """Fetch latest token trades.

        The API documents the response as {"trades": [...]} and exposes
        wallet, type, volume, volumeSol, time, program and tx.
        """
        data = await self._get(
            f"/trades/{mint}",
            params={
                "sortDirection": "DESC",
                "hideArb": "true",
            },
        )
        trades = data.get("trades", [])
        if not isinstance(trades, list):
            return []
        return trades[: settings.smart_money_max_trades_per_token]

    async def get_wallet_quality(
        self,
        wallet: str,
    ) -> SmartMoneyWalletQuality:
        now = time.monotonic()

        cached = self._quality_cache.get(wallet)
        if cached:
            cached_at, quality = cached
            if (
                now - cached_at
                < settings.smart_money_wallet_cache_seconds
            ):
                return quality

        async with self._lock:
            cached = self._quality_cache.get(wallet)
            if cached:
                cached_at, quality = cached
                if (
                    time.monotonic() - cached_at
                    < settings.smart_money_wallet_cache_seconds
                ):
                    return quality

            data = await self._get(
                f"/v2/pnl/wallets/{wallet}/performance",
                params={"period": "30d"},
            )

            totals = data.get("totals") or {}
            streaks = data.get("streaks") or {}
            drawdown = data.get("drawdown") or {}

            quality = SmartMoneyWalletQuality(
                wallet=wallet,
                realized_pnl_usd=float(
                    totals.get("realizedPnl") or 0.0
                ),
                volume_usd=float(
                    totals.get("volume") or 0.0
                ),
                trades=int(
                    totals.get("trades") or 0
                ),
                positive_streak=int(
                    streaks.get("positive") or 0
                ),
                negative_streak=int(
                    streaks.get("negative") or 0
                ),
                drawdown_percent=float(
                    drawdown.get("percent") or 0.0
                ),
            )

            self._quality_cache[wallet] = (
                time.monotonic(),
                quality,
            )
            return quality

    async def find_smart_money(
        self,
        mint: str,
        *,
        first_seen_timestamp: float,
    ) -> SmartMoneySignal:
        """Find meaningful recent Pump.fun buys from tracked wallets.

        `first_seen_timestamp` is the bot's observed launch timestamp.
        The provider trade timestamp is compared against it to measure
        entry latency.
        """
        if not self.enabled:
            return SmartMoneySignal(detected=False)

        try:
            raw_trades = await self.get_token_trades(mint)
        except SolanaTrackerError as exc:
            logger.warning(
                "solana_tracker_token_trades_failed",
                extra={"mint": mint, "error": str(exc)},
            )
            return SmartMoneySignal(detected=False)

        tracked = {
            wallet.strip()
            for wallet in settings.smart_money_wallets
            if wallet.strip()
        }

        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - (
            settings.smart_money_trade_lookback_seconds * 1000
        )

        matches: list[SmartMoneyTrade] = []

        for raw in raw_trades:
            wallet = str(raw.get("wallet") or "")
            trade_type = str(raw.get("type") or "").lower()
            program = str(raw.get("program") or "").lower()

            if wallet not in tracked:
                continue

            if trade_type != "buy":
                continue

            if program not in {
                "pumpfun-amm",
                "pumpfun",
                "pump.fun",
            }:
                continue

            timestamp_ms = int(raw.get("time") or 0)
            if timestamp_ms <= 0:
                continue

            if timestamp_ms < cutoff_ms:
                continue

            amount_usd = float(
                raw.get("volume") or raw.get("volumeUsd") or 0.0
            )
            amount_sol = float(
                raw.get("volumeSol") or 0.0
            )

            if amount_usd < settings.smart_money_min_buy_usd:
                continue

            seconds_after_seen = (
                timestamp_ms / 1000.0
                - first_seen_timestamp
            )

            # A negative latency can happen because Solana Tracker and our
            # watcher have different observation clocks. Keep it, but cap
            # it to zero for scoring.
            matches.append(
                SmartMoneyTrade(
                    wallet=wallet,
                    amount_usd=amount_usd,
                    amount_sol=amount_sol,
                    trade_type=trade_type,
                    program=program,
                    timestamp_ms=timestamp_ms,
                    tx=str(raw.get("tx") or ""),
                    seconds_after_seen=seconds_after_seen,
                )
            )

        # One meaningful latest buy per wallet is enough for Phase 1.
        latest_by_wallet: dict[str, SmartMoneyTrade] = {}
        for trade in matches:
            previous = latest_by_wallet.get(trade.wallet)
            if (
                previous is None
                or trade.timestamp_ms > previous.timestamp_ms
            ):
                latest_by_wallet[trade.wallet] = trade

        matches = sorted(
            latest_by_wallet.values(),
            key=lambda trade: trade.timestamp_ms,
            reverse=True,
        )

        if not matches:
            return SmartMoneySignal(detected=False)

        qualities: dict[str, SmartMoneyWalletQuality] = {}

        for trade in matches:
            try:
                qualities[trade.wallet] = (
                    await self.get_wallet_quality(
                        trade.wallet
                    )
                )
            except SolanaTrackerError as exc:
                logger.warning(
                    "solana_tracker_wallet_quality_failed",
                    extra={
                        "wallet": trade.wallet,
                        "error": str(exc),
                    },
                )

        # Score:
        # - wallet quality: up to 50
        # - buy size: up to 25
        # - recency after observed launch: up to 15
        # - multi-wallet confirmation: up to 10
        best_score = 0.0

        for trade in matches:
            quality = qualities.get(trade.wallet)
            quality_score = (
                quality.score if quality else 50.0
            )

            size_score = min(
                25.0,
                10.0 + (
                    max(
                        0.0,
                        min(
                            15.0,
                            trade.amount_usd / 100.0,
                        ),
                    )
                ),
            )

            latency = max(
                0.0,
                trade.seconds_after_seen,
            )

            if latency <= 10:
                latency_score = 15.0
            elif latency <= 30:
                latency_score = 12.0
            elif latency <= 60:
                latency_score = 9.0
            elif latency <= 120:
                latency_score = 5.0
            else:
                latency_score = 2.0

            score = (
                quality_score * 0.50
                + size_score
                + latency_score
            )

            best_score = max(best_score, score)

        if len(matches) >= 2:
            best_score = min(
                100.0,
                best_score + 10.0,
            )

        return SmartMoneySignal(
            detected=True,
            score=best_score,
            trades=matches,
            qualities=qualities,
        )
