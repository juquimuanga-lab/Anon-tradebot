"""Telegram commands for the isolated Solana arbitrage subsystem."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.arbitrage.scanner import ArbitrageScanner
from app.arbitrage.service import ArbitrageService
from app.arbitrage.jupiter_quotes import configured_venues
from app.arbitrage.live_executor import ArbitrageLiveExecutor
from app.arbitrage import continuous_telegram
from app.security.allowlist import admin_required

service = ArbitrageService()
scanner = ArbitrageScanner(service)
live_executor = ArbitrageLiveExecutor(service)


@admin_required
async def arbitrage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = service.status()
    mode = "ENABLED (paper/observe only)" if status.enabled else "DISABLED"
    live = "ARMED BY ENVIRONMENT" if live_executor.live_enabled else "LOCKED"
    rpc = await scanner.rpc_health.check()
    rpc_state = f"OK ({rpc.provider}, slot {rpc.slot})" if rpc.healthy else f"DOWN ({rpc.error})"
    await update.message.reply_text(
        "⚖️ *Solana Arbitrage*\n\n"
        f"Status: `{mode}`\n"
        f"Live execution gate: `{live}`\n"
        f"RPC data plane: `{rpc_state}`\n"
        f"Running: `{status.running}`\n"
        f"Opportunities checked: `{status.opportunities_seen}`\n"
        f"Profit-qualified: `{status.executable_seen}`\n"
        f"Last result: `{status.last_reason}`\n\n"
        "Observe scans use live Jupiter venue quotes. No transaction is submitted by scanning.",
        parse_mode="Markdown",
    )


@admin_required
async def enable_arbitrage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await service.set_enabled(True)
    await update.message.reply_text("✅ Arbitrage scanning enabled in observe/paper mode.\nSniper lanes are unchanged.")


@admin_required
async def disable_arbitrage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await service.set_enabled(False)
    await update.message.reply_text("⏸ Arbitrage scanner disabled.\nSniper lanes are unchanged.")


@admin_required
async def arbitrage_help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⚖️ *Arbitrage commands*\n\n"
        "/arbitrage — show arbitrage status and RPC health\n"
        "/enablearbitrage — enable quote scanning\n"
        "/disablearbitrage — disable scanning\n"
        "/arbscan <mint> [SOL] — scan live venue spreads\n"
        "/arbdiscover <mint> [SOL] — discover unrestricted Jupiter routes; no amount runs the size sweep\n"
        "/arbhunt [1-10] — shortlist liquid multi-venue Solana tokens and run observe-only Jupiter discovery\n"
        "/arbvenues — show configured venue labels (manual/debug scan only)\n"
        "/arblivestatus — show live execution gate\n"
        "/arblive [1-10] — start/arm the global live arbitrage hunter\n"
        "/arbstop — stop the arbitrage hunter immediately\n"
        "/arbhelp — show this help\n\n"
        "Live mode uses global candidate discovery and unrestricted Jupiter routing. Every candidate is re-quoted before signing; the final net profit must be strictly positive.",
        parse_mode="Markdown",
    )


@admin_required
async def arbitrage_venues_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    venues = configured_venues()
    lines = [f"• `{venue.name}` → `{venue.jupiter_dex_label}` ({venue.fee_bps:g} bps estimate)" for venue in venues]
    await update.message.reply_text("⚖️ *Arbitrage venues*\n\n" + "\n".join(lines), parse_mode="Markdown")


@admin_required
async def arbitrage_scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: `/arbscan <solana_token_mint> [SOL amount]`\nExample: `/arbscan <mint> 0.05`", parse_mode="Markdown")
        return
    token_mint = args[0].strip()
    try:
        amount_sol = float(args[1]) if len(args) > 1 else 0.05
    except ValueError:
        await update.message.reply_text("SOL amount must be a number, e.g. `0.05`.", parse_mode="Markdown")
        return
    await update.message.reply_text(f"🔎 Scanning `{amount_sol:g} SOL` across configured venues…", parse_mode="Markdown")
    try:
        result = await scanner.scan(token_mint, amount_sol)
    except Exception as exc:
        await update.message.reply_text(f"❌ Arbitrage scan failed safely: `{type(exc).__name__}: {exc}`", parse_mode="Markdown")
        return
    if not result.opportunities:
        rpc = result.rpc_health
        detail = f"RPC: {rpc.provider}" if rpc and rpc.healthy else f"RPC unavailable: {rpc.error if rpc else 'unknown'}"
        diagnostics = "\n".join(f"• `{error}`" for error in result.quote_errors[:8])
        message = "No two-venue quote pairs were available for that token/size.\n" + detail
        if diagnostics:
            message += "\n\nJupiter quote diagnostics:\n" + diagnostics
        message += "\n\nUse /arbvenues to inspect the configured venue labels."
        await update.message.reply_text(message, parse_mode="Markdown")
        return
    lines = []
    for item in result.opportunities[:8]:
        status = "✅ QUALIFIED" if item.executable else "— rejected"
        lines.append(f"{status} `{item.buy_venue} → {item.sell_venue}` | net `{item.net_profit_bps:.1f} bps` | reason `{item.reason}`")
    rpc = result.rpc_health
    rpc_line = f"RPC: `{rpc.provider}`" if rpc and rpc.healthy else "RPC: `unavailable`"
    diagnostics = "\n".join(f"• `{error}`" for error in result.quote_errors[:4])
    if diagnostics:
        diagnostics = "\n\nQuote diagnostics:\n" + diagnostics
    await update.message.reply_text("⚖️ *Arbitrage scan result*\n\n" + "\n".join(lines) + f"\n\n{rpc_line}{diagnostics}\n_No transaction was submitted._", parse_mode="Markdown")


@admin_required
async def arbitrage_live_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = "ARMED" if live_executor.live_enabled else "LOCKED"
    hunt_status = continuous_telegram.continuous_hunt.status
    await update.message.reply_text(
        "⚠️ *Arbitrage live execution*\n\n"
        f"Environment gate: `{state}`\n"
        f"Global hunter running: `{'YES' if hunt_status.running else 'NO'}`\n"
        f"Hunter cycles: `{hunt_status.cycles}`\n\n"
        "`/arblive` starts the global candidate hunter when the environment gate is ARMED. "
        "The hunter uses unrestricted Jupiter discovery, then live execution re-quotes both legs before signing. "
        "Only strictly positive final net profit is eligible. Use `/arbstop` to stop it.",
        parse_mode="Markdown",
    )


@admin_required
async def arbitrage_live_execute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await continuous_telegram.arbitrage_live_hunt_start_cmd(update, context)
