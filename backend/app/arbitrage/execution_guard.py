"""Safety policy for Jito arbitrage bundle construction.

The arbitrage executor must never submit a standalone Jito tip transaction.
Jito recommends putting the tip instruction in the same transaction as the
MEV strategy, or using conditional state assertions if a separate tip is
unavoidable. Until the Jupiter instruction-based transaction builder is used,
this guard rejects the old three-transaction layout.
"""
from __future__ import annotations


class UnsafeArbitrageBundle(ValueError):
    """Raised when a bundle layout can expose a standalone tip payment."""


def validate_tip_layout(transaction_count: int, tip_is_embedded: bool) -> None:
    """Require the Jito tip to be embedded in an arbitrage transaction."""
    if transaction_count < 1:
        raise UnsafeArbitrageBundle("arbitrage bundle must contain a transaction")
    if not tip_is_embedded:
        raise UnsafeArbitrageBundle(
            "standalone Jito tip is disabled; embed the tip instruction in the arbitrage transaction"
        )
