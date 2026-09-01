import pytest
import respx
from httpx import Response

from app.arbitrage.jito import JitoClient


@pytest.mark.asyncio
@respx.mock
async def test_jito_bundle_endpoints_use_documented_paths():
    base = "https://example.jito"
    tip = respx.post(f"{base}/api/v1/getTipAccounts").mock(
        return_value=Response(
            200,
            json={"result": ["Tip111111111111111111111111111111111111111"]},
        )
    )
    send = respx.post(f"{base}/api/v1/bundles").mock(
        return_value=Response(200, json={"result": "bundle-1"})
    )
    inflight = respx.post(f"{base}/api/v1/getInflightBundleStatuses").mock(
        return_value=Response(200, json={"result": {"value": []}})
    )

    client = JitoClient(base)
    assert await client.get_tip_accounts()
    assert await client.send_bundle(["tx"]) == "bundle-1"
    assert await client.get_inflight_bundle_statuses(["bundle-1"]) == []

    assert tip.called
    assert send.called
    assert inflight.called
    assert tip.calls[0].request.url.path == "/api/v1/getTipAccounts"
    assert send.calls[0].request.url.path == "/api/v1/bundles"
    assert inflight.calls[0].request.url.path == "/api/v1/getInflightBundleStatuses"


@pytest.mark.asyncio
@respx.mock
async def test_wait_for_bundle_returns_failed_status():
    base = "https://example.jito"
    respx.post(f"{base}/api/v1/getInflightBundleStatuses").mock(
        return_value=Response(
            200,
            json={"result": {"value": [{"status": "Failed"}]}},
        )
    )
    client = JitoClient(base)

    result = await client.wait_for_bundle(
        "bundle-1",
        timeout_seconds=1,
        poll_seconds=0.01,
    )

    assert result["status"] == "Failed"
