"""Telegram handler for the observe-only arbitrage candidate hunter."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.arbitrage.hunt import ArbitrageHunter
from app.security.allowlist import admin_required


@admin_required
async def arbitrage_hunt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) > 1:
        await update.message.reply_text(
            "Usage: `/arbhunt [candidate_limit]`\n"
            "Example: `/arbhunt 5`\n\n"
            "This only discovers and quotes candidates. No transaction is submitted.",
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

    await update.message.reply_text(
        "🛰️ *Arbitrage hunter started*\n\n"
        "Shortlisting liquid, high-volume Solana tokens with multiple venues, "
        "then running the existing Jupiter size sweep.\n\n"
        "_Observe-only. No transaction will be submitted._",
        parse_mode="Markdown",
    )

    hunter = ArbitrageHunter()
    try:
        result = await hunter.hunt(limit)
    except Exception as exc:
        await update.message.reply_text(f"❌ Arbitrage hunt failed safely: `{exc}`", parse_mode="Markdown")
        return
    finally:
        await hunter.close()

    if not result.candidates:
        reason = result.errors[0] if result.errors else "No candidates met the configured filters."
        await update.message.reply_text(
            f"🔎 *No arbitrage candidates found*\n\nReason: `{reason}`\n\n"
            "This was observe-only. No transaction was submitted.",
            parse_mode="Markdown",
        )
        return

    rows: list[str] = []
    for candidate, discovery in result.discoveries:
        opportunity = discovery.opportunity
        if opportunity is None:
            rows.append(f"• `{candidate.symbol}` → no complete Jupiter round-trip")
            continue

        status = "✅" if opportunity.executable else "—"
        rows.append(
            f"{status} `{candidate.symbol}` | `{discovery.amount_sol:g} SOL` | "
            f"net `{opportunity.net_profit_bps:.1f} bps` / "
            f"`{opportunity.net_profit_atomic / 1_000_000_000:.9f} SOL` | "
            f"DEXes `{candidate.dex_count}` | liq `${candidate.liquidity_usd:,.0f}`"
        )

    message = (
        "🛰️ *Arbitrage hunt results*\n\n"
        "Candidates were filtered by liquidity, 24h volume and venue diversity, "
        "then priced through unrestricted Jupiter discovery.\n\n"
        + "\n".join(rows)
        + "\n\n*Top candidate details:*"
    )

    if result.discoveries:
        candidate, discovery = result.discoveries[0]
        opportunity = discovery.opportunity
        if opportunity and discovery.buy_quote and discovery.sell_quote:
            message += (
                f"\n`{candidate.symbol}` — `{candidate.token_mint}`"
                f"\nBuy: `{discovery.buy_quote.route_id or 'unknown'}`"
                f"\nSell: `{discovery.sell_quote.route_id or 'unknown'}`"
                f"\nGross: `{opportunity.gross_profit_bps:.2f} bps`"
                f"\nExecution: `{opportunity.execution_cost_bps:.2f} bps`"
                f"\nRequired: `{opportunity.required_gross_profit_bps:.2f} bps`"
                f"\nNet: `{opportunity.net_profit_bps:.2f} bps`"
            )

    message += "\n\n_Observe-only. No transaction was submitted._"
    await update.message.reply_text(message, parse_mode="Markdown")
