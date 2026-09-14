"""Make Doppler SPCX admission decisions observable without changing gates."""
from __future__ import annotations

import importlib
import logging
import threading

logger = logging.getLogger("app.connectors.doppler_admission_diagnostics")


def install() -> None:
    admission = importlib.import_module("app.connectors.doppler_spcx_admission_patch")
    if getattr(admission, "_doppler_admission_diagnostics_installed", False):
        return
    original_normalize = admission._normalize_launch

    async def traced_normalize(item, doppler, doppler_control):
        mint = item.get("mint")
        event_numeraire = str(item.get("numeraire", ""))
        accepted = await original_normalize(item, doppler, doppler_control)
        state = item.get("doppler_state") or {}
        pool_key = item.get("pool_key") or {}
        if accepted:
            logger.info("doppler_admission_accepted", extra={"mint": mint, "state_numeraire": item.get("numeraire"), "currency0": pool_key.get("currency0"), "currency1": pool_key.get("currency1"), "status": state.get("status"), "tokens_on_curve": state.get("tokens_on_curve")})
        else:
            reason = "initializer state did not prove canonical SPCX/currency0 -> launched token/currency1"
            logger.warning("doppler_admission_rejected: %s", reason, extra={"mint": mint, "event_numeraire": event_numeraire, "state_numeraire": item.get("numeraire"), "currency0": pool_key.get("currency0"), "currency1": pool_key.get("currency1"), "status": state.get("status"), "tokens_on_curve": state.get("tokens_on_curve"), "reason": reason})
        return accepted

    admission._normalize_launch = traced_normalize
    admission._doppler_admission_diagnostics_installed = True
    logger.info("doppler_admission_diagnostics_installed")


def _bootstrap() -> None:
    try:
        install()
    except Exception:
        logger.exception("doppler_admission_diagnostics_bootstrap_failed")
        timer = threading.Timer(1.0, _bootstrap)
        timer.daemon = True
        timer.start()


_timer = threading.Timer(0.5, _bootstrap)
_timer.daemon = True
_timer.start()
