"""Isolated Robinhood Chain WETH -> token Uniswap V4 executor."""
from __future__ import annotations
import logging
import time
from typing import Any
from urllib.parse import urlparse
from eth_abi import encode
from web3 import Web3

CHAIN_ID=4663
RPC_DEFAULT="https://rpc.mainnet.chain.robinhood.com"
WETH="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
POOL_MANAGER="0x8366a39CC670B4001A1121B8F6A443A643e40951"
UNIVERSAL_ROUTER="0x8876789976dEcBfCbBbe364623C63652db8C0904"
QUOTER="0x8dc178efb8111bb0973dd9d722ebeFF267c98f94"
PERMIT2="0x000000000022D473030F116dDEE9F6B43aC78BA3"
BPS=10_000
logger=logging.getLogger("app.execution.robinhood_weth")
ROUTER_ABI=[{"inputs":[{"name":"commands","type":"bytes"},{"name":"inputs","type":"bytes[]"},{"name":"deadline","type":"uint256"}],"name":"execute","outputs":[],"stateMutability":"payable","type":"function"}]
QUOTER_ABI=[{"inputs":[{"components":[{"components":[{"name":"currency0","type":"address"},{"name":"currency1","type":"address"},{"name":"fee","type":"uint24"},{"name":"tickSpacing","type":"int24"},{"name":"hooks","type":"address"}],"name":"poolKey","type":"tuple"},{"name":"zeroForOne","type":"bool"},{"name":"exactAmount","type":"uint128"},{"name":"minHopPriceX36","type":"uint256"},{"name":"hookData","type":"bytes"}],"name":"params","type":"tuple"}],"name":"quoteExactInputSingle","outputs":[{"name":"amountOut","type":"uint256"},{"name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}]
ERC20_ABI=[{"inputs":[],"name":"decimals","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"},{"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"type":"bool"}],"stateMutability":"nonpayable","type":"function"}]
PERMIT2_ABI=[{"inputs":[{"name":"owner","type":"address"},{"name":"token","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"amount","type":"uint160"},{"name":"expiration","type":"uint48"},{"name":"nonce","type":"uint48"}],"stateMutability":"view","type":"function"}]
INITIALIZE_TOPIC=Web3.keccak(text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex()
def _addr(value:str)->str:return Web3.to_checksum_address(value)

def _usable_rpc(value:str)->str:
    text=str(value or "").strip()
    if not text:return ""
    parsed=urlparse(text)
    if parsed.scheme not in {"http","https"} or not parsed.netloc:return ""
    if parsed.netloc.lower()=="robinhood-mainnet.g.alchemy.com":
        path=parsed.path.rstrip("/")
        if not path.startswith("/v2/") or len(path.removeprefix("/v2/"))<8:return ""
    return text.rstrip("/")

class RobinhoodWethExecution:
    def __init__(self,account,rpc_url:str=RPC_DEFAULT,slippage_bps:int=1000):
        self.account=account
        requested=_usable_rpc(rpc_url)
        candidates=[requested] if requested else []
        if RPC_DEFAULT not in candidates:candidates.append(RPC_DEFAULT)
        last_error=None
        for candidate in candidates:
            try:
                w3=Web3(Web3.HTTPProvider(candidate,request_kwargs={"timeout":8}))
                chain_id=int(w3.eth.chain_id)
                if chain_id!=CHAIN_ID: raise RuntimeError(f"RPC chain ID {chain_id} is not Robinhood Chain {CHAIN_ID}")
                self.w3=w3
                if candidate==RPC_DEFAULT and requested!=RPC_DEFAULT:
                    logger.warning("robinhood_weth_rpc_fallback",extra={"reason":"configured RPC unavailable or malformed","provider":"public"})
                break
            except Exception as exc:
                last_error=exc
        else:
            raise RuntimeError("could not reach Robinhood Chain RPC") from last_error
        self.slippage_bps=max(0,min(int(slippage_bps),3000)); self.router=self.w3.eth.contract(address=_addr(UNIVERSAL_ROUTER),abi=ROUTER_ABI); self.quoter=self.w3.eth.contract(address=_addr(QUOTER),abi=QUOTER_ABI); self.weth=self.w3.eth.contract(address=_addr(WETH),abi=ERC20_ABI); self.permit2=self.w3.eth.contract(address=_addr(PERMIT2),abi=PERMIT2_ABI)
    def discover_pool(self,token:str,lookback_blocks:int=100_000)->dict[str,Any]:
        token=_addr(token); weth=_addr(WETH); latest=int(self.w3.eth.block_number); start=max(0,latest-int(lookback_blocks)); step=5_000
        for end in range(latest,start-1,-step):
            frm=max(start,end-step+1)
            logs=self.w3.eth.get_logs({"fromBlock":frm,"toBlock":end,"address":_addr(POOL_MANAGER),"topics":[INITIALIZE_TOPIC,None,weth,token]})
            for log in reversed(logs):
                fee,tick_spacing,hooks,_sqrt,_tick=self.w3.codec.decode(["uint24","int24","address","uint160","int24"],bytes(log["data"]))
                currency0=_addr("0x"+bytes(log["topics"][2])[-20:].hex()); currency1=_addr("0x"+bytes(log["topics"][3])[-20:].hex())
                if currency0.lower()!=weth.lower() or currency1.lower()!=token.lower(): continue
                pool_key={"currency0":currency0,"currency1":currency1,"fee":int(fee),"tickSpacing":int(tick_spacing),"hooks":_addr(hooks)}
                logger.info("robinhood_weth_pool_discovered",extra={"token":token,"pool_key":pool_key,"block":int(log["blockNumber"])})
                return pool_key
        raise RuntimeError(f"No initialized WETH/{token} Uniswap V4 pool found in the last {lookback_blocks} blocks")
    def _require_ready(self,amount_in:int)->None:
        balance=int(self.weth.functions.balanceOf(self.account.address).call())
        if balance<amount_in: raise RuntimeError(f"Insufficient WETH: {balance/1e18:.8f} available, {amount_in/1e18:.8f} required")
        if int(self.weth.functions.allowance(self.account.address,_addr(PERMIT2)).call())<amount_in: raise RuntimeError("WETH is not approved to Permit2. Run /approveweth first.")
        p2_amount,expiration,_=self.permit2.functions.allowance(self.account.address,_addr(WETH),_addr(UNIVERSAL_ROUTER)).call()
        if int(p2_amount)<amount_in or int(expiration)<=int(time.time()): raise RuntimeError("WETH Permit2 allowance for UniversalRouter is missing or expired. Run /approveweth first.")
        if int(self.w3.eth.get_balance(self.account.address))<=0: raise RuntimeError("Robinhood wallet needs ETH for gas")
    @staticmethod
    def _encode_v4_input(pool_key,amount_in,min_out):
        actions=bytes([0x06,0x0c,0x0f]); pool_tuple=[pool_key["currency0"],pool_key["currency1"],int(pool_key["fee"]),int(pool_key["tickSpacing"]),pool_key["hooks"]]
        swap=encode(["(address,address,uint24,int24,address)","bool","uint128","uint128","uint256","bytes"],[pool_tuple,True,int(amount_in),int(min_out),0,b""])
        settle=encode(["address","uint256"],[pool_key["currency0"],int(amount_in)]); take=encode(["address","uint256"],[pool_key["currency1"],int(min_out)])
        return encode(["bytes","bytes[]"],[actions,[swap,settle,take]])
    @staticmethod
    def _encode_permit2_transfer(amount_in): return encode(["address","address","uint160"],[_addr(WETH),_addr(UNIVERSAL_ROUTER),int(amount_in)])
    def _send(self,fn):
        nonce=self.w3.eth.get_transaction_count(self.account.address,"pending"); latest=self.w3.eth.get_block("latest"); base_fee=latest.get("baseFeePerGas")
        if base_fee is not None:
            priority=max(1,int(self.w3.eth.max_priority_fee or 0)); tx={"from":self.account.address,"nonce":nonce,"chainId":CHAIN_ID,"maxPriorityFeePerGas":priority,"maxFeePerGas":int(base_fee)*2+priority,"type":2,"value":0}
        else: tx={"from":self.account.address,"nonce":nonce,"chainId":CHAIN_ID,"gasPrice":int(self.w3.eth.gas_price),"value":0}
        built=fn.build_transaction(tx); built["gas"]=int(self.w3.eth.estimate_gas(built)*1.20); signed=self.account.sign_transaction(built); tx_hash=self.w3.eth.send_raw_transaction(signed.raw_transaction); receipt=self.w3.eth.wait_for_transaction_receipt(tx_hash,timeout=60)
        if receipt.status!=1: raise RuntimeError(f"WETH snipe reverted: {tx_hash.hex()}")
        return tx_hash.hex()
    def buy(self,token:str,amount_weth:float,lookback_blocks:int=100_000)->dict[str,Any]:
        token=_addr(token)
        if amount_weth<=0: raise RuntimeError("WETH amount must be greater than zero")
        amount_in=max(1,int(float(amount_weth)*10**18)); self._require_ready(amount_in); pool_key=self.discover_pool(token,lookback_blocks)
        quote=self.quoter.functions.quoteExactInputSingle({"poolKey":pool_key,"zeroForOne":True,"exactAmount":amount_in,"minHopPriceX36":0,"hookData":b""}).call(); expected=int(quote[0])
        if expected<=0: raise RuntimeError("Robinhood V4 quoter returned zero output")
        min_out=expected*(BPS-self.slippage_bps)//BPS; v4_input=self._encode_v4_input(pool_key,amount_in,min_out); fn=self.router.functions.execute(bytes([0x02,0x10]),[self._encode_permit2_transfer(amount_in),v4_input],int(time.time())+30); simulation_tx=fn.build_transaction({"from":self.account.address,"value":0,"chainId":CHAIN_ID})
        try: self.w3.eth.call(simulation_tx)
        except Exception as exc: raise RuntimeError(f"UniversalRouter simulation reverted: {exc}") from exc
        return {"token":token,"pool_key":pool_key,"amount_weth":amount_weth,"quoted_tokens":expected,"min_tokens":min_out,"tx_hash":self._send(fn)}
