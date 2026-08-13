"""Live Pump.fun execution adapter.

Separate from the existing Meteora/Jupiter WalletExecutionAdapter,
except for one shared piece: once a Pump.fun token graduates off the
bonding curve, this adapter routes through the same Jupiter client
WalletExecutionAdapter already uses for migrated Anoncoin tokens.
Jupiter doesn't care which platform a token launched on.

BUY:
    Pre-migration: Pump.fun SDK -> unsigned transaction -> Python signs -> Helius RPC

SELL:
    Pre-migration: Pump.fun SDK -> unsigned transaction -> Python signs -> Helius RPC
    Post-migration: Jupiter -> unsigned transaction -> Python signs -> Helius RPC

Security:
- Private key never leaves Python.
- Private key is never passed to Node.js.
- Node.js only constructs unsigned transactions.
- Pump.fun positions never fall through to Meteora DBC (Anoncoin's
  bonding curve program) - only to Jupiter, and only post-migration.
"""

import logging
from decimal import Decimal, ROUND_DOWN

from solders.keypair import Keypair

from app.execution.base import (
    ExecutionAdapter,
    OrderResult,
)

from app.execution.onchain.jupiter import (
    JupiterClient,
    JupiterError,
)

from app.execution.onchain.pumpfun import (
    PumpFunError,
    PumpFunTransactionBuildError,
    build_unsigned_buy_transaction,
    build_unsigned_sell_transaction,
    get_pool_info,
)

from app.execution.onchain.solana_rpc import (
    SolanaTxError,
    get_sol_balance,
    send_and_confirm,
    sign_legacy_transaction,
    sign_versioned_transaction,
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
        jupiter_client: JupiterClient,
    ):
        self._keypair = keypair

        self._pubkey = str(
            keypair.pubkey()
        )

        self._rpc_url = rpc_url

        self._default_slippage_bps = int(
            default_slippage_bps
        )

        self._jupiter = jupiter_client

    # ------------------------------------------------------------------
    # Migration check
    # ------------------------------------------------------------------

    async def _is_migrated(
        self,
        token: TokenSnapshot,
    ) -> bool:
        """Authoritatively check whether this token has graduated.

        Reads the bonding curve directly rather than trusting
        token.is_migrated on the snapshot passed in, since that flag
        isn't refreshed while a position sits open - a token can
        graduate at any point while the bot is holding it, and only
        the on-chain state at the moment of the trade can say whether
        THIS particular buy/sell needs to go through the bonding
        curve or through an AMM instead.
        """

        pool_info = (
            await get_pool_info(
                token.mint,
                self._rpc_url,
            )
        )

        return bool(
            pool_info.get(
                "is_migrated"
            )
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
        # amount_tokens is ALREADY the exact amount the position manager
        # wants sold - it is computed upstream as
        # position.amount_tokens * (sell_pct / 100), so sell_pct must
        # NOT be re-applied here.
        #
        # sell_pct is accepted purely for validation/logging context
        # (it must be a valid 0-100 percentage), matching the contract
        # every other execution adapter (wallet_live, paper) follows:
        # amount_tokens is the final amount, full stop.
        #
        # Previously this multiplied by sell_pct a second time, which
        # silently undersold any exit where sell_pct < 100 - e.g. a
        # stop loss firing after an earlier partial take-profit had
        # already reduced the position's remaining_pct below 100.
        # --------------------------------------------------------------

        tokens_to_sell = amount_tokens_float

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
                # Route around the bonding curve once this token has
                # graduated to an AMM.
                #
                # Checked fresh on every attempt rather than trusted
                # from the position's cached snapshot, because the
                # token can graduate at any point while the bot holds
                # it and there's no way to know in advance which exit
                # attempt will be the one that catches it. Building a
                # bonding-curve sell against an already-graduated
                # token fails on-chain with AccountNotInitialized:
                # migration withdraws the bonding curve's token
                # reserves, so the account the old instruction still
                # expects to find funded no longer holds anything.
                # ------------------------------------------------------

                is_migrated = (
                    await self._is_migrated(
                        token
                    )
                )

                if is_migrated:

                    # --------------------------------------------------
                    # Graduated: route through Jupiter, the same way
                    # WalletExecutionAdapter already does for migrated
                    # Anoncoin tokens. Jupiter is origin-agnostic - it
                    # just needs a mint that's live on an AMM and
                    # doesn't care whether that token started on
                    # Pump.fun or anywhere else.
                    # --------------------------------------------------

                    built = (
                        await self._jupiter.sell_quote_tx(
                            token.mint,
                            amount_tokens_raw,
                            slippage_bps,
                            self._pubkey,
                        )
                    )

                    signed = (
                        sign_versioned_transaction(
                            built[
                                "transaction_b64"
                            ],
                            self._keypair,
                        )
                    )

                    signature = (
                        await send_and_confirm(
                            self._rpc_url,
                            signed,
                        )
                    )

                    logger.info(
                        "pumpfun_sell_confirmed",
                        extra={
                            "mint": token.mint,
                            "owner": self._pubkey,
                            "execution_path": (
                                "jupiter_amm_post_migration"
                            ),
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

                # ------------------------------------------------------
                # Not yet graduated: build a completely fresh
                # bonding-curve SELL transaction.
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
                        "execution_path": (
                            "bonding_curve"
                        ),
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
                        "amount_clamped_to_wallet_balance": (
                            built.get(
                                "amount_clamped",
                                False,
                            )
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
                JupiterError,
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
