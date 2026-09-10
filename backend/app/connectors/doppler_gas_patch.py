"""Patch the Doppler executor to use EIP-1559 fees on Robinhood Chain.

This module is imported from the scanner bootstrap, which can run while
``app.execution.doppler_live`` is still being initialized. Keep the executor
import lazy so this module cannot create a circular import during startup.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("app.connectors.doppler_gas_patch")

MAX_RETRIES = 20
RETRY_DELAY_SECONDS = 0.5


def _send_eip1559(self, fn, value: int = 0) -> str:
    # Import at call time so the patch itself never participates in the
    # doppler_live module initialization/import graph.
    from app.execution.doppler_live import CHAIN_ID

    chain_id = int(self._w3.eth.chain_id)
    if chain_id != CHAIN_ID:
        raise RuntimeError(
            f"Refusing to sign: RPC chain ID {chain_id} is not Robinhood Chain {CHAIN_ID}"
        )
    if int(self._w3.eth.get_balance(self._account.address)) <= 0:
        raise RuntimeError("Robinhood admin wallet needs ETH for gas")

    nonce = self._w3.eth.get_transaction_count(self._account.address, "pending")
    latest = self._w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas")

    if base_fee is not None:
        base_fee = int(base_fee)
        try:
            priority_fee = max(1, int(self._w3.eth.max_priority_fee or 0))
        except Exception:
            priority_fee = max(1, base_fee // 10)
        max_fee = base_fee * 2 + priority_fee
        tx_params = {
            "from": self._account.address,
            "value": int(value),
            "nonce": nonce,
            "chainId": CHAIN_ID,
            "maxPriorityFeePerGas": priority_fee,
            "maxFeePerGas": max_fee,
            "type": 2,
        }
    else:
        tx_params = {
            "from": self._account.address,
            "value": int(value),
            "nonce": nonce,
            "chainId": CHAIN_ID,
            "gasPrice": int(self._w3.eth.gas_price),
        }

    tx = fn.build_transaction(tx_params)
    tx["gas"] = int(self._w3.eth.estimate_gas(tx) * 1.20)
    signed = self._account.sign_transaction(tx)
    tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
    if receipt.status != 1:
        raise RuntimeError(f"Doppler transaction reverted: {tx_hash.hex()}")
    return tx_hash.hex()


def _apply_patch(attempt: int = 0) -> None:
    try:
        from app.execution.doppler_live import DopplerExecutionAdapter

        if getattr(DopplerExecutionAdapter, "_eip1559_send_patched", False):
            return

        DopplerExecutionAdapter._send = _send_eip1559
        DopplerExecutionAdapter._eip1559_send_patched = True
        logger.info("doppler_eip1559_send_patched")
    except (ImportError, AttributeError) as exc:
        if attempt >= MAX_RETRIES:
            logger.exception("doppler_gas_patch_failed_after_retries")
            return
        logger.debug(
            "doppler_gas_patch_waiting_for_executor",
            extra={"attempt": attempt + 1, "error": str(exc)},
        )
        threading.Timer(
            RETRY_DELAY_SECONDS,
            _apply_patch,
            kwargs={"attempt": attempt + 1},
        ).start()
    except Exception:
        logger.exception("doppler_gas_patch_apply_failed")


# Apply asynchronously so importing scanners/__init__.py cannot race with the
# definition of DopplerExecutionAdapter itself.
threading.Timer(RETRY_DELAY_SECONDS, _apply_patch).start()
logger.info("doppler_gas_patch_bootstrap_scheduled")
