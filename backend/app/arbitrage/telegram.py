"""Telegram commands for the isolated Solana arbitrage subsystem."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.arbitrage.scanner import ArbitrageScanner
from app.arbitrage.service import ArbitrageService
from app.arbitrage.jupiter_quotes import configured_venues
from app.security.allowlist import admin_required

service = ArbitrageService()
scanner = ArbitrageScanner(service)


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
        "Quote scanning is live, but no arbitrage transaction is submitted.",
        parse_mode="Markdown",
    )


@admin_required
async def enable_arbitrage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await service.set_enabled(True)
    await update.message.reply_text(
        "✅ Arbitrage scanning enabled in observe/paper mode.\n"
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
        "/enablearbitrage — enable quote scanning\n"
        "/disablearbitrage — disable arbitrage\n"
        "/arbscan <mint> [SOL] — scan venue spreads\n"
        "/arbvenues — show configured quote venues\n"
        "/arbhelp — show this help\n\n"
        "Quotes are fetched live, but V2 still does not place live arbitrage trades.",
        parse_mode="Markdown",
    )


@admin_required
async def arbitrage_venues_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    venues = configured_venues()
    lines = [
        f"• `{venue.name}` → `{venue.jupiter_dex_label}` ({venue.fee_bps:g} bps estimate)"
        for venue in venues
    ]
    await update.message.reply_text(
        "⚖️ *Arbitrage venues*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


@admin_required
async def arbitrage_scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: `/arbscan <solana_token_mint> [SOL amount]`\n"
            "Example: `/arbscan <mint> 0.05`",
            parse_mode="Markdown",
        )
        return

    token_mint = args[0].strip()
    try:
        amount_sol = float(args[1]) if len(args) > 1 else 0.05
    except ValueError:
        await update.message.reply_text("SOL amount must be a number, e.g. `0.05`.", parse_mode="Markdown")
        return

    await update.message.reply_text(
        f"🔎 Scanning `{amount_sol:g} SOL` across configured venues…",
        parse_mode="Markdown",
    )

    try:
        result = await scanner.scan(token_mint, amount_sol)
    except Exception as exc:
        await update.message.reply_text(
            f"❌ Arbitrage scan failed safely: `{type(exc).__name__}: {exc}`",
            parse_mode="Markdown",
        )
        return

    if not result.opportunities:
        await update.message.reply_text(
            "No two-venue quote pairs were available for that token/size.\n"
            "Use /arbvenues to inspect the configured venue labels."
        )
        return

    lines = []
    for item in result.opportunities[:8]:
        status = "✅ QUALIFIED" if item.executable else "— rejected"
        lines.append(
            f"{status} `{item.buy_venue} → {item.sell_venue}` | "
            f"net `{item.net_profit_bps:.1f} bps` | "
            f"reason `{item.reason}`"
        )

    await update.message.reply_text(
        "⚖️ *Arbitrage scan result*\n\n"
        + "\n".join(lines)
        + "\n\n_No transaction was submitted._",
        parse_mode="Markdown",
    )
