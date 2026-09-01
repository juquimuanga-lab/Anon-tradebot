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
        "Broadening Solana candidate discovery, then running a rate-limited Jupiter size sweep.\n\n"
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

    stats = result.stats
    if not result.candidates:
        await update.message.reply_text(
            "🔎 *No arbitrage candidates found*\n\n"
            f"Profiles/boost addresses: `{stats.profile_addresses}`\n"
            f"Pair observations: `{stats.search_pairs}`\n"
            f"Unique tokens: `{stats.unique_tokens}`\n"
            f"Liquidity-qualified: `{stats.liquidity_qualified}`\n"
            f"Volume-qualified: `{stats.volume_qualified}`\n"
            f"Venue-qualified: `{stats.venue_qualified}`\n\n"
            "The candidate screen found nothing to send to Jupiter. "
            "No transaction was submitted.",
            parse_mode="Markdown",
        )
        return

    rows: list[str] = []
    complete_routes = 0
    executable = 0
    for candidate, discovery in result.discoveries:
        opportunity = discovery.opportunity
        if opportunity is None:
            reason = discovery.error or "no complete Jupiter round-trip"
            if "HTTP 429" in reason:
                reason = "Jupiter rate-limited after retries"
            rows.append(f"• `{candidate.symbol}` [{candidate.tier}] → `{reason}`")
            continue

        complete_routes += 1
        if opportunity.executable:
            executable += 1
        status = "✅" if opportunity.executable else "🟡"
        rows.append(
            f"{status} `{candidate.symbol}` [{candidate.tier}] | `{discovery.amount_sol:g} SOL` | "
            f"net `{opportunity.net_profit_bps:.1f} bps` / "
            f"`{opportunity.net_profit_atomic / 1_000_000_000:.9f} SOL` | "
            f"DEXes `{candidate.dex_count}` | liq `${candidate.liquidity_usd:,.0f}`"
        )

    message = (
        "🛰️ *Arbitrage hunt results*\n\n"
        f"Pair observations: `{stats.search_pairs}`\n"
        f"Unique tokens: `{stats.unique_tokens}`\n"
        f"Screen-qualified: `{stats.final_candidates}`\n"
        f"Jupiter round-trips: `{complete_routes}`\n"
        f"Jupiter 429s: `{stats.jupiter_429s}`\n"
        f"Executable at current thresholds: `{executable}`\n\n"
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
