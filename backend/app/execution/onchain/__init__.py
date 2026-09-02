"""Execution/on-chain bootstrap.

The Sender Max integration is additive. Existing transaction construction,
signing, confirmation and fallback behavior remain intact.
"""

from __future__ import annotations

import logging

from solders.pubkey import Pubkey

logger = logging.getLogger("app.execution.onchain")

_SENDER_TIP_ACCOUNTS = (
    "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE",
    "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
    "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta",
    "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
    "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD",
    "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
    "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF",
    "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
    "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey",
    "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or",
)

try:
    from . import pumpfun as _pumpfun
    from . import solana_rpc as _solana_rpc
    from . import sender_max as _sender_max

    # Keep the original Pump.fun SDK builder untouched. The wrapper adds only
    # the Sender tip instruction to the unsigned transaction before the
    # existing Python signing path runs.
    _pumpfun.PUMPFUN_BUILDER_PATH = (
        _pumpfun._DBC_BUILDER_DIR / "pumpfun_sender_build_tx.js"
    )

    _original_send_transaction = _solana_rpc._send_transaction
    _sender_tip_pubkeys = tuple(
        bytes(Pubkey.from_string(account))
        for account in _SENDER_TIP_ACCOUNTS
    )

    async def _sender_aware_send_transaction(
        rpc_url: str,
        signed_tx_bytes: bytes,
    ) -> str:
        """Use Sender first for transactions carrying a Sender tip.

        Non-Pump.fun transactions continue directly through the existing RPC
        path. If Sender rejects or is unavailable, fall back to that same
        existing path so this optimization can never disable trading.
        """
        if _sender_max.enabled():
            try:
                # Sender tip accounts are embedded as account keys in the
                # signed transaction. Checking their raw 32-byte pubkeys keeps
                # this fast-path decision allocation-light and avoids changing
                # the transaction parser used by the core RPC module.
                has_sender_tip = any(
                    pubkey in signed_tx_bytes
                    for pubkey in _sender_tip_pubkeys
                )

                if has_sender_tip:
                    return await _sender_max.send_transaction(
                        signed_tx_bytes
                    )
            except Exception as exc:
                logger.warning(
                    "helius_sender_fallback_to_rpc",
                    extra={
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )

        return await _original_send_transaction(
            rpc_url,
            signed_tx_bytes,
        )

    # send_and_confirm resolves _send_transaction at runtime, so replacing
    # the module function preserves the existing confirmation state machine.
    _solana_rpc._send_transaction = _sender_aware_send_transaction

except Exception:
    # The optimization is optional. Never make the existing execution path
    # unavailable because Sender is misconfigured or temporarily unavailable.
    logger.exception("sender_max_bootstrap_failed")
