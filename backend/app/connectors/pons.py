"""Pons v2 / Robinhood Chain on-chain connector.

Uses only Robinhood Chain JSON-RPC/Alchemy and the Pons factory/curve
contracts. No Pons API is required for launch discovery.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from web3 import Web3

from app.config.settings import settings
from app.execution.onchain.robinhood_wallet import resolve_robinhood_rpc_url


logger = logging.getLogger("app.connectors.pons")

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
    {"inputs": [], "name": "realQuoteReserve", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "graduationThreshold", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
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
]


def _rpc_url() -> Optional[str]:
    return resolve_robinhood_rpc_url(settings)


def _build_web3() -> Web3:
    url = _rpc_url()
    if not url:
        raise RuntimeError("Robinhood Chain RPC is not configured")
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
    if not w3.is_connected():
        raise RuntimeError("Robinhood Chain RPC is unreachable")
    chain_id = int(w3.eth.chain_id)
    if chain_id != PONS_CHAIN_ID:
        raise RuntimeError(f"wrong Robinhood RPC chain ID {chain_id}; expected {PONS_CHAIN_ID}")
    return w3


def _eth_usd_sync() -> float:
    """Resolve ETH/USD without making price lookup a hard snapshot dependency."""
    import httpx

    key = getattr(settings, "robinhood_alchemy_api_key", None) or getattr(settings, "alchemy_api_key", None)
    if key:
        try:
            url = f"https://api.g.alchemy.com/prices/v1/{key}/tokens/by-symbol?symbols=ETH"
            with httpx.Client(timeout=5) as client:
                response = client.get(url)
                response.raise_for_status()
                data = response.json().get("data", [])
            prices = data[0].get("prices", []) if data else []
            for price in prices:
                if str(price.get("currency", "")).upper() == "USD":
                    value = float(price["value"])
                    if value > 0:
                        return value
        except Exception as exc:
            logger.debug("pons_eth_usd_alchemy_failed", extra={"error": str(exc)})

    try:
        with httpx.Client(timeout=5) as client:
            response = client.get("https://api.coinbase.com/v2/prices/ETH-USD/spot")
            response.raise_for_status()
            value = float(response.json()["data"]["amount"])
            if value > 0:
                return value
    except Exception as exc:
        logger.debug("pons_eth_usd_public_failed", extra={"error": str(exc)})

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

    async def poll_new_launches(self, from_block: int = 0, max_blocks: int = 10) -> list[dict[str, Any]]:
        """Poll Pons TokenLaunched events using provider-safe <=10 block windows."""
        w3 = await asyncio.to_thread(_build_web3)
        latest = int(await self._call(lambda: w3.eth.block_number))
        window = max(1, min(int(max_blocks or 10), 10))

        start = self._watermark_block or from_block or max(0, latest - 2)
        start = min(int(start), latest)

        factory_address = getattr(settings, "pons_factory_address", None) or PONS_FACTORY_ADDRESS
        factory = w3.eth.contract(address=Web3.to_checksum_address(factory_address), abi=FACTORY_ABI)
        event = factory.events.TokenLaunched()
        entries = []
        cursor = start

        try:
            while cursor <= latest:
                chunk_end = min(cursor + window - 1, latest)
                chunk = await self._call(
                    lambda c=cursor, e=chunk_end: event.get_logs(from_block=c, to_block=e)
                )
                entries.extend(chunk)
                cursor = chunk_end + 1
        except Exception as exc:
            logger.warning(
                "pons_launch_poll_failed",
                extra={"from_block": cursor, "to_block": latest, "error_type": type(exc).__name__, "error": str(exc)},
            )
            return []

        self._watermark_block = latest + 1
        if not self._initialized:
            self._initialized = True
            return []

        discovered: list[dict[str, Any]] = []
        for item in entries:
            args = item["args"]
            discovered.append({
                "mint": Web3.to_checksum_address(args["token"]),
                "curve": Web3.to_checksum_address(args["curve"]),
                "creator": Web3.to_checksum_address(args["deployer"]),
                "deployer": Web3.to_checksum_address(args["deployer"]),
                "pair_token": Web3.to_checksum_address(args["pairToken"]),
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
        """Build a Pons v2 snapshot directly from the factory and curve state."""
        metadata = metadata or {}
        w3 = await asyncio.to_thread(_build_web3)
        token_addr = Web3.to_checksum_address(token)
        token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
        factory_address = getattr(settings, "pons_factory_address", None) or PONS_FACTORY_ADDRESS
        factory = w3.eth.contract(address=Web3.to_checksum_address(factory_address), abi=LAUNCHED_TOKEN_ABI)

        try:
            launch = await self._call(lambda: factory.functions.getLaunchedToken(token_addr).call())
        except Exception as exc:
            raise RuntimeError(f"factory getLaunchedToken failed: {type(exc).__name__}: {exc}") from exc

        if not launch[-1]:
            raise RuntimeError("token is not registered by the active Pons v2 factory")

        curve_addr = Web3.to_checksum_address(launch[1])
        pair_token = Web3.to_checksum_address(launch[4])
        phase = int(launch[10])

        if pair_token.lower() != PONS_NATIVE_QUOTE.lower():
            raise RuntimeError(f"unsupported Pons quote asset {pair_token}; native ETH is required by the current sniper")

        curve = w3.eth.contract(address=curve_addr, abi=CURVE_ABI)
        name = await self._call(lambda: token_contract.functions.name().call())
        symbol = await self._call(lambda: token_contract.functions.symbol().call())
        decimals = int(await self._call(lambda: token_contract.functions.decimals().call()))
        total_supply = int(await self._call(lambda: token_contract.functions.totalSupply().call()))

        if phase != 0:
            return {
                "price_usd": 0.0,
                "price_eth": 0.0,
                "market_cap_usd": 0.0,
                "liquidity_usd": 0.0,
                "holders": 0,
                "volume_24h_usd": 0.0,
                "is_migrated": True,
                "decimals": decimals,
                "name": name,
                "symbol": symbol,
                "total_supply": total_supply,
                "curve": curve_addr,
                "deployer": Web3.to_checksum_address(launch[2]),
                "pair_token": pair_token,
                "phase": phase,
                "graduated": phase == 2,
                "ready_to_graduate": False,
                "sellable_tokens": 0,
                "fee_bps": 0,
                "creator_tax_bps": int(launch[8]),
                "graduation_threshold": int(launch[5]),
                "progress_pct": 100.0 if phase == 2 else 0.0,
                "eth_usd": 0.0,
            }

        reserve_quote, reserve_token = await self._call(lambda: curve.functions.getReserves().call())
        real_quote = await self._call(lambda: curve.functions.realQuoteReserve().call())
        threshold = int(await self._call(lambda: curve.functions.graduationThreshold().call()))
        sellable = await self._call(lambda: curve.functions.sellableTokens().call())
        fee_bps = await self._call(lambda: curve.functions.feeBps().call())
        creator_tax_bps = await self._call(lambda: curve.functions.creatorTaxBps().call())
        ready_to_graduate = await self._call(lambda: curve.functions.readyToGraduate().call())
        graduated = await self._call(lambda: curve.functions.graduated().call())
        eth_usd = await get_eth_usd_price()

        price_eth = (reserve_quote / 10**18) / (reserve_token / 10**decimals) if reserve_token else 0.0
        price_usd = price_eth * eth_usd
        market_cap_usd = (total_supply / 10**decimals) * price_usd
        liquidity_usd = 2.0 * (real_quote / 10**18) * eth_usd

        holders = 0
        volume_quote = 0
        try:
            launch_block = int(metadata.get("launch_block") or 0)
            if launch_block:
                buy_event = curve.events.CurveBuy()
                buy_logs = await self._call(lambda: buy_event.get_logs(from_block=launch_block, to_block="latest"))
                unique_recipients = {str(log["args"]["recipient"]).lower() for log in buy_logs}
                holders = len(unique_recipients)
                volume_quote = sum(int(log["args"].get("quoteIn", 0)) for log in buy_logs)
        except Exception as exc:
            logger.debug("pons_trade_history_unavailable", extra={"token": token, "error": str(exc)})

        progress = min(100.0, max(0.0, (real_quote / threshold) * 100.0)) if threshold else 0.0

        return {
            "price_usd": price_usd,
            "price_eth": price_eth,
            "market_cap_usd": market_cap_usd,
            "liquidity_usd": liquidity_usd,
            "holders": holders,
            "volume_24h_usd": (volume_quote / 10**18) * eth_usd,
            "is_migrated": bool(graduated),
            "decimals": decimals,
            "name": name,
            "symbol": symbol,
            "total_supply": total_supply,
            "curve": curve_addr,
            "deployer": Web3.to_checksum_address(launch[2]),
            "pair_token": pair_token,
            "phase": phase,
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
