"""One-token Robinhood Chain WETH -> ERC20 live-snipe helper.

This lane is deliberately isolated from the existing SPCX Doppler adapter and
Solana sniper. It validates the target token, discovers a WETH v4 pool from
StateView, quotes it, simulates the exact Universal Router call, and only then
signs/broadcasts a small configured buy.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from eth_abi import encode
from web3 import Web3

logger = logging.getLogger("app.connectors.robinhood_weth_snipe")

CHAIN_ID = 4663
RPC_URL = os.getenv("ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
WETH = Web3.to_checksum_address("0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73")
ROUTER = Web3.to_checksum_address("0x8876789976dEcBfCbBbe364623C63652db8C0904")
PERMIT2 = Web3.to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3")
STATEVIEW = Web3.to_checksum_address("0xf3334192d15450cdd385c8b70e03f9a6bd9e673b")
QUOTER = Web3.to_checksum_address("0x8dc178efb8111bb0973dd9d722ebeFF267c98f94")
TARGET = Web3.to_checksum_address("0xE4BEF9d0845a13bD39C57C7ee4463ff5D0cc20B6")

ERC20_ABI = [
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]
STATEVIEW_ABI = [{"inputs": [{"components": [{"name": "currency0", "type": "address"}, {"name": "currency1", "type": "address"}, {"name": "fee", "type": "uint24"}, {"name": "tickSpacing", "type": "int24"}, {"name": "hooks", "type": "address"}], "name": "key", "type": "tuple"}], "name": "getSlot0", "outputs": [{"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"}, {"name": "protocolFee", "type": "uint24"}, {"name": "lpFee", "type": "uint24"}], "stateMutability": "view", "type": "function"}]
QUOTER_ABI = [{"inputs": [{"components": [{"components": [{"name": "currency0", "type": "address"}, {"name": "currency1", "type": "address"}, {"name": "fee", "type": "uint24"}, {"name": "tickSpacing", "type": "int24"}, {"name": "hooks", "type": "address"}], "name": "poolKey", "type": "tuple"}, {"name": "zeroForOne", "type": "bool"}, {"name": "exactAmount", "type": "uint128"}, {"name": "minHopPriceX36", "type": "uint256"}, {"name": "hookData", "type": "bytes"}], "name": "params", "type": "tuple"}], "name": "quoteExactInputSingle", "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "gasEstimate", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"}]
ROUTER_ABI = [{"inputs": [{"name": "commands", "type": "bytes"}, {"name": "inputs", "type": "bytes[]"}, {"name": "deadline", "type": "uint256"}], "name": "execute", "outputs": [], "stateMutability": "payable", "type": "function"}]


def _pool_key() -> dict[str, Any]:
    return {"currency0": WETH, "currency1": TARGET, "fee": 0x800000, "tickSpacing": 200, "hooks": "0x0000000000000000000000000000000000000000"}


def _encode_swap(key: dict[str, Any], amount_in: int, min_out: int) -> bytes:
    actions = bytes([0x06, 0x0c, 0x0f])
    params = encode(["(address,address,uint24,int24,address)", "bool", "uint128", "uint128", "uint256", "bytes"], [[key["currency0"], key["currency1"], key["fee"], key["tickSpacing"], key["hooks"]], True, amount_in, min_out, 0, b""])
    settle = encode(["address", "uint256"], [WETH, amount_in])
    take = encode(["address", "uint256"], [TARGET, min_out])
    return encode(["bytes", "bytes[]"], [actions, [params, settle, take]])


def _permit2_transfer(amount_in: int) -> bytes:
    return encode(["address", "address", "uint160"], [WETH, ROUTER, amount_in])


async def run(amount_weth: float | None = None, wallet: str | None = None) -> dict[str, Any]:
    if os.getenv("ROBINHOOD_WETH_SNIPE_ENABLED", "false").lower() != "true":
        raise RuntimeError("ROBINHOOD_WETH_SNIPE_ENABLED is not true; refusing live WETH snipe")
    if amount_weth is None:
        amount_weth = float(os.getenv("ROBINHOOD_WETH_SNIPE_WETH", "0.01"))
    if amount_weth <= 0:
        raise RuntimeError("ROBINHOOD_WETH_SNIPE_WETH must be greater than zero")

    private_key = os.getenv("ROBINHOOD_WALLET_PRIVATE_KEY") or os.getenv("TRADING_PRIVATE_KEY")
    if not private_key:
        raise RuntimeError("No Robinhood Chain trading private key configured")
    account = Web3().eth.account.from_key(private_key)
    if wallet and account.address.lower() != Web3.to_checksum_address(wallet).lower():
        raise RuntimeError("Configured wallet does not match the requested wallet")

    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 8}))
    if int(w3.eth.chain_id) != CHAIN_ID:
        raise RuntimeError(f"RPC is not Robinhood Chain: {w3.eth.chain_id}")
    token = w3.eth.contract(address=TARGET, abi=ERC20_ABI)
    weth = w3.eth.contract(address=WETH, abi=ERC20_ABI)
    symbol = token.functions.symbol().call()
    decimals = int(token.functions.decimals().call())
    weth_decimals = int(weth.functions.decimals().call())
    amount_in = int(amount_weth * (10 ** weth_decimals))
    balance = int(weth.functions.balanceOf(account.address).call())
    if balance < amount_in:
        raise RuntimeError(f"Insufficient WETH: {balance/(10**weth_decimals):.8f} available")

    key = _pool_key()
    stateview = w3.eth.contract(address=STATEVIEW, abi=STATEVIEW_ABI)
    slot0 = stateview.functions.getSlot0(key).call()
    if int(slot0[0]) <= 0:
        raise RuntimeError("Configured WETH/target v4 pool has no initialized sqrt price")

    quoter = w3.eth.contract(address=QUOTER, abi=QUOTER_ABI)
    quoted = quoter.functions.quoteExactInputSingle({"poolKey": key, "zeroForOne": True, "exactAmount": amount_in, "minHopPriceX36": 0, "hookData": b""}).call()
    expected = int(quoted[0])
    if expected <= 0:
        raise RuntimeError("WETH quote returned zero")
    min_out = expected * 9000 // 10000

    if int(weth.functions.allowance(account.address, PERMIT2).call()) < amount_in:
        raise RuntimeError("WETH is not approved to Permit2. Approve WETH first; no approval is performed by this snipe path.")

    router = w3.eth.contract(address=ROUTER, abi=ROUTER_ABI)
    commands = bytes([0x02, 0x10])
    inputs = [_permit2_transfer(amount_in), _encode_swap(key, amount_in, min_out)]
    deadline = int(time.time()) + 20
    fn = router.functions.execute(commands, inputs, deadline)
    tx = fn.build_transaction({"from": account.address, "nonce": w3.eth.get_transaction_count(account.address, "pending"), "chainId": CHAIN_ID, "value": 0, "type": 2, "maxPriorityFeePerGas": max(1, int(w3.eth.max_priority_fee or 1)), "maxFeePerGas": int(w3.eth.get_block("latest").get("baseFeePerGas") or w3.eth.gas_price) * 2 + max(1, int(w3.eth.max_priority_fee or 1))})
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)

    # Exact pre-sign simulation. If the route reverts, nothing is broadcast.
    try:
        w3.eth.call(tx)
    except Exception as exc:
        logger.warning("robinhood_weth_snipe_simulation_failed", extra={"token": TARGET, "symbol": symbol, "error": str(exc)})
        raise RuntimeError(f"Universal Router simulation failed; transaction was NOT signed: {exc}") from exc

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
    if receipt.status != 1:
        raise RuntimeError(f"WETH snipe reverted: {tx_hash.hex()}")
    logger.info("robinhood_weth_snipe_confirmed", extra={"token": TARGET, "symbol": symbol, "amount_weth": amount_weth, "quoted_tokens": expected, "min_tokens": min_out, "tx_signature": tx_hash.hex()})
    return {"token": TARGET, "symbol": symbol, "amount_weth": amount_weth, "quoted_tokens": expected / (10 ** decimals), "min_tokens": min_out / (10 ** decimals), "tx_signature": tx_hash.hex()}
