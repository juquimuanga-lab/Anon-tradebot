"""Live Pump.fun execution adapter.

This adapter is intentionally separate from the existing Meteora/Jupiter
WalletExecutionAdapter.

Flow:

    Scanner
       |
       v
    ExecutionRouter
       |
       v
    PumpFunExecutionAdapter
       |
       v
    pumpfun.build_unsigned_buy_transaction()
       |
       v
    pumpfun_build_tx.js
       |
       v
    unsigned legacy transaction
       |
       v
    Python signs with wallet Keypair
       |
       v
    solana_rpc.send_and_confirm()
       |
       v
    confirmed Solana transaction

Security:

- The private key never leaves Python.
- The private key is never passed to Node.js.
- Node.js only constructs the unsigned transaction.
- The adapter never routes Pump.fun trades through Meteora/Jupiter.
"""

import logging

from solders.keypair import Keypair

from app.execution.base import (
    ExecutionAdapter,
    OrderResult,
)

from app.execution.onchain.pumpfun import (
    PumpFunError,
    PumpFunTransactionBuildError,
    build_unsigned_buy_transaction,
)

from app.execution.onchain.solana_rpc import (
    SolanaTxError,
    get_sol_balance,
    send_and_confirm,
    sign_legacy_transaction,
)

from app.scoring.rules import TokenSnapshot


logger = logging.getLogger(
    "app.execution.pumpfun_live"
)


