"""Telegram controls for the persistent arbitrage hunter."""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.arbitrage.continuous_hunt import continuous_hunt
from app.arbitrage.hunt import HuntResult
from app.arbitrage.live_executor import ArbitrageLiveExecutor
from app.config.settings import settings
from app.security.allowlist import admin_required
from app.security.secrets_manager import secrets_manager
from app.storage import repository as repo


logger = logging.getLogger("app.arbitrage.telegram")

live_executor = ArbitrageLiveExecutor()

# A shared discovery result can fan out to multiple independent admin wallets.
# Keep concurrency bounded so a single hot opportunity cannot create an
# unbounded number of simultaneous bundle submissions.
_LIVE_EXECUTION_SEMAPHORE = asyncio.Semaphore(4)
_ADMIN_EXECUTION_LOCKS: dict[int, asyncio.Lock] = {}


def _admin_execution_lock(admin_id: int) -> asyncio.Lock:
    lock = _ADMIN_EXECUTION_LOCKS.get(admin_id)
    if lock is None:
        lock = asyncio.Lock()
        _ADMIN_EXECUTION_LOCKS[admin_id] = lock
    return lock


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


async def _live_admin_ids() -> list[int]:
    """Return admins that are independently armed for live arbitrage.

    Each admin must have its own persistent BotState in live mode, trading
    enabled, and its own Solana wallet secret. No admin is selected based on
    who originally started `/arblive`.
    """
    eligible: list[int] = []
    for raw_admin_id in settings.telegram_admin_ids:
        admin_id = int(raw_admin_id)
        try:
            state = await repo.get_or_create_bot_state(admin_id)
            if state.mode != "live" or not state.trading_enabled:
                continue
            private_key = await secrets_manager.get_wallet_private_key(admin_id)
            if not private_key:
                continue
            eligible.append(admin_id)
        except Exception:
            logger.exception("arb_admin_eligibility_check_failed", extra={"admin_id": admin_id})
    return eligible


async def _execute_for_admin(
    *,
    bot,
    admin_id: int,
    candidate,
    discovery,
) -> None:
    """Execute one discovered opportunity using exactly one admin wallet."""
    message = (
        _format_alert(HuntResult((candidate,), ((candidate, discovery),)))
        if candidate is not None and discovery is not None
        else ""
    )
    if not message:
        return

    lock = _admin_execution_lock(admin_id)
    async with lock:
        async with _LIVE_EXECUTION_SEMAPHORE:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=message + "\n\n⚠️ *Live mode: re-quoting before execution…*",
                    parse_mode="Markdown",
                )

                execution = await live_executor.execute_unrestricted(
                    owner_user_id=admin_id,
                    token_mint=candidate.token_mint,
                    amount_sol=discovery.amount_sol,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "arb_live_execution_failed",
                    extra={"admin_id": admin_id, "token_mint": candidate.token_mint},
                )
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        "🛑 *Arbitrage execution refused safely*\n\n"
                        f"`{type(exc).__name__}: {exc}`"
                    ),
                    parse_mode="Markdown",
                )
                return

            if not execution.success:
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        "🛑 *Arbitrage not executed/settled*\n\n"
                        f"Reason: `{execution.reason}`\n"
                        f"Net after priority: `{execution.estimated_net_profit_lamports / 1_000_000_000:.9f} SOL`\n"
                        f"Bundle: `{execution.bundle_id or 'none'}`"
                    ),
                    parse_mode="Markdown",
                )
                return

            signatures = "\n".join(f"`{sig}`" for sig in execution.transaction_signatures) or "none"
            await bot.send_message(
                chat_id=admin_id,
                text=(
                    "✅ *LIVE ARBITRAGE BUNDLE SETTLED*\n\n"
                    f"Token: `{candidate.symbol}`\n"
                    f"Input: `{execution.input_lamports / 1_000_000_000:.9f} SOL`\n"
                    f"Net: `+{execution.estimated_net_profit_lamports / 1_000_000_000:.9f} SOL`\n"
                    f"Bundle: `{execution.bundle_id}`\n"
                    f"Transactions:\n{signatures}"
                ),
                parse_mode="Markdown",
            )


