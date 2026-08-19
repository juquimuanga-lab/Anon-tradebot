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
from app.connectors.fourmeme import fourmeme_client
from app.execution.onchain.jupiter import JupiterClient
from app.execution.router import ExecutionRouter
from app.metrics import metrics
from app.positions.manager import PositionManager
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

    await fourmeme_client.start()
    logger.info(
        "fourmeme_discovery_startup",
        extra={
            "bitquery_configured": fourmeme_client.enabled,
            "trading_enabled": settings.fourmeme_trading_enabled,
            "mempool": True,
        },
    )

    _background_tasks.append(asyncio.create_task(scanner_service.run_forever()))
    _background_tasks.append(asyncio.create_task(position_manager.run_forever()))
    _background_tasks.append(asyncio.create_task(scanner_service.daily_summary_loop()))

    app.state.anoncoin_client = anoncoin_client
    app.state.helius_client = holders_client
    logger.info("bot_started")

    try:
        yield
    finally:
        await fourmeme_client.stop()
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
    data = metrics.as_dict()
    data.update({"win_rate_pct": round(win_rate, 2), "total_pnl": round(total_pnl, 2)})
    return data
