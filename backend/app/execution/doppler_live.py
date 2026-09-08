"""Live execution for SPCX-quoted Doppler Uniswap-v4 launches on Robinhood Chain."""
from __future__ import annotations

import asyncio
import logging
import time

from eth_abi import encode
from eth_account.signers.local import LocalAccount
from web3 import Web3

from app.connectors import doppler_control
from app.execution.base import ExecutionAdapter, OrderResult
from app.scoring.rules import TokenSnapshot

logger = logging.getLogger("app.execution.doppler")
CHAIN_ID = 4663
UNIVERSAL_ROUTER = "0x8876789976dEcBfCbBbe364623C63652db8C0904"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
SPCX = doppler_control.SPCX_TOKEN
BPS = 10_000

ROUTER_ABI = [{"inputs":[{"name":"commands","type":"bytes"},{"name":"inputs","type":"bytes[]"},{"name":"deadline","type":"uint256"}],"name":"execute","outputs":[],"stateMutability":"payable","type":"function"}]
QUOTER_ABI = [{"inputs":[{"components":[{"components":[{"name":"currency0","type":"address"},{"name":"currency1","type":"address"},{"name":"fee","type":"uint24"},{"name":"tickSpacing","type":"int24"},{"name":"hooks","type":"address"}],"name":"poolKey","type":"tuple"},{"name":"zeroForOne","type":"bool"},{"name":"exactAmount","type":"uint128"},{"name":"hookData","type":"bytes"}],"name":"params","type":"tuple"}],"name":"quoteExactInputSingle","outputs":[{"name":"amountOut","type":"uint256"},{"name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}]
ERC20_ABI = [
    {"inputs":[],"name":"decimals","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
]
PERMIT2_ABI = [{"inputs":[{"name":"owner","type":"address"},{"name":"token","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"amount","type":"uint160"},{"name":"expiration","type":"uint48"},{"name":"nonce","type":"uint48"}],"stateMutability":"view","type":"function"}]


def _is_spcx(address: str) -> bool:
    return str(address).lower() == SPCX.lower()


class DopplerExecutionAdapter(ExecutionAdapter):
    mode = "live"

    def __init__(self, account: LocalAccount, rpc_url: str, buy_slippage_bps: int = 1000):
        self._account = account
        self._rpc_url = rpc_url
        self._w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
        self._buy_slippage_bps = max(0, min(int(buy_slippage_bps), 3000))
        self._router = self._w3.eth.contract(address=Web3.to_checksum_address(UNIVERSAL_ROUTER), abi=ROUTER_ABI)
        self._quoter = self._w3.eth.contract(address=Web3.to_checksum_address(V4_QUOTER), abi=QUOTER_ABI)
        self._spcx = self._w3.eth.contract(address=Web3.to_checksum_address(SPCX), abi=ERC20_ABI)
        self._permit2 = self._w3.eth.contract(address=Web3.to_checksum_address(PERMIT2), abi=PERMIT2_ABI)

    def _send(self, fn, value: int = 0) -> str:
        chain_id = int(self._w3.eth.chain_id)
        if chain_id != CHAIN_ID:
            raise RuntimeError(f"Refusing to sign: RPC chain ID {chain_id} is not Robinhood Chain {CHAIN_ID}")
        if int(self._w3.eth.get_balance(self._account.address)) <= 0:
            raise RuntimeError("Robinhood admin wallet needs ETH for gas")
        nonce = self._w3.eth.get_transaction_count(self._account.address, "pending")
        tx = fn.build_transaction({"from": self._account.address, "value": int(value), "nonce": nonce, "chainId": CHAIN_ID, "gasPrice": self._w3.eth.gas_price})
        tx["gas"] = int(self._w3.eth.estimate_gas(tx) * 1.20)
        signed = self._account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
        if receipt.status != 1:
            raise RuntimeError(f"Doppler transaction reverted: {tx_hash.hex()}")
        return tx_hash.hex()

    def _require_spcx_ready(self, amount_in: int) -> None:
        balance = int(self._spcx.functions.balanceOf(self._account.address).call())
        decimals = int(self._spcx.functions.decimals().call())
        if balance < amount_in:
            raise RuntimeError(f"Admin wallet has insufficient SPCX: {balance/(10**decimals):.8f} available, {amount_in/(10**decimals):.8f} required")
        if int(self._spcx.functions.allowance(self._account.address, PERMIT2).call()) < amount_in:
            raise RuntimeError("SPCX is not approved to Permit2; pre-approve canonical SPCX before Doppler sniping.")
        permit_amount, expiration, _nonce = self._permit2.functions.allowance(self._account.address, Web3.to_checksum_address(SPCX), Web3.to_checksum_address(UNIVERSAL_ROUTER)).call()
        if int(permit_amount) < amount_in or int(expiration) <= int(time.time()):
            raise RuntimeError("SPCX Permit2 allowance for UniversalRouter is missing or expired.")

    @staticmethod
    def _encode_v4_swap(pool_key: dict, amount_in: int, min_out: int) -> bytes:
        actions = bytes([0x06, 0x0c, 0x0f])
        pool_key_tuple = [
            pool_key["currency0"],
            pool_key["currency1"],
            int(pool_key["fee"]),
            int(pool_key["tickSpacing"]),
            pool_key["hooks"],
        ]
        swap_param = encode(
            ["(address,address,uint24,int24,address)", "bool", "uint128", "uint128", "uint256", "bytes"],
            [pool_key_tuple, True, int(amount_in), int(min_out), 0, b""],
        )
        settle = encode(["address", "uint256"], [pool_key["currency0"], int(amount_in)])
        take = encode(["address", "uint256"], [pool_key["currency1"], int(min_out)])
        return encode(["bytes", "bytes[]"], [actions, [swap_param, settle, take]])

    @staticmethod
    def _encode_permit2_transfer(amount_in: int) -> bytes:
        return encode(
            ["address", "address", "uint160"],
            [Web3.to_checksum_address(SPCX), Web3.to_checksum_address(UNIVERSAL_ROUTER), int(amount_in)],
        )

    async def buy(self, token: TokenSnapshot, amount_spcx: float) -> OrderResult:
        try:
            market = (getattr(token, "raw_enrichment", {}) or {}).get("pons", {}) or {}
            if not _is_spcx(str(market.get("numeraire", ""))):
                raise RuntimeError("Doppler execution requires the canonical SPCX numeraire")
            pool_key = market.get("pool_key")
            if not pool_key or not _is_spcx(pool_key.get("currency0", "")):
                raise RuntimeError("Doppler SPCX pool key is unavailable or not SPCX/currency0")
            spend_spcx = doppler_control.get_buy_size_spcx()
            if spend_spcx <= 0:
                raise RuntimeError("DOPPLER_BUY_SIZE_SPCX must be configured to a value greater than zero")
            decimals = int(self._spcx.functions.decimals().call())
            amount_in = max(1, int(spend_spcx * (10 ** decimals)))
            await asyncio.to_thread(self._require_spcx_ready, amount_in)
            quoted = await asyncio.to_thread(lambda: self._quoter.functions.quoteExactInputSingle({"poolKey": pool_key, "zeroForOne": True, "exactAmount": amount_in, "hookData": b""}).call())
            expected = int(quoted[0])
            if expected <= 0:
                raise RuntimeError("Doppler SPCX quoter returned zero tokens")
            min_out = expected * (BPS - self._buy_slippage_bps) // BPS
            v4_input = self._encode_v4_swap(pool_key, amount_in, min_out)
            commands = bytes([0x02, 0x10])
            inputs = [self._encode_permit2_transfer(amount_in), v4_input]
            tx_hash = await asyncio.to_thread(self._send, self._router.functions.execute(commands, inputs, int(time.time()) + 20), 0)
            logger.info("doppler_spcx_buy_confirmed", extra={"mint": token.mint, "tx_signature": tx_hash, "amount_spcx": spend_spcx, "quoted_tokens": expected, "min_tokens": min_out})
            return OrderResult(True, "filled", price_usd=float(token.price_usd or 0.0), tx_signature=tx_hash)
        except Exception as exc:
            logger.warning("doppler_spcx_buy_failed", extra={"mint": token.mint, "error": str(exc)})
            return OrderResult(False, "failed", error_message=str(exc))

    async def sell(self, token: TokenSnapshot, amount_tokens: float, sell_pct: float) -> OrderResult:
        return OrderResult(False, "failed", error_message="Doppler SPCX sell routing is not enabled yet; sniper is buy-only.")
