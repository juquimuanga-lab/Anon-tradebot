"""Deterministic simulated new-token feed.

Used only as a fallback while Anoncoin's coin-discovery endpoint is marked
"Coming Soon" in their public docs. Every token produced here is tagged
source="mock_simulated" and every alert about it is prefixed [SIMULATED] so
it is never confused with real market data.
"""
import random
import time
from datetime import datetime, timezone

from app.config.settings import settings
from app.scoring.rules import TokenSnapshot

_CREATOR_POOL = [
    settings.creator_watchlist[0] if settings.creator_watchlist else "7AbRGzM3NBvvUXi7j1Mga2SraTfjpPBMzGpyHcXSzV3v",
    "9zQqf4V6nH1s2s3s4s5s6s7s8s9s0s1s2s3s4s5s6s7s",
    "5xB1t2t3t4t5t6t7t8t9t0t1t2t3t4t5t6t7t8t9t0t1",
]

_counter = 0


def _fake_mint() -> str:
    global _counter
    _counter += 1
    seed = f"{time.time_ns()}-{_counter}"
    rng = random.Random(seed)
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return "SIM" + "".join(rng.choice(alphabet) for _ in range(38))


def generate(max_new: int = 2) -> list[TokenSnapshot]:
    rng = random.Random()
    count = rng.randint(0, max_new)
    tokens = []
    for _ in range(count):
        creator = rng.choice(_CREATOR_POOL)
        liquidity = rng.uniform(500, 50000)
        holders = rng.randint(3, 400)
        market_cap = rng.uniform(5000, 500000)
        price = rng.uniform(0.0000001, 0.001)
        tokens.append(
            TokenSnapshot(
                mint=_fake_mint(),
                ticker_name=f"SimCoin{rng.randint(100, 999)}",
                ticker_symbol=f"SIM{rng.randint(10, 99)}",
                creator_wallet=creator,
                created_on=datetime.now(timezone.utc),
                price_usd=price,
                market_cap_usd=market_cap,
                liquidity_usd=liquidity,
                holders=holders,
                volume_24h_usd=liquidity * rng.uniform(0.1, 1.5),
                is_migrated=rng.random() < 0.1,
                source="mock_simulated",
            )
        )
    return tokens
