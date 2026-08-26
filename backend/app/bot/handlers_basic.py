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
    "/status - mode, rules, balances, positions, and network status\n"
    "/rules - show your active rules by network\n"
    "/listrules - list your saved rule sets (with IDs and platforms)\n"
    "/balance - Solana wallet / paper balance\n"
    "/positions - open positions\n"
    "/history - recent trades\n"
    "/help - this message\n\n"
    "*Solana / Pump.fun:*\n"
    "/connectwallet - register a Solana wallet for live trading\n"
    "/disconnectwallet - remove your stored Solana wallet key (confirm)\n"
    "/setrule - create a SOLANA rule set (Anoncoin + Pump.fun)\n"
    "/paper - switch Solana to paper mode (confirm)\n"
    "/live - switch Solana to live mode (confirm)\n"
    "/enablepumpfun - resume Pump.fun trading only\n"
    "/disablepumpfun - pause Pump.fun trading only (confirm)\n"
    "/pumpfunsnipers - control Fast Sniper / Smart Filter\n"
    "/setfast <id> - assign a rule to the Pump.fun Fast Sniper lane\n"
    "/setsmart <id> - assign a rule to the Pump.fun Smart Filter lane\n"
    "/enablesmartmoney - enable smart-money wallet copying\n"
    "/disablesmartmoney - disable smart-money wallet copying\n\n"
    "*Robinhood Chain / Pons:*\n"
    "/connectrobinhoodwallet - connect your encrypted Robinhood Chain EVM wallet\n"
    "/robinhoodwallet - show Robinhood wallet, chain ID, ETH balance and Pons state\n"
    "/disconnectrobinhoodwallet - remove the encrypted Robinhood wallet key (confirm)\n"
    "/ponsstatus - show Pons/Robinhood status, wallet, ETH balance and mode\n"
    "/setrulepons - create a separate ROBINHOOD / Pons rule set (ETH)\n"
    "/ponslive - switch Pons to LIVE mode (confirm)\n"
    "/ponspaper - switch Pons to PAPER mode\n\n"
    "*Four.meme / BSC:*\n"
    "/connectbscwallet - connect the BSC trading wallet\n"
    "/disconnectbscwallet - remove the stored BSC wallet key (confirm)\n"
    "/setrulefourmeme - create a separate FOUR.MEME / BSC rule set\n"
    "/enablefourmeme - resume Four.meme trading\n"
    "/disablefourmeme - pause Four.meme trading (confirm)\n\n"
    "*Admin controls:*\n"
    "/connect - register your Anoncoin API key\n"
    "/activaterule <id> - activate a rule; activation is isolated by platform\n"
    "/enable - resume automated trading\n"
    "/disable - pause automated trading (confirm)\n"
    "/enableanoncoin - resume Anoncoin trading only\n"
    "/disableanoncoin - pause Anoncoin trading only (confirm)\n"
    "/guardian - GO Guardian AI health dashboard\n\n"
    "Solana uses SOL. Four.meme uses BNB. Robinhood/Pons uses ETH. "
    "Each network has its own wallet and trading mode."
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
    user_id = update.effective_user.id
    state = await repo.get_or_create_bot_state(user_id)
    fast_rule = await repo.get_active_rule_for_strategy(user_id, "solana", "fast")
    smart_rule = await repo.get_active_rule_for_strategy(user_id, "solana", "smart")
    fourmeme_rule = await repo.get_active_rule_for(user_id, "fourmeme")
    pons_rule = await repo.get_active_rule_for(user_id, "pons")
    smart_money_rule = None
    if hasattr(repo, "get_active_smart_money_rule"):
        smart_money_rule = await repo.get_active_smart_money_rule(user_id, "solana")
    positions = await repo.get_open_positions(user_id)
    orders = await repo.get_recent_orders(3, user_id)

    anoncoin_key = await secrets_manager.get_anoncoin_api_key()
    anoncoin_connected = "yes" if anoncoin_key else "no (use /connect)"
    helius_connected = "yes" if settings.helius_api_key else "no"

    wallet_key = await secrets_manager.get_wallet_private_key(user_id)
    wallet_line = "connected (SOL)" if wallet_key else "not connected (use /connectwallet)"

    rh_key = await secrets_manager.get_robinhood_wallet_private_key(user_id)
    rh_enabled = bool(getattr(settings, "robinhood_pons_trading_enabled", False))
    rh_mode = await secrets_manager.get_pons_mode(user_id) or "paper"
    rh_line = "not connected"
    rh_address = "-"
    rh_chain = "4663"
    rh_balance = "-"
    if rh_key:
        try:
            from app.execution.onchain.robinhood_wallet import (
                build_robinhood_web3,
                load_robinhood_account,
                resolve_robinhood_rpc_url,
            )
            account = load_robinhood_account(rh_key)
            w3 = build_robinhood_web3(resolve_robinhood_rpc_url(settings))
            rh_address = account.address
            rh_chain = str(int(w3.eth.chain_id))
            rh_balance = f"{int(w3.eth.get_balance(account.address)) / 10**18:.6f} ETH"
            rh_line = "connected"
        except Exception:
            rh_line = "connected (RPC unavailable)"

    fast_rule_line = f"`{fast_rule.name}` (id {fast_rule.id})" if fast_rule else "none"
    smart_rule_line = f"`{smart_rule.name}` (id {smart_rule.id})" if smart_rule else "none"
    smart_money_rule_line = f"`{smart_money_rule.name}` (id {smart_money_rule.id})" if smart_money_rule else "none"
    fourmeme_rule_line = f"`{fourmeme_rule.name}` (id {fourmeme_rule.id})" if fourmeme_rule else "none - use /setrulefourmeme"
    pons_rule_line = f"`{pons_rule.name}` (id {pons_rule.id})" if pons_rule else "none - use /setrulepons"
    recent_lines = "\n".join(f"  - {o.side} {o.mint[:6]}... [{o.status}]" for o in orders) or "  - none yet"

    text = (
        f"*Solana*\n"
        f"Mode: `{state.mode}` | Trading: `{state.trading_enabled}`\n"
        f"Anoncoin: `{state.anoncoin_trading_enabled}` | Pump.fun: `{state.pumpfun_trading_enabled}`\n"
        f"Fast rule: {fast_rule_line}\n"
        f"Smart rule: {smart_rule_line}\n"
        f"Smart Money Copy: `{getattr(state, 'smart_money_copy_enabled', False)}`\n"
        f"Wallet: `{wallet_line}`\n"
        f"Paper balance: `{state.paper_balance_sol:.3f} SOL`\n\n"
        f"*Robinhood Chain / Pons*\n"
        f"Deployment: `{'ON' if rh_enabled else 'OFF'}`\n"
        f"Mode: `{rh_mode.upper()}`\n"
        f"Wallet: `{rh_line}`\n"
        f"Address: `{rh_address}`\n"
        f"Chain ID: `{rh_chain}`\n"
        f"Balance: `{rh_balance}`\n"
        f"Rule: {pons_rule_line}\n"
        f"Currency: `ETH`\n\n"
        f"*Four.meme / BSC*\n"
        f"Trading: `{state.fourmeme_trading_enabled and settings.fourmeme_trading_enabled}`\n"
        f"Rule: {fourmeme_rule_line}\n\n"
        f"*Connected APIs*\n"
        f"Anoncoin: `{anoncoin_connected}` | Helius: `{helius_connected}`\n\n"
        f"*Positions:* `{len(positions)}`\n"
        f"*Recent trades:*\n{recent_lines}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sol_rule = await repo.get_active_rule_for(update.effective_user.id, "solana")
    fm_rule = await repo.get_active_rule_for(update.effective_user.id, "fourmeme")
    pons_rule = await repo.get_active_rule_for(update.effective_user.id, "pons")
    from app.storage.repository import rule_row_to_params

    def render(rule, label):
        if not rule:
            return f"*{label}:* none"
        p = rule_row_to_params(rule)
        if p.platform == "fourmeme":
            buy, unit = p.max_buy_size_bnb, "BNB"
        elif p.platform == "pons":
            buy, unit = p.max_buy_size_sol, "ETH"
        else:
            buy, unit = p.max_buy_size_sol, "SOL"
        return (
            f"*{label}: {p.name}* (id {rule.id})\n"
            f"Max buy: {buy} {unit} | Min liquidity: ${p.min_liquidity_usd:,.0f} | Holders: {p.min_holders} | Age: {p.max_age_seconds}s\n"
            f"Market cap: ${p.min_market_cap_usd or 0:,.0f} - ${p.max_market_cap_usd or 0:,.0f} | Score: {p.qualify_score_threshold} | SL: {p.stop_loss_pct}% | Trail: {p.trailing_stop_pct or 'none'}"
        )

    await update.message.reply_text(
        render(sol_rule, "SOLANA (Anoncoin + Pump.fun") + "\n\n" +
        render(fm_rule, "FOUR.MEME / BSC") + "\n\n" +
        render(pons_rule, "ROBINHOOD CHAIN / PONS (ETH)"),
        parse_mode="Markdown",
    )


async def listrules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    my_rules = await repo.get_rules_for_admin(update.effective_user.id)
    if not my_rules:
        await update.message.reply_text("You haven't saved any rule sets yet. Use /setrule or /setrulepons to create one.")
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
    """Show the user's real wallet balance independently of trading mode."""
    state = await repo.get_or_create_bot_state(update.effective_user.id)
    wallet_key = await secrets_manager.get_wallet_private_key(update.effective_user.id)

    if not wallet_key:
        await update.message.reply_text(
            f"Mode: {state.mode.upper()}\n"
            f"Paper balance: {state.paper_balance_sol:.3f} SOL\n\n"
            "No wallet connected. Use /connectwallet for live trading."
        )
        return

    from app.execution.onchain.solana_rpc import get_sol_balance
    from app.execution.onchain.wallet_keys import load_keypair

    try:
        keypair = load_keypair(wallet_key)
        address = str(keypair.pubkey())
        sol_balance = await get_sol_balance(settings.solana_rpc_url, address)

        await update.message.reply_text(
            f"Mode: {state.mode.upper()}\n"
            f"Wallet balance: {sol_balance:.4f} SOL\n"
            f"Paper balance: {state.paper_balance_sol:.3f} SOL\n"
            f"Address: `{address}`",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(
            f"Mode: {state.mode.upper()}\n"
            f"Paper balance: {state.paper_balance_sol:.3f} SOL\n\n"
            f"Could not fetch wallet balance right now: {exc}"
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

    open_positions = await repo.get_open_positions(update.effective_user.id)
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
    orders = await repo.get_recent_orders(10, update.effective_user.id)
    if not orders:
        await update.message.reply_text("No trade history yet.")
        return
    lines = [
        f"- {o.created_at:%Y-%m-%d %H:%M} {o.side.upper()} `{o.mint[:8]}` [{o.status}] {o.mode}"
        for o in orders
    ]
    await update.message.reply_text("*Recent trades:*\n" + "\n".join(lines), parse_mode="Markdown")
