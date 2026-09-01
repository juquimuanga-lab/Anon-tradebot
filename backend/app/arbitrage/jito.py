"""Minimal Jito Block Engine client for atomic arbitrage bundles."""
from __future__ import annotations

from typing import Any

import httpx


class JitoError(RuntimeError):
    """Raised when the Jito Block Engine rejects or cannot process a request."""


class JitoClient:
    def __init__(self, base_url: str, timeout_seconds: float = 8.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def _rpc(self, path: str, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{self._base_url}{path}", json=payload)
        if response.status_code != 200:
            raise JitoError(f"Jito HTTP {response.status_code}: {response.text[:500]}")
        body = response.json()
        if body.get("error"):
            raise JitoError(f"Jito {method} error: {body['error']}")
        return body.get("result")

    async def get_tip_accounts(self) -> list[str]:
        return list(await self._rpc("/api/v1/bundles", "getTipAccounts", []))

    async def send_bundle(self, signed_transactions_b64: list[str]) -> str:
        if not signed_transactions_b64:
            raise ValueError("bundle must contain at least one transaction")
        if len(signed_transactions_b64) > 5:
            raise ValueError("Jito bundles support at most 5 transactions")
        result = await self._rpc("/api/v1/bundles", "sendBundle", [signed_transactions_b64, {"encoding": "base64"}])
        if not result:
            raise JitoError("Jito returned an empty bundle id")
        return str(result)

    async def get_bundle_statuses(self, bundle_ids: list[str]) -> list[dict[str, Any]]:
        if not bundle_ids:
            return []
        result = await self._rpc("/api/v1/bundles", "getBundleStatuses", [bundle_ids])
        return list(result.get("value", [])) if isinstance(result, dict) else list(result or [])
