"""Scanner package bootstrap.

The Helius Preconfirmations transport is additive: it feeds the existing
Pump.fun launch pipeline (Fast Sniper) and Smart Money Copy lane while the
existing WSS/RPC paths remain active as fallback and verification.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("app.scanners")

try:
    from app.config.settings import settings
    from . import onchain_watcher as _onchain_watcher
    from . import preconf_fastpath as _preconf

    _original_poll_new_pumpfun_mints = _onchain_watcher.poll_new_pumpfun_mints
    _original_drain_smart_money_buys = _onchain_watcher.drain_smart_money_buys

    async def _preconf_aware_poll_new_pumpfun_mints(rpc_url, watermarks, limit=20):
        # One shared Pump.fun preconf firehose feeds both Fast Sniper and Smart
        # Money Copy. We deliberately do not create a second subscription per lane.
        _preconf.ensure_started(settings.smart_money_wallets)
        preconf_events = _preconf.drain_launches()
        existing = await _original_poll_new_pumpfun_mints(rpc_url, watermarks, limit)

        # Preconf wins on duplicate signatures because it is the earlier signal.
        # Existing WSS/RPC recovery remains responsible for coverage gaps.
        merged = []
        seen = set()
        for item in preconf_events + existing:
            sig = item.get("tx_signature")
            if sig and sig in seen:
                continue
            if sig:
                seen.add(sig)
            merged.append(item)
        if preconf_events:
            logger.info(
                "pumpfun_preconf_fastpath_batch",
                extra={"count": len(preconf_events)},
            )
        return merged

    def _preconf_aware_drain_smart_money_buys(rpc_url, wallets):
        _preconf.ensure_started(wallets)
        preconf_events = _preconf.drain_smart_money(wallets)
        existing = _original_drain_smart_money_buys(rpc_url, wallets)

        merged = []
        seen = set()
        for item in preconf_events + existing:
            sig = item.get("tx_signature")
            if sig and sig in seen:
                continue
            if sig:
                seen.add(sig)
            merged.append(item)
        return merged

    _onchain_watcher.poll_new_pumpfun_mints = _preconf_aware_poll_new_pumpfun_mints
    _onchain_watcher.drain_smart_money_buys = _preconf_aware_drain_smart_money_buys

except Exception:
    # Never make scanner imports fail because an optional low-latency transport
    # is unavailable. The original watcher remains fully operational.
    logger.exception("preconf_bootstrap_failed")
