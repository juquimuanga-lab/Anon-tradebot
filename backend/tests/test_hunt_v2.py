from __future__ import annotations

import pytest

from app.arbitrage.hunt import DexScreenerCandidateSource


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, payloads):
        self.payloads = payloads

    async def get(self, path):
        if path.startswith("/latest/dex/search"):
            return FakeResponse(self.payloads["search"])
        if path == "/token-profiles/latest/v1":
            return FakeResponse(self.payloads["profiles"])
        if path == "/token-boosts/top/v1":
            return FakeResponse(self.payloads["boosts"])
        if path.startswith("/token-pairs/v1/solana/"):
            address = path.rsplit("/", 1)[-1]
            return FakeResponse(self.payloads.get(address, []))
        raise AssertionError(path)


@pytest.mark.asyncio
async def test_hunt_broadens_beyond_multi_dex_requirement(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_HUNT_SEARCH_TERMS", "SOL")
    client = FakeClient(
        {
            "profiles": [],
            "boosts": [],
            "search": {
                "pairs": [
                    {
                        "chainId": "solana",
                        "dexId": "raydium",
                        "baseToken": {"address": "Token111111111111111111111111111111111111", "symbol": "TEST", "name": "Test"},
                        "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL", "name": "Wrapped SOL"},
                        "liquidity": {"usd": 150000},
                        "volume": {"h24": 400000},
                    }
                ]
            },
        }
    )
    source = DexScreenerCandidateSource(client)
    candidates = await source.discover_candidates(5)

    assert len(candidates) == 1
    assert candidates[0].symbol == "TEST"
    assert candidates[0].dex_count == 1
    assert candidates[0].tier == "B"
    assert source.last_stats.unique_tokens == 1
    assert source.last_stats.final_candidates == 1


@pytest.mark.asyncio
async def test_hunt_prefers_strong_multi_dex_candidate(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_HUNT_SEARCH_TERMS", "SOL")
    client = FakeClient(
        {
            "profiles": [],
            "boosts": [],
            "search": {
                "pairs": [
                    {
                        "chainId": "solana",
                        "dexId": "raydium",
                        "baseToken": {"address": "TokenA11111111111111111111111111111111111", "symbol": "A", "name": "A"},
                        "quoteToken": {"address": "So11111111111111111111111111111111111111112"},
                        "liquidity": {"usd": 300000},
                        "volume": {"h24": 1200000},
                    },
                    {
                        "chainId": "solana",
                        "dexId": "orca",
                        "baseToken": {"address": "TokenA11111111111111111111111111111111111", "symbol": "A", "name": "A"},
                        "quoteToken": {"address": "So11111111111111111111111111111111111111112"},
                        "liquidity": {"usd": 250000},
                        "volume": {"h24": 800000},
                    },
                    {
                        "chainId": "solana",
                        "dexId": "raydium",
                        "baseToken": {"address": "TokenB11111111111111111111111111111111111", "symbol": "B", "name": "B"},
                        "quoteToken": {"address": "So11111111111111111111111111111111111111112"},
                        "liquidity": {"usd": 100000},
                        "volume": {"h24": 250000},
                    },
                ]
            },
        }
    )
    source = DexScreenerCandidateSource(client)
    candidates = await source.discover_candidates(5)

    assert [candidate.symbol for candidate in candidates] == ["A", "B"]
    assert candidates[0].tier == "A"
    assert candidates[0].dex_count == 2
