"""FastAPI entrypoint: embeds the Telegram bot (polling) and exposes internal
health/metrics endpoints. No trading action is ever exposed over HTTP -
control is Telegram-only, per the security requirements."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.bot.notifications import Notifier
from app.bot.telegram_app import build_application
from app.config.settings import settings
from app.connectors.anoncoin import AnoncoinClient
from app.connectors.helius import HeliusClient
from app.execution.onchain.jupiter import JupiterClient
from app.execution.router import ExecutionRouter
from app.metrics import metrics
from app.positions.manager import PositionManager
from app.scanners.pumpfun_compat import install_pumpfun_compat
from app.scanners.pumpfun_resource_guard import install_pumpfun_resource_guard
from app.scanners.scanner import ScannerService
from app.security.secrets_manager import secrets_manager
from app.storage import repository as repo
from app.storage.database import init_db
from app.utils.logging_config import configure_logging

configure_logging()
logger = logging.getLogger("app.server")

_background_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Install the narrow Pump.fun RPC compatibility fallback before the
    # scanner starts. This only affects transaction decoding failures; the
    # global launch-safety gate and active Telegram ruleset remain unchanged.
    install_pumpfun_compat()
    # Reduce Pump.fun stream RPC/queue pressure without disabling discovery,
    # safety filters, Smart Money, or HTTP recovery.
    install_pumpfun_resource_guard()

    anoncoin_client = AnoncoinClient(settings.anoncoin_base_url, secrets_manager.get_anoncoin_api_key)
    holders_client = HeliusClient(settings.helius_base_url, settings.helius_api_key)
    jupiter_client = JupiterClient(settings.jupiter_base_url)

    telegram_app = build_application()
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)

    notifier = Notifier(telegram_app.bot)
    execution_router = ExecutionRouter(jupiter_client)
    position_manager = PositionManager(notifier, anoncoin_client, execution_router)
    scanner_service = ScannerService(notifier, position_manager, anoncoin_client, holders_client, execution_router)

    telegram_app.bot_data["position_manager"] = position_manager

    # Four.meme/Bitquery discovery is intentionally not started. Pump.fun and
    # the existing Anoncoin path remain the active launch sources.

    _background_tasks.append(asyncio.create_task(scanner_service.run_forever()))
    _background_tasks.append(asyncio.create_task(position_manager.run_forever()))
    _background_tasks.append(asyncio.create_task(scanner_service.daily_summary_loop()))

    app.state.anoncoin_client = anoncoin_client
    app.state.helius_client = holders_client
    logger.info("bot_started")

    try:
        yield
    finally:
        for task in _background_tasks:
            task.cancel()
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        await anoncoin_client.aclose()
        await holders_client.aclose()
        await jupiter_client.aclose()


app = FastAPI(title="Anoncoin Sniper Bot", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    state = await repo.get_or_create_bot_state()
    return {
        "status": "ok",
        "mode": state.mode,
        "trading_enabled": state.trading_enabled,
    }


@app.get("/api/metrics")
async def get_metrics():
    closed = await repo.get_closed_positions()
    wins = sum(1 for p in closed if p.realized_pnl_usd > 0)
    win_rate = (wins / len(closed) * 100) if closed else 0.0
    total_pnl = sum(p.realized_pnl_usd for p in closed)
    winners = [p.realized_pnl_usd for p in closed if p.realized_pnl_usd > 0]
    losers = [p.realized_pnl_usd for p in closed if p.realized_pnl_usd < 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    data = metrics.as_dict()
    data.update({
        "win_rate_pct": round(win_rate, 2),
        "total_pnl": round(total_pnl, 2),
        "average_winner_usd": round(gross_profit / len(winners), 6) if winners else 0.0,
        "average_loser_usd": round(sum(losers) / len(losers), 6) if losers else 0.0,
        "gross_profit_usd": round(gross_profit, 6),
        "gross_loss_usd": round(gross_loss, 6),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        "expectancy_per_trade_usd": round(total_pnl / len(closed), 6) if closed else 0.0,
        "total_fees_usd": round(sum(float(getattr(p, "total_fees_usd", 0.0) or 0.0) for p in closed), 6),
        "total_network_fees_usd": round(sum(float(getattr(p, "total_network_fee_usd", 0.0) or 0.0) for p in closed), 6),
    })
    return data
