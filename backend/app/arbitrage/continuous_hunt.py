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
        self._last_notified_keys: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> ContinuousHuntStatus:
        return ContinuousHuntStatus(
            running=self.running,
            cycles=self._cycles,
            hotlist_mints=tuple(configured_hotlist_mints()),
            hotlist_scans=self._hotlist_scans,
            global_scans=self._global_scans,
            last_hotlist_result=self._last_hotlist_result,
            last_global_result=self._last_global_result,
        )

    async def start(
        self,
        bot,
        on_profitable: Callable[[HuntResult], Awaitable[None]] | None = None,
    ) -> bool:
        async with self._lock:
            if self.running:
                return False
            self._stop_event = asyncio.Event()
            self._task = asyncio.create_task(self._run(bot, on_profitable))
            return True

    async def stop(self) -> bool:
        async with self._lock:
            if not self.running:
                return False
            self._stop_event.set()
            task = self._task
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def _notify_new_opportunities(
        self,
        result: HuntResult,
        on_profitable: Callable[[HuntResult], Awaitable[None]] | None,
    ) -> None:
        if on_profitable is None:
            return
        profitable = [candidate for candidate in result.candidates if candidate.executable]
        if not profitable:
            return
        fresh = [
            candidate
            for candidate in profitable
            if candidate.key not in self._last_notified_keys
        ]
        if not fresh:
            return
        try:
            await on_profitable(
                HuntResult(
                    candidates=tuple(fresh),
                    scanned_mints=result.scanned_mints,
                    duration_ms=result.duration_ms,
                )
            )
        except Exception:
            telemetry.increment("continuous_hunt_callback_errors")
            raise
        for candidate in fresh:
            self._last_notified_keys.add(candidate.key)

    async def _run(
        self,
        bot,
        on_profitable: Callable[[HuntResult], Awaitable[None]] | None,
    ) -> None:
        hunter = ArbitrageHunter()
        discovery = ArbitrageDiscovery()
        last_hotlist_at = 0.0
        try:
            while not self._stop_event.is_set():
                self._cycles += 1
                now = asyncio.get_running_loop().time()
                hotlist = configured_hotlist_mints()
                if hotlist and now - last_hotlist_at >= _hotlist_interval_seconds():
                    with timed(telemetry, "continuous_hotlist_scan_ms"):
                        result = await hunter.scan(
                            sizes_sol=_hunt_sizes(),
                            mints=hotlist,
                        )
                    self._hotlist_scans += 1
                    self._last_hotlist_result = result
                    last_hotlist_at = now
                    await self._notify_new_opportunities(result, on_profitable)

                with timed(telemetry, "continuous_global_scan_ms"):
                    result = await hunter.scan(
                        sizes_sol=_hunt_sizes(),
                    )
                self._global_scans += 1
                self._last_global_result = result
                await self._notify_new_opportunities(result, on_profitable)

                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=_interval_seconds()
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            self._task = None