LAMPORTS_PER_SOL = 1_000_000_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_stale_blockhash_error(
    exc: Exception,
) -> bool:
    """Return True when the transaction failed because its blockhash expired.

    A stale blockhash is safe to recover from by rebuilding the transaction
    with a fresh blockhash.

    We deliberately do NOT retry arbitrary transaction failures because a
    Pump.fun transaction may have actually reached the network and failed
    for a meaningful reason such as:

        - insufficient SOL
        - slippage exceeded
        - bonding curve completed
        - invalid account
        - insufficient token inventory
        - program constraint failure
    """

    text = str(
        exc
    ).lower()

    normalized = (
        text.replace(
            " ",
            "",
        )
    )

    stale_markers = (
        "blockhashnotfound",
        "blockhashnotfound",
        "transactionexpiredblockheight",
        "lastvalidblockheight",
        "blockheightexceeded",
        "blockhashhasexpired",
        "expiredblockhash",
    )

    if any(
        marker in normalized
        for marker in stale_markers
    ):
        return True

    return (
        "blockhash not found"
        in text
        or
        "block height exceeded"
        in text
        or
        "transaction expired"
        in text
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class PumpFunExecutionAdapter(
    ExecutionAdapter
):
    """Live Pump.fun bonding-curve execution adapter."""

    mode = "live"

    def __init__(
        self,
        keypair: Keypair,
        rpc_url: str,
        default_slippage_bps: int,
    ):
        self._keypair = keypair

        self._pubkey = str(
            keypair.pubkey()
        )

        self._rpc_url = rpc_url

        self._default_slippage_bps = int(
            default_slippage_bps
        )

    # ------------------------------------------------------------------
    # Wallet balance
    # ------------------------------------------------------------------

    async def wallet_balance_sol(
        self,
    ) -> float:
        """Return the wallet's current SOL balance."""

        return await get_sol_balance(
            self._rpc_url,
            self._pubkey,
        )

    # ------------------------------------------------------------------
    # BUY
    # ------------------------------------------------------------------

    async def buy(
        self,
        token: TokenSnapshot,
        amount_sol: float,
    ) -> OrderResult:
        """Buy a Pump.fun token using SOL."""

        try:

            amount_sol_float = float(
                amount_sol
            )

        except (
            TypeError,
            ValueError,
        ) as exc:

            return OrderResult(
                success=False,
                status="failed",
                error_message=(
                    "invalid SOL buy amount: "
                    f"{exc}"
                ),
            )

        if amount_sol_float <= 0:

            return OrderResult(
                success=False,
                status="failed",
                error_message=(
                    "SOL buy amount must be "
                    "greater than zero"
                ),
            )

        amount_lamports = int(
            amount_sol_float
            * LAMPORTS_PER_SOL
        )

        if amount_lamports <= 0:

            return OrderResult(
                success=False,
                status="failed",
                error_message=(
                    "SOL buy amount is too small"
                ),
            )

        slippage_bps = (
            self._default_slippage_bps
        )

        # --------------------------------------------------------------
        # Rebuild + resend only when the blockhash becomes stale.
        #
        # We intentionally keep this small because a sniper transaction
        # should not spend a long time retrying a failed trade.
        # --------------------------------------------------------------

        max_attempts = 3

        for attempt in range(
            max_attempts
        ):

            try:

                # ------------------------------------------------------
                # Build a completely fresh transaction.
                #
                # This fetches a fresh blockhash and returns its
                # last_valid_block_height.
                # ------------------------------------------------------

                built = (
                    await build_unsigned_buy_transaction(
                        mint=token.mint,
                        owner_pubkey=self._pubkey,
                        amount_lamports=(
                            amount_lamports
                        ),
                        slippage_bps=(
                            slippage_bps
                        ),
                        rpc_url=self._rpc_url,
                    )
                )

                transaction_b64 = (
                    built[
                        "transaction_b64"
                    ]
                )

                blockhash = (
                    built[
                        "blockhash"
                    ]
                )

                last_valid_block_height = (
                    built[
                        "last_valid_block_height"
                    ]
                )

                # ------------------------------------------------------
                # Sign ONLY in Python.
                # ------------------------------------------------------

                signed = (
                    sign_legacy_transaction(
                        transaction_b64,
                        blockhash,
                        self._keypair,
                    )
                )

                # ------------------------------------------------------
                # Broadcast + confirmation.
                #
                # last_valid_block_height is deliberately passed through.
                # This prevents the confirmation worker from waiting
                # indefinitely for an expired transaction.
                # ------------------------------------------------------

                signature = (
                    await send_and_confirm(
                        self._rpc_url,
                        signed,
                        last_valid_block_height,
                    )
                )

                logger.info(
                    "pumpfun_buy_confirmed",
                    extra={
                        "mint": token.mint,
                        "owner": self._pubkey,
                        "amount_sol": (
                            amount_sol_float
                        ),
                        "amount_lamports": (
                            amount_lamports
                        ),
                        "attempt": attempt + 1,
                        "tx_signature": signature,
                        "blockhash": blockhash,
                        "last_valid_block_height": (
                            last_valid_block_height
                        ),
                        "priority_fee_micro_lamports": (
                            built.get(
                                "priority_fee_micro_lamports"
                            )
                        ),
                        "priority_fee_source": (
                            built.get(
                                "priority_fee_source"
                            )
                        ),
                    },
                )

                return OrderResult(
                    success=True,
                    status="filled",
                    price_usd=(
                        token.price_usd
                    ),
                    tx_signature=signature,
                )

            except (
                PumpFunTransactionBuildError,
                PumpFunError,
                SolanaTxError,
            ) as exc:

                # ------------------------------------------------------
                # Only rebuild/retry when the transaction is stale.
                # ------------------------------------------------------

                if (
                    attempt
                    < max_attempts - 1
                    and _is_stale_blockhash_error(
                        exc
                    )
                ):

                    logger.warning(
                        "pumpfun_retrying_fresh_blockhash",
                        extra={
                            "mint": token.mint,
                            "attempt": (
                                attempt + 1
                            ),
                            "error": str(exc),
                        },
                    )

                    continue

                logger.warning(
                    "pumpfun_buy_failed",
                    extra={
                        "mint": token.mint,
                        "attempt": (
                            attempt + 1
                        ),
                        "error": str(exc),
                    },
                )

                return OrderResult(
                    success=False,
                    status="failed",
                    error_message=str(
                        exc
                    ),
                )

            except Exception as exc:

                logger.exception(
                    "pumpfun_buy_unexpected_error",
                    extra={
                        "mint": token.mint,
                    },
                )

                return OrderResult(
                    success=False,
                    status="failed",
                    error_message=(
                        "unexpected Pump.fun "
                        f"execution error: {exc}"
                    ),
                )

        return OrderResult(
            success=False,
            status="failed",
            error_message=(
                "Pump.fun buy failed after "
                f"{max_attempts} attempts"
            ),
        )

    # ------------------------------------------------------------------
    # SELL
    # ------------------------------------------------------------------

    async def sell(
        self,
        token: TokenSnapshot,
        amount_tokens: float,
        sell_pct: float,
    ) -> OrderResult:
        """Pump.fun selling is not enabled by this adapter yet.

        We intentionally fail closed instead of routing the sell through
        Jupiter or Meteora.

        The Pump.fun sell builder will be added separately once the buy
        path has been verified on-chain.
        """

        logger.warning(
            "pumpfun_sell_not_enabled",
            extra={
                "mint": token.mint,
                "amount_tokens": (
                    amount_tokens
                ),
                "sell_pct": sell_pct,
            },
        )

        return OrderResult(
            success=False,
            status="failed",
            error_message=(
                "Pump.fun selling is not enabled "
                "yet. The token will not be routed "
                "through Meteora/Jupiter."
            ),
  )
