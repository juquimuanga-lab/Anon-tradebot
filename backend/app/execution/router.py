"""
Resolves the correct execution adapter for a trade.

Execution sources:

    Anoncoin/Meteora
        -> existing WalletExecutionAdapter

    Pump.fun
        -> Pump.fun adapter
           (will be enabled once pumpfun execution is installed)

Paper mode:
    -> PaperExecutionAdapter

The router deliberately fails closed for Pump.fun until the dedicated
Pump.fun execution adapter is available. This prevents Pump.fun tokens
from accidentally being sent through the Anoncoin/Meteora execution path.
"""

import logging
from typing import Optional

from app.config.settings import settings
from app.execution.base import ExecutionAdapter
from app.execution.onchain.jupiter import JupiterClient
from app.execution.onchain.wallet_keys import (
    InvalidWalletKeyError,
    load_keypair,
)
from app.execution.paper import (
    PaperExecutionAdapter,
)
from app.execution.wallet_live import (
    NoWalletConnectedAdapter,
    WalletExecutionAdapter,
)
from app.security.secrets_manager import (
    secrets_manager,
)


logger = logging.getLogger(
    "app.execution.router"
)


# ---------------------------------------------------------------------------
# Launch source constants
# ---------------------------------------------------------------------------

SOURCE_ANONCOIN = (
    "anoncoin_onchain"
)

SOURCE_PUMPFUN = (
    "pumpfun"
)

SOURCE_MOCK = (
    "mock_simulated"
)


class PumpFunExecutionUnavailableAdapter:
    """
    Safe placeholder adapter for Pump.fun.

    Pump.fun execution is intentionally not enabled until the dedicated
    Pump.fun transaction builder/executor has been installed.

    This prevents the router from accidentally sending a Pump.fun token
    through the existing Meteora/Jupiter execution path.
    """

    def __init__(
        self,
        reason: Optional[str] = None,
    ):
        self._reason = (
            reason
            or
            "Pump.fun live execution is not installed yet."
        )

    async def buy(
        self,
        token,
        amount_sol: float,
    ):
        from app.execution.base import (
            OrderResult,
        )

        logger.warning(
            "pumpfun_execution_blocked",
            extra={
                "mint": getattr(
                    token,
                    "mint",
                    None,
                ),
                "reason": self._reason,
            },
        )

        return OrderResult(
            success=False,
            status="failed",
            error_message=(
                self._reason
                + " Pump.fun trades are blocked "
                  "until the dedicated execution adapter "
                  "is enabled."
            ),
        )

    async def sell(
        self,
        token,
        amount_tokens: float,
    ):
        from app.execution.base import (
            OrderResult,
        )

        logger.warning(
            "pumpfun_sell_execution_blocked",
            extra={
                "mint": getattr(
                    token,
                    "mint",
                    None,
                ),
                "reason": self._reason,
            },
        )

        return OrderResult(
            success=False,
            status="failed",
            error_message=(
                self._reason
                + " Pump.fun selling is blocked "
                  "until the dedicated execution adapter "
                  "is enabled."
            ),
        )


