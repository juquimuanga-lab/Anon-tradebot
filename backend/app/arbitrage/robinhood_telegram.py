"""Telegram controls for isolated Robinhood Chain arbitrage discovery."""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.arbitrage.robinhood_arbitrage import RobinhoodArbitrage
from app.security.allowlist import admin_required

logger = logging.getLogger("app.arbitrage.robinhood.telegram")
engine = RobinhoodArbitrage()
_hunt_task: asyncio.Task | None = None
_hunt_chat_id: int | None = None


def _fmt(opportunity) -> str:
    sign = "+" if opportunity.net_profit_wei > 0 else ""
    return (
        "🦅 *ROBINHOOD ARBITRAGE OPPORTUNITY*\n\n"
        f"Token: `{opportunity.token}`\n"
        f"Size: `{opportunity.amount_eth:g} ETH`\n"
        f"Buy: `{opportunity.buy_source}`\n"
        f"Sell: `{opportunity.sell_source}`\n"
        f"Gross: `{opportunity.gross_profit_wei / 1e18:.9f} ETH` (`{opportunity.gross_profit_bps:.2f} bps`)\n"
        f"Estimated gas: `{opportunity.gas_wei / 1e18:.9f} ETH`\n"
        f"Net: `{sign}{opportunity.net_profit_wei / 1e18:.9f} ETH` (`{sign}{opportunity.net_profit_bps:.2f} bps`)\n"
        f"Status: `{'QUALIFIED' if opportunity.executable else 'rejected'}`"
    )


@admin_required
async def robinhood_arbitrage_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    configured = bool(__import__("os").getenv("ONEINCH_API_KEY", "").strip())
    running = _hunt_task is not None and not _hunt_task.done()
    await update.message.reply_text(
        "🦅 *Robinhood Chain Arbitrage*\n\n"
        "Network: `Robinhood Chain (4663)`\n"
        f"1inch API: `{'READY' if configured else 'NOT CONFIGURED'}`\n"
        f"Hunter: `{'RUNNING' if running else 'STOPPED'}`\n\n"
        "This lane is isolated from the Solana sniper and Solana arbitrage executor. It discovers cross-DEX spreads using 1inch source-restricted quotes. No transaction is submitted by this hunter yet.",
        parse_mode="Markdown",
    )


@admin_required
async def robinhood_arbitrage_scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: `/rharbscan <token_address> [ETH amount]`", parse_mode="Markdown")
        return
    token = args[0].strip()
    try:
        amount = float(args[1]) if len(args) > 1 else 0.05
    except ValueError:
        await update.message.reply_text("ETH amount must be a number.")
        return
    await update.message.reply_text(f"🔎 Scanning Robinhood Chain for `{amount:g} ETH`…", parse_mode="Markdown")
    try:
        opportunities = await engine.scan(token, amount)
    except Exception as exc:
        await update.message.reply_text(f"❌ Robinhood scan failed safely: `{type(exc).__name__}: {exc}`", parse_mode="Markdown")
        return
    if not opportunities:
        await update.message.reply_text("No cross-source quotes were available for that token/size.", parse_mode="Markdown")
        return
    lines = [_fmt(item) for item in opportunities[:5]]
    await update.message.reply_text("\n\n".join(lines) + "\n\n_No transaction was submitted._", parse_mode="Markdown")


async def _hunt_loop(bot, chat_id: int) -> None:
    global _hunt_task
    try:
        while True:
            try:
                opportunities = await engine.hunt()
                for opportunity in opportunities[:3]:
                    if opportunity.executable:
                        await bot.send_message(chat_id=chat_id, text=_fmt(opportunity), parse_mode="Markdown")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("robinhood_arb_hunt_cycle_failed")
            await asyncio.sleep(15.0)
    finally:
        _hunt_task = None


@admin_required
async def robinhood_arbitrage_hunt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _hunt_task, _hunt_chat_id
    if _hunt_task is not None and not _hunt_task.done():
        await update.message.reply_text("🦅 Robinhood arbitrage hunter is already running. Use `/rharbstop` to stop it.")
        return
    _hunt_chat_id = update.effective_chat.id
    _hunt_task = asyncio.create_task(_hunt_loop(context.bot, _hunt_chat_id), name="robinhood-arbitrage-hunt")
    await update.message.reply_text(
        "🚀 *Robinhood Chain arbitrage hunter started*\n\n"
        "It scans multi-DEX candidates, then re-quotes individual 1inch liquidity sources every 15 seconds.\n\n"
        "Observe-only for now: no transaction is submitted. Use `/rharbstop` to stop it.",
        parse_mode="Markdown",
    )


@admin_required
async def robinhood_arbitrage_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _hunt_task
    if _hunt_task is None or _hunt_task.done():
        await update.message.reply_text("ℹ️ Robinhood arbitrage hunter is not running.")
        return
    _hunt_task.cancel()
    await update.message.reply_text("🛑 Robinhood arbitrage hunter stopped.")
