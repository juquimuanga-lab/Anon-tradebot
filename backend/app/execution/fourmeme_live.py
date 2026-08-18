"""Four.meme V2 BSC execution adapter using Helper3 + TokenManager2."""
from __future__ import annotations

import logging
from web3 import Web3
from eth_account.signers.local import LocalAccount

from app.execution.base import ExecutionAdapter, OrderResult
from app.scoring.rules import TokenSnapshot

logger=logging.getLogger("app.execution.fourmeme")
ZERO="0x0000000000000000000000000000000000000000"

HELPER_ABI=[{"inputs":[{"name":"token","type":"address"}],"name":"getTokenInfo","outputs":[{"type":"uint256"},{"type":"address"},{"type":"address"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[{"name":"token","type":"address"},{"name":"amount","type":"uint256"},{"name":"funds","type":"uint256"}],"name":"tryBuy","outputs":[{"type":"address"},{"type":"address"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"},{"type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"name":"token","type":"address"},{"name":"amount","type":"uint256"}],"name":"trySell","outputs":[{"type":"address"},{"type":"address"},{"type":"uint256"},{"type":"uint256"}],"stateMutability":"view","type":"function"}]
TM_ABI=[{"inputs":[{"name":"token","type":"address"},{"name":"to","type":"address"},{"name":"funds","type":"uint256"},{"name":"minAmount","type":"uint256"}],"name":"buyTokenAMAP","outputs":[],"stateMutability":"payable","type":"function"},{"inputs":[{"name":"token","type":"address"},{"name":"amount","type":"uint256"}],"name":"sellToken","outputs":[],"stateMutability":"nonpayable","type":"function"}]
ERC20_ABI=[{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"type":"bool"}],"stateMutability":"nonpayable","type":"function"}]

class FourMemeExecutionAdapter(ExecutionAdapter):
    mode="live"
    def __init__(self, account: LocalAccount, rpc_url: str, slippage_bps:int, helper_address:str, token_manager_address:str):
        self._account=account
        self._pubkey=account.address
        self._rpc_url=rpc_url
        self._w3=Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout":3}))
        self._helper=self._w3.eth.contract(address=Web3.to_checksum_address(helper_address), abi=HELPER_ABI)
        self._manager_default=Web3.to_checksum_address(token_manager_address)
        self._slippage_bps=max(0,min(int(slippage_bps),5000))

    def _send(self, fn, value=0):
        nonce=self._w3.eth.get_transaction_count(self._account.address,"pending")
        gas_price=self._w3.eth.gas_price
        tx=fn.build_transaction({"from":self._account.address,"value":value,"nonce":nonce,"chainId":56,"gasPrice":gas_price})
        tx["gas"] = int(self._w3.eth.estimate_gas(tx)*1.15)
        signed=self._account.sign_transaction(tx)
        tx_hash=self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt=self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        if receipt.status != 1:
            raise RuntimeError(f"Four.meme transaction reverted: {tx_hash.hex()}")
        return tx_hash.hex()

    async def buy(self, token:TokenSnapshot, amount_sol:float)->OrderResult:
        try:
            address=Web3.to_checksum_address(token.mint)
            info=self._helper.functions.getTokenInfo(address).call()
            version, manager, quote, liquidity_added = int(info[0]), info[1], info[2], bool(info[11])
            if liquidity_added:
                raise RuntimeError("Four.meme token has already migrated; pre-graduation TokenManager2 trading is disabled")
            if version != 2:
                raise RuntimeError(f"unsupported Four.meme token version {version}; only V2 is enabled")
            if quote.lower()!=ZERO.lower():
                raise RuntimeError("Four.meme token uses a non-BNB quote; BNB execution is required for this first integration")
            funds=max(1,int(float(amount_sol)*10**18))
            q=self._helper.functions.tryBuy(address,0,funds).call()
            estimated_amount=int(q[2]); msg_value=int(q[5]); funds_param=int(q[7])
            if estimated_amount<=0 or msg_value<=0:
                raise RuntimeError("Four.meme returned an invalid buy quote")
            min_amount=estimated_amount*(10000-self._slippage_bps)//10000
            manager_contract=self._w3.eth.contract(address=Web3.to_checksum_address(manager),abi=TM_ABI)
            tx_hash=self._send(manager_contract.functions.buyTokenAMAP(address,self._account.address,funds_param,min_amount),value=msg_value)
            fill_price=float(token.price_usd or 0.0)
            logger.info("fourmeme_buy_confirmed",extra={"mint":token.mint,"tx_signature":tx_hash,"amount_bnb":float(msg_value)/10**18,"min_amount":min_amount,"version":version})
            return OrderResult(True,"filled",price_usd=fill_price,tx_signature=tx_hash)
        except Exception as exc:
            logger.warning("fourmeme_buy_failed",extra={"mint":token.mint,"error":str(exc)})
            return OrderResult(False,"failed",error_message=str(exc))

    async def sell(self, token:TokenSnapshot, amount_tokens:float, sell_pct:float)->OrderResult:
        try:
            address=Web3.to_checksum_address(token.mint)
            info=self._helper.functions.getTokenInfo(address).call()
            version, manager, quote, liquidity_added = int(info[0]), info[1], info[2], bool(info[11])
            if liquidity_added:
                raise RuntimeError("Four.meme token has migrated; BSC AMM exit adapter is not enabled yet")
            if version != 2 or quote.lower()!=ZERO.lower():
                raise RuntimeError("Four.meme sell requires a V2 BNB-quoted token")
            decimals=18
            amount=max(1,int(float(amount_tokens)*10**decimals))
            quote_result=self._helper.functions.trySell(address,amount).call()
            estimated_funds=int(quote_result[2])
            min_funds=estimated_funds*(10000-self._slippage_bps)//10000
            manager_contract=self._w3.eth.contract(address=Web3.to_checksum_address(manager),abi=TM_ABI)
            # Four.meme V2 requires ERC20 approval before sell.
            token_contract=self._w3.eth.contract(address=address,abi=ERC20_ABI)
            self._send(token_contract.functions.approve(Web3.to_checksum_address(manager),amount))
            tx_hash=self._send(manager_contract.functions.sellToken(address,amount))
            logger.info("fourmeme_sell_confirmed",extra={"mint":token.mint,"tx_signature":tx_hash,"amount_tokens":amount_tokens,"estimated_funds":estimated_funds,"min_funds":min_funds})
            return OrderResult(True,"filled",price_usd=token.price_usd,tx_signature=tx_hash)
        except Exception as exc:
            logger.warning("fourmeme_sell_failed",extra={"mint":token.mint,"error":str(exc)})
            return OrderResult(False,"failed",error_message=str(exc))
