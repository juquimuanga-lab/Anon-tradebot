"""Telegram controls for the persistent observe-only arbitrage hunter."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.arbitrage.continuous_hunt import continuous_hunt
from app.arbitrage.hunt import HuntResult
from app.security.allowlist import admin_required


def _format_alert(result: HuntResult) -> str:
    for candidate, discovery in result.discoveries:
        opportunity = discovery.opportunity
        if opportunity is not None and opportunity.executable:
            return (
                "🚨 *PROFITABLE ARBITRAGE FOUND*\n\n"
                f"Token: `{candidate.symbol}` [{candidate.tier}]\n"
                f"Mint: `{candidate.token_mint}`\n"
                f"Size: `{discovery.amount_sol:g} SOL`\n"
                f"Buy: `{discovery.buy_quote.route_id if discovery.buy_quote else 'unknown'}`\n"
                f"Sell: `{discovery.sell_quote.route_id if discovery.sell_quote else 'unknown'}`\n\n"
                f"Gross: `{opportunity.gross_profit_bps:.2f} bps`\n"
                f"Execution: `{opportunity.execution_cost_bps:.2f} bps`\n"
                f"Required: `{opportunity.required_gross_profit_bps:.2f} bps`\n"
                f"Net: `+{opportunity.net_profit_bps:.2f} bps`\n"
                f"Profit: `+{opportunity.net_profit_atomic / 1_000_000_000:.9f} SOL`\n\n"
                "⚠️ *Observe-only. No transaction was submitted.*"
            )
    return ""


async def _on_profitable(result: HuntResult, update: Update) -> None:
    message = _format_alert(result)
    if message and update.effective_chat:
        await update.effective_chat.send_message(message, parse_mode="Markdown")


@admin_required
async def arbitrage_hunt_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) > 1:
        await update.message.reply_text("Usage: `/arbhunt [candidate_limit]`", parse_mode="Markdown")
        return
    limit = None
    if args:
        try:
            limit = int(args[0])
            if limit < 1 or limit > 10:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Candidate limit must be an integer from 1 to 10.")
            return

    started = await continuous_hunt.start(
        limit,
        on_profitable=lambda result: _on_profitable(result, update),
    )
    if started:
        await update.message.reply_text(
            "🛰️ *24/7 arbitrage hunter started*\n\n"
            "It will continuously scan until you send `/arbstop`. Qualifying opportunities will be reported here while the hunt continues.\n"
            "Observe-only: no transaction will be submitted.\n\n"
            "Use `/arbstatus` to check the hunter.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("🛰️ Arbitrage hunter is already running. Use `/arbstatus` or `/arbstop`.")


@admin_required
async def arbitrage_hunt_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stopped = await continuous_hunt.stop()
    if stopped:
        await update.message.reply_text("🛑 *Arbitrage hunter stopped.*", parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ Arbitrage hunter is not running.")


@admin_required
async def arbitrage_hunt_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = continuous_hunt.status
    last = status.last_result
    if not status.running and last is None:
        await update.message.reply_text("ℹ️ Arbitrage hunter is not running and has no scan history.")
        return
    stats = last.stats if last else None
    await update.message.reply_text(
        "🛰️ *Arbitrage hunter status*\n\n"
        f"Running: `{'YES' if status.running else 'NO'}`\n"
        f"Cycles: `{status.cycles}`\n"
        f"Last candidates: `{stats.final_candidates if stats else 0}`\n"
        f"Last Jupiter round-trips: `{stats.jupiter_round_trips if stats else 0}`\n"
        f"Last Jupiter 429s: `{stats.jupiter_429s if stats else 0}`\n\n"
        "Observe-only. No transaction submission.",
        parse_mode="Markdown",
    )
