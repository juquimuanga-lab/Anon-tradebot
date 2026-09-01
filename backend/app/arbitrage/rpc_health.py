"""RPC health/failover helper for the isolated arbitrage observer.

Helius is the primary Solana RPC when configured. Alchemy is an optional
fallback. This module is observational only: it never signs, sends, or
changes transactions and it does not modify the existing sniper RPC path.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config.settings import settings


@dataclass(frozen=True)
class RpcHealth:
    healthy: bool
    provider: str
    slot: int | None = None
    error: str | None = None


class ArbitrageRpcHealth:
    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self.timeout_seconds = timeout_seconds

    def endpoints(self) -> tuple[tuple[str, str], ...]:
        endpoints: list[tuple[str, str]] = []
        primary = settings.solana_rpc_url
        if primary:
            endpoints.append(("helius" if settings.helius_api_key and primary.startswith(settings.helius_base_url) else "primary", primary))
        fallback = settings.alchemy_solana_rpc_url
        if fallback and fallback != primary:
            endpoints.append(("alchemy", fallback))
        return tuple(endpoints)

    async def check(self) -> RpcHealth:
        last_error = "no Solana RPC configured"
        for provider, url in self.endpoints():
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    health = await client.post(
                        url,
                        json={"jsonrpc": "2.0", "id": 1, "method": "getHealth", "params": []},
                    )
                    if health.status_code != 200:
                        last_error = f"{provider}: HTTP {health.status_code}"
                        continue
                    health_body = health.json()
                    if health_body.get("error"):
                        last_error = f"{provider}: getHealth error"
                        continue
                    if health_body.get("result") != "ok":
                        last_error = f"{provider}: unhealthy"
                        continue

                    slot_response = await client.post(
                        url,
                        json={"jsonrpc": "2.0", "id": 2, "method": "getSlot", "params": [{"commitment": "processed"}]},
                    )
                    slot = None
                    if slot_response.status_code == 200:
                        slot_body = slot_response.json()
                        if not slot_body.get("error") and slot_body.get("result") is not None:
                            slot = int(slot_body["result"])
                    return RpcHealth(True, provider, slot=slot)
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                last_error = f"{provider}: {type(exc).__name__}"
                continue
        return RpcHealth(False, "none", error=last_error)
