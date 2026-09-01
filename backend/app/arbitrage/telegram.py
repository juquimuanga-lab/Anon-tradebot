"""Telegram commands for the isolated Solana arbitrage subsystem."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.arbitrage.scanner import ArbitrageScanner
from app.arbitrage.service import ArbitrageService
from app.arbitrage.jupiter_quotes import configured_venues
from app.arbitrage.live_executor import ArbitrageLiveExecutor
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
        "/arbvenues — show configured venues\n"
        "/arblivestatus — show live execution gate\n"
        "/arblive <mint> <SOL> <buy_venue> <sell_venue> — explicitly submit one atomic bundle\n"
        "/arbhelp — show this help\n\n"
        "Observe mode uses live quotes but never submits transactions. Live execution requires ARBITRAGE_LIVE_TRADING_ENABLED=true and an explicitly invoked /arblive command.",
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
        await update.message.reply_text("No two-venue quote pairs were available for that token/size.\n" + detail + "\nUse /arbvenues to inspect the configured venue labels.")
        return
    lines = []
    for item in result.opportunities[:8]:
        status = "✅ QUALIFIED" if item.executable else "— rejected"
        lines.append(f"{status} `{item.buy_venue} → {item.sell_venue}` | net `{item.net_profit_bps:.1f} bps` | reason `{item.reason}`")
    rpc = result.rpc_health
    rpc_line = f"RPC: `{rpc.provider}`" if rpc and rpc.healthy else "RPC: `unavailable`"
    await update.message.reply_text("⚖️ *Arbitrage scan result*\n\n" + "\n".join(lines) + f"\n\n{rpc_line}\n_No transaction was submitted._", parse_mode="Markdown")


@admin_required
async def arbitrage_live_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = "ARMED" if live_executor.live_enabled else "LOCKED"
    await update.message.reply_text(
        "⚠️ *Arbitrage live execution*\n\n"
        f"Environment gate: `{state}`\n\n"
        "Default deployment state is LOCKED.\n"
        "The scanner cannot submit trades automatically.\n"
        "When explicitly armed, `/arblive` re-quotes both legs, applies the minimum-profit gate, simulates the required transactions, submits an atomic Jito bundle, waits for settlement, and reconciles every transaction signature.",
        parse_mode="Markdown",
    )


@admin_required
async def arbitrage_live_execute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) != 4:
        await update.message.reply_text(
            "Usage: `/arblive <mint> <SOL> <buy_venue> <sell_venue>`\n"
            "Example: `/arblive <mint> 0.05 raydium orca_whirlpool`\n\n"
            "Use /arbvenues for valid venue names.",
            parse_mode="Markdown",
        )
        return
    if not live_executor.live_enabled:
        await update.message.reply_text("🔒 Live arbitrage is LOCKED. Set `ARBITRAGE_LIVE_TRADING_ENABLED=true` in deployment before using /arblive.", parse_mode="Markdown")
        return

    token_mint, amount_raw, buy_name, sell_name = args
    try:
        amount_sol = float(amount_raw)
    except ValueError:
        await update.message.reply_text("SOL amount must be numeric.")
        return
    venues = {venue.name: venue for venue in configured_venues()}
    if buy_name not in venues or sell_name not in venues or buy_name == sell_name:
        await update.message.reply_text("Invalid venue pair. Use /arbvenues and choose two different venue names.")
        return

    await update.message.reply_text("⚠️ Re-quoting, building and simulating the two legs before atomic submission…")
    try:
        result = await live_executor.execute(
            owner_user_id=int(update.effective_user.id),
            token_mint=token_mint,
            amount_sol=amount_sol,
            buy_venue=venues[buy_name],
            sell_venue=venues[sell_name],
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Live arbitrage refused safely: `{type(exc).__name__}: {exc}`", parse_mode="Markdown")
        return

    if not result.success:
        signatures = "\n".join(f"`{sig}`" for sig in result.transaction_signatures[:3]) or "none"
        await update.message.reply_text(
            "🛑 *Arbitrage not settled*\n\n"
            f"Reason: `{result.reason}`\n"
            f"Settlement: `{result.settlement_status or 'not submitted'}`\n"
            f"Bundle ID: `{result.bundle_id or 'none'}`\n"
            f"Estimated net: `{result.estimated_net_profit_lamports / 1_000_000_000:.9f} SOL`\n"
            f"Transactions observed:\n{signatures}",
            parse_mode="Markdown",
        )
        return

    signatures = "\n".join(f"`{sig}`" for sig in result.transaction_signatures)
    await update.message.reply_text(
        "✅ *Arbitrage bundle settled*\n\n"
        f"Buy: `{result.buy_venue}`\n"
        f"Sell: `{result.sell_venue}`\n"
        f"Input: `{result.input_lamports / 1_000_000_000:.9f} SOL`\n"
        f"Guaranteed token amount: `{result.guaranteed_token_amount}`\n"
        f"Estimated net: `{result.estimated_net_profit_lamports / 1_000_000_000:.9f} SOL`\n"
        f"Settlement: `{result.settlement_status}`\n"
        f"Bundle ID: `{result.bundle_id}`\n"
        f"Transactions:\n{signatures}",
        parse_mode="Markdown",
    )
