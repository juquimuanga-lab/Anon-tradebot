"""StateView-backed Doppler snapshot patch.

The Robinhood DopplerLens quoter can revert while a freshly-created dynamic
pool is still initializing its hook state. StateView is the canonical
read-only interface for Uniswap v4 pool state, so use it for the launch-time
snapshot instead of making the sniper depend on a simulated swap quote.
"""
from __future__ import annotations

import asyncio
import logging

from web3 import Web3

from app.config.settings import settings
from app.execution.onchain.robinhood_wallet import resolve_robinhood_rpc_url

logger = logging.getLogger("app.connectors.doppler_stateview_patch")

STATE_VIEW = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
ZERO = "0x0000000000000000000000000000000000000000"

STATE_VIEW_ABI = [
    {
        "inputs": [{"name": "poolId", "type": "bytes32"}],
        "name": "getSlot0",
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "protocolFee", "type": "uint24"},
            {"name": "lpFee", "type": "uint24"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "poolId", "type": "bytes32"}],
        "name": "getLiquidity",
        "outputs": [{"name": "liquidity", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def _w3() -> Web3:
    rpc = resolve_robinhood_rpc_url(settings)
    return Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))


def _pool_id(key: tuple) -> bytes:
    """Return Uniswap v4 PoolId = keccak256(abi.encode(PoolKey))."""
    from eth_abi import encode

    encoded = encode(
        ["address", "address", "uint24", "int24", "address"],
        [
            Web3.to_checksum_address(key[0]),
            Web3.to_checksum_address(key[1]),
            int(key[2]),
            int(key[3]),
            Web3.to_checksum_address(key[4]),
        ],
    )
    return Web3.keccak(encoded)


def _price_from_sqrt(sqrt_price_x96: int, quote_decimals: int, asset_decimals: int) -> float:
    if not sqrt_price_x96:
        return 0.0
    # V4 sqrt price is token1/token0 in raw units. Convert to quote per whole
    # asset token because currency0 is the numeraire and currency1 is the asset.
    raw_ratio = (float(sqrt_price_x96) ** 2) / float(2 ** 192)
    return raw_ratio * (10 ** asset_decimals) / (10 ** quote_decimals)


def _virtual_reserves(sqrt_price_x96: int, liquidity: int) -> tuple[float, float]:
    if not sqrt_price_x96 or not liquidity:
        return 0.0, 0.0
    q96 = float(2 ** 96)
    sqrt_p = float(sqrt_price_x96) / q96
    # Active virtual reserves represented by the current Uniswap v4 liquidity.
    amount0 = float(liquidity) / sqrt_p
    amount1 = float(liquidity) * sqrt_p
    return amount0, amount1


