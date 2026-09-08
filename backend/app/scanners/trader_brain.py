"""Deterministic chart-structure entry timing for Pump.fun Smart Filter.

This module deliberately does not call an LLM in the execution hot path. It
builds a short-lived, trader-like state machine from the snapshots the scanner
already produces and separates *candidate quality* from *entry timing*.

Graduation Hunter answers: "Is this token worth hunting?"
Trader Brain answers: "Is this a good moment to enter?"

The integration is intentionally monkey-patched so the existing scanner,
execution adapters, risk controls, and other strategy lanes remain unchanged.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("app.scanners.trader_brain")

SOURCE_PUMPFUN = "pumpfun"
SMART_STRATEGY = "smart"

# Short-lived market structure. The brain is deliberately more permissive than
# v1: it should reject bad/chased entries without requiring a perfect chart.
MAX_OBSERVATIONS = 64
MAX_HISTORY_SECONDS = 180.0
MIN_OBSERVATIONS = 3

BREAKOUT_MIN_PCT = 1.5
BREAKOUT_MAX_PCT = 18.0

PULLBACK_MIN_PCT = 4.0
PULLBACK_MAX_PCT = 22.0
RECLAIM_MIN_BOUNCE_PCT = 2.5
RECLAIM_REQUIRED_RECOVERY_PCT = 85.0

# Hard anti-chase veto. We keep this strict because it protects against the
# exact late-entry pattern the original Trader Brain was built to prevent.
EXHAUSTION_SHORT_RUNUP_PCT = 10.0
EXHAUSTION_FROM_LOW_PCT = 25.0
EXHAUSTION_NEAR_PEAK_PCT = 8.0

# Flow thresholds are intentionally lower than v1. Existing creator,
# liquidity, Graduation Hunter, hard-filter and execution risk controls remain
# authoritative outside this module.
MIN_HEALTHY_BUY_PRESSURE = 0.58
MIN_RECLAIM_BUY_PRESSURE = 0.58
MIN_BREAKOUT_BUY_PRESSURE = 0.60
MIN_MOMENTUM_BUY_PRESSURE = 0.60

MIN_HEALTHY_BUY_SELL_RATIO = 1.15
MIN_RECLAIM_BUY_SELL_RATIO = 1.20
MIN_BREAKOUT_BUY_SELL_RATIO = 1.25
MIN_MOMENTUM_BUY_SELL_RATIO = 1.30

MIN_RECLAIM_VELOCITY_SOL_PER_SEC = 0.004
MIN_BREAKOUT_VELOCITY_SOL_PER_SEC = 0.004
MIN_MOMENTUM_VELOCITY_SOL_PER_SEC = 0.005

# Momentum continuation prevents the brain from waiting forever for a
# pullback on a genuinely healthy trend. It still has an anti-chase cap.
MOMENTUM_MAX_SHORT_MOVE_PCT = 15.0
MOMENTUM_MAX_FROM_RECENT_LOW_PCT = 22.0
MOMENTUM_MIN_RECENT_RANGE_PCT = 2.0


@dataclass
class Observation:
    timestamp: float
    price: float
    market_cap: float
    liquidity: float
    volume: float
    buy_pressure: float
    buy_sell_ratio: float
    buy_velocity: float
    real_sol: float


@dataclass
class BrainState:
    observations: deque[Observation] = field(
        default_factory=lambda: deque(maxlen=MAX_OBSERVATIONS)
    )
    peak_market_cap: float = 0.0
    peak_timestamp: float = 0.0
    pullback_low_market_cap: float = 0.0
    pullback_started_at: float = 0.0
    last_phase: str = "warming_up"
    last_decision: str = "wait"
    last_reason: str = ""


class TraderBrain:
    """Short-lived price-action state machine for Smart Pump.fun entries."""

    def __init__(self) -> None:
        self._states: dict[str, BrainState] = {}

    def reset(self, mint: str) -> None:
        self._states.pop(mint, None)

    def _state(self, mint: str) -> BrainState:
        state = self._states.get(mint)
        if state is None:
            state = BrainState()
            self._states[mint] = state
        return state

    @staticmethod
    def _signal(token: Any, name: str, default: float = 0.0) -> float:
        safety = (getattr(token, "raw_enrichment", {}) or {}).get(
            "pumpfun_launch_safety"
        ) or {}
        signals = safety.get("signals") or {}
        try:
            return float(signals.get(name, default) or default)
        except (TypeError, ValueError):
            return default

    def observe(self, token: Any, rule: Any | None = None) -> dict[str, Any]:
        """Record one Pump.fun Smart snapshot and classify its structure."""
        if getattr(token, "source", "") != SOURCE_PUMPFUN:
            return {"phase": "not_applicable"}
        if rule is not None and getattr(rule, "strategy", SMART_STRATEGY) != SMART_STRATEGY:
            return {"phase": "not_applicable"}

        price = float(getattr(token, "price_usd", 0.0) or 0.0)
        market_cap = float(getattr(token, "market_cap_usd", 0.0) or 0.0)
        if price <= 0.0 or market_cap <= 0.0:
            return {"phase": "data_pending"}

        now = time.monotonic()
        state = self._state(token.mint)
        observation = Observation(
            timestamp=now,
            price=price,
            market_cap=market_cap,
            liquidity=float(getattr(token, "liquidity_usd", 0.0) or 0.0),
            volume=float(getattr(token, "volume_24h_usd", 0.0) or 0.0),
            buy_pressure=self._signal(token, "buy_pressure", 0.0),
            buy_sell_ratio=self._signal(token, "buy_sell_ratio", 0.0),
            buy_velocity=self._signal(token, "buy_velocity_sol_per_sec", 0.0),
            real_sol=float(getattr(token, "real_sol_reserves_sol", 0.0) or 0.0),
        )
        state.observations.append(observation)

        cutoff = now - MAX_HISTORY_SECONDS
        while state.observations and state.observations[0].timestamp < cutoff:
            state.observations.popleft()

        if market_cap >= state.peak_market_cap:
            state.peak_market_cap = market_cap
            state.peak_timestamp = now

        result = self._classify(state)
        state.last_phase = result["phase"]
        state.last_decision = result.get("decision", "wait")
        state.last_reason = str(result.get("reason") or "")
        token.raw_enrichment["trader_brain"] = result
        return result

    def _classify(self, state: BrainState) -> dict[str, Any]:
        observations = list(state.observations)
        if len(observations) < MIN_OBSERVATIONS:
            return {
                "phase": "warming_up",
                "decision": "wait",
                "confidence": 0.0,
                "reason": f"building chart context ({len(observations)}/{MIN_OBSERVATIONS})",
            }

        current = observations[-1]
        previous = observations[-2]
        recent = observations[-8:]
        prior = observations[:-1]
        recent_low = min(item.market_cap for item in recent)
        prior_high = max(item.market_cap for item in prior)
        peak = max(state.peak_market_cap, current.market_cap)

        short_change = self._pct_change(previous.market_cap, current.market_cap)
        from_recent_low = self._pct_change(recent_low, current.market_cap)
        from_peak = self._pct_change(peak, current.market_cap)
        recent_range = self._range_pct(recent)
        pressure_delta = current.buy_pressure - previous.buy_pressure
        ratio_delta = current.buy_sell_ratio - previous.buy_sell_ratio
        near_peak = abs(from_peak) <= EXHAUSTION_NEAR_PEAK_PCT

        # 1. Hard anti-chase veto.
        exhaustion = (
            from_recent_low >= EXHAUSTION_FROM_LOW_PCT
            and short_change >= EXHAUSTION_SHORT_RUNUP_PCT
            and near_peak
            and (
                current.buy_pressure < MIN_HEALTHY_BUY_PRESSURE
                or pressure_delta <= -0.08
                or ratio_delta <= -0.50
            )
        )
        if exhaustion:
            return {
                "phase": "extended",
                "decision": "wait",
                "confidence": 0.94,
                "reason": "vertical expansion is too close to the local high; flow is weakening",
                "from_recent_low_pct": round(from_recent_low, 2),
                "distance_from_peak_pct": round(from_peak, 2),
                "short_change_pct": round(short_change, 2),
                "buy_pressure": round(current.buy_pressure, 4),
                "buy_sell_ratio": round(current.buy_sell_ratio, 4),
            }

        # 2. Pullback + reclaim.
        pullback_pct = -from_peak
        if PULLBACK_MIN_PCT <= pullback_pct <= PULLBACK_MAX_PCT:
            if state.pullback_low_market_cap <= 0.0 or current.market_cap < state.pullback_low_market_cap:
                state.pullback_low_market_cap = current.market_cap
                state.pullback_started_at = current.timestamp

            pullback_low = state.pullback_low_market_cap
            bounce = self._pct_change(pullback_low, current.market_cap)
            prior_structure_recovery = (
                current.market_cap / prior_high * 100.0
                if prior_high > 0.0
                else 0.0
            )
            if (
                bounce >= RECLAIM_MIN_BOUNCE_PCT
                and current.buy_pressure >= MIN_RECLAIM_BUY_PRESSURE
                and current.buy_sell_ratio >= MIN_RECLAIM_BUY_SELL_RATIO
                and current.buy_velocity >= MIN_RECLAIM_VELOCITY_SOL_PER_SEC
                and prior_structure_recovery >= RECLAIM_REQUIRED_RECOVERY_PCT
            ):
                return {
                    "phase": "reclaim",
                    "decision": "enter",
                    "confidence": 0.88,
                    "reason": "healthy pullback followed by flow-backed reclaim of prior structure",
                    "pullback_from_peak_pct": round(pullback_pct, 2),
                    "bounce_from_pullback_low_pct": round(bounce, 2),
                    "structure_recovery_pct": round(prior_structure_recovery, 2),
                    "buy_pressure": round(current.buy_pressure, 4),
                    "buy_sell_ratio": round(current.buy_sell_ratio, 4),
                    "buy_velocity_sol_per_sec": round(current.buy_velocity, 6),
                }
            return {
                "phase": "pullback",
                "decision": "wait",
                "confidence": 0.78,
                "reason": "pullback is forming; wait for buyers to reclaim structure",
                "pullback_from_peak_pct": round(pullback_pct, 2),
                "bounce_from_pullback_low_pct": round(bounce, 2),
                "structure_recovery_pct": round(prior_structure_recovery, 2),
            }

        # 3. Controlled breakout.
        breakout_pct = self._pct_change(prior_high, current.market_cap)
        controlled_breakout = (
            breakout_pct >= BREAKOUT_MIN_PCT
            and breakout_pct <= BREAKOUT_MAX_PCT
            and current.buy_pressure >= MIN_BREAKOUT_BUY_PRESSURE
            and current.buy_sell_ratio >= MIN_BREAKOUT_BUY_SELL_RATIO
            and current.buy_velocity >= MIN_BREAKOUT_VELOCITY_SOL_PER_SEC
            and short_change <= BREAKOUT_MAX_PCT
        )
        if controlled_breakout:
            state.pullback_low_market_cap = 0.0
            return {
                "phase": "breakout",
                "decision": "enter",
                "confidence": 0.86,
                "reason": "controlled breakout with confirming demand and capital velocity",
                "breakout_pct": round(breakout_pct, 2),
                "short_change_pct": round(short_change, 2),
                "buy_pressure": round(current.buy_pressure, 4),
                "buy_sell_ratio": round(current.buy_sell_ratio, 4),
                "buy_velocity_sol_per_sec": round(current.buy_velocity, 6),
            }

        # 4. Momentum continuation. This catches a healthy trend that keeps
        # climbing without giving the bot a textbook pullback.
        trend_observations = observations[-4:]
        positive_steps = sum(
            1
            for left, right in zip(trend_observations, trend_observations[1:])
            if right.market_cap > left.market_cap
        )
        momentum = (
            positive_steps >= 2
            and current.market_cap > previous.market_cap
            and current.buy_pressure >= MIN_MOMENTUM_BUY_PRESSURE
            and current.buy_sell_ratio >= MIN_MOMENTUM_BUY_SELL_RATIO
            and current.buy_velocity >= MIN_MOMENTUM_VELOCITY_SOL_PER_SEC
            and short_change <= MOMENTUM_MAX_SHORT_MOVE_PCT
            and from_recent_low <= MOMENTUM_MAX_FROM_RECENT_LOW_PCT
            and recent_range >= MOMENTUM_MIN_RECENT_RANGE_PCT
            and not near_peak
        )
        if momentum:
            return {
                "phase": "trending",
                "decision": "enter",
                "confidence": 0.82,
                "reason": "healthy momentum continuation with sustained demand and controlled price action",
                "positive_steps": positive_steps,
                "short_change_pct": round(short_change, 2),
                "from_recent_low_pct": round(from_recent_low, 2),
                "recent_range_pct": round(recent_range, 2),
                "buy_pressure": round(current.buy_pressure, 4),
                "buy_sell_ratio": round(current.buy_sell_ratio, 4),
                "buy_velocity_sol_per_sec": round(current.buy_velocity, 6),
            }

        # Healthy rising structure that is not yet an entry.
        rising = (
            current.market_cap > previous.market_cap
            and current.buy_pressure >= MIN_HEALTHY_BUY_PRESSURE
            and current.buy_sell_ratio >= MIN_HEALTHY_BUY_SELL_RATIO
        )
        if rising and recent_range <= 22.0:
            return {
                "phase": "trending",
                "decision": "wait",
                "confidence": 0.68,
                "reason": "trend is healthy but entry structure is not confirmed yet",
                "recent_range_pct": round(recent_range, 2),
            }

        # Falling flow near a high is distribution, not a buy signal.
        if near_peak and (
            current.buy_pressure < 0.55
            or current.buy_sell_ratio < 1.0
        ):
            return {
                "phase": "distribution",
                "decision": "wait",
                "confidence": 0.90,
                "reason": "price is near the local high while underlying demand is deteriorating",
                "distance_from_peak_pct": round(from_peak, 2),
                "buy_pressure": round(current.buy_pressure, 4),
                "buy_sell_ratio": round(current.buy_sell_ratio, 4),
            }

        if recent_range <= 12.0 and current.buy_pressure >= MIN_HEALTHY_BUY_PRESSURE:
            return {
                "phase": "accumulation",
                "decision": "wait",
                "confidence": 0.72,
                "reason": "compressed range with healthy demand; wait for expansion",
                "recent_range_pct": round(recent_range, 2),
            }

        return {
            "phase": "watch",
            "decision": "wait",
            "confidence": 0.58,
            "reason": "no acceptable entry structure yet",
        }

    @staticmethod
    def _pct_change(old: float, new: float) -> float:
        if old <= 0.0:
            return 0.0
        return (new - old) / old * 100.0

    @staticmethod
    def _range_pct(observations: list[Observation]) -> float:
        if not observations:
            return 0.0
        low = min(item.market_cap for item in observations)
        high = max(item.market_cap for item in observations)
        if low <= 0.0:
            return 0.0
        return (high - low) / low * 100.0

    def evaluate_entry(self, token: Any, rule: Any) -> dict[str, Any]:
        """Return the current entry decision for a Smart Pump.fun token."""
        if getattr(token, "source", "") != SOURCE_PUMPFUN:
            return {"decision": "allow", "phase": "not_applicable"}
        if getattr(rule, "strategy", SMART_STRATEGY) != SMART_STRATEGY:
            return {"decision": "allow", "phase": "not_applicable"}

        state = self._states.get(token.mint)
        if state is None or not state.observations:
            self.observe(token, rule)
            state = self._states[token.mint]
        result = self._classify(state)
        token.raw_enrichment["trader_brain"] = result
        return result


_BRAIN = TraderBrain()
_INSTALLED = False


def _install_method_patches() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    try:
        from app.scanners.scanner import ScannerService
        from app.storage import repository as repo
    except Exception:
        logger.debug("trader_brain_patch_deferred", exc_info=True)
        return

    original_screen = getattr(ScannerService, "_screen_and_maybe_trade", None)
    original_maybe_trade = getattr(ScannerService, "_maybe_trade", None)
    if original_screen is None or original_maybe_trade is None:
        logger.warning("trader_brain_patch_targets_missing")
        return

    if getattr(original_screen, "_trader_brain_patch", False):
        _INSTALLED = True
        return

    async def screen_wrapper(self, token, rule, notify_on_fail):
        try:
            if (
                getattr(token, "source", "") == SOURCE_PUMPFUN
                and getattr(rule, "strategy", SMART_STRATEGY) == SMART_STRATEGY
            ):
                _BRAIN.observe(token, rule)
        except Exception:
            # Entry intelligence must never break the existing scanner.
            logger.warning(
                "trader_brain_observation_failed",
                extra={"mint": getattr(token, "mint", "")},
                exc_info=True,
            )
        return await original_screen(self, token, rule, notify_on_fail)

    screen_wrapper._trader_brain_patch = True
    ScannerService._screen_and_maybe_trade = screen_wrapper

    async def maybe_trade_wrapper(self, token, rule, score_result):
        if (
            getattr(token, "source", "") == SOURCE_PUMPFUN
            and getattr(rule, "strategy", SMART_STRATEGY) == SMART_STRATEGY
        ):
            try:
                decision = _BRAIN.evaluate_entry(token, rule)
                logger.info(
                    "graduation_trader_brain_decision",
                    extra={
                        "mint": token.mint,
                        "rule_id": getattr(rule, "id", None),
                        "decision": decision.get("decision"),
                        "phase": decision.get("phase"),
                        "confidence": decision.get("confidence"),
                        "reason": decision.get("reason"),
                        "market_cap_usd": getattr(token, "market_cap_usd", 0.0),
                    },
                )
                if decision.get("decision") == "wait":
                    reason = (
                        "Trader Brain: "
                        + str(decision.get("reason") or "entry timing not confirmed")
                    )
                    await repo.save_trade_decision(
                        token.mint,
                        rule.id,
                        "wait",
                        reason,
                        score_result.score,
                    )
                    token.raw_enrichment["trader_brain_wait"] = True
                    return False
            except Exception:
                # Fail open only for the *new* intelligence layer. The original
                # scanner and all existing safety/risk controls remain intact.
                logger.exception(
                    "trader_brain_entry_evaluation_failed",
                    extra={"mint": getattr(token, "mint", "")},
                )

        return await original_maybe_trade(self, token, rule, score_result)

    maybe_trade_wrapper._trader_brain_patch = True
    ScannerService._maybe_trade = maybe_trade_wrapper
    _INSTALLED = True
    logger.info("graduation_trader_brain_installed")


def install_trader_brain() -> None:
    """Install lazily once ScannerService is fully imported."""
    _install_method_patches()
