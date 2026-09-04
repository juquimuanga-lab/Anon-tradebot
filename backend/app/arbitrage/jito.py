"""Jito Block Engine client with optional regional failover."""
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx


DEFAULT_JITO_BLOCK_ENGINE_URL = "https://mainnet.block-engine.jito.wtf"
RETRYABLE_HTTP_STATUS_CODES = {429, 502, 503, 504}


class JitoError(RuntimeError):
    """Raised when the Jito Block Engine rejects or cannot process a request."""


class JitoClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 8.0,
        fallback_urls: list[str] | None = None,
    ) -> None:
        """Create a Jito client with primary-first regional failover.

        ``JITO_BLOCK_ENGINE_URLS`` may contain a comma-separated ordered list
        of regional endpoints. The primary ``base_url`` is always tried first;
        fallback regions are only used for transport failures and retryable
        HTTP responses. This keeps the normal path unchanged while making a
        regional outage/rate-limit less likely to strand a live opportunity.
        """
        configured = os.getenv("JITO_BLOCK_ENGINE_URLS", "").strip()
        configured_urls = [item.strip().rstrip("/") for item in configured.split(",") if item.strip()]
        primary = base_url.rstrip("/") if base_url else DEFAULT_JITO_BLOCK_ENGINE_URL
        candidates = [primary]
        candidates.extend(configured_urls)
        candidates.extend(item.rstrip("/") for item in (fallback_urls or []) if item)
        self._base_urls = tuple(dict.fromkeys(candidates))
        self._base_url = self._base_urls[0]
        self._timeout = timeout_seconds

    @property
    def base_urls(self) -> tuple[str, ...]:
        return self._base_urls

    async def _rpc(self, path: str, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        failures: list[str] = []

        for index, base_url in enumerate(self._base_urls):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(f"{base_url}{path}", json=payload)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                failures.append(f"{base_url}: {type(exc).__name__}: {exc}")
                if index + 1 < len(self._base_urls):
                    continue
                raise JitoError("Jito transport failure: " + " | ".join(failures)) from exc

            if response.status_code in RETRYABLE_HTTP_STATUS_CODES and index + 1 < len(self._base_urls):
                failures.append(f"{base_url}: HTTP {response.status_code}")
                continue
            if response.status_code != 200:
                detail = response.text[:500].replace("\n", " ")
                raise JitoError(
                    f"Jito HTTP {response.status_code}"
                    + (f": {detail}" if detail else "")
                )

            body = response.json()
            if body.get("error"):
                # JSON-RPC errors are normally request-specific rather than a
                # regional transport failure, so do not silently duplicate a
                # potentially valid bundle submission.
                raise JitoError(f"Jito {method} error: {body['error']}")
            return body.get("result")

        raise JitoError("Jito request failed: " + " | ".join(failures))

    async def get_tip_accounts(self) -> list[str]:
        return list(await self._rpc("/api/v1/getTipAccounts", "getTipAccounts", []))

    async def get_tip_floor(self, url: str) -> dict[str, float]:
        """Return the latest Jito landed-tip percentile data."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url)
        if response.status_code != 200:
            raise JitoError(f"Jito tip floor HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise JitoError("Jito tip floor returned an unexpected payload")
        values: dict[str, float] = {}
        for key, value in payload[0].items():
            try:
                values[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return values

    async def send_bundle(self, signed_transactions_b64: list[str]) -> str:
        if not signed_transactions_b64:
            raise ValueError("bundle must contain at least one transaction")
        if len(signed_transactions_b64) > 5:
            raise ValueError("Jito bundles support at most 5 transactions")
        result = await self._rpc(
            "/api/v1/bundles",
            "sendBundle",
            [signed_transactions_b64, {"encoding": "base64"}],
        )
        if not result:
            raise JitoError("Jito returned an empty bundle id")
        return str(result)

    async def get_bundle_statuses(self, bundle_ids: list[str]) -> list[dict[str, Any]]:
        if not bundle_ids:
            return []
        result = await self._rpc(
            "/api/v1/getBundleStatuses",
            "getBundleStatuses",
            [bundle_ids],
        )
        return list(result.get("value", [])) if isinstance(result, dict) else list(result or [])

    async def get_inflight_bundle_statuses(self, bundle_ids: list[str]) -> list[dict[str, Any]]:
        """Check the short-lived Jito inflight state for a submitted bundle."""
        if not bundle_ids:
            return []
        result = await self._rpc(
            "/api/v1/getInflightBundleStatuses",
            "getInflightBundleStatuses",
            [bundle_ids],
        )
        return list(result.get("value", [])) if isinstance(result, dict) else list(result or [])

    async def wait_for_bundle(
        self,
        bundle_id: str,
        timeout_seconds: float = 20.0,
        poll_seconds: float = 0.5,
    ) -> dict[str, Any]:
        """Wait until Jito reports the bundle landed/failed/invalid."""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_status: dict[str, Any] = {"status": "Pending"}

        while asyncio.get_running_loop().time() < deadline:
            inflight = await self.get_inflight_bundle_statuses([bundle_id])
            if inflight:
                last_status = dict(inflight[0])
                state = str(last_status.get("status", "")).lower()
                if state == "landed":
                    break
                if state in {"failed", "invalid"}:
                    return last_status

            statuses = await self.get_bundle_statuses([bundle_id])
            if statuses:
                status = dict(statuses[0])
                last_status = status
                confirmation = str(
                    status.get("confirmation_status")
                    or status.get("confirmationStatus")
                    or ""
                ).lower()
                if status.get("err") not in (None, {"Ok": None}):
                    status["status"] = "Failed"
                    return status
                if confirmation in {"processed", "confirmed", "finalized"}:
                    status["status"] = "Landed"
                    return status

            await asyncio.sleep(poll_seconds)

        last_status["status"] = "Timeout"
        return last_status
