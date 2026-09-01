import os

import httpx
import pytest

from app.arbitrage.jupiter_quotes import (
    DEFAULT_JUPITER_API_BASE_URL,
    DEFAULT_JUPITER_LITE_BASE_URL,
    JupiterArbitrageQuoteProvider,
    VenueConfig,
)


@pytest.mark.asyncio
async def test_api_key_selects_authenticated_jupiter_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUPITER_API_KEY", "test-key")
    monkeypatch.delenv("JUPITER_BASE_URL", raising=False)

    provider = JupiterArbitrageQuoteProvider()
    try:
        assert provider.base_url == DEFAULT_JUPITER_API_BASE_URL
        assert provider._client.headers["x-api-key"] == "test-key"
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_without_api_key_uses_lite_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    monkeypatch.delenv("JUPITER_BASE_URL", raising=False)

    provider = JupiterArbitrageQuoteProvider()
    try:
        assert provider.base_url == DEFAULT_JUPITER_LITE_BASE_URL
        assert "x-api-key" not in provider._client.headers
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_quote_includes_auth_and_surfaces_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUPITER_API_KEY", "test-key")

    provider = JupiterArbitrageQuoteProvider()
    captured: dict[str, object] = {}

    async def fake_get(path: str, **kwargs: object) -> httpx.Response:
        captured["path"] = path
        captured["params"] = kwargs["params"]
        request = httpx.Request("GET", "https://api.jup.ag/swap/v1/quote")
        return httpx.Response(429, request=request, text='{"error":"rate limited"}')

    monkeypatch.setattr(provider._client, "get", fake_get)
    try:
        with pytest.raises(Exception, match="HTTP 429.*rate limited"):
            await provider.quote(
                VenueConfig("raydium", "Raydium"),
                "buy",
                10_000_000,
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGgZwyTDt1v",
            )
        assert captured["path"] == "/quote"
        assert captured["params"]["dexes"] == "Raydium"  # type: ignore[index]
        assert provider._client.headers["x-api-key"] == "test-key"
    finally:
        await provider.aclose()
