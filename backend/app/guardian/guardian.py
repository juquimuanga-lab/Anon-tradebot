"""GO Guardian V1 - always-on trading supervisor.

This module is intentionally deterministic in V1. It observes scanner and execution
telemetry continuously, detects operational anomalies, and can pause one admin's
trading as a circuit breaker. It never changes trading rules or entry parameters.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from app.config.settings import settings


@dataclass
class Event:
    ts: float
    kind: str
    owner_id: int | None
    data: dict[str, Any]


class GuardianAgent:
    def __init__(self) -> None:
        self._events: deque[Event] = deque(maxlen=10000)
        self._last_tick = 0.0
        self._lock = asyncio.Lock()
        self._paused_owners: dict[int, str] = {}
        self._last_alert: dict[int, float] = {}

    def _window(self, seconds: float) -> list[Event]:
        cutoff = time.time() - seconds
        return [e for e in self._events if e.ts >= cutoff]

    async def record(self, kind: str, owner_id: int | None = None, **data: Any) -> None:
        if not settings.guardian_enabled:
            return
        async with self._lock:
            self._events.append(Event(time.time(), kind, owner_id, data))

    async def tick(self) -> None:
        if not settings.guardian_enabled:
            return
        now = time.time()
        if now - self._last_tick < settings.guardian_tick_seconds:
            return
        self._last_tick = now
        async with self._lock:
            owners = {e.owner_id for e in self._events if e.owner_id is not None}
        for owner_id in owners:
            await self._evaluate_owner(owner_id)

    async def _evaluate_owner(self, owner_id: int) -> None:
        from app.storage import repository as repo
        events = [e for e in self._window(settings.guardian_window_seconds) if e.owner_id in (None, owner_id)]
        attempts = sum(e.kind == 'buy_attempt' and e.owner_id == owner_id for e in events)
        failures = sum(e.kind == 'buy_failed' and e.owner_id == owner_id for e in events)
        candidates = sum(e.kind == 'candidate' for e in events)
        qualified = sum(e.kind == 'qualified' and e.owner_id == owner_id for e in events)
        reasons = Counter()
        for e in events:
            if e.kind == 'rejected' and e.owner_id == owner_id:
                reason = str(e.data.get('reason') or 'unknown')
                reasons[reason] += 1
        state = await repo.get_or_create_bot_state(owner_id)
        status = 'HEALTHY'
        pause_reason = None
        if attempts >= settings.guardian_min_buy_attempts:
            rate = failures / max(attempts, 1) * 100.0
            if rate >= settings.guardian_pause_failure_rate_pct:
                status = 'EMERGENCY'
                pause_reason = f'Buy failure rate {rate:.0f}% ({failures}/{attempts})'
                if state.guardian_auto_pause_enabled and state.trading_enabled:
                    await repo.update_bot_state(
                        owner_id,
                        trading_enabled=False,
                        guardian_last_status='PAUSED',
                        guardian_pause_reason=pause_reason,
                    )
                    await repo.write_audit_log(str(owner_id), 'guardian_auto_pause', {'reason': pause_reason})
                    self._paused_owners[owner_id] = pause_reason
                    return
        if candidates >= settings.guardian_min_candidates_for_filter_warning and qualified == 0:
            status = 'WARNING'
        if owner_id in self._paused_owners:
            status = 'PAUSED'
            pause_reason = self._paused_owners[owner_id]
        if state.guardian_last_status != status or state.guardian_pause_reason != pause_reason:
            await repo.update_bot_state(owner_id, guardian_last_status=status, guardian_pause_reason=pause_reason)

    async def snapshot(self, owner_id: int) -> dict[str, Any]:
        from app.storage import repository as repo
        state = await repo.get_or_create_bot_state(owner_id)
        window = self._window(settings.guardian_window_seconds)
        mine = [e for e in window if e.owner_id in (None, owner_id)]
        own = [e for e in mine if e.owner_id == owner_id]
        c = Counter(e.kind for e in mine)
        own_c = Counter(e.kind for e in own)
        reasons = Counter(str(e.data.get('reason') or 'unknown') for e in own if e.kind == 'rejected')
        smart = [e for e in mine if e.kind == 'smart_money_buy']
        diagnosis = 'System operating normally.'
        if c['candidate'] >= settings.guardian_min_candidates_for_filter_warning and own_c['qualified'] == 0:
            top = reasons.most_common(1)
            diagnosis = 'Qualification bottleneck detected.'
            if top:
                diagnosis += f' Primary rejection: {top[0][0]} ({top[0][1]}).'
        if own_c['buy_attempt'] >= settings.guardian_min_buy_attempts:
            rate = own_c['buy_failed'] / max(own_c['buy_attempt'], 1) * 100.0
            if rate >= settings.guardian_pause_failure_rate_pct:
                diagnosis = f'Execution anomaly: buy failure rate {rate:.0f}%.'
        if state.guardian_pause_reason:
            diagnosis = state.guardian_pause_reason
        return {
            'enabled': state.guardian_enabled,
            'auto_pause': state.guardian_auto_pause_enabled,
            'status': state.guardian_last_status or ('PAUSED' if not state.trading_enabled and state.guardian_pause_reason else 'HEALTHY'),
            'pause_reason': state.guardian_pause_reason,
            'window_seconds': settings.guardian_window_seconds,
            'candidates': c['candidate'],
            'qualified': own_c['qualified'],
            'buy_attempts': own_c['buy_attempt'],
            'buy_success': own_c['buy_success'],
            'buy_failed': own_c['buy_failed'],
            'smart_money_buys': len(smart),
            'top_rejections': reasons.most_common(3),
            'diagnosis': diagnosis,
            'trading_enabled': state.trading_enabled,
        }

    async def pause(self, owner_id: int, reason: str = 'Paused by admin through GO Guardian') -> None:
        from app.storage import repository as repo
        self._paused_owners[owner_id] = reason
        await repo.update_bot_state(owner_id, trading_enabled=False, guardian_last_status='PAUSED', guardian_pause_reason=reason)
        await repo.write_audit_log(str(owner_id), 'guardian_manual_pause', {'reason': reason})

    async def resume(self, owner_id: int) -> None:
        from app.storage import repository as repo
        self._paused_owners.pop(owner_id, None)
        await repo.update_bot_state(owner_id, trading_enabled=True, guardian_last_status='HEALTHY', guardian_pause_reason=None)
        await repo.write_audit_log(str(owner_id), 'guardian_resume', {})


guardian = GuardianAgent()