class ExecutionRouter:
    """
    Select the appropriate execution adapter.

    The existing constructor remains compatible with the rest of the
    application.
    """

    def __init__(
        self,
        jupiter_client: JupiterClient,
    ):
        self._paper_adapter = (
            PaperExecutionAdapter()
        )

        self._jupiter = (
            jupiter_client
        )

        # Dedicated Pump.fun execution will be attached here after the
        # Pump.fun transaction builder is implemented.
        self._pumpfun_adapter = (
            PumpFunExecutionUnavailableAdapter()
        )

    # ------------------------------------------------------------------
    # Adapter selection
    # ------------------------------------------------------------------

    async def get_adapter(
        self,
        mode: str,
        owner_user_id: Optional[int],
        source: str = SOURCE_ANONCOIN,
    ) -> ExecutionAdapter:
        """
        Resolve the execution adapter.

        Parameters
        ----------
        mode:
            "paper" or "live"

        owner_user_id:
            Telegram/admin owner of the trading rule.

        source:
            Launch source.

            SOURCE_ANONCOIN
                Existing Meteora/Jupiter wallet execution.

            SOURCE_PUMPFUN
                Dedicated Pump.fun execution.

            SOURCE_MOCK
                Only allowed through paper execution.

        IMPORTANT:
            source defaults to SOURCE_ANONCOIN to preserve compatibility
            with existing callers while the rest of the codebase is being
            migrated to explicit source-aware routing.
        """

        # --------------------------------------------------------------
        # Paper mode
        # --------------------------------------------------------------

        if mode == "paper":

            return self._paper_adapter


        # --------------------------------------------------------------
        # Unknown source
        # --------------------------------------------------------------

        if source not in {
            SOURCE_ANONCOIN,
            SOURCE_PUMPFUN,
            SOURCE_MOCK,
        }:

            logger.error(
                "unknown_execution_source",
                extra={
                    "source": source,
                    "owner_user_id": owner_user_id,
                },
            )

            return NoWalletConnectedAdapter(
                (
                    "Unsupported execution source: "
                    f"{source}"
                )
            )


        # --------------------------------------------------------------
        # Mock tokens must never execute live.
        # --------------------------------------------------------------

        if source == SOURCE_MOCK:

            logger.warning(
                "mock_live_execution_blocked",
                extra={
                    "owner_user_id": owner_user_id,
                },
            )

            return NoWalletConnectedAdapter(
                (
                    "Simulated/mock tokens cannot be "
                    "executed in live mode."
                )
            )


        # --------------------------------------------------------------
        # Pump.fun
        #
        # IMPORTANT:
        #
        # We do this BEFORE loading the wallet because the Pump.fun
        # execution adapter is not installed yet.
        #
        # This guarantees that a Pump.fun token cannot accidentally fall
        # through into WalletExecutionAdapter.
        # --------------------------------------------------------------

        if source == SOURCE_PUMPFUN:

            logger.info(
                "pumpfun_execution_adapter_selected",
                extra={
                    "owner_user_id": owner_user_id,
                    "status": "waiting_for_adapter",
                },
            )

            return (
                self._pumpfun_adapter
            )


        # --------------------------------------------------------------
        # Existing Anoncoin/Meteora path
        # --------------------------------------------------------------

        if owner_user_id is None:

            return NoWalletConnectedAdapter(
                (
                    "No wallet owner is associated "
                    "with this trade."
                )
            )


        # --------------------------------------------------------------
        # Retrieve admin wallet
        # --------------------------------------------------------------

        raw_key = (
            await secrets_manager
            .get_wallet_private_key(
                owner_user_id
            )
        )


        if not raw_key:

            return NoWalletConnectedAdapter(
                (
                    "Live trading needs a connected "
                    "wallet. The admin who owns this "
                    "rule should run /connectwallet first."
                )
            )


        # --------------------------------------------------------------
        # Decode wallet key
        # --------------------------------------------------------------

        try:

            keypair = load_keypair(
                raw_key
            )

        except InvalidWalletKeyError as exc:

            logger.error(
                "stored_wallet_key_invalid",
                extra={
                    "owner_user_id": (
                        owner_user_id
                    ),
                },
            )

            return NoWalletConnectedAdapter(
                (
                    "Stored wallet key is invalid: "
                    f"{exc}"
                )
            )


        # --------------------------------------------------------------
        # Existing Anoncoin/Meteora wallet adapter
        # --------------------------------------------------------------

        logger.debug(
            "anoncoin_wallet_execution_adapter_selected",
            extra={
                "owner_user_id": owner_user_id,
                "source": source,
            },
        )


        return WalletExecutionAdapter(
            keypair,
            settings.solana_rpc_url,
            self._jupiter,
            settings.default_slippage_bps,
        )
