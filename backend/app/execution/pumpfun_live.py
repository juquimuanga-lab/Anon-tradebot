"""Live Pump.fun execution adapter.

Separate from the existing Meteora/Jupiter WalletExecutionAdapter.

BUY:
    Pump.fun SDK -> unsigned transaction -> Python signs -> Helius RPC

SELL:
    Pump.fun SDK -> unsigned transaction -> Python signs -> Helius RPC

Security:
- Private key never leaves Python.
- Private key is never passed to Node.js.
- Node.js only constructs unsigned transactions.
- Pump.fun positions never fall through to Meteora/Jupiter.
"""

import logging
from decimal import Decimal, ROUND_DOWN

from solders.keypair import Keypair

from app.execution.base import (
    ExecutionAdapter,
    OrderResult,
)

from app.execution.onchain.pumpfun import (
    PumpFunError,
    PumpFunTransactionBuildError,
    build_unsigned_buy_transaction,
    build_unsigned_sell_transaction,
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
    """Return True when a transaction failed because its blockhash expired."""

    text = str(
        exc
    ).lower()

    normalized = text.replace(
        " ",
        "",
    )

    stale_markers = (
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
        "blockhash not found" in text
        or
        "block height exceeded" in text
        or
        "transaction expired" in text
    )


def _token_amount_to_raw(
    amount_tokens: float,
    decimals: int,
) -> int:
    """Convert human-readable SPL token amount to exact raw units.

    Decimal is used instead of floating-point multiplication so we don't
    accidentally create an invalid raw token amount.
    """

    if amount_tokens <= 0:
        raise ValueError(
            "token amount must be greater than zero"
        )

    if decimals < 0 or decimals > 18:
        raise ValueError(
            f"invalid token decimals: {decimals}"
        )

    multiplier = Decimal(
        10
    ) ** Decimal(
        decimals
    )

    raw = (
        Decimal(
            str(amount_tokens)
        )
        * multiplier
    ).quantize(
        Decimal("1"),
        rounding=ROUND_DOWN,
    )

    raw_int = int(
        raw
    )

    if raw_int <= 0:
        raise ValueError(
            "token amount is too small for token decimals"
        )

    return raw_int


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
        """Return current wallet SOL balance."""

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

        max_attempts = 3

        for attempt in range(
            max_attempts
        ):

            try:

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

                signed = (
                    sign_legacy_transaction(
                        transaction_b64,
                        blockhash,
                        self._keypair,
                    )
                )

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
        """Sell a Pump.fun bonding-curve position."""

        try:

            amount_tokens_float = float(
                amount_tokens
            )

        except (
            TypeError,
            ValueError,
        ) as exc:

            return OrderResult(
                success=False,
                status="failed",
                error_message=(
                    "invalid token sell amount: "
                    f"{exc}"
                ),
            )

        if amount_tokens_float <= 0:

            return OrderResult(
                success=False,
                status="failed",
                error_message=(
                    "token sell amount must be "
                    "greater than zero"
                ),
            )

        try:

            sell_pct_float = float(
                sell_pct
            )

        except (
            TypeError,
            ValueError,
        ) as exc:

            return OrderResult(
                success=False,
                status="failed",
                error_message=(
                    "invalid sell percentage: "
                    f"{exc}"
                ),
            )

        if (
            sell_pct_float <= 0
            or sell_pct_float > 100
        ):

            return OrderResult(
                success=False,
                status="failed",
                error_message=(
                    "sell percentage must be "
                    "greater than 0 and no more than 100"
                ),
            )

        # --------------------------------------------------------------
        # amount_tokens is the current amount being considered by the
        # position manager. sell_pct determines how much of that amount
        # should actually be sold.
        # --------------------------------------------------------------

        tokens_to_sell = (
            amount_tokens_float
            * (
                sell_pct_float
                / 100.0
            )
        )

        if tokens_to_sell <= 0:

            return OrderResult(
                success=False,
                status="failed",
                error_message=(
                    "calculated Pump.fun sell amount "
                    "is zero"
                ),
            )

        try:

            decimals = int(
                token.decimals
            )

            amount_tokens_raw = (
                _token_amount_to_raw(
                    tokens_to_sell,
                    decimals,
                )
            )

        except (
            TypeError,
            ValueError,
        ) as exc:

            return OrderResult(
                success=False,
                status="failed",
                error_message=(
                    f"invalid Pump.fun token amount: {exc}"
                ),
            )

        slippage_bps = (
            self._default_slippage_bps
        )

        max_attempts = 3

        for attempt in range(
            max_attempts
        ):

            try:

                # ------------------------------------------------------
                # Build a completely fresh SELL transaction.
                # ------------------------------------------------------

                built = (
                    await build_unsigned_sell_transaction(
                        mint=token.mint,
                        owner_pubkey=self._pubkey,
                        amount_tokens_raw=(
                            amount_tokens_raw
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
                # ------------------------------------------------------

                signature = (
                    await send_and_confirm(
                        self._rpc_url,
                        signed,
                        last_valid_block_height,
                    )
                )

                logger.info(
                    "pumpfun_sell_confirmed",
                    extra={
                        "mint": token.mint,
                        "owner": self._pubkey,
                        "amount_tokens_requested": (
                            amount_tokens_float
                        ),
                        "sell_pct": (
                            sell_pct_float
                        ),
                        "amount_tokens_sold": (
                            tokens_to_sell
                        ),
                        "amount_tokens_raw": (
                            amount_tokens_raw
                        ),
                        "token_decimals": (
                            decimals
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
                        "expected_sol_lamports": (
                            built.get(
                                "expected_sol_lamports"
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

                if (
                    attempt
                    < max_attempts - 1
                    and _is_stale_blockhash_error(
                        exc
                    )
                ):

                    logger.warning(
                        "pumpfun_sell_retrying_fresh_blockhash",
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
                    "pumpfun_sell_failed",
                    extra={
                        "mint": token.mint,
                        "amount_tokens_sold": (
                            tokens_to_sell
                        ),
                        "amount_tokens_raw": (
                            amount_tokens_raw
                        ),
                        "sell_pct": (
                            sell_pct_float
                        ),
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
                    "pumpfun_sell_unexpected_error",
                    extra={
                        "mint": token.mint,
                        "amount_tokens_sold": (
                            tokens_to_sell
                        ),
                    },
                )

                return OrderResult(
                    success=False,
                    status="failed",
                    error_message=(
                        "unexpected Pump.fun "
                        f"sell execution error: {exc}"
                    ),
                )

        return OrderResult(
            success=False,
            status="failed",
            error_message=(
                "Pump.fun sell failed after "
                f"{max_attempts} attempts"
            ),
        )