async def _on_profitable(result: HuntResult, bot) -> None:
    """Fan one shared discovery out to every independently armed admin."""
    candidate = None
    discovery = None
    for candidate_item, discovery_item in result.discoveries:
        opportunity = discovery_item.opportunity
        if opportunity is not None and opportunity.executable:
            candidate = candidate_item
            discovery = discovery_item
            break
    if candidate is None or discovery is None:
        return

    admin_ids = await _live_admin_ids()
    if not admin_ids:
        logger.info("arb_no_eligible_live_admins", extra={"token_mint": candidate.token_mint})
        return

    await asyncio.gather(
        *(
            _execute_for_admin(
                bot=bot,
                admin_id=admin_id,
                candidate=candidate,
                discovery=discovery,
            )
            for admin_id in admin_ids
        )
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
        on_profitable=lambda result: _on_profitable(result, context.bot),
    )
    if started:
        await update.message.reply_text(
            "🚀 *LIVE GLOBAL ARBITRAGE HUNTER STARTED*\n\n"
            "Discovery is shared across the deployment. Every admin participates only when that admin's own BotState is LIVE + Trading ON and that admin has a connected wallet.\n\n"
            "Each qualifying opportunity is re-quoted and executed separately against each eligible admin wallet. Existing wallet, balance, trade-size, simulation, Jito bundle and settlement checks remain active per admin.\n\n"
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
    hotlist = status.last_hotlist_result
    global_result = status.last_global_result
    if not status.running and hotlist is None and global_result is None:
        await update.message.reply_text("ℹ️ Arbitrage hunter is not running and has no scan history.")
        return

    hotlist_candidates = len(hotlist.candidates) if hotlist else 0
    hotlist_round_trips = (
        sum(1 for _, discovery in hotlist.discoveries if discovery.opportunity is not None)
        if hotlist else 0
    )
    hotlist_429s = (
        sum(1 for _, discovery in hotlist.discoveries if discovery.error and "HTTP 429" in discovery.error)
        if hotlist else 0
    )
    global_stats = global_result.stats if global_result else None
    live_admins = await _live_admin_ids()
    admin_status_lines = []
    for raw_admin_id in settings.telegram_admin_ids:
        admin_id = int(raw_admin_id)
        try:
            state = await repo.get_or_create_bot_state(admin_id)
            has_wallet = bool(await secrets_manager.get_wallet_private_key(admin_id))
            state_label = f"{state.mode.upper()} / Trading {'ON' if state.trading_enabled else 'OFF'} / Wallet {'CONNECTED' if has_wallet else 'NOT CONNECTED'}"
        except Exception:
            state_label = "STATUS ERROR"
        admin_status_lines.append(f"`{admin_id}` — {state_label}")

    await update.effective_chat.send_message(
        "🛰️ *Arbitrage hunter status*\n\n"
        f"Running: `{'YES' if status.running else 'NO'}`\n"
        f"Cycles: `{status.cycles}`\n\n"
        "👥 *Per-admin live participation*\n"
        f"Eligible live admins: `{len(live_admins)}`\n"
        + ("\n".join(admin_status_lines) if admin_status_lines else "None configured")
        + "\n\n"
        "🔥 *Hotlist*\n"
        f"Mints configured: `{len(status.hotlist_mints)}`\n"
        f"Last hotlist candidates: `{hotlist_candidates}`\n"
        f"Last hotlist Jupiter round-trips: `{hotlist_round_trips}`\n"
        f"Last hotlist Jupiter 429s: `{hotlist_429s}`\n"
        f"Hotlist scans: `{status.hotlist_scans}`\n\n"
        "🌎 *Global discovery*\n"
        f"Last global candidates: `{global_stats.final_candidates if global_stats else 0}`\n"
        f"Last global Jupiter round-trips: `{global_stats.jupiter_round_trips if global_stats else 0}`\n"
        f"Last global Jupiter 429s: `{global_stats.jupiter_429s if global_stats else 0}`\n"
        f"Global scans: `{status.global_scans}`\n\n"
        "Discovery is shared; live execution is isolated per eligible admin wallet.",
        parse_mode="Markdown",
    )
