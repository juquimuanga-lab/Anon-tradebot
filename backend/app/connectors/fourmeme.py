"""Bitquery Four.meme launch discovery and market-data client for BSC."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import websockets
from web3 import Web3

from app.config.settings import settings

logger = logging.getLogger("app.connectors.fourmeme")

FOURMEME_PROXY = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"
FOURMEME_WS = "wss://streaming.bitquery.io/graphql"
HELPER_ABI=[{"inputs":[{"name":"token","type":"address"}],"name":"getTokenInfo","outputs":[{"type":"uint256"},{"type":"address"},{"type":"address"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"bool"}],"stateMutability":"view","type":"function"}]

TOKEN_CREATE_QUERY = r"""
subscription FourMemeTokenCreate {
  EVM(network: bsc, mempool: true) {
    Events(
      where: {
        Transaction: { To: { is: "0x5c952063c7fc8610ffdb798152d69f0b9550762b" } }
        Log: { Signature: { Name: { is: "TokenCreate" } } }
      }
    ) {
      Arguments {
        Name
        Type
        Value {
          ... on EVM_ABI_Address_Value_Arg { address }
          ... on EVM_ABI_String_Value_Arg { string }
          ... on EVM_ABI_BigInt_Value_Arg { bigInteger }
          ... on EVM_ABI_Integer_Value_Arg { integer }
          ... on EVM_ABI_Bytes_Value_Arg { hex }
          ... on EVM_ABI_Boolean_Value_Arg { bool }
        }
      }
      Transaction { Hash From To Gas GasPrice }
      Block { Time }
    }
  }
}
"""

MARKET_QUERY = r"""
query FourMemeMarket($token: String!) {
  Trading {
    Pairs(
      where: {
        Interval: { Time: { Duration: { eq: 1 } } }
        Price: { IsQuotedInUsd: true }
        Market: { Protocol: { is: "fourmeme_v1" }, Network: { is: "Binance Smart Chain" } }
        Token: { Address: { is: $token } }
      }
      limit: { count: 1 }
      orderBy: { descending: Block_Time }
    ) {
      Token { Name Symbol Address }
      Price { Average { Mean } Ohlc { Close } }
      Volume { Usd }
      Block { Time }
    }
    bnb: Pairs(
      where: {
        Interval: { Time: { Duration: { eq: 1 } } }
        Price: { IsQuotedInUsd: true }
        Market: { Network: { is: "Binance Smart Chain" } }
        Token: { Address: { is: "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" } }
      }
      limit: { count: 1 }
      orderBy: { descending: Block_Time }
    ) { Price { Average { Mean } } }
  }
  EVM(network: bsc, dataset: combined) {
    Holders(
      where: { Currency: { SmartContract: { is: $token } }, Balance: { Amount: { gt: "0" } } }
    ) {
      holders: uniq(of: Holder_Address)
    }
  }
}
"""

class FourMemeClient:
    def __init__(self, token: Optional[str] = None):
        self.token = token or settings.bitquery_api_token or settings.bitquery_api_key
        self._queue: deque[dict] = deque(maxlen=settings.fourmeme_max_event_queue)
        self._seen: set[str] = set()
        self._task: Optional[asyncio.Task] = None
        self._w3 = Web3(Web3.HTTPProvider(settings.bsc_rpc_url, request_kwargs={"timeout": 2}))
        self._helper = self._w3.eth.contract(address=Web3.to_checksum_address(settings.fourmeme_helper3_address), abi=HELPER_ABI)
        self._stopped = False
        self._events_received = 0
        self._tokens_queued = 0
        self._last_event_at = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def start(self) -> None:
        if not self.enabled:
            logger.warning("fourmeme_discovery_not_configured", extra={"bitquery_configured": False})
            return
        if self._task and not self._task.done():
            return
        self._stopped = False
        startup_log = {
            "bitquery_configured": True,
            "mempool": True,
            "queue_limit": settings.fourmeme_max_event_queue,
        }
        logger.info(
            "fourmeme_discovery_starting " + json.dumps(
                startup_log, default=str, separators=(",", ":"), sort_keys=True
            ),
            extra=startup_log,
        )
        self._task = asyncio.create_task(self._run(), name="fourmeme-bitquery")
        self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="fourmeme-heartbeat")

    async def stop(self) -> None:
        self._stopped = True
        for task in (self._task, self._heartbeat_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._heartbeat_task = None

    async def _heartbeat(self) -> None:
        while not self._stopped:
            await asyncio.sleep(60)
            heartbeat_log = {
                "configured": self.enabled,
                "connected": bool(self._task and not self._task.done()),
                "events_received": self._events_received,
                "tokens_queued": self._tokens_queued,
                "queue_depth": len(self._queue),
                "last_event_at": self._last_event_at,
            }
            logger.info(
                "fourmeme_bitquery_heartbeat " + json.dumps(
                    heartbeat_log, default=str, separators=(",", ":"), sort_keys=True
                ),
                extra=heartbeat_log,
            )

    async def _run(self) -> None:
        delay = 1.0
        while not self._stopped:
            try:
                # Bitquery documents the graphql-ws protocol and bearer token
                # authentication for this endpoint.
                url = f"{FOURMEME_WS}?token={self.token}"
                async with websockets.connect(
                    url,
                    subprotocols=["graphql-ws"],
                    max_size=None,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    await ws.send(json.dumps({"type": "connection_init"}))
                    ack = json.loads(await ws.recv())
                    if ack.get("type") != "connection_ack":
                        raise RuntimeError(f"Bitquery connection not acknowledged: {ack}")
                    await ws.send(json.dumps({
                        "type": "start",
                        "id": "fourmeme-token-create",
                        "payload": {"query": TOKEN_CREATE_QUERY, "variables": {}},
                    }))
                    connection_log = {
                        "mempool": True,
                        "endpoint": FOURMEME_WS,
                        "factory": FOURMEME_PROXY,
                    }
                    logger.info(
                        "fourmeme_bitquery_connected " + json.dumps(
                            connection_log, default=str, separators=(",", ":"), sort_keys=True
                        ),
                        extra=connection_log,
                    )
                    delay = 1.0
                    while not self._stopped:
                        raw = await ws.recv()
                        message = json.loads(raw)
                        if message.get("type") in {"error", "connection_error"}:
                            raise RuntimeError(str(message))
                        if message.get("type") != "data":
                            continue
                        events = (message.get("payload", {}).get("data", {})
                                  .get("EVM", {}).get("Events", []))
                        self._events_received += len(events)
                        for event in events:
                            logger.info(
                                "fourmeme_raw_event_received " + json.dumps(
                                    {
                                        "argument_names": [
                                            str(arg.get("Name") or "")
                                            for arg in (event.get("Arguments", []) or [])
                                            if isinstance(arg, dict)
                                        ],
                                        "tx_hash": (event.get("Transaction") or {}).get("Hash"),
                                        "tx_from": (event.get("Transaction") or {}).get("From"),
                                        "tx_to": (event.get("Transaction") or {}).get("To"),
                                    },
                                    default=str,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                )
                            )
                            item = self._normalize_event(event)
                            if item:
                                tx = item.get("tx_hash", "")
                                if tx and tx in self._seen:
                                    continue
                                if tx:
                                    self._seen.add(tx)
                                self._queue.append(item)
                                self._tokens_queued += 1
                                self._last_event_at = time.time()
                                detection_log = {
                                    "token": item.get("mint"),
                                    "creator": item.get("creator"),
                                    "tx_hash": tx,
                                    "mempool": True,
                                    "events_received": self._events_received,
                                    "queue_depth": len(self._queue),
                                }
                                logger.info(
                                    "fourmeme_token_create_detected " + json.dumps(
                                        detection_log, default=str, separators=(",", ":"), sort_keys=True
                                    ),
                                    extra=detection_log,
                                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_log = {
                    "error": str(exc),
                    "retry_seconds": delay,
                    "events_received": self._events_received,
                    "tokens_queued": self._tokens_queued,
                }
                logger.warning(
                    "fourmeme_bitquery_stream_error " + json.dumps(
                        error_log, default=str, separators=(",", ":"), sort_keys=True
                    ),
                    extra=error_log,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)

    @staticmethod
    def _value(value: dict) -> Any:
        if not isinstance(value, dict):
            return None
        for key in ("address", "string", "bigInteger", "integer", "hex", "bool"):
            if value.get(key) is not None:
                return value[key]
        return None

    @staticmethod
    def _is_evm_address(value: Any) -> bool:
        return isinstance(value, str) and bool(
            re.fullmatch(r"0x[a-fA-F0-9]{40}", value)
        )

    def _normalize_event(self, event: dict) -> Optional[dict]:
        """Normalize a Bitquery Four.meme TokenCreate event safely.

        Prefer documented semantic argument names, then conservatively fall
        back to an address candidate that is not the Four.meme proxy, creator,
        sender, or destination. Diagnostic logs expose argument names and
        transaction metadata, never the API token.
        """
        arguments = event.get("Arguments", []) or []
        args: dict[str, Any] = {}
        named_values: list[tuple[str, Any]] = []

        for arg in arguments:
            if not isinstance(arg, dict):
                continue
            name = str(arg.get("Name") or "").strip().lower()
            value = self._value(arg.get("Value") or {})
            named_values.append((name, value))
            if name:
                args[name] = value

        tx = event.get("Transaction") or {}
        tx_from = tx.get("From")
        tx_to = tx.get("To")

        token = None
        semantic_keys = (
            "token", "tokenaddress", "token_address", "tokencontract",
            "token_contract", "smartcontract", "contract", "meme", "base",
        )
        for key in semantic_keys:
            candidate = args.get(key)
            if self._is_evm_address(candidate) and candidate.lower() != FOURMEME_PROXY:
                token = candidate
                break

        creator = (
            args.get("creator")
            or args.get("owner")
            or args.get("deployer")
            or args.get("user")
            or tx_from
            or ""
        )

        if token is None:
            excluded = {
                FOURMEME_PROXY.lower(),
                str(creator).lower() if self._is_evm_address(creator) else "",
                str(tx_from).lower() if self._is_evm_address(tx_from) else "",
                str(tx_to).lower() if self._is_evm_address(tx_to) else "",
            }
            candidates = [
                value for _, value in named_values
                if self._is_evm_address(value) and value.lower() not in excluded
            ]
            if candidates:
                token = candidates[0]

        if token is None:
            logger.warning(
                "fourmeme_event_normalization_failed " + json.dumps(
                    {
                        "reason": "no_token_address_found",
                        "argument_names": [name for name, _ in named_values],
                        "address_candidates": [
                            value for _, value in named_values
                            if self._is_evm_address(value)
                        ],
                        "tx_hash": tx.get("Hash"),
                        "tx_from": tx_from,
                        "tx_to": tx_to,
                        "events_received": self._events_received,
                    },
                    default=str,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return None

        token_source = next(
            (
                name for name, value in named_values
                if value == token and name in semantic_keys
            ),
            "address_fallback",
        )
        logger.info(
            "fourmeme_event_normalized " + json.dumps(
                {
                    "token": token,
                    "token_source": token_source,
                    "creator": creator,
                    "argument_names": [name for name, _ in named_values],
                    "tx_hash": tx.get("Hash"),
                },
                default=str,
                separators=(",", ":"),
                sort_keys=True,
            )
        )

        launch_time = (
            args.get("launchtime")
            or args.get("launch_time")
            or args.get("launch")
        )
        created = datetime.now(timezone.utc)
        if isinstance(launch_time, (int, float, str)):
            try:
                created = datetime.fromtimestamp(float(launch_time), tz=timezone.utc)
            except Exception:
                pass

        return {
            "mint": token,
            "creator": creator or "",
            "ticker_name": args.get("name") or args.get("tokenname") or "",
            "ticker_symbol": (
                args.get("symbol")
                or args.get("shortname")
                or args.get("tokensymbol")
                or ""
            ),
            "total_supply": (
                args.get("totalsupply")
                or args.get("total_supply")
                or 1000000000
            ),
            "created_on": created,
            "tx_signature": tx.get("Hash"),
            "raw": event,
        }

    async def drain(self, limit: int = 100) -> list[dict]:
        result=[]
        while self._queue and len(result)<limit:
            result.append(self._queue.popleft())
        return result

    async def market_snapshot(self, token: str) -> dict:
        if not self.enabled:
            raise RuntimeError("BITQUERY_API_TOKEN is not configured")
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(
                "https://streaming.bitquery.io/graphql",
                headers={"Authorization": f"Bearer {self.token}", "X-API-KEY": self.token, "Content-Type": "application/json"},
                json={"query": MARKET_QUERY, "variables": {"token": token}},
            )
            response.raise_for_status()
            payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(str(payload["errors"]))
        data = payload.get("data", {})
        trading = data.get("Trading", {})
        pairs = trading.get("Pairs", []) or []
        bnb_rows = trading.get("bnb", []) or []
        holders_rows = data.get("EVM", {}).get("Holders", []) or []
        row = pairs[0] if pairs else {}
        price = float(((row.get("Price") or {}).get("Average") or {}).get("Mean") or 0.0)
        volume = float((row.get("Volume") or {}).get("Usd") or 0.0)
        bnb_price = float((((bnb_rows[0] if bnb_rows else {}).get("Price") or {}).get("Average") or {}).get("Mean") or 0.0)
        holders = int((holders_rows[0].get("holders") or 0) if holders_rows else 0)
        funds_bnb = 0.0
        liquidity_added = False
        try:
            info = self._helper.functions.getTokenInfo(Web3.to_checksum_address(token)).call()
            funds_bnb = float(info[9]) / 10**18
            liquidity_added = bool(info[11])
            if not price and int(info[3]) > 0 and bnb_price > 0:
                # lastPrice is quote-per-token in 18-decimal units.
                price = (float(info[3]) / 10**18) * bnb_price
        except Exception as exc:
            logger.debug("fourmeme_helper_snapshot_unavailable", extra={"token": token, "error": str(exc)})
        liquidity_usd = funds_bnb * bnb_price * 2.0 if funds_bnb > 0 and bnb_price > 0 else 0.0
        return {"price_usd": price, "market_cap_usd": price * 1_000_000_000.0, "volume_24h_usd": volume, "holders": holders, "liquidity_usd": liquidity_usd, "funds_bnb": funds_bnb, "bnb_price_usd": bnb_price, "liquidity_added": liquidity_added, "timestamp": time.time()}

# Singleton used by ScannerService.
fourmeme_client = FourMemeClient()
