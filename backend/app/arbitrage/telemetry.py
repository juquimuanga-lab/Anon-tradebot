"""Low-overhead arbitrage latency and execution outcome telemetry."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

logger = logging.getLogger("app.arbitrage.telemetry")


@dataclass
class ArbitrageTelemetry:
    """Accumulate lightweight process-local counters and latency samples."""

    counters: dict[str, int] = field(default_factory=dict)
    latency_ms: dict[str, list[float]] = field(default_factory=dict)

    def increment(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def observe(self, name: str, milliseconds: float) -> None:
        samples = self.latency_ms.setdefault(name, [])
        samples.append(round(max(milliseconds, 0.0), 3))
        if len(samples) > 200:
            del samples[:-200]

    def snapshot(self) -> dict[str, object]:
        latencies: dict[str, dict[str, float | int]] = {}
        for name, samples in self.latency_ms.items():
            if not samples:
                continue
            ordered = sorted(samples)
            latencies[name] = {
                "count": len(samples),
                "avg_ms": round(sum(samples) / len(samples), 3),
                "p50_ms": ordered[(len(ordered) - 1) // 2],
                "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
                "max_ms": max(samples),
            }
        return {"counters": dict(self.counters), "latency_ms": latencies}

    def log_snapshot(self, event: str = "arbitrage_telemetry") -> None:
        logger.info(event, extra=self.snapshot())


@contextmanager
def timed(telemetry: ArbitrageTelemetry, name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        telemetry.observe(name, (time.perf_counter() - started) * 1000.0)


telemetry = ArbitrageTelemetry()