def install() -> None:
    from app.connectors.doppler import DopplerClient

    if getattr(DopplerClient, "_stateview_snapshot_patch_installed", False):
        return

    original = DopplerClient.market_snapshot

    async def market_snapshot(self, token: str, metadata: dict) -> dict:
        logger.info(
            "doppler_stateview_snapshot_started",
            extra={
                "mint": token,
                "tx_hash": metadata.get("tx_hash"),
                "block_number": metadata.get("block_number"),
                "reason": "use_stateview_instead_of_lens_quote",
            },
        )

        # Import the existing connector constants/ABI so discovery and state
        # decoding remain centralized in doppler.py.
        from app.connectors import doppler as base

        w3 = await asyncio.to_thread(_w3)
        token_addr = Web3.to_checksum_address(token)
        initializer = w3.eth.contract(
            address=Web3.to_checksum_address(base.DOPPLER_INITIALIZER),
            abi=base.INITIALIZER_ABI,
        )
        state = await asyncio.to_thread(lambda: initializer.functions.getState(token_addr).call())
        numeraire, tokens_on_curve, hook, _calldata, status, key, far_tick = state
        key = tuple(key)

        if str(key[0]).lower() != str(numeraire).lower():
            raise RuntimeError("Doppler pool key is not in canonical numeraire/asset order")
        if str(key[1]).lower() != token_addr.lower():
            raise RuntimeError("Doppler pool key asset does not match launch token")

        token_contract = w3.eth.contract(address=token_addr, abi=base.ERC20_ABI)
        quote_contract = None if str(numeraire).lower() == ZERO else w3.eth.contract(
            address=Web3.to_checksum_address(numeraire), abi=base.ERC20_ABI
        )
        name = await asyncio.to_thread(lambda: token_contract.functions.name().call())
        symbol = await asyncio.to_thread(lambda: token_contract.functions.symbol().call())
        decimals = int(await asyncio.to_thread(lambda: token_contract.functions.decimals().call()))
        supply = int(await asyncio.to_thread(lambda: token_contract.functions.totalSupply().call()))

        quote_symbol = "ETH"
        quote_decimals = 18
        if quote_contract is not None:
            quote_symbol = await asyncio.to_thread(lambda: quote_contract.functions.symbol().call())
            quote_decimals = int(await asyncio.to_thread(lambda: quote_contract.functions.decimals().call()))

        pool_id = _pool_id(key)
        state_view = w3.eth.contract(
            address=Web3.to_checksum_address(STATE_VIEW), abi=STATE_VIEW_ABI
        )
        slot0, liquidity = await asyncio.gather(
            asyncio.to_thread(lambda: state_view.functions.getSlot0(pool_id).call()),
            asyncio.to_thread(lambda: state_view.functions.getLiquidity(pool_id).call()),
        )
        sqrt_price = int(slot0[0])
        tick = int(slot0[1])
        protocol_fee = int(slot0[2])
        lp_fee = int(slot0[3])
        liquidity = int(liquidity)

        if sqrt_price <= 0 or liquidity <= 0:
            raise RuntimeError(
                f"Doppler pool state not initialized: sqrt_price={sqrt_price} liquidity={liquidity}"
            )

        amount0_raw, amount1_raw = _virtual_reserves(sqrt_price, liquidity)
        price_quote = _price_from_sqrt(sqrt_price, quote_decimals, decimals)

        try:
            if str(numeraire).lower() == ZERO or str(numeraire).lower() == base.WETH.lower():
                quote_usd = await asyncio.to_thread(base._alchemy_symbol_price, "ETH")
            else:
                quote_usd = await asyncio.to_thread(base._alchemy_symbol_price, quote_symbol)
        except Exception as exc:
            logger.warning(
                "doppler_stateview_quote_usd_failed",
                extra={"mint": token, "quote_symbol": quote_symbol, "error": str(exc)},
            )
            quote_usd = 0.0

        price_usd = price_quote * quote_usd
        supply_whole = supply / (10 ** decimals)
        market_cap_usd = supply_whole * price_usd
        quote_reserve = amount0_raw / (10 ** quote_decimals)
        asset_reserve = amount1_raw / (10 ** decimals)
        liquidity_usd = quote_reserve * quote_usd + asset_reserve * price_usd

        pool_key = {
            "currency0": Web3.to_checksum_address(key[0]),
            "currency1": Web3.to_checksum_address(key[1]),
            "fee": int(key[2]),
            "tickSpacing": int(key[3]),
            "hooks": Web3.to_checksum_address(key[4]),
        }
        snapshot = {
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
            "pool_id": pool_id.hex(),
            "status": int(status),
            "far_tick": int(far_tick),
            "tokens_on_curve": int(tokens_on_curve),
            "sqrt_price_x96": sqrt_price,
            "tick": tick,
            "protocol_fee": protocol_fee,
            "lp_fee": lp_fee,
            "pool_liquidity": liquidity,
            "quote_reserve": quote_reserve,
            "asset_reserve": asset_reserve,
            "tx_hash": metadata.get("tx_hash"),
            "launcher": metadata.get("launcher"),
        }
        logger.info(
            "doppler_stateview_snapshot_ready",
            extra={
                "mint": token,
                "symbol": symbol,
                "pool_id": pool_id.hex(),
                "sqrt_price_x96": sqrt_price,
                "tick": tick,
                "pool_liquidity": liquidity,
                "price_quote": price_quote,
                "quote_usd": quote_usd,
                "price_usd": price_usd,
                "market_cap_usd": market_cap_usd,
                "liquidity_usd": liquidity_usd,
            },
        )
        return snapshot

    DopplerClient.market_snapshot = market_snapshot
    DopplerClient._stateview_snapshot_patch_installed = True
    logger.info("doppler_stateview_snapshot_patch_installed", extra={"state_view": STATE_VIEW})


install()
