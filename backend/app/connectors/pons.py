"""Pons v2 / Robinhood Chain on-chain connector.

Uses only Robinhood Chain JSON-RPC/Alchemy and the Pons factory/curve
contracts. No Pons API is required for launch discovery.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

from web3 import Web3

from app.config.settings import settings


PONS_CHAIN_ID = 4663
PONS_FACTORY_ADDRESS = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
PONS_NATIVE_QUOTE = "0x0000000000000000000000000000000000000000"

FACTORY_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "token", "type": "address"},
            {"indexed": True, "name": "curve", "type": "address"},
            {"indexed": True, "name": "deployer", "type": "address"},
            {"indexed": False, "name": "pairToken", "type": "address"},
            {"indexed": False, "name": "launchConfigId", "type": "uint256"},
            {"indexed": False, "name": "graduationThreshold", "type": "uint256"},
        ],
        "name": "TokenLaunched",
        "type": "event",
    },
]

LAUNCHED_TOKEN_ABI = [
    {
        "inputs": [{"name": "token", "type": "address"}],
        "name": "getLaunchedToken",
        "outputs": [
            {"name": "token", "type": "address"},
            {"name": "curve", "type": "address"},
            {"name": "deployer", "type": "address"},
            {"name": "creatorFeeRecipient", "type": "address"},
            {"name": "pairToken", "type": "address"},
            {"name": "graduationThreshold", "type": "uint256"},
            {"name": "poolFee", "type": "uint24"},
            {"name": "tickSpacing", "type": "int24"},
            {"name": "creatorTaxBps", "type": "uint16"},
            {"name": "buybackEnabled", "type": "bool"},
            {"name": "phase", "type": "uint8"},
            {"name": "sweptQuote", "type": "uint256"},
            {"name": "sweptTokens", "type": "uint256"},
            {"name": "sweptAt", "type": "uint256"},
            {"name": "exists", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

CURVE_ABI = [
    {"inputs": [], "name": "getReserves", "outputs": [{"type": "uint256"}, {"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "sellableTokens", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "feeBps", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "creatorTaxBps", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "recipient", "type": "address"}], "name": "currentSnipeTaxBps", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "graduated", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "readyToGraduate", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"anonymous": False, "inputs": [
        {"indexed": True, "name": "buyer", "type": "address"},
        {"indexed": True, "name": "recipient", "type": "address"},
        {"indexed": False, "name": "quoteIn", "type": "uint256"},
        {"indexed": False, "name": "tokensOut", "type": "uint256"},
        {"indexed": False, "name": "fee", "type": "uint256"},
        {"indexed": False, "name": "tax", "type": "uint256"},
    ], "name": "CurveBuy", "type": "event"},
]

ERC20_ABI = [
    {"inputs": [], "name": "name", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalSupply", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
]


def _rpc_url() -> Optional[str]:
    return getattr(settings, "robinhood_rpc_url", None) or getattr(settings, "robinhood_alchemy_rpc_url", None)


def _build_web3() -> Web3:
    url = _rpc_url()
    if not url:
        raise RuntimeError("Robinhood Chain RPC is not configured")
    return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 5}))


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

    async def _call(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def poll_new_launches(self, from_block: int = 0, max_blocks: int = 250) -> list[dict[str, Any]]:
        w3 = await asyncio.to_thread(_build_web3)
        latest = await self._call(lambda: w3.eth.block_number)
        start = self._watermark_block or from_block or max(0, latest - 2)
        start = min(start, latest)
        if latest - start > max_blocks:
            start = latest - max_blocks
        if start > latest:
            return []

        factory_address = getattr(settings, "pons_factory_address", None) or PONS_FACTORY_ADDRESS
        factory = w3.eth.contract(address=Web3.to_checksum_address(factory_address), abi=FACTORY_ABI)
        event = factory.events.TokenLaunched()
        entries = await self._call(lambda: event.get_logs(from_block=start, to_block=latest))
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

        token_per_eth = (reserve_token / 10**decimals) / (reserve_quote / 10**18) if reserve_quote else 0.0
        price_eth = (reserve_quote / 10**18) / (reserve_token / 10**decimals) if reserve_token else 0.0
        price_usd = price_eth * eth_usd
        market_cap_usd = (total_supply / 10**decimals) * price_usd
        liquidity_usd = 2.0 * (reserve_quote / 10**18) * eth_usd
        # Holder detection must never silently turn an RPC/logging failure into
        # ``holders=0``.  That was causing valid Pons candidates to pass the
        # Graduation Hunter and then be rejected by the normal rule because the
        # snapshot happened to be incomplete.
        #
        # We derive current holders from ERC-20 Transfer events rather than
        # merely counting CurveBuy recipients.  A recipient can later transfer
        # all tokens away, while a holder can receive tokens without buying from
        # the curve.  Replaying transfers from launch -> latest gives us the
        # current non-zero holder set without requiring one RPC balanceOf call
        # per wallet.
        holders = 0
        holders_ready = False
        volume_quote = 0
        launch_block = int(metadata.get("launch_block") or 0)

        async def _event_logs(event, start_block: int, end_block: int, chunk_size: int = 2000):
            logs = []
            cursor = start_block
            while cursor <= end_block:
                chunk_end = min(cursor + chunk_size - 1, end_block)
                last_exc = None
                for attempt, delay in enumerate((0.0, 0.35, 0.8), start=1):
                    if delay:
                        await asyncio.sleep(delay)
                    try:
                        part = await self._call(
                            lambda s=cursor, e=chunk_end: event.get_logs(
                                from_block=s, to_block=e
                            )
                        )
                        logs.extend(part)
                        last_exc = None
                        break
                    except Exception as exc:
                        last_exc = exc
                if last_exc is not None:
                    raise last_exc
                cursor = chunk_end + 1
            return logs

        if launch_block:
            latest_block = await self._call(lambda: w3.eth.block_number)

            try:
                transfer_event = token_contract.events.Transfer()
                transfer_logs = await _event_logs(
                    transfer_event, launch_block, latest_block
                )

                balances: dict[str, int] = {}
                zero_address = "0x0000000000000000000000000000000000000000"
                for log in transfer_logs:
                    args = log["args"]
                    sender = str(args["from"]).lower()
                    recipient = str(args["to"]).lower()
                    value = int(args["value"])
                    if sender != zero_address:
                        balances[sender] = balances.get(sender, 0) - value
                    if recipient != zero_address:
                        balances[recipient] = balances.get(recipient, 0) + value

                holders = sum(1 for balance in balances.values() if balance > 0)
                holders_ready = True

                buy_event = curve.events.CurveBuy()
                buy_logs = await _event_logs(
                    buy_event, launch_block, latest_block
                )
                volume_quote = sum(
                    int(log["args"].get("quoteIn", 0)) for log in buy_logs
                )

                logger.info(
                    "pons_holder_snapshot_ready",
                    extra={
                        "mint": token_addr,
                        "launch_block": launch_block,
                        "latest_block": latest_block,
                        "transfer_events": len(transfer_logs),
                        "curve_buy_events": len(buy_logs),
                        "holders": holders,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "pons_holder_snapshot_not_ready",
                    extra={
                        "mint": token_addr,
                        "launch_block": launch_block,
                        "error": str(exc),
                    },
                )
        else:
            logger.warning(
                "pons_holder_snapshot_not_ready",
                extra={
                    "mint": token_addr,
                    "error": "launch_block missing from discovery metadata",
                },
            )

        progress = 0.0
        threshold = int(launch[5])
        if threshold:
            progress = min(100.0, max(0.0, ((reserve_quote / 10**18) / (threshold / 10**18)) * 100.0))

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
