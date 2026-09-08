"""Install the Doppler launch lane without disturbing the existing Pons lane.

The scanner already has a mature Robinhood/Pons pipeline.  Rather than fork
that hot path, this module extends the Pons Robinhood lane at import time:
Doppler launches are discovered, snapshotted with their v4 pool state, and
handed to a dedicated execution adapter. Existing Pons behavior is untouched.
"""
from __future__ import annotations

import logging

from app.connectors.pons import pons_client, PonsClient

logger = logging.getLogger("app.connectors.doppler_bootstrap")

try:
    from app.connectors.doppler import doppler_client

    _original_poll = pons_client.poll_new_launches
    _original_snapshot = PonsClient.market_snapshot

    async def _poll_combined(*args, **kwargs):
        pons = await _original_poll(*args, **kwargs)
        try:
            doppler = await doppler_client.poll_new_launches()
        except Exception as exc:
            logger.exception("doppler_launch_poll_failed", extra={"error": str(exc)})
            doppler = []
        if doppler:
            logger.info("doppler_launch_batch", extra={"returned": len(doppler)})
        return pons + doppler

    async def _snapshot_combined(self, token, metadata=None):
        metadata = metadata or {}
        if metadata.get("source") == "doppler":
            return await doppler_client.market_snapshot(token, metadata)
        return await _original_snapshot(self, token, metadata)

    pons_client.poll_new_launches = _poll_combined
    PonsClient.market_snapshot = _snapshot_combined

    # Extend the already-selected Pons Robinhood execution adapter.  The
    # scanner continues to use the same rule/platform namespace, so existing
    # Telegram rules immediately see the new launch source.
    try:
        from app.execution.pons_live import PonsExecutionAdapter
        from app.execution.doppler_live import DopplerExecutionAdapter
        from app.execution.onchain.robinhood_wallet import resolve_robinhood_rpc_url
        from app.config.settings import settings

        _pons_buy = PonsExecutionAdapter.buy
        _pons_sell = PonsExecutionAdapter.sell

        async def _buy(self, token, amount_eth):
            market = (getattr(token, "raw_enrichment", {}) or {}).get("pons", {}) or {}
            if market.get("venue") == "doppler_v4":
                executor = getattr(self, "_doppler_executor", None)
                if executor is None:
                    executor = DopplerExecutionAdapter(
                        account=self._account,
                        rpc_url=self._rpc_url,
                        buy_slippage_bps=getattr(settings, "doppler_buy_slippage_bps", getattr(self, "_buy_slippage_bps", 1000)),
                    )
                    self._doppler_executor = executor
                return await executor.buy(token, amount_eth)
            return await _pons_buy(self, token, amount_eth)

        async def _sell(self, token, amount_tokens, sell_pct):
            market = (getattr(token, "raw_enrichment", {}) or {}).get("pons", {}) or {}
            if market.get("venue") == "doppler_v4":
                executor = getattr(self, "_doppler_executor", None)
                if executor is None:
                    executor = DopplerExecutionAdapter(
                        account=self._account,
                        rpc_url=self._rpc_url,
                        buy_slippage_bps=getattr(settings, "doppler_buy_slippage_bps", getattr(self, "_buy_slippage_bps", 1000)),
                    )
                    self._doppler_executor = executor
                return await executor.sell(token, amount_tokens, sell_pct)
            return await _pons_sell(self, token, amount_tokens, sell_pct)

        PonsExecutionAdapter.buy = _buy
        PonsExecutionAdapter.sell = _sell
    except Exception:
        # Discovery must remain available even if the optional live execution
        # adapter cannot be imported in a reduced/test environment.
        logger.exception("doppler_execution_hook_install_failed")

    logger.info("doppler_robinhood_lane_installed")
except Exception:
    # Do not make the entire scanner fail to import because an optional
    # Doppler integration is unavailable in a test or migration environment.
    logger.exception("doppler_robinhood_lane_install_failed")
