"""Solscan Pro API v2 connector used to enrich tokens with on-chain data."""
import logging
from typing import Optional

import httpx

from app.execution.retry import with_backoff
from app.security.redact import redact_text

logger = logging.getLogger("app.connectors.solscan")


class SolscanAPIError(Exception):
    pass


class SolscanClient:
    def __init__(self, base_url: str, api_key: Optional[str]):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=15.0)

    async def aclose(self):
        await self._client.aclose()

    def _headers(self) -> dict:
        if not self._api_key:
            raise SolscanAPIError("Solscan API key not configured")
        return {"accept": "application/json", "token": self._api_key}

    @with_backoff()
    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        try:
            resp = await self._client.get(path, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise SolscanAPIError(redact_text(f"network error calling {path}: {exc}"))
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SolscanAPIError(redact_text(f"Solscan API error on {path}: {exc.response.status_code}"))
        return resp.json()

    async def get_token_meta(self, mint: str) -> dict:
        return await self._get("/token/meta", {"address": mint})

    async def get_token_holders(self, mint: str, page: int = 1, page_size: int = 10) -> dict:
        return await self._get(
            "/token/holders", {"address": mint, "page": page, "page_size": page_size}
        )

    async def get_latest_tokens(self, platform_id: Optional[str] = None, page: int = 1, page_size: int = 10) -> dict:
        params = {"page": page, "page_size": page_size}
        if platform_id:
            params["platform_id"] = platform_id
        return await self._get("/token/latest", params)

    async def get_account_defi_activities(self, account: str, page_size: int = 10) -> dict:
        return await self._get(
            "/account/defi/activities", {"address": account, "page_size": page_size}
        )
