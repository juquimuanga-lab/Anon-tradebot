"""Anoncoin API connector.

Public docs (docs.anoncoin.it) currently mark coin-discovery, coin-details,
my-profile and create-coin as "Coming Soon", and there is no documented
trade/execution endpoint at all. Every call here is defensive: on 404/501/
"coming soon" style failures we raise AnoncoinUnavailable so callers can fall
back to another data source instead of crashing.
"""
import logging
from typing import Awaitable, Callable, Optional

import httpx

from app.execution.retry import with_backoff
from app.security.redact import redact_text

logger = logging.getLogger("app.connectors.anoncoin")


class AnoncoinAPIError(Exception):
    pass


class AnoncoinUnavailable(AnoncoinAPIError):
    """Raised when an endpoint is not live yet or unreachable."""


class AnoncoinClient:
    def __init__(self, base_url: str, api_key_provider: Callable[[], Awaitable[Optional[str]]]):
        self._base_url = base_url.rstrip("/")
        self._api_key_provider = api_key_provider
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=15.0)

    async def _headers(self) -> dict:
        key = await self._api_key_provider()
        if not key:
            raise AnoncoinAPIError("Anoncoin API key not configured. Use /connect first.")
        return {"x-api-key": key}

    async def aclose(self):
        await self._client.aclose()

    @with_backoff()
    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        try:
            headers = await self._headers()
            resp = await self._client.get(path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise AnoncoinUnavailable(redact_text(f"network error calling {path}: {exc}"))
        if resp.status_code in (404, 501, 502, 503):
            raise AnoncoinUnavailable(f"{path} is not available yet (status {resp.status_code})")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AnoncoinAPIError(redact_text(f"Anoncoin API error on {path}: {exc.response.status_code}"))
        return resp.json()

    async def get_coins(self, sort_by: str = "new", limit: int = 20) -> list:
        data = await self._get("/services/v2/coins", {"sortBy": sort_by, "limit": limit})
        return data if isinstance(data, list) else data.get("data", [])

    async def get_coin_details(self, mint: str) -> dict:
        data = await self._get("/services/v2/coin-details", {"ca": mint})
        return data.get("data", data) if isinstance(data, dict) else data

    async def get_my_profile(self) -> dict:
        data = await self._get("/services/v2/my-profile")
        return data.get("data", data) if isinstance(data, dict) else data

    async def get_top_holders(self, next_token: Optional[str] = None) -> dict:
        params = {"nextToken": next_token} if next_token else None
        data = await self._get("/services/v2/top-holders", params)
        return data.get("data", data) if isinstance(data, dict) else data
