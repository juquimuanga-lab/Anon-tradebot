"""Persistent observe-only arbitrage hunt controller.

Runs candidate discovery repeatedly until explicitly stopped. Never signs or
submits transactions. Qualifying opportunities are reported through the
callback but do not stop the watcher.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.arbitrage.hunt import ArbitrageHunter, HuntResult

DEFAULT_INTERVAL_SECONDS = 10.0


def _interval_seconds() -> float:
    try:
        return max(float(os.getenv("ARBITRAGE_HUNT_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS))), 1.0)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS


@dataclass(frozen=True)
class ContinuousHuntStatus:
    running: bool
    cycles: int
    last_result: HuntResult | None = None


class ContinuousArbitrageHunt:
    """One process-wide background hunt controlled by /arbstop."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._cycles = 0
        self._last_result: HuntResult | None = None
        self._lock = asyncio.Lock()
        self._alerted: set[tuple[str, float, str, str]] = set()

    @property
    def status(self) -> ContinuousHuntStatus:
        return ContinuousHuntStatus(self.running, self._cycles, self._last_result)

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
            self._last_result = None
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
            hunter = ArbitrageHunter()
            try:
                self._cycles += 1
                result = await hunter.hunt(limit)
                self._last_result = result
                await self._notify_new_opportunities(result, on_profitable)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A single failed cycle must not kill the 24/7 watcher.
                pass
            finally:
                await hunter.close()

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=_interval_seconds())
            except asyncio.TimeoutError:
                continue

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
            self._alerted.add(key)
            await on_profitable(HuntResult((candidate,), ((candidate, discovery),)))


def _has_executable(result: HuntResult) -> bool:
    return any(
        discovery.opportunity is not None and discovery.opportunity.executable
        for _, discovery in result.discoveries
    )


continuous_hunt = ContinuousArbitrageHunt()
