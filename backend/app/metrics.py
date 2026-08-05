"""Process-local counters exposed via /api/metrics and /status."""
from dataclasses import dataclass, field


@dataclass
class Metrics:
    tokens_scanned: int = 0
    tokens_qualified: int = 0
    trades_placed: int = 0
    error_count: int = 0
    degraded_count: int = 0

    def as_dict(self) -> dict:
        return {
            "tokens_scanned": self.tokens_scanned,
            "tokens_qualified": self.tokens_qualified,
            "trades_placed": self.trades_placed,
            "error_count": self.error_count,
            "degraded_count": self.degraded_count,
        }


metrics = Metrics()
