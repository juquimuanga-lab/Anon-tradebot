import pytest
import respx
from httpx import Response

from app.arbitrage.jito import JitoClient


@pytest.mark.asyncio
@respx.mock
async def test_jito_falls_back_to_next_region_on_retryable_http_error():
    primary = "https://primary.jito"
    secondary = "https://secondary.jito"
    first = respx.post(f"{primary}/api/v1/bundles").mock(
        return_value=Response(503, text="region unavailable")
    )
    second = respx.post(f"{secondary}/api/v1/bundles").mock(
        return_value=Response(200, json={"result": "bundle-secondary"})
    )

    client = JitoClient(primary, fallback_urls=[secondary])
    assert await client.send_bundle(["tx"]) == "bundle-secondary"
    assert first.called
    assert second.called


@pytest.mark.asyncio
@respx.mock
async def test_jito_does_not_fail_over_on_json_rpc_error():
    primary = "https://primary.jito"
    secondary = "https://secondary.jito"
    first = respx.post(f"{primary}/api/v1/bundles").mock(
        return_value=Response(
            200,
            json={"error": {"code": -32000, "message": "invalid bundle"}},
        )
    )
    second = respx.post(f"{secondary}/api/v1/bundles").mock(
        return_value=Response(200, json={"result": "should-not-be-used"})
    )

    client = JitoClient(primary, fallback_urls=[secondary])
    with pytest.raises(Exception, match="Jito sendBundle error"):
        await client.send_bundle(["tx"])

    assert first.called
    assert not second.called


def test_jito_deduplicates_configured_regions(monkeypatch):
    monkeypatch.setenv(
        "JITO_BLOCK_ENGINE_URLS",
        "https://secondary.jito, https://primary.jito, https://secondary.jito",
    )
    client = JitoClient("https://primary.jito")
    assert client.base_urls == (
        "https://primary.jito",
        "https://secondary.jito",
    )
