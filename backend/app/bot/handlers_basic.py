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
    "/rules - show your active rule\n"
    "/listrules - list your saved rule sets (with IDs)\n"
    "/balance - wallet / paper balance\n"
    "/positions - open positions\n"
    "/history - recent trades\n"
    "/help - this message\n\n"
    "*Admin only:*\n"
    "/connect - register your Anoncoin API key\n"
    "/connectwallet - register a wallet for live on-chain trading (base58 or JSON key)\n"
    "/disconnectwallet - remove your stored wallet key (confirm)\n"
    "/setrule - create a SOLANA rule set (Anoncoin + Pump.fun)\n"
    "/setrulefourmeme - create a separate FOUR.MEME / BSC rule set\n"
    "/activaterule <id> - activate a rule; activation is isolated by platform\n"
    "/enable - resume automated trading\n"
    "/disable - pause automated trading (confirm)\n"
    "/enableanoncoin - resume Anoncoin trading only\n"
    "/disableanoncoin - pause Anoncoin trading only (confirm)\n"
    "/enablepumpfun - resume Pump.fun trading only\n"
    "/disablepumpfun - pause Pump.fun trading only (confirm)\n"
    "/pumpfunsnipers - control Fast Sniper / Smart Filter (both can run together)\n"
    "/setfast <id> - assign a rule to the Pump.fun Fast Sniper lane\n"
    "/setsmart <id> - assign a rule to the Pump.fun Smart Filter lane\n"
    "/enablesmartmoney - enable copying the configured smart-money wallet\n"
    "/disablesmartmoney - disable smart-money wallet copying\n"
    "/paper - switch to paper mode (confirm)\n"
    "/live - switch to live mode (confirm)\n"
    "\nEach admin's rules run independently - activating or editing your rule "
    "never affects another admin's."
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
    fast_rule = await repo.get_active_rule_for_strategy(update.effective_user.id, "solana", "fast")
    smart_rule = await repo.get_active_rule_for_strategy(update.effective_user.id, "solana", "smart")
    fourmeme_rule = await repo.get_active_rule_for(update.effective_user.id, "fourmeme")
    smart_money_rule = None
    if hasattr(repo, "get_active_smart_money_rule"):
        smart_money_rule = await repo.get_active_smart_money_rule(update.effective_user.id, "solana")
    positions = await repo.get_open_positions()
    orders = await repo.get_recent_orders(3)

    anoncoin_key = await secrets_manager.get_anoncoin_api_key()
    anoncoin_connected = "yes" if anoncoin_key else "no (use /connect)"
    helius_connected = "yes" if settings.helius_api_key else "no"

    wallet_key = await secrets_manager.get_wallet_private_key(update.effective_user.id)
    wallet_line = "connected (use /balance to check funds)" if wallet_key else "not connected (use /connectwallet for live trading)"

    fast_rule_line = f"`{fast_rule.name}` (id {fast_rule.id})" if fast_rule else "none"
    smart_rule_line = f"`{smart_rule.name}` (id {smart_rule.id})" if smart_rule else "none"
    smart_money_rule_line = f"`{smart_money_rule.name}` (id {smart_money_rule.id})" if smart_money_rule else "none"
    fourmeme_rule_line = f"`{fourmeme_rule.name}` (id {fourmeme_rule.id})" if fourmeme_rule else "none - use /setrulefourmeme"
    recent_lines = "\n".join(
        f"  - {o.side} {o.mint[:6]}... [{o.status}]" for o in orders
    ) or "  - none yet"

    text = (
        f"*Mode:* {state.mode} | *Trading enabled:* {state.trading_enabled}\n"
        f"*Anoncoin trading:* {state.anoncoin_trading_enabled} | "
        f"*Pump.fun trading:* {state.pumpfun_trading_enabled} | "
        f"*Smart Money Copy:* {getattr(state, 'smart_money_copy_enabled', False)} | "
        f"*Four.meme trading:* {state.fourmeme_trading_enabled and settings.fourmeme_trading_enabled}\n"
        f"*Fast rule:* {fast_rule_line}\n"
        f"*Smart rule:* {smart_rule_line}\n"
        f"*Smart Money rule:* {smart_money_rule_line}\n"
        f"*Four.meme rule:* {fourmeme_rule_line}\n"
        f"*Connected APIs:* Anoncoin: {anoncoin_connected}, Helius: {helius_connected}\n"
        f"*Your wallet:* {wallet_line}\n"
        f"*Paper balance:* {state.paper_balance_sol:.3f} SOL\n"
        f"*Open positions:* {len(positions)}\n"
        f"*Recent trades:*\n{recent_lines}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sol_rule = await repo.get_active_rule_for(update.effective_user.id, "solana")
    fm_rule = await repo.get_active_rule_for(update.effective_user.id, "fourmeme")
    from app.storage.repository import rule_row_to_params

    def render(rule, label):
        if not rule:
            return f"*{label}:* none"
        p = rule_row_to_params(rule)
        buy = p.max_buy_size_bnb if p.platform == "fourmeme" else p.max_buy_size_sol
        unit = "BNB" if p.platform == "fourmeme" else "SOL"
        return (
            f"*{label}: {p.name}* (id {rule.id})\n"
            f"Max buy: {buy} {unit} | Min liquidity: ${p.min_liquidity_usd:,.0f} | Holders: {p.min_holders} | Age: {p.max_age_seconds}s\n"
            f"Market cap: ${p.min_market_cap_usd or 0:,.0f} - ${p.max_market_cap_usd or 0:,.0f} | Score: {p.qualify_score_threshold} | SL: {p.stop_loss_pct}% | Trail: {p.trailing_stop_pct or 'none'}"
        )

    await update.message.reply_text(
        render(sol_rule, "SOLANA (Anoncoin + Pump.fun)") + "\n\n" + render(fm_rule, "FOUR.MEME / BSC"),
        parse_mode="Markdown",
    )


async def listrules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    my_rules = await repo.get_rules_for_admin(update.effective_user.id)
    if not my_rules:
        await update.message.reply_text("You haven't saved any rule sets yet. Use /setrule to create one.")
        return
    lines = [f"- {'[ACTIVE] ' if r.is_active else ''}{r.name} (id {r.id}) [{getattr(r, 'platform', 'solana')}]" for r in my_rules]
    await update.message.reply_text(
        "*Your saved rule sets:*\n" + "\n".join(lines) + "\n\nSwitch with `/activaterule <id>`.",
        parse_mode="Markdown",
    )


async def activaterule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This action is restricted to the bot admin(s).")
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: `/activaterule <id>` - see /listrules for your rule IDs.", parse_mode="Markdown")
        return

    rule_id = int(args[0])
    rule = await repo.activate_rule_for_admin(rule_id, update.effective_user.id)
    if not rule:
        await update.message.reply_text(
            f"No rule with id {rule_id} found among your own rule sets. Check /listrules - "
            "you can only activate rules you created yourself."
        )
        return
    await repo.write_audit_log(str(update.effective_user.id), "activate_rule", {"rule_id": rule.id})
    await update.message.reply_text(
        f"Activated `{rule.name}` (id {rule.id}) as your active rule. This only affects your own trading, "
        "not other admins'.",
        parse_mode="Markdown",
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await repo.get_or_create_bot_state()
    if state.mode == "paper":
        await update.message.reply_text(f"Paper balance: {state.paper_balance_sol:.3f} SOL")
        return

    wallet_key = await secrets_manager.get_wallet_private_key(update.effective_user.id)
    if not wallet_key:
        await update.message.reply_text("No wallet connected. Use /connectwallet to trade live.")
        return

    from app.execution.onchain.solana_rpc import get_sol_balance
    from app.execution.onchain.wallet_keys import load_keypair

    try:
        keypair = load_keypair(wallet_key)
        sol_balance = await get_sol_balance(settings.solana_rpc_url, str(keypair.pubkey()))
        await update.message.reply_text(f"Wallet balance: {sol_balance:.4f} SOL\nAddress: `{keypair.pubkey()}`", parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Could not fetch wallet balance right now: {exc}")


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
