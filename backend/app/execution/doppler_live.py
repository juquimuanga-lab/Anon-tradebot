"""Live execution for Doppler Uniswap-v4 launches on Robinhood Chain.

Phase 1 deliberately executes only pools whose quote currency is native ETH or
WETH. Stock-quoted Doppler launches are detected and fully enriched, but are
held back from live execution until a quote-to-ETH route is available. This is
safer than silently spending the wrong asset or assuming every Doppler pool is
ETH-quoted.
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
from app.execution.onchain.robinhood_wallet import ROBINHOOD_CHAIN_ID if False else None

logger = logging.getLogger("app.execution.doppler")

CHAIN_ID = 4663
UNIVERSAL_ROUTER = "0x8876789976dEcBfCbBbe364623C63652db8C0904"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
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

ERC20_ABI = [
    {"inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
]


def _is_eth_like(address: str) -> bool:
    return address.lower() in {NATIVE.lower(), WETH.lower()}


class DopplerExecutionAdapter(ExecutionAdapter):
    mode = "live"

    def __init__(self, account: LocalAccount, rpc_url: str, buy_slippage_bps: int = 1000):
        self._account = account
        self._rpc_url = rpc_url
        self._w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
        self._buy_slippage_bps = max(0, min(int(buy_slippage_bps), 3000))
        self._router = self._w3.eth.contract(address=Web3.to_checksum_address(UNIVERSAL_ROUTER), abi=ROUTER_ABI)

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
        # Robinhood's deployed Universal Router extends the canonical v4
        # ExactInputSingleParams with minHopPriceX36 immediately before hookData.
        # Actions: SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL.
        actions = bytes([0x06, 0x0c, 0x0f])
        swap_param = encode(
            ["(address,address,uint24,int24,address)", "bool", "uint128", "uint128", "uint256", "bytes"],
            [
                (
                    pool_key["currency0"], pool_key["currency1"], int(pool_key["fee"]),
                    int(pool_key["tickSpacing"]), pool_key["hooks"],
                ),
                bool(zero_for_one), int(amount_in), int(min_out), 0, b"",
            ],
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
            if not _is_eth_like(quote):
                raise RuntimeError(
                    f"Doppler launch is stock/token quoted ({quote}); ETH->quote multi-hop routing is not enabled yet"
                )

            amount_in = max(1, int(float(amount_eth) * 10**18))
            # Re-quote immediately before signing using the Robinhood V4 quoter.
            from app.connectors.doppler import doppler_client, DOPPLER_LENS_QUOTER, LENS_ABI
            w3 = self._w3
            lens = w3.eth.contract(address=Web3.to_checksum_address(DOPPLER_LENS_QUOTER), abi=LENS_ABI)
            quote_result = await asyncio.to_thread(lambda: lens.functions.quoteDopplerLensData({
                "poolKey": pool_key,
                "zeroForOne": str(pool_key["currency0"]).lower() == quote.lower(),
                "exactAmount": amount_in,
                "hookData": b"",
            }).call())
            # The lens returns state, not an exact amount-out. Use the current
            # pool price as a conservative quote and let amountOutMinimum be
            # protected by the configured slippage cap.
            sqrt_price = int(quote_result[0])
            ratio = (sqrt_price * sqrt_price) / float(2 ** 192)
            if ratio <= 0:
                raise RuntimeError("Doppler pool returned zero price")
            zero_for_one = str(pool_key["currency0"]).lower() == quote.lower()
            if zero_for_one:
                # currency0 -> currency1
                expected = int(amount_in * ratio)
            else:
                expected = int(amount_in / ratio)
            if expected <= 0:
                raise RuntimeError("Doppler quote returned zero tokens")
            min_out = expected * (BPS - self._buy_slippage_bps) // BPS
            commands, inputs = self._encode_v4_swap(pool_key, zero_for_one, amount_in, min_out)
            tx_hash = await asyncio.to_thread(self._send, commands, inputs, amount_in if quote.lower() == NATIVE.lower() else 0)
            logger.info("doppler_buy_confirmed", extra={"mint": token.mint, "tx_signature": tx_hash, "amount_eth": float(amount_in) / 1e18, "min_tokens": min_out, "quote": quote})
            return OrderResult(True, "filled", price_usd=float(token.price_usd or 0.0), tx_signature=tx_hash)
        except Exception as exc:
            logger.warning("doppler_buy_failed", extra={"mint": token.mint, "error": str(exc)})
            return OrderResult(False, "failed", error_message=str(exc))

    async def sell(self, token: TokenSnapshot, amount_tokens: float, sell_pct: float) -> OrderResult:
        return OrderResult(
            False,
            "failed",
            error_message="Doppler sell routing is intentionally disabled until quote-to-ETH multi-hop execution is enabled.",
        )
