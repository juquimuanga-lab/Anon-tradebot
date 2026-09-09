"""Doppler / Long-style launch connector for Robinhood Chain.

This connector is intentionally independent from Pons. It watches the
canonical Doppler Airlock Create events emitted by Long and other Doppler
front-ends on Robinhood Chain.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from web3 import Web3

from app.config.settings import settings
from app.execution.onchain.robinhood_wallet import resolve_robinhood_rpc_url

logger = logging.getLogger("app.connectors.doppler")

CHAIN_ID = 4663
AIRLOCK = "0xeb7c034704ef8dcd2d32324c1545f62fb4ad0862"
LONG_LAUNCHER = "0x22e99278308b393ea1260859b181ad7e78f5eeed"
DOPPLER_INITIALIZER = "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544"
DOPPLER_LENS_QUOTER = "0xf4c22465532f64777ffcd7770831aeca38f35c04"
STATE_VIEW = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
DYNAMIC_FEE = 0x800000

CREATE_ABI = [{
    "anonymous": False,
    "inputs": [
        {"indexed": False, "name": "asset", "type": "address"},
        {"indexed": True, "name": "numeraire", "type": "address"},
        {"indexed": False, "name": "initializer", "type": "address"},
        {"indexed": False, "name": "poolOrHook", "type": "address"},
    ],
    "name": "Create",
    "type": "event",
}]

INITIALIZER_ABI = [{
    "inputs": [{"name": "asset", "type": "address"}],
    "name": "getState",
    "outputs": [
        {"name": "numeraire", "type": "address"},
        {"name": "totalTokensOnBondingCurve", "type": "uint256"},
        {"name": "dopplerHook", "type": "address"},
        {"name": "graduationDopplerHookCalldata", "type": "bytes"},
        {"name": "status", "type": "uint8"},
        {"name": "poolKey", "type": "tuple", "components": [
            {"name": "currency0", "type": "address"},
            {"name": "currency1", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "tickSpacing", "type": "int24"},
            {"name": "hooks", "type": "address"},
        ]},
        {"name": "farTick", "type": "int24"},
    ],
    "stateMutability": "view",
    "type": "function",
}]

ERC20_ABI = [
    {"inputs": [], "name": "name", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalSupply", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]

LENS_ABI = [{
    "inputs": [{"components": [
        {"components": [
            {"name": "currency0", "type": "address"},
            {"name": "currency1", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "tickSpacing", "type": "int24"},
            {"name": "hooks", "type": "address"},
        ], "name": "poolKey", "type": "tuple"},
        {"name": "zeroForOne", "type": "bool"},
        {"name": "exactAmount", "type": "uint128"},
        {"name": "hookData", "type": "bytes"},
    ], "name": "params", "type": "tuple"}],
    "name": "quoteDopplerLensData",
    "outputs": [{"components": [
        {"name": "sqrtPriceX96", "type": "uint160"},
        {"name": "amount0", "type": "uint256"},
        {"name": "amount1", "type": "uint256"},
        {"name": "tick", "type": "int24"},
    ], "name": "returnData", "type": "tuple"}],
    "stateMutability": "nonpayable",
    "type": "function",
}]

LOG_CHUNK = 100
RETRY_DELAYS = (0.0, 0.25, 0.75)


def _w3() -> Web3:
    rpc = resolve_robinhood_rpc_url(settings)
    return Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))


def _price_from_sqrt(sqrt_price_x96: int) -> float:
    if not sqrt_price_x96:
        return 0.0
    return (2 ** 192) / float(sqrt_price_x96 * sqrt_price_x96)


def _alchemy_symbol_price(symbol: str) -> float:
    key = getattr(settings, "robinhood_alchemy_api_key", None) or getattr(settings, "alchemy_api_key", None)
    if not key or not symbol:
        return 0.0
    import httpx
    url = f"https://api.g.alchemy.com/prices/v1/{key}/tokens/by-symbol?symbols={symbol}"
    with httpx.Client(timeout=5) as client:
        response = client.get(url)
        response.raise_for_status()
        data = response.json().get("data", [])
    for row in data:
        for price in row.get("prices", []):
            if str(price.get("currency", "")).upper() == "USD":
                return float(price["value"])
    return 0.0


class DopplerClient:
    def __init__(self) -> None:
        self._watermark = 0

    async def _logs(self, event, start: int, end: int) -> list[Any]:
        out: list[Any] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + LOG_CHUNK - 1, end)
            last = None
            for delay in RETRY_DELAYS:
                if delay:
                    await asyncio.sleep(delay)
                try:
                    part = await asyncio.to_thread(
                        lambda s=cursor, e=chunk_end: event.get_logs(from_block=s, to_block=e)
                    )
                    out.extend(part)
                    last = None
                    break
                except Exception as exc:
                    last = exc
            if last is not None:
                if chunk_end > cursor:
                    half = max(1, (chunk_end - cursor + 1) // 2)
                    out.extend(await self._logs(event, cursor, cursor + half - 1))
                    cursor += half
                    continue
                raise last
            cursor = chunk_end + 1
        return out

    async def poll_new_launches(self, max_blocks: int = 300) -> list[dict[str, Any]]:
        w3 = await asyncio.to_thread(_w3)
        latest = int(await asyncio.to_thread(lambda: w3.eth.block_number))
        configured = int(getattr(settings, "doppler_factory_start_block", 0) or 0)
        start = self._watermark or configured or max(0, latest - 10)
        if latest - start > max_blocks:
            start = latest - max_blocks
        if start > latest:
            return []

        airlock = w3.eth.contract(
            address=Web3.to_checksum_address(AIRLOCK),
            abi=CREATE_ABI,
        )
        logs = await self._logs(airlock.events.Create(), start, latest)
        self._watermark = latest + 1

        logger.info(
            "doppler_airlock_poll",
            extra={
                "latest_block": latest,
                "from_block": start,
                "to_block": latest,
                "create_logs": len(logs),
                "airlock": AIRLOCK,
                "initializer": DOPPLER_INITIALIZER,
            },
        )

        result: list[dict[str, Any]] = []
        for log in logs:
            args = log["args"]
            initializer = Web3.to_checksum_address(args["initializer"])
            if initializer.lower() != DOPPLER_INITIALIZER.lower():
                continue

            tx_hash = log["transactionHash"].hex()
            tx = None
            tx_to = ""
            creator = ""
            try:
                tx = await asyncio.to_thread(lambda h=tx_hash: w3.eth.get_transaction(h))
                tx_to = Web3.to_checksum_address(tx["to"]) if tx.get("to") else ""
                creator = tx.get("from") or ""
            except Exception as exc:
                logger.warning(
                    "doppler_launch_transaction_lookup_failed",
                    extra={"tx_hash": tx_hash, "error": str(exc)},
                )

            result.append({
                "mint": Web3.to_checksum_address(args["asset"]),
                "creator": creator,
                "numeraire": Web3.to_checksum_address(args["numeraire"]),
                "initializer": initializer,
                "pool_or_hook": Web3.to_checksum_address(args["poolOrHook"]),
                "launcher": tx_to,
                "tx_hash": tx_hash,
                "block_number": int(log["blockNumber"]),
                "launch_block": int(log["blockNumber"]),
                "log_index": int(log["logIndex"]),
                "created_on": datetime.now(timezone.utc),
                "source": "doppler",
                "venue": "doppler_v4",
            })

        logger.info(
            "doppler_airlock_launches_decoded",
            extra={"create_logs": len(logs), "doppler_launches": len(result)},
        )
        return result

    async def market_snapshot(self, token: str, metadata: dict[str, Any]) -> dict[str, Any]:
        w3 = await asyncio.to_thread(_w3)
        token_addr = Web3.to_checksum_address(token)
        initializer = w3.eth.contract(address=Web3.to_checksum_address(DOPPLER_INITIALIZER), abi=INITIALIZER_ABI)
        state = await asyncio.to_thread(lambda: initializer.functions.getState(token_addr).call())
        numeraire, tokens_on_curve, hook, _calldata, status, key, far_tick = state
        key = tuple(key)
        if str(key[0]).lower() != str(numeraire).lower():
            raise RuntimeError("Doppler pool key is not in canonical numeraire/asset order")
        if str(key[1]).lower() != token_addr.lower():
            raise RuntimeError("Doppler pool key asset does not match launch token")

        token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
        quote_contract = w3.eth.contract(address=Web3.to_checksum_address(numeraire), abi=ERC20_ABI) if int(numeraire, 16) != 0 else None
        name = await asyncio.to_thread(lambda: token_contract.functions.name().call())
        symbol = await asyncio.to_thread(lambda: token_contract.functions.symbol().call())
        decimals = int(await asyncio.to_thread(lambda: token_contract.functions.decimals().call()))
        supply = int(await asyncio.to_thread(lambda: token_contract.functions.totalSupply().call()))

        quote_symbol = "ETH"
        quote_decimals = 18
        if quote_contract is not None:
            quote_symbol = await asyncio.to_thread(lambda: quote_contract.functions.symbol().call())
            quote_decimals = int(await asyncio.to_thread(lambda: quote_contract.functions.decimals().call()))

        lens = w3.eth.contract(address=Web3.to_checksum_address(DOPPLER_LENS_QUOTER), abi=LENS_ABI)
        pool_key = {
            "currency0": Web3.to_checksum_address(key[0]),
            "currency1": Web3.to_checksum_address(key[1]),
            "fee": int(key[2]),
            "tickSpacing": int(key[3]),
            "hooks": Web3.to_checksum_address(key[4]),
        }
        lens_result = await asyncio.to_thread(lambda: lens.functions.quoteDopplerLensData({
            "poolKey": pool_key,
            "zeroForOne": True,
            "exactAmount": 1,
            "hookData": b"",
        }).call())
        sqrt_price = int(lens_result[0])
        amount0 = int(lens_result[1])
        amount1 = int(lens_result[2])

        quote_usd = 0.0
        if numeraire.lower() == "0x0000000000000000000000000000000000000000" or numeraire.lower() == WETH.lower():
            try:
                from app.connectors.pons import get_eth_usd_price
                quote_usd = await get_eth_usd_price()
            except Exception:
                quote_usd = 0.0
        else:
            quote_usd = await asyncio.to_thread(_alchemy_symbol_price, quote_symbol)

        price_quote = _price_from_sqrt(sqrt_price)
        price_usd = price_quote * quote_usd
        supply_whole = supply / (10 ** decimals)
        market_cap_usd = supply_whole * price_usd
        quote_reserve = amount0 / (10 ** quote_decimals)
        asset_reserve = amount1 / (10 ** decimals)
        liquidity_usd = quote_reserve * quote_usd + asset_reserve * price_usd

        return {
            "venue": "doppler_v4",
            "price_usd": price_usd,
            "price_quote": price_quote,
            "market_cap_usd": market_cap_usd,
            "liquidity_usd": liquidity_usd,
            "holders": 0,
            "holders_ready": False,
            "volume_24h_usd": 0.0,
            "is_migrated": int(status) != 0,
            "decimals": decimals,
            "name": name,
            "symbol": symbol,
            "total_supply": supply,
            "numeraire": Web3.to_checksum_address(numeraire),
            "quote_symbol": quote_symbol,
            "quote_decimals": quote_decimals,
            "quote_usd": quote_usd,
            "hook": Web3.to_checksum_address(hook),
            "pool_key": pool_key,
            "pool_id": None,
            "status": int(status),
            "far_tick": int(far_tick),
            "tokens_on_curve": int(tokens_on_curve),
            "sqrt_price_x96": sqrt_price,
            "quote_reserve": quote_reserve,
            "asset_reserve": asset_reserve,
            "tx_hash": metadata.get("tx_hash"),
            "launcher": metadata.get("launcher"),
        }


doppler_client = DopplerClient()
