"""Resolves the correct execution adapter for a trade: paper mode always uses
the paper adapter; live mode uses the wallet belonging to whichever admin
owns the rule/position (per-admin wallets), or fails safely if that admin
hasn't connected one yet."""
import logging
from typing import Optional

from app.config.settings import settings
from app.execution.base import ExecutionAdapter
from app.execution.onchain.jupiter import JupiterClient
from app.execution.onchain.wallet_keys import InvalidWalletKeyError, load_keypair
from app.execution.paper import PaperExecutionAdapter
from app.execution.wallet_live import NoWalletConnectedAdapter, WalletExecutionAdapter
from app.security.secrets_manager import secrets_manager

logger = logging.getLogger("app.execution.router")


class ExecutionRouter:
    def __init__(self, jupiter_client: JupiterClient):
        self._paper_adapter = PaperExecutionAdapter()
        self._jupiter = jupiter_client

    async def get_adapter(self, mode: str, owner_user_id: Optional[int]) -> ExecutionAdapter:
        if mode == "paper":
            return self._paper_adapter

        if owner_user_id is None:
            return NoWalletConnectedAdapter("No wallet owner is associated with this trade.")

        raw_key = await secrets_manager.get_wallet_private_key(owner_user_id)
        if not raw_key:
            return NoWalletConnectedAdapter(
                "Live trading needs a connected wallet. The admin who owns this rule "
                "should run /connectwallet first."
            )

        try:
            keypair = load_keypair(raw_key)
        except InvalidWalletKeyError as exc:
            logger.error("stored_wallet_key_invalid", extra={"owner_user_id": owner_user_id})
            return NoWalletConnectedAdapter(f"Stored wallet key is invalid: {exc}")

        return WalletExecutionAdapter(keypair, settings.solana_rpc_url, self._jupiter, settings.default_slippage_bps)
