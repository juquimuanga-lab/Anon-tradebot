"""Read-only commands available to any Telegram user."""
from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import settings
from app.security.allowlist import is_admin
from app.security.secrets_manager import secrets_manager
from app.storage import repository as repo

HELP_TEXT = (
    "*Anoncoin Sniper Bot*\n\n"
    "*Read-only (anyone):*\n"
    "/status - mode, rules, balance, positions\n"
    "/rules - show active rule set\n"
    "/listrules - list saved rule sets\n"
    "/balance - wallet / paper balance\n"
    "/positions - open positions\n"
    "/history - recent trades\n"
    "/help - this message\n\n"
    "*Admin only:*\n"
    "/connect - register your Anoncoin API key\n"
    "/setrule - create a rule set step by step\n"
    "/enable - resume automated trading\n"
    "/disable - pause automated trading (confirm)\n"
    "/paper - switch to paper mode (confirm)\n"
    "/live - switch to live mode (confirm)\n"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await repo.get_or_create_bot_state()
    await update.message.reply_text(
        "Welcome to the Anoncoin Sniper Bot.\nUse /help to see all commands. "
        "The bot starts in *paper trading* mode by default.",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await repo.get_or_create_bot_state()
    active_rule = await repo.get_active_rule()
    positions = await repo.get_open_positions()
    orders = await repo.get_recent_orders(3)

    anoncoin_key = await secrets_manager.get_anoncoin_api_key()
    anoncoin_connected = "yes" if anoncoin_key else "no (use /connect)"
    solscan_connected = "yes" if settings.solscan_api_key else "no"

    rule_line = f"`{active_rule.name}` (id {active_rule.id})" if active_rule else "none - use /setrule"
    recent_lines = "\n".join(
        f"  - {o.side} {o.mint[:6]}... [{o.status}]" for o in orders
    ) or "  - none yet"

    text = (
        f"*Mode:* {state.mode} | *Trading enabled:* {state.trading_enabled}\n"
        f"*Active rule:* {rule_line}\n"
        f"*Connected APIs:* Anoncoin: {anoncoin_connected}, Solscan: {solscan_connected}\n"
        f"*Paper balance:* {state.paper_balance_sol:.3f} SOL\n"
        f"*Open positions:* {len(positions)}\n"
        f"*Recent trades:*\n{recent_lines}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    active_rule = await repo.get_active_rule()
    if not active_rule:
        await update.message.reply_text("No active rule set yet. Use /setrule to create one.")
        return
    from app.storage.repository import rule_row_to_params

    p = rule_row_to_params(active_rule)
    text = (
        f"*Active rule: {p.name}*\n"
        f"Max buy: {p.max_buy_size_sol} SOL\n"
        f"Min liquidity: ${p.min_liquidity_usd:,.0f}\n"
        f"Min holders: {p.min_holders}\n"
        f"Max age: {p.max_age_seconds}s\n"
        f"Creator allowlist: {p.creator_allowlist or 'none'}\n"
        f"Creator denylist: {p.creator_denylist or 'none'}\n"
        f"Bonding curve phase: {p.bonding_curve_phase}\n"
        f"Market cap range: {p.min_market_cap_usd or 0} - {p.max_market_cap_usd or '∞'}\n"
        f"Max slippage: {p.max_slippage_pct}%\n"
        f"Max trades/hr: {p.max_trades_per_hour} | Cooldown: {p.cooldown_seconds}s\n"
        f"Take profit: {[(l.gain_pct, l.sell_pct) for l in p.take_profit_levels] or 'none'}\n"
        f"Stop loss: {p.stop_loss_pct}% | Trailing stop: {p.trailing_stop_pct or 'none'}\n"
        f"Sell on volume drop: {p.sell_on_volume_drop_pct or 'none'}\n"
        f"Time-based exit: {p.time_based_exit_seconds or 'none'}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def listrules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    all_rules = await repo.get_all_rules()
    if not all_rules:
        await update.message.reply_text("No rule sets saved yet. Use /setrule to create one.")
        return
    lines = [f"- {'[ACTIVE] ' if r.is_active else ''}{r.name} (id {r.id})" for r in all_rules]
    await update.message.reply_text("*Saved rule sets:*\n" + "\n".join(lines), parse_mode="Markdown")


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await repo.get_or_create_bot_state()
    if state.mode == "paper":
        await update.message.reply_text(f"Paper balance: {state.paper_balance_sol:.3f} SOL")
        return
    await update.message.reply_text(
        "Live balance lookup depends on Anoncoin's /my-profile endpoint, which their public docs "
        "currently mark as 'Coming Soon'. It will appear here automatically once Anoncoin ships it."
    )


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if args and args[0] == "close":
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("Manual close is restricted to the bot admin(s).")
            return
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("Usage: `/positions close <id>`", parse_mode="Markdown")
            return
        position_id = int(args[1])
        from app.bot.confirmations import confirmation_store

        token = confirmation_store.create("close_position", {"position_id": position_id})
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Confirm close", callback_data=f"confirm:{token}:yes"),
              InlineKeyboardButton("Cancel", callback_data=f"confirm:{token}:no")]]
        )
        await update.message.reply_text(f"Close position {position_id} now?", reply_markup=keyboard)
        return

    open_positions = await repo.get_open_positions()
    if not open_positions:
        await update.message.reply_text("No open positions.")
        return
    lines = []
    for p in open_positions:
        token = await repo.get_token(p.mint)
        ticker = token.ticker_symbol if token else p.mint[:8]
        lines.append(
            f"- `{ticker}` entry ${p.entry_price_usd:.6f} | invested {p.amount_sol_invested:.3f} SOL | "
            f"remaining {p.remaining_pct:.0f}% | id {p.id}"
        )
    text = "*Open positions:*\n" + "\n".join(lines)
    if is_admin(update.effective_user.id):
        text += "\n\nAdmin: send `/positions close <id>` to close manually."
    await update.message.reply_text(text, parse_mode="Markdown")


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    orders = await repo.get_recent_orders(10)
    if not orders:
        await update.message.reply_text("No trade history yet.")
        return
    lines = [
        f"- {o.created_at:%Y-%m-%d %H:%M} {o.side.upper()} `{o.mint[:8]}` [{o.status}] {o.mode}"
        for o in orders
    ]
    await update.message.reply_text("*Recent trades:*\n" + "\n".join(lines), parse_mode="Markdown")
