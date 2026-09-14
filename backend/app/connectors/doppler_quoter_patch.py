"""Compatibility patch for Robinhood's Doppler V4 quoter ABI.

Robinhood's deployed V4 quoter includes minHopPriceX36 in the
ExactInputSingleParams tuple. The existing execution adapter's ABI predates
that field. Keep the execution adapter intact and normalize the quoter ABI at
startup, defaulting minHopPriceX36 to zero for the existing buy strategy.
"""
from __future__ import annotations

import importlib
import logging
import threading

from web3 import Web3

logger = logging.getLogger("app.connectors.doppler_quoter_patch")

QUOTER_ABI = [{
    "inputs": [{
        "components": [{
            "components": [
                {"name": "currency0", "type": "address"},
                {"name": "currency1", "type": "address"},
                {"name": "fee", "type": "uint24"},
                {"name": "tickSpacing", "type": "int24"},
                {"name": "hooks", "type": "address"},
            ],
            "name": "poolKey",
            "type": "tuple",
        },
        {"name": "zeroForOne", "type": "bool"},
        {"name": "exactAmount", "type": "uint128"},
        {"name": "minHopPriceX36", "type": "uint256"},
        {"name": "hookData", "type": "bytes"},
        ],
        "name": "params",
        "type": "tuple",
    }],
    "name": "quoteExactInputSingle",
    "outputs": [
        {"name": "amountOut", "type": "uint256"},
        {"name": "gasEstimate", "type": "uint256"},
    ],
    "stateMutability": "nonpayable",
    "type": "function",
}]


def install() -> None:
    # Import concrete modules rather than relying on app.connectors package
    # re-exports. This module is imported during scanners bootstrap, while the
    # connectors package may still be initializing.
    doppler_control = importlib.import_module("app.connectors.doppler_control")
    doppler_live = importlib.import_module("app.connectors.doppler_live")
    admission_patch = importlib.import_module("app.connectors.doppler_spcx_admission_patch")
    admission_patch.install()

    cls = doppler_live.DopplerExecutionAdapter
    if getattr(cls, "_robinhood_quoter_patch_installed", False):
        return

    original_init = cls.__init__

    def patched_init(self, account, rpc_url, buy_slippage_bps=1000):
        original_init(self, account, rpc_url, buy_slippage_bps)
        self._quoter = self._w3.eth.contract(
            address=Web3.to_checksum_address(doppler_live.V4_QUOTER),
            abi=QUOTER_ABI,
        )

    cls.__init__ = patched_init

    async def robinhood_buy(self, token, amount_spcx):
        import asyncio
        import time
        from app.execution.base import OrderResult

        try:
            raw = getattr(token, "raw_enrichment", {}) or {}
            market = raw.get("doppler") or raw.get("pons") or {}
            if str(market.get("numeraire", "")).lower() != doppler_control.SPCX_TOKEN.lower():
                raise RuntimeError("Doppler execution requires the canonical SPCX numeraire")
            pool_key = market.get("pool_key")
            if not pool_key or str(pool_key.get("currency0", "")).lower() != doppler_control.SPCX_TOKEN.lower():
                raise RuntimeError("Doppler SPCX pool key is unavailable or not SPCX/currency0")
            spend_spcx = doppler_control.get_buy_size_spcx()
            if spend_spcx <= 0:
                raise RuntimeError("DOPPLER_BUY_SIZE_SPCX must be configured to a value greater than zero")
            decimals = int(self._spcx.functions.decimals().call())
            amount_in = max(1, int(spend_spcx * (10 ** decimals)))
            await asyncio.to_thread(self._require_spcx_ready, amount_in)
            quote_params = {
                "poolKey": pool_key,
                "zeroForOne": True,
                "exactAmount": amount_in,
                "minHopPriceX36": 0,
                "hookData": b"",
            }
            quoted = await asyncio.to_thread(
                lambda: self._quoter.functions.quoteExactInputSingle(quote_params).call()
            )
            expected = int(quoted[0])
            if expected <= 0:
                raise RuntimeError("Doppler SPCX quoter returned zero tokens")
            min_out = expected * (doppler_live.BPS - self._buy_slippage_bps) // doppler_live.BPS
            v4_input = self._encode_v4_swap(pool_key, amount_in, min_out)
            commands = bytes([0x02, 0x10])
            inputs = [self._encode_permit2_transfer(amount_in), v4_input]
            tx_hash = await asyncio.to_thread(
                self._send,
                self._router.functions.execute(commands, inputs, int(time.time()) + 20),
                0,
            )
            logger.info(
                "doppler_spcx_buy_confirmed",
                extra={
                    "mint": token.mint,
                    "tx_signature": tx_hash,
                    "amount_spcx": spend_spcx,
                    "quoted_tokens": expected,
                    "min_tokens": min_out,
                },
            )
            return OrderResult(True, "filled", price_usd=float(token.price_usd or 0.0), tx_signature=tx_hash)
        except Exception as exc:
            logger.warning(
                "doppler_spcx_buy_failed",
                extra={"mint": token.mint, "error": str(exc)},
            )
            return OrderResult(False, "failed", error_message=str(exc))

    cls.buy = robinhood_buy
    cls._robinhood_quoter_patch_installed = True
    logger.info(
        "doppler_quoter_patch_installed",
        extra={"quoter": doppler_live.V4_QUOTER, "min_hop_price_x36": 0},
    )


def _bootstrap_later() -> None:
    try:
        install()
    except Exception:
        logger.exception("doppler_quoter_patch_bootstrap_failed")


# Do not call install() synchronously. This module is imported from
# scanners.__init__, which itself participates in the application bootstrap.
# A short daemon timer moves installation until after package/module imports
# have settled, matching the runtime-safe bootstrap pattern used elsewhere.
_timer = threading.Timer(0.5, _bootstrap_later)
_timer.daemon = True
_timer.start()
