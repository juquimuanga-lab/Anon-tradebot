"""Pons v2 bonding-curve execution on Robinhood Chain."""
from __future__ import annotations

import logging
import asyncio
from web3 import Web3
from eth_account.signers.local import LocalAccount

from app.execution.base import ExecutionAdapter, OrderResult
from app.scoring.rules import TokenSnapshot
from app.config.settings import settings
from app.execution.onchain.robinhood_wallet import resolve_robinhood_rpc_url

ROBINHOOD_CHAIN_ID = 4663
from app.connectors.pons import CURVE_ABI, PONS_NATIVE_QUOTE, LAUNCHED_TOKEN_ABI, PONS_FACTORY_ADDRESS, ERC20_ABI

logger = logging.getLogger("app.execution.pons")
BPS = 10_000

CURVE_EXEC_ABI = CURVE_ABI + [
    {"inputs": [{"name": "quoteIn", "type": "uint256"}, {"name": "minTokensOut", "type": "uint256"}, {"name": "recipient", "type": "address"}], "name": "buy", "outputs": [{"type": "uint256"}], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "tokensIn", "type": "uint256"}, {"name": "minQuoteOut", "type": "uint256"}, {"name": "recipient", "type": "address"}], "name": "sell", "outputs": [{"type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
]


def _amount_out(in_amount: int, reserve_in: int, reserve_out: int) -> int:
    return (in_amount * reserve_out) // (reserve_in + in_amount)


def _quote_buy(curve, quote_in: int, recipient: str):
    reserve_quote, reserve_token = curve.functions.getReserves().call()
    sellable = int(curve.functions.sellableTokens().call())
    fee_bps = int(curve.functions.feeBps().call())
    creator_tax = int(curve.functions.creatorTaxBps().call())
    snipe_bps = int(curve.functions.currentSnipeTaxBps(recipient).call())
    max_snipe = max(0, BPS - fee_bps - creator_tax - 100)
    snipe_bps = min(snipe_bps, max_snipe)
    net = quote_in * (BPS - fee_bps - creator_tax - snipe_bps) // BPS
    tokens_out = _amount_out(net, int(reserve_quote), int(reserve_token))
    if sellable and tokens_out > sellable:
        tokens_out = sellable
    return tokens_out, fee_bps, creator_tax, snipe_bps


def _quote_sell(curve, tokens_in: int):
    reserve_quote, reserve_token = curve.functions.getReserves().call()
    fee_bps = int(curve.functions.feeBps().call())
    creator_tax = int(curve.functions.creatorTaxBps().call())
    gross = _amount_out(tokens_in, int(reserve_token), int(reserve_quote))
    return gross * (BPS - fee_bps - creator_tax) // BPS


class PonsExecutionAdapter(ExecutionAdapter):
    mode = "live"

    def __init__(self, account: LocalAccount, rpc_url: str, slippage_bps: int = 1000):
        self._account = account
        self._pubkey = account.address
        self._rpc_url = rpc_url
        self._w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
        self._slippage_bps = max(0, min(int(slippage_bps), 3000))
        factory_address = getattr(settings, "pons_factory_address", None) or PONS_FACTORY_ADDRESS
        self._factory = self._w3.eth.contract(address=Web3.to_checksum_address(factory_address), abi=LAUNCHED_TOKEN_ABI)

    def _send(self, fn, value=0):
        chain_id = int(self._w3.eth.chain_id)
        if chain_id != ROBINHOOD_CHAIN_ID:
            raise RuntimeError(f"Refusing to sign: RPC chain ID {chain_id} is not Robinhood Chain {ROBINHOOD_CHAIN_ID}")
        balance = int(self._w3.eth.get_balance(self._account.address))
        required_value = int(value)
        if balance <= required_value:
            raise RuntimeError("Robinhood wallet has insufficient ETH for transaction value and gas")
        nonce = self._w3.eth.get_transaction_count(self._account.address, "pending")
        gas_price = self._w3.eth.gas_price
        tx = fn.build_transaction({
            "from": self._account.address,
            "value": int(value),
            "nonce": nonce,
            "chainId": 4663,
            "gasPrice": gas_price,
        })
        tx["gas"] = int(self._w3.eth.estimate_gas(tx) * 1.20)
        signed = self._account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
        if receipt.status != 1:
            raise RuntimeError(f"Pons transaction reverted: {tx_hash.hex()}")
        return tx_hash.hex()

    def _curve(self, token: str):
        launch = self._factory.functions.getLaunchedToken(Web3.to_checksum_address(token)).call()
        if not launch[-1]:
            raise RuntimeError("token is not a Pons launch from the active factory")
        if Web3.to_checksum_address(launch[4]).lower() != PONS_NATIVE_QUOTE.lower():
            raise RuntimeError("Pons custom-pair execution is not enabled")
        if int(launch[10]) != 0:
            raise RuntimeError("Pons launch is no longer on the bonding curve")
        return Web3.to_checksum_address(launch[1])

    async def buy(self, token: TokenSnapshot, amount_eth: float) -> OrderResult:
        try:
            curve_addr = await asyncio.to_thread(self._curve, token.mint)
            curve = self._w3.eth.contract(address=curve_addr, abi=CURVE_EXEC_ABI)
            quote_in = max(1, int(float(amount_eth) * 10**18))
            expected, fee_bps, creator_tax, snipe_tax = await asyncio.to_thread(_quote_buy, curve, quote_in, self._account.address)
            if expected <= 0:
                raise RuntimeError("Pons quote returned zero tokens")
            min_out = expected * (BPS - self._slippage_bps) // BPS
            tx_hash = await asyncio.to_thread(
                self._send,
                curve.functions.buy(quote_in, min_out, self._account.address),
                quote_in,
            )
            logger.info("pons_buy_confirmed", extra={"mint": token.mint, "tx_signature": tx_hash, "quote_eth": float(quote_in)/1e18, "min_tokens": min_out, "fee_bps": fee_bps, "creator_tax_bps": creator_tax, "snipe_tax_bps": snipe_tax})
            return OrderResult(True, "filled", price_usd=float(token.price_usd or 0.0), tx_signature=tx_hash)
        except Exception as exc:
            logger.warning("pons_buy_failed", extra={"mint": token.mint, "error": str(exc)})
            return OrderResult(False, "failed", error_message=str(exc))

    async def sell(self, token: TokenSnapshot, amount_tokens: float, sell_pct: float) -> OrderResult:
        try:
            curve_addr = await asyncio.to_thread(self._curve, token.mint)
            curve = self._w3.eth.contract(address=curve_addr, abi=CURVE_EXEC_ABI)
            decimals = int(getattr(token, "decimals", 18) or 18)
            token_amount = max(1, int(float(amount_tokens) * (10 ** decimals)))
            expected = await asyncio.to_thread(_quote_sell, curve, token_amount)
            if expected <= 0:
                raise RuntimeError("Pons sell quote returned zero")
            min_out = expected * (BPS - self._slippage_bps) // BPS
            token_contract = self._w3.eth.contract(address=Web3.to_checksum_address(token.mint), abi=ERC20_ABI)
            await asyncio.to_thread(self._send, token_contract.functions.approve(curve_addr, token_amount), 0)
            tx_hash = await asyncio.to_thread(self._send, curve.functions.sell(token_amount, min_out, self._account.address), 0)
            logger.info("pons_sell_confirmed", extra={"mint": token.mint, "tx_signature": tx_hash, "amount_tokens": amount_tokens, "min_quote_out": min_out})
            return OrderResult(True, "filled", price_usd=float(token.price_usd or 0.0), tx_signature=tx_hash)
        except Exception as exc:
            logger.warning("pons_sell_failed", extra={"mint": token.mint, "error": str(exc)})
            return OrderResult(False, "failed", error_message=str(exc))


async def get_robinhood_token_balance(rpc_url: str, wallet_address: str, token_address: str) -> float:
    """Read an ERC-20 balance on Robinhood Chain without touching private keys."""
    def _read() -> float:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
        if int(w3.eth.chain_id) != ROBINHOOD_CHAIN_ID:
            raise RuntimeError("RPC is not connected to Robinhood Chain")
        abi = [
            {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
            {"inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
        ]
        token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=abi)
        decimals = int(token.functions.decimals().call())
        raw = int(token.functions.balanceOf(Web3.to_checksum_address(wallet_address)).call())
        return raw / (10 ** decimals)
    return await asyncio.to_thread(_read)
