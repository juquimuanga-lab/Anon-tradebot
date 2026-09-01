"""Telegram presentation for unrestricted Jupiter arbitrage discovery."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.arbitrage.discovery import ArbitrageDiscovery
from app.security.allowlist import admin_required


@admin_required
async def arbitrage_discover_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: `/arbdiscover <solana_token_mint> [SOL amount]`\n"
            "Example: `/arbdiscover <mint> 0.01`",
            parse_mode="Markdown",
        )
        return

    token_mint = args[0].strip()
    try:
        amount_sol = float(args[1]) if len(args) > 1 else 0.01
    except ValueError:
        await update.message.reply_text("SOL amount must be a number, e.g. `0.01`.", parse_mode="Markdown")
        return

    await update.message.reply_text(
        f"🧭 Discovering best Jupiter routes for `{amount_sol:g} SOL`…",
        parse_mode="Markdown",
    )

    discovery = ArbitrageDiscovery()
    try:
        result = await discovery.discover(token_mint, amount_sol)
    except Exception as exc:
        await update.message.reply_text(
            f"❌ Discovery failed safely: `{type(exc).__name__}: {exc}`",
            parse_mode="Markdown",
        )
        return
    finally:
        await discovery.close()

    if result.error:
        await update.message.reply_text(
            f"No unrestricted Jupiter round-trip was found.\n\nReason: `{result.error}`\n\n_No transaction was submitted._",
            parse_mode="Markdown",
        )
        return

    buy = result.buy_quote
    sell = result.sell_quote
    opportunity = result.opportunity
    if not buy or not sell or not opportunity:
        await update.message.reply_text("No complete unrestricted Jupiter round-trip was found.\n\n_No transaction was submitted._")
        return

    buy_route = buy.route_id or "unknown"
    sell_route = sell.route_id or "unknown"
    status = "✅ PROFIT QUALIFIED" if opportunity.executable else "— below profit threshold"
    await update.message.reply_text(
        "🧭 *Jupiter route discovery*\n\n"
        f"{status}\n"
        f"Input: `{amount_sol:g} SOL`\n"
        f"Buy route: `{buy_route}`\n"
        f"Sell route: `{sell_route}`\n"
        f"Token amount: `{buy.output_amount_atomic}`\n"
        f"Final SOL: `{sell.output_amount_atomic / 1_000_000_000:.9f}`\n"
        f"Gross: `{opportunity.gross_profit_atomic / 1_000_000_000:.9f} SOL` (`{opportunity.gross_profit_bps:.2f} bps`)\n"
        f"External costs: `{opportunity.total_cost_atomic / 1_000_000_000:.9f} SOL` (`{opportunity.execution_cost_bps:.2f} bps`)\n"
        f"Required gross edge: `{opportunity.required_gross_profit_bps:.2f} bps`\n"
        f"Net: `{opportunity.net_profit_atomic / 1_000_000_000:.9f} SOL` (`{opportunity.net_profit_bps:.1f} bps`)\n"
        f"Reason: `{opportunity.reason}`\n\n"
        "This is discovery/observe mode. No transaction was submitted.",
        parse_mode="Markdown",
    )
