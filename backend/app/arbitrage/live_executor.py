"""Gated live Solana arbitrage execution.

This module is deliberately separate from the sniper execution router.
Live execution is disabled unless ARBITRAGE_LIVE_TRADING_ENABLED=true is set
in the deployment environment and the caller explicitly invokes execute().

The two swap transactions plus a Jito tip transaction are submitted as one
bundle. Jito bundles are atomic/all-or-nothing. The sell leg is sized to the
buy leg's minimum guaranteed output so a worse-but-valid buy fill cannot leave
the bundle holding an unexpected amount of the token.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

from app.arbitrage.jupiter_quotes import VenueConfig
from app.arbitrage.jito import JitoClient
from app.arbitrage.service import ArbitrageService
from app.execution.onchain.jupiter import SOL_MINT
from app.execution.onchain.solana_rpc import sign_versioned_transaction
from app.security.secrets_manager import secrets_manager
from app.execution.onchain.wallet_keys import load_keypair
from app.config.settings import settings

logger = logging.getLogger("app.arbitrage.live_executor")
LAMPORTS_PER_SOL = 1_000_000_000


class ArbitrageLiveExecutionError(RuntimeError):
    """Raised when an arbitrage bundle cannot be safely executed."""


@dataclass(frozen=True)
class LiveExecutionResult:
    success: bool
    bundle_id: str | None = None
    buy_venue: str | None = None
    sell_venue: str | None = None
    input_lamports: int = 0
    guaranteed_token_amount: int = 0
    estimated_net_profit_lamports: int = 0
    reason: str = ""


class ArbitrageLiveExecutor:
    """Build, simulate and atomically submit an arbitrage bundle."""

    def __init__(self, service: ArbitrageService | None = None) -> None:
        self._service = service or ArbitrageService()
        self._jito = JitoClient(os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf"))
        self._live_enabled = os.getenv("ARBITRAGE_LIVE_TRADING_ENABLED", "false").lower() == "true"
        self._min_profit_bps = float(os.getenv("ARBITRAGE_LIVE_MIN_PROFIT_BPS", "50"))
        self._min_profit_lamports = int(os.getenv("ARBITRAGE_LIVE_MIN_PROFIT_LAMPORTS", "5000000"))
        self._slippage_bps = max(1, min(int(os.getenv("ARBITRAGE_LIVE_SLIPPAGE_BPS", "30")), 300))
        self._tip_lamports = max(1000, int(os.getenv("ARBITRAGE_LIVE_JITO_TIP_LAMPORTS", "2000000")))

    @property
    def live_enabled(self) -> bool:
        return self._live_enabled

    async def _wallet(self, owner_user_id: int) -> Keypair:
        raw_key = await secrets_manager.get_wallet_private_key(owner_user_id)
        if not raw_key:
            raise ArbitrageLiveExecutionError("no Solana wallet is connected")
        try:
            return load_keypair(raw_key)
        except Exception as exc:
            raise ArbitrageLiveExecutionError("stored Solana wallet key is invalid") from exc

    async def _quote(self, input_mint: str, output_mint: str, amount: int, venue: VenueConfig) -> dict[str, Any]:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount,
            "slippageBps": self._slippage_bps,
            "dexes": venue.jupiter_dex_label,
            "restrictIntermediateTokens": "true",
            "instructionVersion": "V2",
        }
        headers = {}
        api_key = os.getenv("JUPITER_API_KEY")
        if api_key:
            headers["x-api-key"] = api_key
        async with httpx.AsyncClient(base_url=settings.jupiter_base_url.rstrip("/"), timeout=8.0) as client:
            response = await client.get("/quote", params=params, headers=headers)
        if response.status_code != 200:
            raise ArbitrageLiveExecutionError(f"Jupiter quote failed: HTTP {response.status_code}")
        payload = response.json()
        if payload.get("error") or int(payload.get("outAmount") or 0) <= 0:
            raise ArbitrageLiveExecutionError(f"Jupiter returned no executable quote for {venue.name}")
        return payload

    async def _build_swap(self, quote: dict[str, Any], user_pubkey: str) -> tuple[bytes, int, int]:
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "priorityLevel": "veryHigh",
                    "maxLamports": 1_000_000,
                }
            },
        }
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("JUPITER_API_KEY")
        if api_key:
            headers["x-api-key"] = api_key
        async with httpx.AsyncClient(base_url=settings.jupiter_base_url.rstrip("/"), timeout=10.0) as client:
            response = await client.post("/swap", json=body, headers=headers)
        if response.status_code != 200:
            raise ArbitrageLiveExecutionError(f"Jupiter swap build failed: HTTP {response.status_code}")
        payload = response.json()
        raw = payload.get("swapTransaction")
        if not raw:
            raise ArbitrageLiveExecutionError("Jupiter swap response did not contain a transaction")
        return base64.b64decode(raw), int(payload.get("prioritizationFeeLamports") or 0), int(payload.get("lastValidBlockHeight") or 0)

    async def _simulate(self, rpc_url: str, signed_tx: bytes) -> None:
        encoded = base64.b64encode(signed_tx).decode("ascii")
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "simulateTransaction",
            "params": [encoded, {"encoding": "base64", "sigVerify": True, "replaceRecentBlockhash": False}],
        }
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(rpc_url, json=payload)
        if response.status_code != 200:
            raise ArbitrageLiveExecutionError(f"RPC simulation HTTP {response.status_code}")
        body = response.json()
        if body.get("error"):
            raise ArbitrageLiveExecutionError(f"RPC simulation failed: {body['error']}")
        value = ((body.get("result") or {}).get("value") or {})
        if value.get("err") is not None:
            raise ArbitrageLiveExecutionError(f"transaction simulation failed: {value['err']}")

    async def _tip_transaction(self, keypair: Keypair, tip_account: str, rpc_url: str) -> bytes:
        async with AsyncClient(rpc_url) as client:
            latest = await client.get_latest_blockhash(commitment="processed")
        tx = Transaction.new_signed_with_payer(
            [transfer(TransferParams(
                from_pubkey=keypair.pubkey(),
                to_pubkey=Pubkey.from_string(tip_account),
                lamports=self._tip_lamports,
            ))],
            keypair.pubkey(),
            [keypair],
            latest.value.blockhash,
        )
        return bytes(tx)

    async def execute(
        self,
        owner_user_id: int,
        token_mint: str,
        amount_sol: float,
        buy_venue: VenueConfig,
        sell_venue: VenueConfig,
    ) -> LiveExecutionResult:
        if not self._live_enabled:
            return LiveExecutionResult(False, buy_venue=buy_venue.name, sell_venue=sell_venue.name, reason="live_arbitrage_disabled")
        if amount_sol <= 0:
            return LiveExecutionResult(False, reason="amount_must_be_positive")

        keypair = await self._wallet(owner_user_id)
        user_pubkey = str(keypair.pubkey())
        input_lamports = int(amount_sol * LAMPORTS_PER_SOL)
        rpc_url = settings.solana_rpc_url
        if not rpc_url:
            raise ArbitrageLiveExecutionError("SOLANA_RPC_URL/Helius RPC is not configured")

        # Fresh buy quote immediately before transaction construction.
        buy_quote = await self._quote(SOL_MINT, token_mint, input_lamports, buy_venue)
        guaranteed_tokens = int(buy_quote.get("otherAmountThreshold") or 0)
        if guaranteed_tokens <= 0:
            raise ArbitrageLiveExecutionError("buy quote has no positive minimum output")

        # The sell leg consumes only the minimum token amount guaranteed by the
        # buy leg. This avoids depending on a dynamic post-buy token balance.
        sell_quote = await self._quote(token_mint, SOL_MINT, guaranteed_tokens, sell_venue)
        final_lamports = int(sell_quote.get("outAmount") or 0)
        if final_lamports <= 0:
            raise ArbitrageLiveExecutionError("sell quote has no positive output")

        gross = final_lamports - input_lamports
        estimated_cost = int(input_lamports * buy_venue.fee_bps / 10_000)
        estimated_cost += int(final_lamports * sell_venue.fee_bps / 10_000)
        estimated_cost += self._tip_lamports
        net_before_priority = gross - estimated_cost
        net_bps = (net_before_priority / input_lamports * 10_000) if input_lamports else 0.0
        if net_before_priority < self._min_profit_lamports or net_bps < self._min_profit_bps:
            return LiveExecutionResult(False, buy_venue=buy_venue.name, sell_venue=sell_venue.name, input_lamports=input_lamports, guaranteed_token_amount=guaranteed_tokens, estimated_net_profit_lamports=net_before_priority, reason="live_profit_gate_failed")

        buy_unsigned, buy_priority, buy_last_valid = await self._build_swap(buy_quote, user_pubkey)
        sell_unsigned, sell_priority, sell_last_valid = await self._build_swap(sell_quote, user_pubkey)
        net_after_priority = net_before_priority - buy_priority - sell_priority
        if net_after_priority < self._min_profit_lamports or (net_after_priority / input_lamports * 10_000) < self._min_profit_bps:
            return LiveExecutionResult(False, buy_venue=buy_venue.name, sell_venue=sell_venue.name, input_lamports=input_lamports, guaranteed_token_amount=guaranteed_tokens, estimated_net_profit_lamports=net_after_priority, reason="actual_priority_fee_profit_gate_failed")

        buy_signed = sign_versioned_transaction(base64.b64encode(buy_unsigned).decode("ascii"), keypair)
        sell_signed = sign_versioned_transaction(base64.b64encode(sell_unsigned).decode("ascii"), keypair)
        tip_account = (await self._jito.get_tip_accounts())[0]
        tip_signed = await self._tip_transaction(keypair, tip_account, rpc_url)

        # Jupiter's build path already performs swap simulation when dynamic
        # compute units are enabled. We additionally simulate the buy leg on
        # our configured RPC before submitting the bundle. The sell leg cannot
        # be independently simulated against the post-buy state without
        # mutating a real bank; atomic bundling is the protection for that leg.
        await self._simulate(rpc_url, buy_signed)
        await self._simulate(rpc_url, tip_signed)

        encoded_bundle = [
            base64.b64encode(buy_signed).decode("ascii"),
            base64.b64encode(sell_signed).decode("ascii"),
            base64.b64encode(tip_signed).decode("ascii"),
        ]
        bundle_id = await self._jito.send_bundle(encoded_bundle)
        logger.warning("arbitrage_live_bundle_submitted", extra={
            "bundle_id": bundle_id,
            "mint": token_mint,
            "buy_venue": buy_venue.name,
            "sell_venue": sell_venue.name,
            "input_lamports": input_lamports,
            "guaranteed_tokens": guaranteed_tokens,
            "estimated_net_profit_lamports": net_after_priority,
            "buy_last_valid_block_height": buy_last_valid,
            "sell_last_valid_block_height": sell_last_valid,
        })
        return LiveExecutionResult(True, bundle_id=bundle_id, buy_venue=buy_venue.name, sell_venue=sell_venue.name, input_lamports=input_lamports, guaranteed_token_amount=guaranteed_tokens, estimated_net_profit_lamports=net_after_priority, reason="bundle_submitted")
