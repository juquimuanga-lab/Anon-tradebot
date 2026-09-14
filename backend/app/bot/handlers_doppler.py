"""Admin Telegram controls for the isolated Robinhood Doppler/SPCX sniper lane."""
from __future__ import annotations
import math
import time
import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from web3 import Web3
from app.security.allowlist import admin_required
from app.security.secrets_manager import secrets_manager
from app.storage import repository as repo
from app.config.settings import settings
from app.connectors import doppler_control
from app.execution.onchain.robinhood_wallet import load_robinhood_account, build_robinhood_web3, resolve_robinhood_rpc_url
from app.execution.robinhood_weth_live import RobinhoodWethExecution, WETH
PERMIT2="0x000000000022D473030F116dDEE9F6B43aC78BA3"
UNIVERSAL_ROUTER="0x8876789976dEcBfCbBbe364623C63652db8C0904"
SPCX=doppler_control.SPCX_TOKEN
DEFAULT_WETH_TARGET="0xE4BEF9d0845a13bD39C57C7ee4463ff5D0cc20B6"
SPCX_ABI=[{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"type":"bool"}],"stateMutability":"nonpayable","type":"function"}]
PERMIT2_ABI=[{"inputs":[{"name":"token","type":"address"},{"name":"spender","type":"address"},{"name":"amount","type":"uint160"},{"name":"expiration","type":"uint48"}],"name":"approve","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"name":"owner","type":"address"},{"name":"token","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"amount","type":"uint160"},{"name":"expiration","type":"uint48"},{"name":"nonce","type":"uint48"}],"stateMutability":"view","type":"function"}]
BALANCE_ABI=[{"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"decimals","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"}]
async def _get_account_and_w3(user_id:int):
    raw_key=await secrets_manager.get_robinhood_wallet_private_key(user_id)
    if not raw_key: raise RuntimeError("No Robinhood Chain wallet is connected. Run /connectrobinhoodwallet first.")
    rpc_url=resolve_robinhood_rpc_url(settings); account=load_robinhood_account(raw_key); w3=build_robinhood_web3(rpc_url)
    if int(w3.eth.chain_id)!=4663: raise RuntimeError(f"RPC returned chain ID {int(w3.eth.chain_id)}, expected 4663.")
    return account,w3
def _send(w3,account,fn)->str:
    if int(w3.eth.get_balance(account.address))<=0: raise RuntimeError("Robinhood wallet needs ETH for gas.")
    nonce=w3.eth.get_transaction_count(account.address,"pending"); latest=w3.eth.get_block("latest"); base_fee=latest.get("baseFeePerGas")
    if base_fee is not None:
        base_fee=int(base_fee)
        try: priority_fee=max(1,int(w3.eth.max_priority_fee or 0))
        except Exception: priority_fee=max(1,int(base_fee//10))
        tx_params={"from":account.address,"nonce":nonce,"chainId":4663,"maxPriorityFeePerGas":priority_fee,"maxFeePerGas":base_fee*2+priority_fee,"type":2}
    else: tx_params={"from":account.address,"nonce":nonce,"chainId":4663,"gasPrice":int(w3.eth.gas_price)}
    tx=fn.build_transaction(tx_params); tx["gas"]=int(w3.eth.estimate_gas(tx)*1.20); signed=account.sign_transaction(tx); tx_hash=w3.eth.send_raw_transaction(signed.raw_transaction); receipt=w3.eth.wait_for_transaction_receipt(tx_hash,timeout=45)
    if receipt.status!=1: raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")
    return tx_hash.hex()
@admin_required
async def dopplerstatus_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    user_id=update.effective_user.id; enabled=doppler_control.is_enabled(); size=doppler_control.get_buy_size_spcx(); deployment=doppler_control.deployment_enabled()
    try:
        account,w3=await _get_account_and_w3(user_id); spcx=w3.eth.contract(address=Web3.to_checksum_address(SPCX),abi=BALANCE_ABI); permit2=w3.eth.contract(address=Web3.to_checksum_address(PERMIT2),abi=PERMIT2_ABI); decimals=int(spcx.functions.decimals().call()); balance=int(spcx.functions.balanceOf(account.address).call())/10**decimals; direct=int(spcx.functions.allowance(account.address,Web3.to_checksum_address(PERMIT2)).call())/10**decimals; p2_amount,expiration,_=permit2.functions.allowance(account.address,Web3.to_checksum_address(SPCX),Web3.to_checksum_address(UNIVERSAL_ROUTER)).call(); p2_amount=int(p2_amount)/10**decimals; exp_ok=int(expiration)>int(time.time()); ready=deployment and enabled and size>0 and balance>=size and direct>=size and p2_amount>=size and exp_ok
        text=("🎯 *Robinhood Doppler / SPCX Sniper*\n\n" f"Deployment gate: `{'ON' if deployment else 'OFF'}`\n" f"Doppler sniper: `{'ON' if enabled else 'OFF'}`\n" f"Live snipe size: `{size:g} SPCX`\n" f"Wallet: `{account.address}`\n" f"SPCX balance: `{balance:.6f}`\n" f"SPCX required per snipe: `{size:.6f}`\n" f"SPCX → Permit2: `{direct:.6f}`\n" f"Permit2 → Router: `{p2_amount:.6f}` ({'valid' if exp_ok else 'expired/missing'})\n" f"Overall readiness: `{'READY' if ready else 'NOT READY'}`\n\n" f"Canonical SPCX: `{SPCX}`\n" f"Anoncoin fingerprint: token address ends in `{doppler_control.ANONCOIN_ADDRESS_SUFFIX}`\nOnly canonical SPCX-quoted launches matching the fingerprint are accepted.\nThe live snipe size is checked again immediately before every buy.")
    except Exception as exc: text=("🎯 *Robinhood Doppler / SPCX Sniper*\n\n" f"Deployment gate: `{'ON' if deployment else 'OFF'}`\n" f"Doppler sniper: `{'ON' if enabled else 'OFF'}`\n" f"Live snipe size: `{size:g} SPCX`\n" f"Wallet readiness: `NOT READY`\n" f"Reason: `{str(exc)}`")
    await update.message.reply_text(text,parse_mode="Markdown")
@admin_required
async def enable_doppler_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    if not doppler_control.deployment_enabled(): await update.message.reply_text("Doppler is blocked by the deployment gate. Set ROBINHOOD_DOPPLER_TRADING_ENABLED=true in Railway first."); return
    try: await _get_account_and_w3(update.effective_user.id)
    except Exception as exc: await update.message.reply_text(f"❌ Cannot enable Doppler: {exc}"); return
    if doppler_control.get_buy_size_spcx()<=0: await update.message.reply_text("❌ Set a buy size first with /setdopplersize <SPCX>."); return
    doppler_control.set_enabled(True); await repo.write_audit_log(str(update.effective_user.id),"enable_doppler",{"buy_size_spcx":doppler_control.get_buy_size_spcx()}); await update.message.reply_text(f"🟢 Doppler/SPCX sniper is ON. Buy size: {doppler_control.get_buy_size_spcx():g} SPCX per qualifying launch.")
@admin_required
async def disable_doppler_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    doppler_control.set_enabled(False); await repo.write_audit_log(str(update.effective_user.id),"disable_doppler",{}); await update.message.reply_text("🔴 Doppler/SPCX sniper is OFF. The direct Doppler lane is disabled; the existing Solana/Pump.fun lanes are unchanged.")
@admin_required
async def set_doppler_size_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    if not context.args: await update.message.reply_text("Usage: /setdopplersize <SPCX>\nExample: /setdopplersize 0.05"); return
    try: value=float(context.args[0])
    except (TypeError,ValueError): await update.message.reply_text("❌ Buy size must be a number in SPCX."); return
    if not math.isfinite(value) or value<=0 or value>1_000_000: await update.message.reply_text("❌ Buy size must be finite, greater than 0 and no more than 1,000,000 SPCX."); return
    doppler_control.set_buy_size_spcx(value); await repo.write_audit_log(str(update.effective_user.id),"set_doppler_buy_size",{"amount_spcx":value}); await update.message.reply_text(f"✅ Live Doppler buy size is now {value:g} SPCX per qualifying launch.")
@admin_required
async def approve_spcx_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    try:
        account,w3=await _get_account_and_w3(update.effective_user.id); spcx=w3.eth.contract(address=Web3.to_checksum_address(SPCX),abi=SPCX_ABI+BALANCE_ABI); permit2=w3.eth.contract(address=Web3.to_checksum_address(PERMIT2),abi=PERMIT2_ABI); max_uint256=(1<<256)-1; max_uint160=(1<<160)-1; await update.message.reply_text("Sending SPCX approval transactions. Keep the bot running until both confirm."); tx1=_send(w3,account,spcx.functions.approve(Web3.to_checksum_address(PERMIT2),max_uint256)); tx2=_send(w3,account,permit2.functions.approve(Web3.to_checksum_address(SPCX),Web3.to_checksum_address(UNIVERSAL_ROUTER),max_uint160,(1<<48)-1)); await repo.write_audit_log(str(update.effective_user.id),"approve_doppler_spcx",{"tx1":tx1,"tx2":tx2}); await update.message.reply_text(f"SPCX approvals complete.\n\nSPCX → Permit2: {tx1}\nPermit2 → UniversalRouter: {tx2}")
    except Exception as exc: await update.message.reply_text(f"SPCX approval failed: {exc}")
@admin_required
async def approve_weth_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    try:
        account,w3=await _get_account_and_w3(update.effective_user.id); weth=w3.eth.contract(address=Web3.to_checksum_address(WETH),abi=SPCX_ABI+BALANCE_ABI); permit2=w3.eth.contract(address=Web3.to_checksum_address(PERMIT2),abi=PERMIT2_ABI); max_uint256=(1<<256)-1; max_uint160=(1<<160)-1; balance=int(weth.functions.balanceOf(account.address).call())/1e18
        if balance<=0: raise RuntimeError("Wallet has no WETH on Robinhood Chain.")
        await update.message.reply_text(f"WETH balance detected: {balance:.8f}. Sending WETH approvals..."); tx1=_send(w3,account,weth.functions.approve(Web3.to_checksum_address(PERMIT2),max_uint256)); tx2=_send(w3,account,permit2.functions.approve(Web3.to_checksum_address(WETH),Web3.to_checksum_address(UNIVERSAL_ROUTER),max_uint160,(1<<48)-1)); await repo.write_audit_log(str(update.effective_user.id),"approve_robinhood_weth",{"tx1":tx1,"tx2":tx2}); await update.message.reply_text(f"✅ WETH approvals complete.\n\nWETH → Permit2: `{tx1}`\nPermit2 → UniversalRouter: `{tx2}`")
    except Exception as exc: await update.message.reply_text(f"❌ WETH approval failed: {exc}")
@admin_required
async def weth_snipe_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    token=DEFAULT_WETH_TARGET; amount=0.01
    if context.args: token=context.args[0]
    if len(context.args)>=2:
        try: amount=float(context.args[1])
        except (TypeError,ValueError): await update.message.reply_text("❌ WETH amount must be a number. Usage: /wethsnipe <token> <weth_amount>"); return
    if not math.isfinite(amount) or amount<=0: await update.message.reply_text("❌ WETH amount must be greater than zero."); return
    try: checksum_token=Web3.to_checksum_address(token)
    except ValueError: await update.message.reply_text("❌ Invalid token address."); return
    await update.message.reply_text(f"🔎 Inspecting `{checksum_token}` for a Robinhood V4 WETH pool and quoting `{amount:g} WETH`. No transaction is signed unless the exact Universal Router call simulates successfully.",parse_mode="Markdown")
    try:
        account,_=await _get_account_and_w3(update.effective_user.id); executor=RobinhoodWethExecution(account=account,rpc_url=resolve_robinhood_rpc_url(settings),slippage_bps=1000); result=await asyncio.to_thread(executor.buy,checksum_token,amount); await repo.write_audit_log(str(update.effective_user.id),"robinhood_weth_snipe",result); await update.message.reply_text(f"🟢 WETH snipe filled.\n\nToken: `{result['token']}`\nWETH spent: `{result['amount_weth']}`\nQuoted tokens: `{result['quoted_tokens']}`\nMinimum tokens: `{result['min_tokens']}`\nTX: `{result['tx_hash']}`",parse_mode="Markdown")
    except Exception as exc:
        await repo.write_audit_log(str(update.effective_user.id),"robinhood_weth_snipe_failed",{"token":checksum_token,"amount_weth":amount,"error":str(exc)}); await update.message.reply_text(f"🛑 WETH snipe NOT sent.\n\nReason: `{str(exc)}`",parse_mode="Markdown")
