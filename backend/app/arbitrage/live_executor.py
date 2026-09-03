"""Gated live Solana arbitrage execution with transaction-level safety checks."""
from __future__ import annotations

import base64
import logging
import os
import random
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair

from app.arbitrage.fee_model import (
    DEFAULT_BASE_FEE_LAMPORTS_PER_SIGNATURE,
    MIN_JITO_TIP_LAMPORTS,
    calculate_profitability,
    max_affordable_jito_tip,
)
from app.arbitrage.jupiter_bundle import (
    JupiterInstructionBuildError,
    build_signed_swap_with_tip,
    build_signed_swap_without_tip,
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
DEFAULT_JITO_TIP_FLOOR_URL = "https://bundles.jito.wtf/api/v1/bundles/tip_floor"
DEFAULT_BUNDLE_BASE_FEE_LAMPORTS = DEFAULT_BASE_FEE_LAMPORTS_PER_SIGNATURE * 2


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
    priority_fee_lamports: int = 0
    jito_tip_lamports: int = 0
    base_fee_lamports: int = 0


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
        self._max_trade_lamports = int(
            Decimal(os.getenv("ARBITRAGE_LIVE_MAX_SOL", "0.10")) * LAMPORTS_PER_SOL
        )
        self._reserve_lamports = int(
            Decimal(os.getenv("ARBITRAGE_LIVE_RESERVE_SOL", "0.02")) * LAMPORTS_PER_SOL
        )
        self._slippage_bps = max(
            1, min(int(os.getenv("ARBITRAGE_LIVE_SLIPPAGE_BPS", "30")), 300)
        )
        self._fallback_tip_lamports = max(
            MIN_JITO_TIP_LAMPORTS,
            int(os.getenv("ARBITRAGE_LIVE_JITO_FALLBACK_TIP_LAMPORTS", "100000")),
        )
        try:
            percentile = int(os.getenv("ARBITRAGE_LIVE_JITO_TIP_PERCENTILE", "50"))
        except ValueError:
            percentile = 50
        self._tip_percentile = min(99, max(25, percentile))
        try:
            multiplier = float(os.getenv("ARBITRAGE_LIVE_JITO_TIP_MULTIPLIER", "1.0"))
        except ValueError:
            multiplier = 1.0
        self._tip_multiplier = max(0.1, min(multiplier, 3.0))
        self._tip_floor_url = os.getenv(
            "ARBITRAGE_LIVE_JITO_TIP_FLOOR_URL", DEFAULT_JITO_TIP_FLOOR_URL
        )
        self._tip_cache_ttl_seconds = max(
            1.0, float(os.getenv("ARBITRAGE_LIVE_JITO_TIP_CACHE_SECONDS", "5"))
        )
        self._cached_tip_lamports = 0
        self._cached_tip_at = 0.0
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

    async def _wallet_balance(self, rpc_url: str, owner: Keypair) -> int:
        try:
            async with AsyncClient(rpc_url) as rpc:
                result = await rpc.get_balance(owner.pubkey(), commitment="confirmed")
            return int(result.value)
        except Exception as exc:
            raise ArbitrageLiveExecutionError("unable to verify wallet SOL balance") from exc

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
            "restrictIntermediateTokens": "true",
        }
        if venue.jupiter_dex_label:
            params["dexes"] = venue.jupiter_dex_label
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

    async def _dynamic_jito_tip_lamports(self) -> int:
        """Read a recent Jito landed-tip percentile, with a safe local fallback."""
        now = time.monotonic()
        if self._cached_tip_lamports >= MIN_JITO_TIP_LAMPORTS and (
            now - self._cached_tip_at < self._tip_cache_ttl_seconds
        ):
            return self._cached_tip_lamports

        try:
            values = await self._jito.get_tip_floor(self._tip_floor_url)
            field = f"landed_tips_{self._tip_percentile}th_percentile"
            raw_value = values.get(field)
            if raw_value is None:
                raise ArbitrageLiveExecutionError(f"Jito tip floor missing {field}")
            # Jito's REST response reports tip amounts in SOL.
            market_tip = int(float(raw_value) * LAMPORTS_PER_SOL)
            market_tip = max(MIN_JITO_TIP_LAMPORTS, market_tip)
            market_tip = int(market_tip * self._tip_multiplier)
            self._cached_tip_lamports = market_tip
            self._cached_tip_at = now
            return market_tip
        except Exception as exc:
            logger.warning(
                "jito_tip_floor_unavailable_using_fallback",
                extra={"error": str(exc), "fallback_lamports": self._fallback_tip_lamports},
            )
            return self._fallback_tip_lamports

    async def execute_unrestricted(
        self,
        owner_user_id: int,
        token_mint: str,
        amount_sol: float,
    ) -> LiveExecutionResult:
        """Execute a freshly re-quoted unrestricted Jupiter round-trip.

        Used by the global arbitrage hunter. Discovery chooses the candidate
        and size; this method re-quotes both legs without restricting Jupiter
        to the configured venue list before any transaction is signed.
        """
        unrestricted = VenueConfig("jupiter_best_route", "", 0.0)
        return await self.execute(
            owner_user_id=owner_user_id,
            token_mint=token_mint,
            amount_sol=amount_sol,
            buy_venue=unrestricted,
            sell_venue=unrestricted,
        )

    @staticmethod
    def _positive_int(payload: dict[str, Any], field: str, label: str) -> int:
        try:
            value = int(payload.get(field) or 0)
        except (TypeError, ValueError) as exc:
            raise ArbitrageLiveExecutionError(f"{label} has invalid {field}") from exc
        if value <= 0:
            raise ArbitrageLiveExecutionError(f"{label} has no positive {field}")
        return value

    async def _simulate(self, rpc_url: str, signed_tx: bytes) -> None:
        """Simulate a signed leg before submission."""
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
        bundle_error = bundle_status.get("err")
        if bundle_error not in (None, {"Ok": None}):
            return False, f"bundle_error:{bundle_error}", tuple(
                str(sig) for sig in (bundle_status.get("transactions") or [])
            )

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
        if len(signatures) != 2 or any(not sig for sig in signatures):
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
        try:
            amount_decimal = Decimal(str(amount_sol))
        except (InvalidOperation, ValueError) as exc:
            raise ArbitrageLiveExecutionError("amount_sol is not a valid decimal") from exc
        input_lamports = int(amount_decimal * LAMPORTS_PER_SOL)
        if input_lamports <= 0:
            return LiveExecutionResult(False, reason="amount_too_small")
        if input_lamports > self._max_trade_lamports:
            return LiveExecutionResult(
                False,
                buy_venue=buy_venue.name,
                sell_venue=sell_venue.name,
                input_lamports=input_lamports,
                reason="live_trade_size_limit_exceeded",
            )

        keypair = await self._wallet(owner_user_id)
        user_pubkey = str(keypair.pubkey())
        rpc_url = settings.solana_rpc_url
        if not rpc_url:
            raise ArbitrageLiveExecutionError("SOLANA_RPC_URL/Helius RPC is not configured")

        balance_lamports = await self._wallet_balance(rpc_url, keypair)
        required_balance = input_lamports + self._fallback_tip_lamports + self._reserve_lamports
        if balance_lamports < required_balance:
            return LiveExecutionResult(
                False,
                buy_venue=buy_venue.name,
                sell_venue=sell_venue.name,
                input_lamports=input_lamports,
                reason="insufficient_wallet_reserve",
            )

        buy_quote = await self._quote(SOL_MINT, token_mint, input_lamports, buy_venue)
        buy_out = self._positive_int(buy_quote, "outAmount", "buy quote")
        guaranteed_tokens = self._positive_int(
            buy_quote, "otherAmountThreshold", "buy quote"
        )
        if guaranteed_tokens > buy_out:
            raise ArbitrageLiveExecutionError("buy quote minimum output exceeds quoted output")

        sell_quote = await self._quote(
            token_mint, SOL_MINT, guaranteed_tokens, sell_venue
        )
        sell_out = self._positive_int(sell_quote, "outAmount", "sell quote")
        guaranteed_sol = self._positive_int(
            sell_quote, "otherAmountThreshold", "sell quote"
        )
        if guaranteed_sol > sell_out:
            raise ArbitrageLiveExecutionError("sell quote minimum output exceeds quoted output")

        base_profit = calculate_profitability(
            input_atomic=input_lamports,
            final_output_atomic=guaranteed_sol,
            venue_cost_atomic_value=0,
            base_fee_atomic=DEFAULT_BUNDLE_BASE_FEE_LAMPORTS,
        )

        # Build both legs without a tip first. This gives us actual Jupiter
        # priority-fee estimates before deciding how much auction budget the
        # opportunity can afford.
        try:
            tip_accounts = await self._jito.get_tip_accounts()
            if not tip_accounts:
                raise ArbitrageLiveExecutionError("Jito returned no tip accounts")
            tip_account = random.choice(tip_accounts)
            buy_signed, buy_priority = await build_signed_swap_without_tip(
                base_url=settings.jupiter_base_url,
                quote_response=buy_quote,
                user_pubkey=user_pubkey,
                keypair=keypair,
                rpc_url=rpc_url,
                api_key=os.getenv("JUPITER_API_KEY"),
            )
            _, sell_priority_estimate = await build_signed_swap_without_tip(
                base_url=settings.jupiter_base_url,
                quote_response=sell_quote,
                user_pubkey=user_pubkey,
                keypair=keypair,
                rpc_url=rpc_url,
                api_key=os.getenv("JUPITER_API_KEY"),
            )
        except JupiterInstructionBuildError as exc:
            raise ArbitrageLiveExecutionError(str(exc)) from exc

        priority_estimate = buy_priority + sell_priority_estimate
        max_tip = max_affordable_jito_tip(
            gross_profit_atomic=base_profit.gross_profit_atomic,
            venue_cost_atomic_value=base_profit.venue_cost_atomic,
            base_fee_atomic=base_profit.base_fee_atomic,
            priority_fee_atomic=priority_estimate,
        )
        if max_tip < MIN_JITO_TIP_LAMPORTS:
            return LiveExecutionResult(
                False,
                buy_venue=buy_venue.name,
                sell_venue=sell_venue.name,
                input_lamports=input_lamports,
                guaranteed_token_amount=guaranteed_tokens,
                estimated_net_profit_lamports=base_profit.gross_profit_atomic - base_profit.base_fee_atomic - priority_estimate,
                reason="jito_tip_profit_gate_failed",
                priority_fee_lamports=priority_estimate,
                base_fee_lamports=DEFAULT_BUNDLE_BASE_FEE_LAMPORTS,
                jito_tip_lamports=0,
            )

        market_tip = await self._dynamic_jito_tip_lamports()
        selected_tip = min(market_tip, max_tip)
        if selected_tip < MIN_JITO_TIP_LAMPORTS:
            return LiveExecutionResult(
                False,
                buy_venue=buy_venue.name,
                sell_venue=sell_venue.name,
                input_lamports=input_lamports,
                guaranteed_token_amount=guaranteed_tokens,
                estimated_net_profit_lamports=base_profit.gross_profit_atomic - base_profit.base_fee_atomic - priority_estimate,
                reason="jito_tip_market_too_expensive",
                priority_fee_lamports=priority_estimate,
                base_fee_lamports=DEFAULT_BUNDLE_BASE_FEE_LAMPORTS,
                jito_tip_lamports=market_tip,
            )

        required_balance = (
            input_lamports
            + selected_tip
            + priority_estimate
            + DEFAULT_BUNDLE_BASE_FEE_LAMPORTS
            + self._reserve_lamports
        )
        if balance_lamports < required_balance:
            return LiveExecutionResult(
                False,
                buy_venue=buy_venue.name,
                sell_venue=sell_venue.name,
                input_lamports=input_lamports,
                guaranteed_token_amount=guaranteed_tokens,
                estimated_net_profit_lamports=base_profit.gross_profit_atomic - base_profit.base_fee_atomic - priority_estimate - selected_tip,
                reason="insufficient_dynamic_tip_reserve",
                priority_fee_lamports=priority_estimate,
                base_fee_lamports=DEFAULT_BUNDLE_BASE_FEE_LAMPORTS,
                jito_tip_lamports=selected_tip,
            )

        try:
            sell_signed, sell_priority = await build_signed_swap_with_tip(
                base_url=settings.jupiter_base_url,
                quote_response=sell_quote,
                user_pubkey=user_pubkey,
                keypair=keypair,
                rpc_url=rpc_url,
                tip_account=tip_account,
                tip_lamports=selected_tip,
                api_key=os.getenv("JUPITER_API_KEY"),
            )
        except JupiterInstructionBuildError as exc:
            raise ArbitrageLiveExecutionError(str(exc)) from exc

        final_priority = buy_priority + sell_priority
        final_profit = calculate_profitability(
            input_atomic=input_lamports,
            final_output_atomic=guaranteed_sol,
            venue_cost_atomic_value=0,
            base_fee_atomic=DEFAULT_BUNDLE_BASE_FEE_LAMPORTS,
            priority_fee_atomic=final_priority,
            jito_tip_atomic=selected_tip,
        )
        if final_profit.net_profit_atomic <= 0:
            return LiveExecutionResult(
                False,
                buy_venue=buy_venue.name,
                sell_venue=sell_venue.name,
                input_lamports=input_lamports,
                guaranteed_token_amount=guaranteed_tokens,
                estimated_net_profit_lamports=final_profit.net_profit_atomic,
                reason="final_profit_gate_failed",
                priority_fee_lamports=final_priority,
                base_fee_lamports=DEFAULT_BUNDLE_BASE_FEE_LAMPORTS,
                jito_tip_lamports=selected_tip,
            )

        required_balance = (
            input_lamports
            + selected_tip
            + final_priority
            + DEFAULT_BUNDLE_BASE_FEE_LAMPORTS
            + self._reserve_lamports
        )
        if balance_lamports < required_balance:
            return LiveExecutionResult(
                False,
                buy_venue=buy_venue.name,
                sell_venue=sell_venue.name,
                input_lamports=input_lamports,
                guaranteed_token_amount=guaranteed_tokens,
                estimated_net_profit_lamports=final_profit.net_profit_atomic,
                reason="insufficient_final_fee_reserve",
                priority_fee_lamports=final_priority,
                base_fee_lamports=DEFAULT_BUNDLE_BASE_FEE_LAMPORTS,
                jito_tip_lamports=selected_tip,
            )

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
                "guaranteed_sol_output": guaranteed_sol,
                "estimated_net_profit_lamports": final_profit.net_profit_atomic,
                "base_fee_lamports": DEFAULT_BUNDLE_BASE_FEE_LAMPORTS,
                "priority_fee_lamports": final_priority,
                "jito_tip_lamports": selected_tip,
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
                estimated_net_profit_lamports=final_profit.net_profit_atomic,
                reason=f"bundle_{settlement_state.lower()}",
                settlement_status=settlement_state,
                transaction_signatures=tuple(
                    str(sig) for sig in (settlement.get("transactions") or [])
                ),
                priority_fee_lamports=final_priority,
                base_fee_lamports=DEFAULT_BUNDLE_BASE_FEE_LAMPORTS,
                jito_tip_lamports=selected_tip,
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
                estimated_net_profit_lamports=final_profit.net_profit_atomic,
                reason=reason,
                settlement_status=settlement_state,
                transaction_signatures=signatures,
                priority_fee_lamports=final_priority,
                base_fee_lamports=DEFAULT_BUNDLE_BASE_FEE_LAMPORTS,
                jito_tip_lamports=selected_tip,
            )

        return LiveExecutionResult(
            True,
            bundle_id=bundle_id,
            buy_venue=buy_venue.name,
            sell_venue=sell_venue.name,
            input_lamports=input_lamports,
            guaranteed_token_amount=guaranteed_tokens,
            estimated_net_profit_lamports=final_profit.net_profit_atomic,
            reason="settled",
            settlement_status=settlement_state,
            transaction_signatures=signatures,
            priority_fee_lamports=final_priority,
            base_fee_lamports=DEFAULT_BUNDLE_BASE_FEE_LAMPORTS,
            jito_tip_lamports=selected_tip,
        )
