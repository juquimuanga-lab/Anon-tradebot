"""Live execution for Doppler Uniswap-v4 launches on Robinhood Chain.

Phase 1 executes only Doppler pools whose quote currency is native ETH.
Stock/WETH-quoted launches are detected and enriched but held back from live
execution until the multi-hop quote routing path is enabled.
"""
from __future__ import annotations

import asyncio
import logging
import time

from eth_abi import encode
from eth_account.signers.local import LocalAccount
from web3 import Web3

from app.execution.base import ExecutionAdapter, OrderResult
from app.scoring.rules import TokenSnapshot

logger = logging.getLogger("app.execution.doppler")

CHAIN_ID = 4663
UNIVERSAL_ROUTER = "0x8876789976dEcBfCbBbe364623C63652db8C0904"
V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
NATIVE = "0x0000000000000000000000000000000000000000"
BPS = 10_000

ROUTER_ABI = [{
    "inputs": [
        {"name": "commands", "type": "bytes"},
        {"name": "inputs", "type": "bytes[]"},
        {"name": "deadline", "type": "uint256"},
    ],
    "name": "execute",
    "outputs": [],
    "stateMutability": "payable",
    "type": "function",
}]

QUOTER_ABI = [{
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
    "name": "quoteExactInputSingle",
    "outputs": [
        {"name": "amountOut", "type": "uint256"},
        {"name": "gasEstimate", "type": "uint256"},
    ],
    "stateMutability": "nonpayable",
    "type": "function",
}]


def _is_native(address: str) -> bool:
    return address.lower() == NATIVE.lower()


class DopplerExecutionAdapter(ExecutionAdapter):
    mode = "live"

    def __init__(self, account: LocalAccount, rpc_url: str, buy_slippage_bps: int = 1000):
        self._account = account
        self._w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
        self._buy_slippage_bps = max(0, min(int(buy_slippage_bps), 3000))
        self._router = self._w3.eth.contract(
            address=Web3.to_checksum_address(UNIVERSAL_ROUTER),
            abi=ROUTER_ABI,
        )
        self._quoter = self._w3.eth.contract(
            address=Web3.to_checksum_address(V4_QUOTER),
            abi=QUOTER_ABI,
        )

    def _send(self, commands: bytes, inputs: list[bytes], value: int) -> str:
        chain_id = int(self._w3.eth.chain_id)
        if chain_id != CHAIN_ID:
            raise RuntimeError(f"Refusing to sign: RPC chain ID {chain_id} is not Robinhood Chain {CHAIN_ID}")
        balance = int(self._w3.eth.get_balance(self._account.address))
        if balance <= int(value):
            raise RuntimeError("Robinhood wallet has insufficient ETH for transaction value and gas")
        nonce = self._w3.eth.get_transaction_count(self._account.address, "pending")
        tx = self._router.functions.execute(
            commands,
            inputs,
            int(time.time()) + 20,
        ).build_transaction({
            "from": self._account.address,
            "value": int(value),
            "nonce": nonce,
            "chainId": CHAIN_ID,
            "gasPrice": self._w3.eth.gas_price,
        })
        tx["gas"] = int(self._w3.eth.estimate_gas(tx) * 1.20)
        signed = self._account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
        if receipt.status != 1:
            raise RuntimeError(f"Doppler transaction reverted: {tx_hash.hex()}")
        return tx_hash.hex()

    @staticmethod
    def _encode_v4_swap(pool_key: dict, zero_for_one: bool, amount_in: int, min_out: int) -> tuple[bytes, list[bytes]]:
        # Robinhood's deployed Universal Router extends the v4
        # ExactInputSingleParams with minHopPriceX36 immediately before hookData.
        actions = bytes([0x06, 0x0c, 0x0f])  # SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL
        swap_param = encode(
            ["(address,address,uint24,int24,address)", "bool", "uint128", "uint128", "uint256", "bytes"],
            [[
                pool_key["currency0"], pool_key["currency1"], int(pool_key["fee"]),
                int(pool_key["tickSpacing"]), pool_key["hooks"],
            ], bool(zero_for_one), int(amount_in), int(min_out), 0, b""],
        )
        currency_in = pool_key["currency0"] if zero_for_one else pool_key["currency1"]
        currency_out = pool_key["currency1"] if zero_for_one else pool_key["currency0"]
        settle = encode(["address", "uint256"], [currency_in, int(amount_in)])
        take = encode(["address", "uint256"], [currency_out, int(min_out)])
        v4_input = encode(["bytes", "bytes[]"], [actions, [swap_param, settle, take]])
        return b"\x10", [v4_input]

    async def buy(self, token: TokenSnapshot, amount_eth: float) -> OrderResult:
        try:
            market = (getattr(token, "raw_enrichment", {}) or {}).get("pons", {}) or {}
            pool_key = market.get("pool_key")
            if not pool_key:
                raise RuntimeError("Doppler pool key is unavailable")
            quote = str(market.get("numeraire", pool_key.get("currency0", "")))
            if not _is_native(quote):
                raise RuntimeError(
                    f"Doppler launch is {quote}-quoted; ETH->quote multi-hop routing is not enabled yet"
                )

            amount_in = max(1, int(float(amount_eth) * 10**18))
            zero_for_one = str(pool_key["currency0"]).lower() == quote.lower()
            quoted = await asyncio.to_thread(lambda: self._quoter.functions.quoteExactInputSingle({
                "poolKey": pool_key,
                "zeroForOne": zero_for_one,
                "exactAmount": amount_in,
                "hookData": b"",
            }).call())
            expected = int(quoted[0])
            if expected <= 0:
                raise RuntimeError("Doppler V4 quoter returned zero tokens")
            min_out = expected * (BPS - self._buy_slippage_bps) // BPS
            commands, inputs = self._encode_v4_swap(pool_key, zero_for_one, amount_in, min_out)
            tx_hash = await asyncio.to_thread(self._send, commands, inputs, amount_in)
            logger.info("doppler_buy_confirmed", extra={"mint": token.mint, "tx_signature": tx_hash, "amount_eth": float(amount_in) / 1e18, "quoted_tokens": expected, "min_tokens": min_out, "quote": quote})
            return OrderResult(True, "filled", price_usd=float(token.price_usd or 0.0), tx_signature=tx_hash)
        except Exception as exc:
            logger.warning("doppler_buy_failed", extra={"mint": token.mint, "error": str(exc)})
            return OrderResult(False, "failed", error_message=str(exc))

    async def sell(self, token: TokenSnapshot, amount_tokens: float, sell_pct: float) -> OrderResult:
        return OrderResult(False, "failed", error_message="Doppler sell routing is disabled until quote-to-ETH multi-hop execution is enabled.")
