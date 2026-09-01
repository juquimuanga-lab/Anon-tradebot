from __future__ import annotations

import httpx
import pytest

from app.arbitrage.jupiter_quotes import JupiterArbitrageError, JupiterArbitrageQuoteProvider


class FakeAsyncClient:
    def __init__(self):
        self.calls = 0

    async def get(self, path, params=None):
        self.calls += 1
        if self.calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=httpx.Request("GET", "https://example.com"), text='{"message":"too many requests"}')
        return httpx.Response(
            200,
            json={
                "outAmount": "1000",
                "priceImpactPct": "0",
                "routePlan": [],
            },
            request=httpx.Request("GET", "https://example.com"),
        )

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_jupiter_retries_429(monkeypatch):
    monkeypatch.setenv("JUPITER_MAX_429_RETRIES", "1")
    monkeypatch.setenv("JUPITER_429_BACKOFF_SECONDS", "0.1")

    provider = JupiterArbitrageQuoteProvider(base_url="https://example.com")
    await provider._client.aclose()
    provider._client = FakeAsyncClient()

    quote = await provider.unrestricted_quote("input", "output", 1)
    assert quote is not None
    assert provider._client.calls == 2
    await provider.aclose()


@pytest.mark.asyncio
async def test_jupiter_raises_after_429_retries(monkeypatch):
    monkeypatch.setenv("JUPITER_MAX_429_RETRIES", "1")
    monkeypatch.setenv("JUPITER_429_BACKOFF_SECONDS", "0.1")

    class Always429(FakeAsyncClient):
        async def get(self, path, params=None):
            self.calls += 1
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                request=httpx.Request("GET", "https://example.com"),
                text='{"message":"too many requests"}',
            )

    provider = JupiterArbitrageQuoteProvider(base_url="https://example.com")
    await provider._client.aclose()
    fake = Always429()
    provider._client = fake

    with pytest.raises(JupiterArbitrageError, match="HTTP 429"):
        await provider.unrestricted_quote("input", "output", 1)
    assert fake.calls == 2
    await provider.aclose()
