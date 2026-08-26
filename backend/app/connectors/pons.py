"""Pons v2 / Robinhood Chain on-chain connector.

Uses Robinhood Chain JSON-RPC/Alchemy and the Pons factory/curve contracts.
The holder snapshot is incremental and retryable so a temporary log-query
failure does not permanently block an otherwise valid Pons candidate.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from web3 import Web3

from app.config.settings import settings

logger = logging.getLogger("app.connectors.pons")

PONS_CHAIN_ID = 4663
PONS_FACTORY_ADDRESS = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
PONS_NATIVE_QUOTE = "0x0000000000000000000000000000000000000000"
PONS_LOG_CHUNK = 50
PONS_LOG_RETRY_DELAYS = (0.0, 0.35, 0.9)
PONS_SNAPSHOT_RETRY_SECONDS = 2.0

FACTORY_ABI = [{"anonymous": False, "inputs": [{"indexed": True, "name": "token", "type": "address"}, {"indexed": True, "name": "curve", "type": "address"}, {"indexed": True, "name": "deployer", "type": "address"}, {"indexed": False, "name": "pairToken", "type": "address"}, {"indexed": False, "name": "launchConfigId", "type": "uint256"}, {"indexed": False, "name": "graduationThreshold", "type": "uint256"}], "name": "TokenLaunched", "type": "event"}]
LAUNCHED_TOKEN_ABI = [{"inputs": [{"name": "token", "type": "address"}], "name": "getLaunchedToken", "outputs": [{"name": "token", "type": "address"}, {"name": "curve", "type": "address"}, {"name": "deployer", "type": "address"}, {"name": "creatorFeeRecipient", "type": "address"}, {"name": "pairToken", "type": "address"}, {"name": "graduationThreshold", "type": "uint256"}, {"name": "poolFee", "type": "uint24"}, {"name": "tickSpacing", "type": "int24"}, {"name": "creatorTaxBps", "type": "uint16"}, {"name": "buybackEnabled", "type": "bool"}, {"name": "phase", "type": "uint8"}, {"name": "sweptQuote", "type": "uint256"}, {"name": "sweptTokens", "type": "uint256"}, {"name": "sweptAt", "type": "uint256"}, {"name": "exists", "type": "bool"}], "stateMutability": "view", "type": "function"}]
CURVE_ABI = [
    {"inputs": [], "name": "getReserves", "outputs": [{"type": "uint256"}, {"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "sellableTokens", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "feeBps", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "creatorTaxBps", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "recipient", "type": "address"}], "name": "currentSnipeTaxBps", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "graduated", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "readyToGraduate", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"anonymous": False, "inputs": [{"indexed": True, "name": "buyer", "type": "address"}, {"indexed": True, "name": "recipient", "type": "address"}, {"indexed": False, "name": "quoteIn", "type": "uint256"}, {"indexed": False, "name": "tokensOut", "type": "uint256"}, {"indexed": False, "name": "fee", "type": "uint256"}, {"indexed": False, "name": "tax", "type": "uint256"}], "name": "CurveBuy", "type": "event"},
]
ERC20_ABI = [
    {"inputs": [], "name": "name", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalSupply", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"anonymous": False, "inputs": [{"indexed": True, "name": "from", "type": "address"}, {"indexed": True, "name": "to", "type": "address"}, {"indexed": False, "name": "value", "type": "uint256"}], "name": "Transfer", "type": "event"},
]

def _rpc_url() -> Optional[str]:
    return getattr(settings, "robinhood_rpc_url", None) or getattr(settings, "robinhood_alchemy_rpc_url", None)

def _build_web3() -> Web3:
    url = _rpc_url()
    if not url:
        raise RuntimeError("Robinhood Chain RPC is not configured")
    return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))

def _eth_usd_sync() -> float:
    key = getattr(settings, "robinhood_alchemy_api_key", None) or getattr(settings, "alchemy_api_key", None)
    if not key:
        return 0.0
    import httpx
    url = f"https://api.g.alchemy.com/prices/v1/{key}/tokens/by-symbol?symbols=ETH"
    with httpx.Client(timeout=5) as client:
        response = client.get(url)
        response.raise_for_status()
        data = response.json().get("data", [])
    prices = data[0].get("prices", []) if data else []
    for price in prices:
        if str(price.get("currency", "")).upper() == "USD":
            return float(price["value"])
    return 0.0

async def get_eth_usd_price() -> float:
    try:
        return await asyncio.to_thread(_eth_usd_sync)
    except Exception:
        return 0.0

class PonsClient:
    def __init__(self) -> None:
        self._watermark_block = 0
        self._initialized = False
        self._holder_state: dict[str, dict[str, Any]] = {}

    async def _call(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def _get_logs_chunked(self, event, start_block: int, end_block: int, initial_chunk: int = PONS_LOG_CHUNK, max_chunk: int = PONS_LOG_CHUNK):
        """Read logs with bounded retries and adaptive chunking."""
        logs = []
        cursor = int(start_block)
        chunk_size = max(1, min(int(initial_chunk), int(max_chunk)))
        while cursor <= int(end_block):
            chunk_end = min(cursor + chunk_size - 1, int(end_block))
            last_exc = None
            for delay in PONS_LOG_RETRY_DELAYS:
                if delay:
                    await asyncio.sleep(delay)
                try:
                    part = await self._call(lambda s=cursor, e=chunk_end: event.get_logs(from_block=s, to_block=e))
                    logs.extend(part)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
            if last_exc is not None:
                if chunk_size > 1:
                    chunk_size = max(1, chunk_size // 2)
                    continue
                raise last_exc
            cursor = chunk_end + 1
        return logs

    async def poll_new_launches(self, from_block: int = 0, max_blocks: int = 250) -> list[dict[str, Any]]:
        w3 = await asyncio.to_thread(_build_web3)
        latest = int(await self._call(lambda: w3.eth.block_number))
        start = self._watermark_block or from_block or max(0, latest - 2)
        start = min(int(start), latest)
        if latest - start > max_blocks:
            start = latest - int(max_blocks)
        if start > latest:
            return []
        factory_address = getattr(settings, "pons_factory_address", None) or PONS_FACTORY_ADDRESS
        factory = w3.eth.contract(address=Web3.to_checksum_address(factory_address), abi=FACTORY_ABI)
        entries = await self._get_logs_chunked(factory.events.TokenLaunched(), start, latest, initial_chunk=10, max_chunk=25)
        self._watermark_block = latest + 1
        if not self._initialized:
            self._initialized = True
            return []
        discovered: list[dict[str, Any]] = []
        for item in entries:
            args = item["args"]
            pair_token = Web3.to_checksum_address(args["pairToken"])
            discovered.append({
                "mint": Web3.to_checksum_address(args["token"]),
                "curve": Web3.to_checksum_address(args["curve"]),
                "creator": Web3.to_checksum_address(args["deployer"]),
                "deployer": Web3.to_checksum_address(args["deployer"]),
                "pair_token": pair_token,
                "launch_config_id": int(args["launchConfigId"]),
                "graduation_threshold": int(args["graduationThreshold"]),
                "tx_hash": item["transactionHash"].hex(),
                "block_number": int(item["blockNumber"]),
                "launch_block": int(item["blockNumber"]),
                "log_index": int(item["logIndex"]),
                "created_on": datetime.now(timezone.utc),
                "source": "pons",
            })
        return discovered

    async def _update_holder_snapshot(self, token_contract, curve, token_addr: str, launch_block: int, latest_block: int) -> dict[str, Any]:
        """Incrementally replay Transfer/CurveBuy logs without partial commits."""
        key = token_addr.lower()
        state = self._holder_state.setdefault(key, {
            "launch_block": int(launch_block),
            "last_block": int(launch_block) - 1,
            "balances": {},
            "volume_quote": 0,
            "ready": False,
            "next_retry": 0.0,
        })
        state["launch_block"] = int(launch_block)

        now = asyncio.get_running_loop().time()
        if now < float(state.get("next_retry", 0.0)):
            return state

        start_block = max(int(launch_block), int(state.get("last_block", launch_block - 1)) + 1)
        if start_block > int(latest_block):
            state["ready"] = True
            return state

        transfer_event = token_contract.events.Transfer()
        buy_event = curve.events.CurveBuy()
        try:
            logger.info("pons_snapshot_started", extra={"mint": token_addr, "from_block": start_block, "to_block": latest_block, "incremental": state["ready"]})
            transfer_logs = await self._get_logs_chunked(transfer_event, start_block, latest_block)
            buy_logs = await self._get_logs_chunked(buy_event, start_block, latest_block)

            # Work on copies so a failed second query can never double-count
            # transfers when the same range is retried.
            new_balances: dict[str, int] = dict(state["balances"])
            zero_address = "0x0000000000000000000000000000000000000000"
            for log in transfer_logs:
                args = log["args"]
                sender = str(args["from"]).lower()
                recipient = str(args["to"]).lower()
                value = int(args["value"])
                if sender != zero_address:
                    new_balances[sender] = new_balances.get(sender, 0) - value
                if recipient != zero_address:
                    new_balances[recipient] = new_balances.get(recipient, 0) + value

            new_volume_quote = int(state["volume_quote"]) + sum(int(log["args"].get("quoteIn", 0)) for log in buy_logs)
            state["balances"] = new_balances
            state["volume_quote"] = new_volume_quote
            state["last_block"] = int(latest_block)
            state["ready"] = True
            state["next_retry"] = 0.0
            holders = sum(1 for balance in new_balances.values() if balance > 0)
            logger.info("pons_holder_snapshot_ready", extra={"mint": token_addr, "launch_block": launch_block, "from_block": start_block, "latest_block": latest_block, "transfer_events": len(transfer_logs), "curve_buy_events": len(buy_logs), "holders": holders, "holder_method": "incremental_erc20_transfer_replay"})
            return state
        except Exception as exc:
            state["next_retry"] = now + PONS_SNAPSHOT_RETRY_SECONDS
            logger.warning("pons_holder_snapshot_retry", extra={"mint": token_addr, "launch_block": launch_block, "from_block": start_block, "latest_block": latest_block, "retry_seconds": PONS_SNAPSHOT_RETRY_SECONDS, "error_type": type(exc).__name__, "error": str(exc)})
            return state

    async def market_snapshot(self, token: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        metadata = metadata or {}
        w3 = await asyncio.to_thread(_build_web3)
        token_addr = Web3.to_checksum_address(token)
        token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
        factory_address = getattr(settings, "pons_factory_address", None) or PONS_FACTORY_ADDRESS
        launch_contract = w3.eth.contract(address=Web3.to_checksum_address(factory_address), abi=LAUNCHED_TOKEN_ABI)
        launch = await self._call(lambda: launch_contract.functions.getLaunchedToken(token_addr).call())
        if not launch[-1]:
            raise RuntimeError("token is not registered by the active Pons factory")

        curve_addr = Web3.to_checksum_address(launch[1])
        pair_token = Web3.to_checksum_address(launch[4])
        if pair_token.lower() != PONS_NATIVE_QUOTE.lower():
            raise RuntimeError("Pons custom-pair launches are not enabled in this first Robinhood integration")
        curve = w3.eth.contract(address=curve_addr, abi=CURVE_ABI)

        reserve_quote, reserve_token = await self._call(lambda: curve.functions.getReserves().call())
        sellable = await self._call(lambda: curve.functions.sellableTokens().call())
        fee_bps = await self._call(lambda: curve.functions.feeBps().call())
        creator_tax_bps = await self._call(lambda: curve.functions.creatorTaxBps().call())
        graduated = await self._call(lambda: curve.functions.graduated().call())
        ready_to_graduate = await self._call(lambda: curve.functions.readyToGraduate().call())
        name = await self._call(lambda: token_contract.functions.name().call())
        symbol = await self._call(lambda: token_contract.functions.symbol().call())
        decimals = int(await self._call(lambda: token_contract.functions.decimals().call()))
        total_supply = int(await self._call(lambda: token_contract.functions.totalSupply().call()))

        eth_usd = await get_eth_usd_price()
        price_eth = (reserve_quote / 10**18) / (reserve_token / 10**decimals) if reserve_token else 0.0
        price_usd = price_eth * eth_usd
        market_cap_usd = (total_supply / 10**decimals) * price_usd
        liquidity_usd = 2.0 * (reserve_quote / 10**18) * eth_usd

        launch_block = int(metadata.get("launch_block") or 0)
        latest_block = int(await self._call(lambda: w3.eth.block_number))
        holders = 0
        holders_ready = False
        volume_quote = 0
        if launch_block:
            state = await self._update_holder_snapshot(token_contract, curve, token_addr, launch_block, latest_block)
            balances = state.get("balances", {})
            holders = sum(1 for balance in balances.values() if balance > 0)
            holders_ready = bool(state.get("ready", False))
            volume_quote = int(state.get("volume_quote", 0))
        else:
            logger.warning("pons_snapshot_retry", extra={"mint": token_addr, "reason": "launch_block_missing"})

        if eth_usd <= 0:
            raise RuntimeError("ETH/USD price unavailable")
        if price_usd <= 0 or market_cap_usd <= 0:
            raise RuntimeError(f"invalid Pons valuation price_usd={price_usd} market_cap_usd={market_cap_usd}")

        threshold = int(launch[5])
        progress = min(100.0, max(0.0, (reserve_quote / threshold) * 100.0)) if threshold else 0.0
        return {
            "price_usd": price_usd,
            "price_eth": price_eth,
            "market_cap_usd": market_cap_usd,
            "liquidity_usd": liquidity_usd,
            "holders": holders,
            "holders_ready": holders_ready,
            "volume_24h_usd": (volume_quote / 10**18) * eth_usd,
            "is_migrated": bool(graduated),
            "decimals": decimals,
            "name": name,
            "symbol": symbol,
            "total_supply": total_supply,
            "curve": curve_addr,
            "deployer": Web3.to_checksum_address(launch[2]),
            "pair_token": pair_token,
            "phase": int(launch[10]),
            "graduated": bool(graduated),
            "ready_to_graduate": bool(ready_to_graduate),
            "sellable_tokens": int(sellable),
            "fee_bps": int(fee_bps),
            "creator_tax_bps": int(creator_tax_bps),
            "graduation_threshold": threshold,
            "progress_pct": progress,
            "eth_usd": eth_usd,
        }

pons_client = PonsClient()
