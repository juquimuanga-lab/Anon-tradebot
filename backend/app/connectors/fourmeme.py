"""Bitquery Four.meme launch discovery and market-data client for BSC."""
from __future__ import annotations

import asyncio
import json
import logging
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

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def start(self) -> None:
        if not self.enabled or self._task and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run(), name="fourmeme-bitquery")

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

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
                    logger.info("fourmeme_bitquery_connected", extra={"mempool": True})
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
                        for event in events:
                            item = self._normalize_event(event)
                            if item:
                                tx = item.get("tx_hash", "")
                                if tx and tx in self._seen:
                                    continue
                                if tx:
                                    self._seen.add(tx)
                                self._queue.append(item)
                                logger.info("fourmeme_token_create_detected", extra={
                                    "token": item.get("mint"),
                                    "creator": item.get("creator"),
                                    "tx_hash": tx,
                                    "mempool": True,
                                })
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("fourmeme_bitquery_stream_error", extra={"error": str(exc), "retry_seconds": delay})
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

    def _normalize_event(self, event: dict) -> Optional[dict]:
        args = {}
        for arg in event.get("Arguments", []) or []:
            name = str(arg.get("Name") or "").lower()
            args[name] = self._value(arg.get("Value") or {})
        token = (args.get("token") or args.get("tokenaddress") or args.get("base") or args.get("meme"))
        if not isinstance(token, str) or not token.startswith("0x") or token.lower() == FOURMEME_PROXY:
            # If Bitquery changes argument labels, keep an address candidate
            # that is not the known Four.meme proxy.
            for value in args.values():
                if isinstance(value, str) and value.startswith("0x") and value.lower() != FOURMEME_PROXY:
                    token = value
                    break
        if not token:
            return None
        tx = event.get("Transaction") or {}
        creator = args.get("creator") or args.get("owner") or tx.get("From")
        launch_time = args.get("launchtime") or args.get("launch_time")
        created = datetime.now(timezone.utc)
        if isinstance(launch_time, (int, float, str)):
            try:
                created = datetime.fromtimestamp(float(launch_time), tz=timezone.utc)
            except Exception:
                pass
        return {
            "mint": token,
            "creator": creator or "",
            "ticker_name": args.get("name") or "",
            "ticker_symbol": args.get("symbol") or args.get("shortname") or "",
            "total_supply": args.get("totalsupply") or 1000000000,
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
