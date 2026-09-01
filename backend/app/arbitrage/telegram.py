"""Telegram commands for the isolated Solana arbitrage subsystem."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.arbitrage.service import ArbitrageService
from app.security.allowlist import admin_required

service = ArbitrageService()


@admin_required
async def arbitrage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = service.status()
    mode = "ENABLED (paper/observe only)" if status.enabled else "DISABLED"
    await update.message.reply_text(
        "⚖️ *Solana Arbitrage*\n\n"
        f"Status: `{mode}`\n"
        f"Running: `{status.running}`\n"
        f"Opportunities checked: `{status.opportunities_seen}`\n"
        f"Profit-qualified: `{status.executable_seen}`\n"
        f"Last result: `{status.last_reason}`\n\n"
        "V1 does not place live arbitrage trades.",
        parse_mode="Markdown",
    )


@admin_required
async def enable_arbitrage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await service.set_enabled(True)
    await update.message.reply_text(
        "✅ Arbitrage scanner enabled in safe observe/paper mode.\n"
        "Sniper lanes are unchanged."
    )


@admin_required
async def disable_arbitrage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await service.set_enabled(False)
    await update.message.reply_text(
        "⏸ Arbitrage scanner disabled.\n"
        "Sniper lanes are unchanged."
    )


@admin_required
async def arbitrage_help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⚖️ *Arbitrage commands*\n\n"
        "/arbitrage — show arbitrage status\n"
        "/enablearbitrage — enable observe/paper scanning\n"
        "/disablearbitrage — disable arbitrage\n"
        "/arbhelp — show this help\n\n"
        "Live execution is intentionally not enabled in V1.",
        parse_mode="Markdown",
    )
