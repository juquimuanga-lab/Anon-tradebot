"""Gated live Solana arbitrage execution with safe Jito tip placement."""
from __future__ import annotations

import base64
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any

import httpx

from solders.keypair import Keypair

from app.arbitrage.jupiter_bundle import (
    JupiterInstructionBuildError,
    build_signed_swap_with_tip,
)
from app.arbitrage.jupiter_quotes import VenueConfig
from app.arbitrage.jito import JitoClient
from app.arbitrage.service import ArbitrageService
from app.execution.onchain.jupiter import SOL_MINT
from app.execution.onchain.solana_rpc import get_transaction_details
from app.security.secrets_manager import secrets_manager
from app.execution.onchain.wallet_keys import load_keypair
from app.config.settings import settings

logger = logging.getLogger("app.arbitrage.live_executor")
LAMPORTS_PER_SOL = 1_000_000_000


class ArbitrageLiveExecutionError(RuntimeError):
    """Raised when an arbitrage bundle cannot safely be executed."""


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
    settlement_status: str | None = None
    transaction_signatures: tuple[str, ...] = field(default_factory=tuple)


class ArbitrageLiveExecutor:
    """Build, sign, submit and reconcile a two-leg atomic arbitrage bundle."""

    def __init__(self, service: ArbitrageService | None = None) -> None:
        self._service = service or ArbitrageService()
        self._jito = JitoClient(
            os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf")
        )
        self._live_enabled = (
            os.getenv("ARBITRAGE_LIVE_TRADING_ENABLED", "false").lower() == "true"
        )
        self._min_profit_bps = float(os.getenv("ARBITRAGE_LIVE_MIN_PROFIT_BPS", "50"))
        self._min_profit_lamports = int(
            os.getenv("ARBITRAGE_LIVE_MIN_PROFIT_LAMPORTS", "5000000")
        )
        self._slippage_bps = max(
            1, min(int(os.getenv("ARBITRAGE_LIVE_SLIPPAGE_BPS", "30")), 300)
        )
        self._tip_lamports = max(
            1000, int(os.getenv("ARBITRAGE_LIVE_JITO_TIP_LAMPORTS", "2000000"))
        )
        self._settlement_timeout_seconds = max(
            3.0,
            float(os.getenv("ARBITRAGE_LIVE_SETTLEMENT_TIMEOUT_SECONDS", "20")),
        )
        self._settlement_poll_seconds = max(
            0.25,
            float(os.getenv("ARBITRAGE_LIVE_SETTLEMENT_POLL_SECONDS", "0.5")),
        )

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

    async def _quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        venue: VenueConfig,
    ) -> dict[str, Any]:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount,
            "slippageBps": self._slippage_bps,
            "dexes": venue.jupiter_dex_label,
            "restrictIntermediateTokens": "true",
        }
        headers: dict[str, str] = {}
        api_key = os.getenv("JUPITER_API_KEY")
        if api_key:
            headers["x-api-key"] = api_key
        async with httpx.AsyncClient(
            base_url=settings.jupiter_base_url.rstrip("/"), timeout=8.0
        ) as client:
            response = await client.get("/quote", params=params, headers=headers)
        if response.status_code != 200:
            raise ArbitrageLiveExecutionError(
                f"Jupiter quote failed: HTTP {response.status_code}"
            )
        payload = response.json()
        if payload.get("error") or int(payload.get("outAmount") or 0) <= 0:
            raise ArbitrageLiveExecutionError(
                f"Jupiter returned no executable quote for {venue.name}"
            )
        return payload

    async def _simulate(self, rpc_url: str, signed_tx: bytes) -> None:
        """Simulate one transaction where its state is independently valid.

        The sell leg is intentionally NOT simulated in isolation because its
        token input is created by the preceding buy transaction in the bundle.
        Jito's bundle simulator/Block Engine performs ordered bundle simulation.
        """
        encoded = base64.b64encode(signed_tx).decode("ascii")
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "simulateTransaction",
            "params": [
                encoded,
                {
                    "encoding": "base64",
                    "sigVerify": True,
                    "replaceRecentBlockhash": False,
                },
            ],
        }
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(rpc_url, json=payload)
        if response.status_code != 200:
            raise ArbitrageLiveExecutionError(
                f"RPC simulation HTTP {response.status_code}"
            )
        body = response.json()
        if body.get("error"):
            raise ArbitrageLiveExecutionError(f"RPC simulation failed: {body['error']}")
        value = ((body.get("result") or {}).get("value") or {})
        if value.get("err") is not None:
            raise ArbitrageLiveExecutionError(
                f"transaction simulation failed: {value['err']}"
            )

    async def _reconcile_landed_bundle(
        self,
        rpc_url: str,
        bundle_status: dict[str, Any],
    ) -> tuple[bool, str, tuple[str, ...]]:
        """Verify every landed bundle transaction succeeded on-chain."""
        confirmation = str(
            bundle_status.get("confirmation_status")
            or bundle_status.get("confirmationStatus")
            or ""
        ).lower()
        signatures = tuple(
            str(sig) for sig in (bundle_status.get("transactions") or [])
        )

        if confirmation not in {"processed", "confirmed", "finalized"}:
            return False, f"bundle_not_confirmed:{confirmation or 'unknown'}", signatures
        if len(signatures) != 2:
            return False, f"unexpected_bundle_transaction_count:{len(signatures)}", signatures

        for signature in signatures:
            transaction = await get_transaction_details(rpc_url, signature)
            if not transaction:
                return False, f"transaction_details_missing:{signature}", signatures
            meta = transaction.get("meta") or {}
            if meta.get("err") is not None:
                return False, f"transaction_failed:{signature}:{meta.get('err')}", signatures

        return True, "settled", signatures

    async def execute(
        self,
        owner_user_id: int,
        token_mint: str,
        amount_sol: float,
        buy_venue: VenueConfig,
        sell_venue: VenueConfig,
    ) -> LiveExecutionResult:
        if not self._live_enabled:
            return LiveExecutionResult(
                False,
                buy_venue=buy_venue.name,
                sell_venue=sell_venue.name,
                reason="live_arbitrage_disabled",
            )
        if amount_sol <= 0:
            return LiveExecutionResult(False, reason="amount_must_be_positive")

        keypair = await self._wallet(owner_user_id)
        user_pubkey = str(keypair.pubkey())
        input_lamports = int(amount_sol * LAMPORTS_PER_SOL)
        rpc_url = settings.solana_rpc_url
        if not rpc_url:
            raise ArbitrageLiveExecutionError("SOLANA_RPC_URL/Helius RPC is not configured")

        buy_quote = await self._quote(
            SOL_MINT, token_mint, input_lamports, buy_venue
        )
        guaranteed_tokens = int(buy_quote.get("otherAmountThreshold") or 0)
        if guaranteed_tokens <= 0:
            raise ArbitrageLiveExecutionError("buy quote has no positive minimum output")

        sell_quote = await self._quote(
            token_mint, SOL_MINT, guaranteed_tokens, sell_venue
        )
        final_lamports = int(sell_quote.get("outAmount") or 0)
        if final_lamports <= 0:
            raise ArbitrageLiveExecutionError("sell quote has no positive output")

        gross = final_lamports - input_lamports
        estimated_cost = int(input_lamports * buy_venue.fee_bps / 10_000)
        estimated_cost += int(final_lamports * sell_venue.fee_bps / 10_000)
        estimated_cost += self._tip_lamports
        net_before_priority = gross - estimated_cost
        net_bps = (
            net_before_priority / input_lamports * 10_000
            if input_lamports
            else 0.0
        )
        if (
            net_before_priority < self._min_profit_lamports
            or net_bps < self._min_profit_bps
        ):
            return LiveExecutionResult(
                False,
                buy_venue=buy_venue.name,
                sell_venue=sell_venue.name,
                input_lamports=input_lamports,
                guaranteed_token_amount=guaranteed_tokens,
                estimated_net_profit_lamports=net_before_priority,
                reason="live_profit_gate_failed",
            )

        try:
            tip_accounts = await self._jito.get_tip_accounts()
            if not tip_accounts:
                raise ArbitrageLiveExecutionError("Jito returned no tip accounts")
            tip_account = random.choice(tip_accounts)

            # Buy has no tip. Sell carries the tip in the SAME transaction.
            # Therefore a failed sell cannot pay the Jito tip independently.
            buy_signed, buy_priority = await build_signed_swap_with_tip(
                base_url=settings.jupiter_base_url,
                quote_response=buy_quote,
                user_pubkey=user_pubkey,
                keypair=keypair,
                rpc_url=rpc_url,
                tip_account=tip_account,
                tip_lamports=0,
                api_key=os.getenv("JUPITER_API_KEY"),
            )
        except JupiterInstructionBuildError as exc:
            # Rebuild the buy without a tip through the instruction path is not
            # safe if the helper enforces the minimum. A zero-tip instruction
            # path is supported explicitly by the helper below.
            raise ArbitrageLiveExecutionError(str(exc)) from exc

        # The helper's tip floor is only for tipped transactions. The buy leg
        # is rebuilt through the same instruction endpoint with a zero tip by
        # using the dedicated no-tip builder.
        from app.arbitrage.jupiter_bundle import build_signed_swap_without_tip

        buy_signed, buy_priority = await build_signed_swap_without_tip(
            base_url=settings.jupiter_base_url,
            quote_response=buy_quote,
            user_pubkey=user_pubkey,
            keypair=keypair,
            rpc_url=rpc_url,
            api_key=os.getenv("JUPITER_API_KEY"),
        )
        sell_signed, sell_priority = await build_signed_swap_with_tip(
            base_url=settings.jupiter_base_url,
            quote_response=sell_quote,
            user_pubkey=user_pubkey,
            keypair=keypair,
            rpc_url=rpc_url,
            tip_account=tip_account,
            tip_lamports=self._tip_lamports,
            api_key=os.getenv("JUPITER_API_KEY"),
        )

        net_after_priority = net_before_priority - buy_priority - sell_priority
        if (
            net_after_priority < self._min_profit_lamports
            or (net_after_priority / input_lamports * 10_000) < self._min_profit_bps
        ):
            return LiveExecutionResult(
                False,
                buy_venue=buy_venue.name,
                sell_venue=sell_venue.name,
                input_lamports=input_lamports,
                guaranteed_token_amount=guaranteed_tokens,
                estimated_net_profit_lamports=net_after_priority,
                reason="actual_priority_fee_profit_gate_failed",
            )

        # Only the buy is simulated independently. The sell depends on the
        # buy's state and is validated by Jito's ordered bundle simulation.
        await self._simulate(rpc_url, buy_signed)

        bundle_id = await self._jito.send_bundle(
            [
                base64.b64encode(buy_signed).decode("ascii"),
                base64.b64encode(sell_signed).decode("ascii"),
            ]
        )
        logger.warning(
            "arbitrage_live_bundle_submitted",
            extra={
                "bundle_id": bundle_id,
                "mint": token_mint,
                "buy_venue": buy_venue.name,
                "sell_venue": sell_venue.name,
                "input_lamports": input_lamports,
                "guaranteed_tokens": guaranteed_tokens,
                "estimated_net_profit_lamports": net_after_priority,
            },
        )

        settlement = await self._jito.wait_for_bundle(
            bundle_id,
            timeout_seconds=self._settlement_timeout_seconds,
            poll_seconds=self._settlement_poll_seconds,
        )
        settlement_state = str(settlement.get("status") or "Unknown")
        if settlement_state.lower() in {"failed", "invalid", "timeout"}:
            return LiveExecutionResult(
                False,
                bundle_id=bundle_id,
                buy_venue=buy_venue.name,
                sell_venue=sell_venue.name,
                input_lamports=input_lamports,
                guaranteed_token_amount=guaranteed_tokens,
                estimated_net_profit_lamports=net_after_priority,
                reason=f"bundle_{settlement_state.lower()}",
                settlement_status=settlement_state,
                transaction_signatures=tuple(
                    str(sig) for sig in (settlement.get("transactions") or [])
                ),
            )

        reconciled, reason, signatures = await self._reconcile_landed_bundle(
            rpc_url, settlement
        )
        if not reconciled:
            return LiveExecutionResult(
                False,
                bundle_id=bundle_id,
                buy_venue=buy_venue.name,
                sell_venue=sell_venue.name,
                input_lamports=input_lamports,
                guaranteed_token_amount=guaranteed_tokens,
                estimated_net_profit_lamports=net_after_priority,
                reason=reason,
                settlement_status=settlement_state,
                transaction_signatures=signatures,
            )

        return LiveExecutionResult(
            True,
            bundle_id=bundle_id,
            buy_venue=buy_venue.name,
            sell_venue=sell_venue.name,
            input_lamports=input_lamports,
            guaranteed_token_amount=guaranteed_tokens,
            estimated_net_profit_lamports=net_after_priority,
            reason="settled",
            settlement_status=settlement_state,
            transaction_signatures=signatures,
        )
