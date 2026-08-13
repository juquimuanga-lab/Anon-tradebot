"""
Resolves the correct execution adapter for a trade.

Execution sources:

    Anoncoin/Meteora
        -> existing WalletExecutionAdapter
           (Meteora DBC pre-migration, Jupiter post-migration)

    Pump.fun
        -> PumpFunExecutionAdapter
           (bonding curve pre-migration, Jupiter post-migration)

Paper mode:
    -> PaperExecutionAdapter

The router keeps the two live execution paths completely separate.
A Pump.fun token must never accidentally fall through into the
Anoncoin/Meteora executor. Both adapters share the same Jupiter
client for their post-migration leg, since Jupiter is origin-agnostic
once a token is trading on an AMM.
"""

import logging
from typing import Optional

from app.config.settings import settings

from app.execution.base import (
    ExecutionAdapter,
)

from app.execution.onchain.jupiter import (
    JupiterClient,
)

from app.execution.onchain.wallet_keys import (
    InvalidWalletKeyError,
    load_keypair,
)

from app.execution.paper import (
    PaperExecutionAdapter,
)

from app.execution.pumpfun_live import (
    PumpFunExecutionAdapter,
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


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class ExecutionRouter:
    """
    Select the correct execution adapter.

    Anoncoin:
        Meteora DBC for pre-migration tokens.
        Jupiter for migrated tokens.

    Pump.fun:
        Dedicated Pump.fun bonding-curve executor for pre-migration
        tokens. Jupiter for migrated (graduated) tokens - the
        adapter checks each trade's on-chain migration status
        itself and routes accordingly.

    Paper:
        Paper execution regardless of launch source.
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

    # ------------------------------------------------------------------
    # Wallet loading
    # ------------------------------------------------------------------

    async def _load_wallet_adapter(
        self,
        owner_user_id: Optional[int],
        source: str,
    ) -> Optional[object]:
        """
        Load the wallet belonging to the Telegram/admin rule owner.

        Returns None when a usable wallet is unavailable.

        The private key remains inside Python and is never passed to
        the Node transaction builder.
        """

        if owner_user_id is None:

            return None

        raw_key = (
            await secrets_manager
            .get_wallet_private_key(
                owner_user_id
            )
        )

        if not raw_key:

            return None

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
                    "source": source,
                },
            )

            return None

        return keypair

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
                Dedicated Pump.fun bonding-curve execution,
                Jupiter after the token migrates.

            SOURCE_MOCK
                Never allowed to execute live.
        """

        # --------------------------------------------------------------
        # PAPER MODE
        # --------------------------------------------------------------

        if mode == "paper":

            return self._paper_adapter


        # --------------------------------------------------------------
        # Validate source
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
                    "owner_user_id": (
                        owner_user_id
                    ),
                },
            )

            return NoWalletConnectedAdapter(
                (
                    "Unsupported execution source: "
                    f"{source}"
                )
            )


        # --------------------------------------------------------------
        # Mock tokens
        #
        # Never allow simulated tokens to reach a live wallet.
        # --------------------------------------------------------------

        if source == SOURCE_MOCK:

            logger.warning(
                "mock_live_execution_blocked",
                extra={
                    "owner_user_id": (
                        owner_user_id
                    ),
                },
            )

            return NoWalletConnectedAdapter(
                (
                    "Simulated/mock tokens cannot "
                    "be executed in live mode."
                )
            )


        # --------------------------------------------------------------
        # Both real sources require a wallet.
        # --------------------------------------------------------------

        if owner_user_id is None:

            return NoWalletConnectedAdapter(
                (
                    "No wallet owner is associated "
                    "with this trade."
                )
            )


        # --------------------------------------------------------------
        # Load admin wallet.
        #
        # The same connected wallet used by the Telegram admin is used
        # for both Anoncoin and Pump.fun.
        # --------------------------------------------------------------

        keypair = (
            await self._load_wallet_adapter(
                owner_user_id,
                source,
            )
        )


        if keypair is None:

            return NoWalletConnectedAdapter(
                (
                    "Live trading needs a connected "
                    "wallet. The admin who owns this "
                    "rule should run /connectwallet first."
                )
            )


        # --------------------------------------------------------------
        # PUMP.FUN
        #
        # IMPORTANT:
        #
        # This branch happens before the Anoncoin/Meteora adapter.
        #
        # Therefore a Pump.fun token can NEVER accidentally be sent
        # through Meteora DBC/Jupiter.
        # --------------------------------------------------------------

        if source == SOURCE_PUMPFUN:

            logger.info(
                "pumpfun_execution_adapter_selected",
                extra={
                    "owner_user_id": (
                        owner_user_id
                    ),
                    "source": source,
                    "wallet": str(
                        keypair.pubkey()
                    ),
                },
            )

            return PumpFunExecutionAdapter(
                keypair=keypair,
                rpc_url=(
                    settings.solana_rpc_url
                ),
                default_slippage_bps=(
                    settings.default_slippage_bps
                ),
                jupiter_client=(
                    self._jupiter
                ),
            )


        # --------------------------------------------------------------
        # ANONCOIN / METEORA
        # --------------------------------------------------------------

        if source == SOURCE_ANONCOIN:

            logger.debug(
                "anoncoin_wallet_execution_adapter_selected",
                extra={
                    "owner_user_id": (
                        owner_user_id
                    ),
                    "source": source,
                    "wallet": str(
                        keypair.pubkey()
                    ),
                },
            )

            return WalletExecutionAdapter(
                keypair,
                settings.solana_rpc_url,
                self._jupiter,
                settings.default_slippage_bps,
            )


        # --------------------------------------------------------------
        # Defensive fallback
        # --------------------------------------------------------------

        return NoWalletConnectedAdapter(
            (
                "No execution adapter is available "
                f"for source: {source}"
            )
        )
