"""Persistent arbitrage hunt controller.

Each cycle gives the configured hotlist first-class priority, then falls back
to the existing global candidate discovery. Live execution remains handled by
the existing Telegram callback and executor; this controller never signs or
submits transactions itself.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.arbitrage.discovery import ArbitrageDiscovery, DiscoveryResult
from app.arbitrage.hunt import ArbitrageHunter, HuntCandidate, HuntResult
from app.arbitrage.hotlist import configured_hotlist_mints
from app.arbitrage.telemetry import telemetry, timed

DEFAULT_INTERVAL_SECONDS = 10.0
DEFAULT_HOTLIST_INTERVAL_SECONDS = 1.5


def _interval_seconds() -> float:
    try:
        return max(float(os.getenv("ARBITRAGE_HUNT_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS))), 1.0)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS


def _hotlist_interval_seconds() -> float:
    try:
        return max(
            float(os.getenv("ARBITRAGE_HOTLIST_INTERVAL_SECONDS", str(DEFAULT_HOTLIST_INTERVAL_SECONDS))),
            1.0,
        )
    except ValueError:
        return DEFAULT_HOTLIST_INTERVAL_SECONDS


def _hunt_sizes() -> tuple[float, ...]:
    raw = os.getenv(
        "ARBITRAGE_HUNT_DISCOVERY_SIZES_SOL",
        "0.02,0.04,0.10,0.50,1.00",
    ).strip()
    if not raw:
        return (0.02, 0.04, 0.10, 0.50, 1.00)
    values: list[float] = []
    for item in raw.split(","):
        try:
            value = float(item.strip())
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return tuple(dict.fromkeys(values)) or (0.02, 0.04, 0.10, 0.50, 1.00)


@dataclass(frozen=True)
class ContinuousHuntStatus:
    running: bool
    cycles: int
    hotlist_mints: tuple[str, ...] = ()
    hotlist_scans: int = 0
    global_scans: int = 0
    last_hotlist_result: HuntResult | None = None
    last_global_result: HuntResult | None = None

    @property
    def last_result(self) -> HuntResult | None:
        """Compatibility alias for callers that previously read last_result."""
        return self.last_global_result or self.last_hotlist_result


class ContinuousArbitrageHunt:
    """One process-wide background hunt controlled by /arbstop."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._cycles = 0
        self._hotlist_scans = 0
        self._global_scans = 0
        self._last_hotlist_result: HuntResult | None = None
        self._last_global_result: HuntResult | None = None
        self._lock = asyncio.Lock()
        self._alerted: set[tuple[str, float, str, str]] = set()

    @property
    def status(self) -> ContinuousHuntStatus:
        return ContinuousHuntStatus(
            self.running,
            self._cycles,
            configured_hotlist_mints(),
            self._hotlist_scans,
            self._global_scans,
            self._last_hotlist_result,
            self._last_global_result,
        )

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self,
        limit: int | None = None,
        on_profitable: Callable[[HuntResult], Awaitable[None]] | None = None,
    ) -> bool:
        async with self._lock:
            if self.running:
                return False
            self._stop_event = asyncio.Event()
            self._cycles = 0
            self._hotlist_scans = 0
            self._global_scans = 0
            self._last_hotlist_result = None
            self._last_global_result = None
            self._alerted.clear()
            self._task = asyncio.create_task(self._run(limit, on_profitable))
            return True

    async def stop(self) -> bool:
        async with self._lock:
            if not self.running:
                return False
            self._stop_event.set()
            task = self._task
            self._task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def _run(
        self,
        limit: int | None,
        on_profitable: Callable[[HuntResult], Awaitable[None]] | None,
    ) -> None:
        while not self._stop_event.is_set():
            cycle_started = asyncio.get_running_loop().time()
            self._cycles += 1
            telemetry.increment("hunt_cycles")

            # Hotlist pass: every known productive mint is checked directly
            # and frequently, independent of broad DexScreener filters.
            hotlist_result = await self._scan_hotlist(limit, on_profitable)
            self._last_hotlist_result = hotlist_result

            # Keep global discovery as a secondary source so the bot can still
            # discover new opportunities without starving the proven hotlist.
            if not self._stop_event.is_set():
                hunter: ArbitrageHunter | None = None
                try:
                    hunter = ArbitrageHunter()
                    self._global_scans += 1
                    with timed(telemetry, "global_hunt_ms"):
                        global_result = await hunter.hunt(limit)
                    await self._notify_new_opportunities(global_result, on_profitable)
                    self._last_global_result = global_result
                    telemetry.increment("global_hunt_successes")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    telemetry.increment("global_hunt_errors")
                    # A failed global cycle must not kill the 24/7 watcher.
                    pass
                finally:
                    if hunter is not None:
                        try:
                            await hunter.close()
                        except Exception:
                            pass

            telemetry.observe(
                "hunt_cycle_ms",
                (asyncio.get_running_loop().time() - cycle_started) * 1000.0,
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=_interval_seconds())
            except asyncio.TimeoutError:
                continue

    async def _scan_hotlist(
        self,
        limit: int | None,
        on_profitable: Callable[[HuntResult], Awaitable[None]] | None,
    ) -> HuntResult:
        mints = configured_hotlist_mints()
        if limit is not None:
            mints = mints[:limit]

        discoveries: list[tuple[HuntCandidate, DiscoveryResult]] = []
        errors: list[str] = []
        candidates: list[HuntCandidate] = []
        discovery = ArbitrageDiscovery()
        self._hotlist_scans += 1
        telemetry.increment("hotlist_scans")
        try:
            for mint in mints:
                candidate = HuntCandidate(
                    token_mint=mint,
                    symbol=mint[:8],
                    name="Hotlist token",
                    liquidity_usd=0.0,
                    volume_24h_usd=0.0,
                    dex_count=0,
                    score=0.0,
                    tier="HOT",
                    hotlist=True,
                )
                candidates.append(candidate)
                try:
                    with timed(telemetry, "hotlist_discovery_ms"):
                        result = await discovery.discover(mint, sizes_sol=_hunt_sizes())
                    discoveries.append((candidate, result))
                    telemetry.increment("hotlist_discoveries")
                    await self._notify_new_opportunities(
                        HuntResult((candidate,), ((candidate, result),)), on_profitable
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    telemetry.increment("hotlist_discovery_errors")
                    errors.append(f"{mint[:8]}: {exc}")
                if not self._stop_event.is_set():
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=_hotlist_interval_seconds())
                    except asyncio.TimeoutError:
                        pass
        finally:
            await discovery.close()

        discoveries.sort(key=lambda item: (
            bool(item[1].opportunity and item[1].opportunity.executable),
            item[1].opportunity.net_profit_atomic if item[1].opportunity else -1,
            item[1].opportunity.net_profit_bps if item[1].opportunity else float("-inf"),
        ), reverse=True)
        return HuntResult(tuple(candidates), tuple(discoveries), tuple(errors))

    async def _notify_new_opportunities(
        self,
        result: HuntResult,
        on_profitable: Callable[[HuntResult], Awaitable[None]] | None,
    ) -> None:
        if on_profitable is None:
            return
        for candidate, discovery in result.discoveries:
            opportunity = discovery.opportunity
            if opportunity is None or not opportunity.executable:
                continue
            buy_route = discovery.buy_quote.route_id if discovery.buy_quote else "unknown"
            sell_route = discovery.sell_quote.route_id if discovery.sell_quote else "unknown"
            key = (candidate.token_mint, discovery.amount_sol, buy_route, sell_route)
            if key in self._alerted:
                continue
            try:
                await on_profitable(HuntResult((candidate,), ((candidate, discovery),)))
                telemetry.increment("profitable_opportunity_callbacks")
            except asyncio.CancelledError:
                raise
            except Exception:
                telemetry.increment("profitable_opportunity_callback_errors")
                # Leave the key unmarked when the callback fails so the next
                # discovery cycle can retry it. This is especially important
                # for live execution, where a stale quote, temporary RPC issue,
                # or per-admin wallet refusal may safely reject one attempt.
                continue
            self._alerted.add(key)


def _has_executable(result: HuntResult) -> bool:
    return any(
        discovery.opportunity is not None and discovery.opportunity.executable
        for _, discovery in result.discoveries
    )


continuous_hunt = ContinuousArbitrageHunt()
