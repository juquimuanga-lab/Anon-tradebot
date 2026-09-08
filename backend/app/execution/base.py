"""Execution adapter contract. Trade execution is isolated here so the real
Anoncoin trade endpoint (not yet published) can be plugged in later without
touching scanning, scoring or Telegram control code."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from app.scoring.rules import TokenSnapshot


@dataclass
class OrderResult:
    success: bool
    status: str  # filled | failed | pending
    price_usd: float = 0.0
    tx_signature: Optional[str] = None
    error_message: Optional[str] = None


class ExecutionNotAvailableError(Exception):
    pass


class ExecutionAdapter(ABC):
    mode: str

    @abstractmethod
    async def buy(self, token: TokenSnapshot, amount_sol: float) -> OrderResult:
        ...

    @abstractmethod
    async def sell(self, token: TokenSnapshot, amount_tokens: float, sell_pct: float) -> OrderResult:
        ...

    async def cleanup_closed_token_accounts(
        self,
        token: TokenSnapshot | str,
        dust_threshold_tokens: float = 10.0,
    ) -> dict:
        """Recover rent from empty/dust SPL token accounts after a full close.

        The concrete Solana adapters keep the wallet keypair and RPC URL on
        the adapter instance. Using those existing credentials here avoids
        duplicating cleanup logic in each launch-source adapter and also lets
        the position manager pass either a TokenSnapshot or a mint string.

        Cleanup is maintenance only: callers should treat failures as
        non-fatal to the already-completed trade.
        """
        keypair = getattr(self, "_keypair", None)
        rpc_url = getattr(self, "_rpc_url", None)
        mint = token.mint if isinstance(token, TokenSnapshot) else str(token)

        if keypair is None or not rpc_url:
            return {
                "accounts": 0,
                "closed": 0,
                "burned": 0,
                "error": "adapter_wallet_credentials_unavailable",
            }

        from app.execution.onchain.token_account_cleanup import (
            cleanup_token_accounts,
        )

        return await cleanup_token_accounts(
            rpc_url,
            keypair,
            mint,
            dust_threshold_tokens,
        )
