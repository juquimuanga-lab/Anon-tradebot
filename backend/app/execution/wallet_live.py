"""Wallet-based on-chain execution adapter.

Routes pre-graduation tokens through Meteora's Dynamic Bonding Curve (the
program Anoncoin itself builds on, per their own `meteoraConfigKey` field)
and post-graduation (migrated) tokens through the Jupiter aggregator. The
private key lives only in this process's memory for the duration of signing.
"""
import logging

from solders.keypair import Keypair

from app.execution.base import ExecutionAdapter, OrderResult
from app.execution.onchain import meteora_dbc
from app.execution.onchain.jupiter import JupiterClient
from app.execution.onchain.solana_rpc import (
    SolanaTxError,
    get_sol_balance,
    send_and_confirm,
    sign_legacy_transaction,
    sign_versioned_transaction,
)
from app.execution.onchain.meteora_dbc import DbcBuildError
from app.execution.onchain.jupiter import JupiterError, SOL_MINT
from app.scoring.rules import TokenSnapshot

logger = logging.getLogger("app.execution.wallet_live")

LAMPORTS_PER_SOL = 1_000_000_000


def _is_stale_blockhash_error(exc: Exception) -> bool:
    """True for a preflight BlockhashNotFound (or similarly-worded expiry).
    Safe to retry: a preflight rejection means the RPC node refused to even
    broadcast the transaction, so it never reached the network - there's no
    risk of the original later landing after we've already sent a fresh one.
    Common with load-balanced public RPC endpoints where the node that built
    the blockhash and the node that simulates the send aren't in sync."""
    text = str(exc).lower()
    return "blockhashnotfound" in text.replace(" ", "") or "blockhash not found" in text


class WalletExecutionAdapter(ExecutionAdapter):
    mode = "live"

    def __init__(self, keypair: Keypair, rpc_url: str, jupiter_client: JupiterClient, default_slippage_bps: int):
        self._keypair = keypair
        self._pubkey = str(keypair.pubkey())
        self._rpc_url = rpc_url
        self._jupiter = jupiter_client
        self._default_slippage_bps = default_slippage_bps

    async def wallet_balance_sol(self) -> float:
        return await get_sol_balance(self._rpc_url, self._pubkey)

    async def _execute(self, action: str, token: TokenSnapshot, amount_in_base_units: int) -> OrderResult:
        slippage_bps = self._default_slippage_bps
        for attempt in range(3):  # up to 2 retries, only for a stale/unrecognized blockhash - see _is_stale_blockhash_error
            try:
                if not token.is_migrated:
                    built = await meteora_dbc.build_unsigned_swap(
                        action, token.mint, self._pubkey, amount_in_base_units, slippage_bps, self._rpc_url
                    )
                    signed = sign_legacy_transaction(built["transaction_b64"], built["blockhash"], self._keypair)
                    signature = await send_and_confirm(
                        self._rpc_url, signed, built.get("last_valid_block_height")
                    )
                else:
                    if action == "buy":
                        built = await self._jupiter.buy_quote_tx(
                            token.mint, amount_in_base_units, slippage_bps, self._pubkey
                        )
                    else:
                        built = await self._jupiter.sell_quote_tx(
                            token.mint, amount_in_base_units, slippage_bps, self._pubkey
                        )
                    signed = sign_versioned_transaction(built["transaction_b64"], self._keypair)
                    signature = await send_and_confirm(self._rpc_url, signed)

                return OrderResult(success=True, status="filled", price_usd=token.price_usd, tx_signature=signature)
            except (DbcBuildError, JupiterError, SolanaTxError) as exc:
                if attempt < 2 and _is_stale_blockhash_error(exc):
                    logger.warning(
                        "retrying_with_fresh_blockhash",
                        extra={"mint": token.mint, "action": action, "attempt": attempt, "error": str(exc)},
                    )
                    continue
                logger.warning("onchain_execution_failed", extra={"mint": token.mint, "action": action, "error": str(exc)})
                return OrderResult(success=False, status="failed", error_message=str(exc))
            except Exception as exc:  # defensive: never let a swap crash the bot
                logger.exception("onchain_execution_unexpected_error")
                return OrderResult(success=False, status="failed", error_message=f"unexpected error: {exc}")

    async def buy(self, token: TokenSnapshot, amount_sol: float) -> OrderResult:
        amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)
        return await self._execute("buy", token, amount_lamports)

    async def sell(self, token: TokenSnapshot, amount_tokens: float, sell_pct: float) -> OrderResult:
        amount_raw = int(amount_tokens * (10 ** token.decimals))
        return await self._execute("sell", token, amount_raw)


class NoWalletConnectedAdapter(ExecutionAdapter):
    mode = "live"

    def __init__(self, reason: str):
        self._reason = reason

    async def buy(self, token: TokenSnapshot, amount_sol: float) -> OrderResult:
        return OrderResult(success=False, status="failed", error_message=self._reason)

    async def sell(self, token: TokenSnapshot, amount_tokens: float, sell_pct: float) -> OrderResult:
        return OrderResult(success=False, status="failed", error_message=self._reason)
