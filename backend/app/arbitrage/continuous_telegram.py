"""Telegram controls for the persistent arbitrage hunter."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.arbitrage.continuous_hunt import continuous_hunt
from app.arbitrage.hunt import HuntResult
from app.arbitrage.live_executor import ArbitrageLiveExecutor
from app.security.allowlist import admin_required


live_executor = ArbitrageLiveExecutor()


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
                f"Profit: `+{opportunity.net_profit_atomic / 1_000_000_000:.9f} SOL`"
            )
    return ""


async def _notify_observe(result: HuntResult, update: Update) -> None:
    message = _format_alert(result)
    if message and update.effective_chat:
        await update.effective_chat.send_message(
            message + "\n\n⚠️ *Observe-only. No transaction was submitted.*",
            parse_mode="Markdown",
        )


async def _on_profitable(result: HuntResult, update: Update) -> None:
    message = _format_alert(result)
    if not message or not update.effective_chat:
        return

    candidate, discovery = result.discoveries[0]
    opportunity = discovery.opportunity
    if opportunity is None or not opportunity.executable:
        return

    await update.effective_chat.send_message(
        message + "\n\n⚠️ *Live mode: re-quoting before execution…*",
        parse_mode="Markdown",
    )

    try:
        execution = await live_executor.execute_unrestricted(
            owner_user_id=int(update.effective_user.id),
            token_mint=candidate.token_mint,
            amount_sol=discovery.amount_sol,
        )
    except Exception as exc:
        await update.effective_chat.send_message(
            f"🛑 *Arbitrage execution refused safely*\n\n`{type(exc).__name__}: {exc}`",
            parse_mode="Markdown",
        )
        return

    if not execution.success:
        await update.effective_chat.send_message(
            "🛑 *Arbitrage not executed/settled*\n\n"
            f"Reason: `{execution.reason}`\n"
            f"Net after priority: `{execution.estimated_net_profit_lamports / 1_000_000_000:.9f} SOL`\n"
            f"Bundle: `{execution.bundle_id or 'none'}`",
            parse_mode="Markdown",
        )
        return

    signatures = "\n".join(f"`{sig}`" for sig in execution.transaction_signatures) or "none"
    await update.effective_chat.send_message(
        "✅ *LIVE ARBITRAGE BUNDLE SETTLED*\n\n"
        f"Token: `{candidate.symbol}`\n"
        f"Input: `{execution.input_lamports / 1_000_000_000:.9f} SOL`\n"
        f"Net: `+{execution.estimated_net_profit_lamports / 1_000_000_000:.9f} SOL`\n"
        f"Bundle: `{execution.bundle_id}`\n"
        f"Transactions:\n{signatures}",
        parse_mode="Markdown",
    )


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
        on_profitable=lambda result: _notify_observe(result, update),
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
async def arbitrage_live_hunt_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) > 1:
        await update.message.reply_text("Usage: `/arblive [candidate_limit]`", parse_mode="Markdown")
        return
    if not live_executor.live_enabled:
        await update.message.reply_text(
            "🔒 *Live arbitrage is LOCKED.*\n\nSet `ARBITRAGE_LIVE_TRADING_ENABLED=true` in Railway and redeploy before using `/arblive`.",
            parse_mode="Markdown",
        )
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
            "🚀 *LIVE GLOBAL ARBITRAGE HUNTER STARTED*\n\n"
            "The bot will globally screen candidates, discover unrestricted Jupiter routes, and automatically attempt qualifying positive-net opportunities.\n\n"
            "Every live opportunity is re-quoted before signing. Existing wallet, balance, trade-size, simulation, Jito bundle and settlement checks remain active.\n\n"
            "Use `/arbstop` to stop it immediately or `/arbstatus` to check it.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("🛰️ An arbitrage hunter is already running. Use `/arbstatus` or `/arbstop`.")


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
        "Global discovery is observe-only until `/arblive` arms live execution. ",
        parse_mode="Markdown",
    )
