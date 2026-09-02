import asyncio

from app.arbitrage.hunt import DexScreenerCandidateSource
from app.arbitrage.hotlist import DEFAULT_HOTLIST_MINTS, configured_hotlist_mints


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self):
        self.calls = []

    async def get(self, path):
        self.calls.append(path)
        if path == "/token-profiles/latest/v1":
            return FakeResponse([
                {"chainId": "solana", "tokenAddress": "TOKEN_A"},
                {"chainId": "ethereum", "tokenAddress": "NOT_SOLANA"},
            ])
        if path == "/token-boosts/top/v1":
            return FakeResponse([{ "chainId": "solana", "tokenAddress": "TOKEN_A" }])
        if path == "/token-pairs/v1/solana/TOKEN_A":
            return FakeResponse([
                {
                    "chainId": "solana",
                    "dexId": "raydium",
                    "baseToken": {"address": "TOKEN_A", "symbol": "TEST", "name": "Test Token"},
                    "quoteToken": {"address": "QUOTE_1", "symbol": "USDC"},
                    "liquidity": {"usd": 400_000},
                    "volume": {"h24": 700_000},
                },
                {
                    "chainId": "solana",
                    "dexId": "orca",
                    "baseToken": {"address": "TOKEN_A", "symbol": "TEST", "name": "Test Token"},
                    "quoteToken": {"address": "QUOTE_2", "symbol": "SOL"},
                    "liquidity": {"usd": 300_000},
                    "volume": {"h24": 600_000},
                },
            ])
        for mint in DEFAULT_HOTLIST_MINTS:
            if path == f"/token-pairs/v1/solana/{mint}":
                return FakeResponse([])
        raise AssertionError(path)

    async def aclose(self):
        pass


def test_candidate_source_filters_to_solana_and_requires_multiple_venues():
    client = FakeClient()
    source = DexScreenerCandidateSource(client)

    candidates = asyncio.run(source.discover_candidates(limit=10))

    # The six configured hotlist mints are always included, while the ordinary
    # candidate retains the original global screening behaviour.
    assert len(candidates) == 7
    candidate = next(item for item in candidates if item.token_mint == "TOKEN_A")
    assert candidate.symbol == "TEST"
    assert candidate.dex_count == 2
    assert candidate.liquidity_usd == 400_000
    assert candidate.volume_24h_usd == 1_300_000
    assert all(item.hotlist for item in candidates if item.token_mint in DEFAULT_HOTLIST_MINTS)


def test_candidate_source_returns_empty_when_filters_are_not_met_for_non_hotlist():
    client = FakeClient()
    original = client.get

    async def low_volume(path):
        response = await original(path)
        if path == "/token-pairs/v1/solana/TOKEN_A":
            payload = response.json()
            for pair in payload:
                pair["volume"]["h24"] = 1_000
            return FakeResponse(payload)
        return response

    client.get = low_volume
    source = DexScreenerCandidateSource(client)

    candidates = asyncio.run(source.discover_candidates(limit=10))

    assert all(candidate.token_mint != "TOKEN_A" for candidate in candidates)
    assert len(candidates) == len(DEFAULT_HOTLIST_MINTS)


def test_default_hotlist_is_stable():
    assert configured_hotlist_mints() == DEFAULT_HOTLIST_MINTS
