"""Helius connector used to enrich tokens with real holder counts.

Replaces the Solscan Pro API connector, which required a paid, actively
subscribed plan. Helius's free tier covers this: `getTokenAccounts` is a
Helius RPC extension (not standard Solana RPC) that returns every token
account for a mint, paginated via a cursor. We dedupe by owner to get a
holder count - Helius has no single ready-made "holder count" field, unlike
Solscan's /token/holders endpoint.
"""
import logging
from typing import Optional

import httpx

from app.execution.retry import with_backoff
from app.security.redact import redact_text

logger = logging.getLogger("app.connectors.helius")


class HeliusAPIError(Exception):
    pass


class HeliusClient:
    def __init__(self, base_url: str, api_key: Optional[str]):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=10.0)

    async def aclose(self):
        await self._client.aclose()

    @with_backoff()
    async def get_token_holder_count(self, mint: str, max_pages: int = 5) -> int:
        """Returns the number of distinct owners holding this mint.

        max_pages caps this at 5 * 1000 = 5000 accounts checked, which is
        far more than a freshly-launched token will ever have - this is a
        safety limit against a pathological loop, not an expected ceiling.
        """
        if not self._api_key:
            raise HeliusAPIError("Helius API key not configured")

        owners: set[str] = set()
        cursor: Optional[str] = None

        for _ in range(max_pages):
            params: dict = {"mint": mint, "limit": 1000}
            if cursor:
                params["cursor"] = cursor

            try:
                resp = await self._client.post(
                    "/",
                    params={"api-key": self._api_key},
                    json={
                        "jsonrpc": "2.0",
                        "id": "anon-tradebot-holders",
                        "method": "getTokenAccounts",
                        "params": params,
                    },
                )
            except httpx.HTTPError as exc:
                raise HeliusAPIError(redact_text(f"Helius unavailable: {exc}")) from exc

            if resp.status_code in (401, 403):
                raise HeliusAPIError(redact_text(f"Helius auth error: {resp.status_code} - check API key"))
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise HeliusAPIError(redact_text(f"Helius API error: {exc.response.status_code}")) from exc

            body = resp.json()
            if "error" in body:
                raise HeliusAPIError(redact_text(f"Helius RPC error: {body['error']}"))

            result = body.get("result") or {}
            accounts = result.get("token_accounts") or []
            for acct in accounts:
                owner = acct.get("owner")
                if owner:
                    owners.add(owner)

            cursor = result.get("cursor")
            if not accounts or not cursor:
                break

        return len(owners)
