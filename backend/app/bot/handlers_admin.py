"""Admin-only commands: connecting the Anoncoin API key, rule activation
confirmations, kill switch, and paper/live mode switching."""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from app.bot.confirmations import confirmation_store
from app.bot.validation import is_plausible_api_key
from app.security.allowlist import admin_required
from app.security.redact import mask_secret
from app.security.secrets_manager import secrets_manager
from app.storage import repository as repo
from app.execution.onchain import rent_recovery
from app.config.settings import settings
from app.guardian import guardian

logger = logging.getLogger("app.bot.admin")

CONNECT_WAITING_KEY = 2
RENT_RECOVERY_WAITING_SIGNATURES = 3
BURN_CLOSE_WAITING_ACCOUNTS = 4


@admin_required
async def connect_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Send your Anoncoin profile API key now (generate it at anoncoin.it -> profile -> API Key). "
        "Your message will be deleted immediately after I store it encrypted. Send /cancel to abort."
    )
    return CONNECT_WAITING_KEY


async def connect_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    message_id = update.message.message_id

    if text == "/cancel":
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    if not is_plausible_api_key(text):
        await update.message.reply_text("That doesn't look like a valid API key. Try again or /cancel.")
        return CONNECT_WAITING_KEY

    await secrets_manager.set_anoncoin_api_key(text)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        logger.warning("could_not_delete_key_message")

    await repo.write_audit_log(str(update.effective_user.id), "connect_anoncoin_key", {})
    await update.message.reply_text(
        f"Anoncoin API key stored securely (ending in `{mask_secret(text)}`). "
        "It is encrypted at rest and will never be shown again.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


@admin_required
async def enable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await repo.update_bot_state(update.effective_user.id, trading_enabled=True)
    await repo.write_audit_log(str(update.effective_user.id), "enable_trading", {})
    await update.message.reply_text("Automated trading resumed.")


@admin_required
async def disable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm disable", callback_data="actionconfirm:disable_all:yes"),
          InlineKeyboardButton("Cancel", callback_data="actionconfirm:disable_all:no")]]
    )
    await update.message.reply_text(
        "This will immediately pause ALL automated trading - no new buys, and no "
        "automated stop loss / take profit / trailing stop on positions you already "
        "have open. Manual `/positions close <id>` still works while paused. Confirm?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@admin_required
async def enableanoncoin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await repo.update_bot_state(update.effective_user.id, anoncoin_trading_enabled=True)
    await repo.write_audit_log(str(update.effective_user.id), "enable_anoncoin", {})
    await update.message.reply_text("Anoncoin trading resumed (Pump.fun unaffected).")


@admin_required
async def disableanoncoin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm disable Anoncoin", callback_data="actionconfirm:disable_anoncoin:yes"),
          InlineKeyboardButton("Cancel", callback_data="actionconfirm:disable_anoncoin:no")]]
    )
    await update.message.reply_text(
        "This pauses new Anoncoin buys only - Pump.fun keeps trading, and any open "
        "Anoncoin positions you already hold keep their automated stop loss / take "
        "profit. Confirm?",
        reply_markup=keyboard,
    )


@admin_required
async def enablepumpfun_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await repo.update_bot_state(update.effective_user.id, pumpfun_trading_enabled=True)
    await repo.write_audit_log(str(update.effective_user.id), "enable_pumpfun", {})
    await update.message.reply_text("Pump.fun trading resumed (Anoncoin unaffected).")


@admin_required
async def enablefourmeme_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not settings.fourmeme_trading_enabled:
        await update.message.reply_text(
            "Four.meme is disabled at deployment level. Set FOURMEME_TRADING_ENABLED=true in Railway first, then run /enablefourmeme."
        )
        return
    await repo.update_bot_state(update.effective_user.id, fourmeme_trading_enabled=True)
    await repo.write_audit_log(str(update.effective_user.id), "enable_fourmeme", {})
    await update.message.reply_text("Four.meme trading resumed. Pump.fun and Anoncoin are unaffected.")


@admin_required
async def disablefourmeme_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm disable Four.meme", callback_data="actionconfirm:disable_fourmeme:yes"),
          InlineKeyboardButton("Cancel", callback_data="actionconfirm:disable_fourmeme:no")]]
    )
    await update.message.reply_text(
        "This pauses new Four.meme buys only. Pump.fun and Anoncoin keep trading, and existing Four.meme positions keep their automated exits. Confirm?",
        reply_markup=keyboard,
    )


@admin_required
async def disablepumpfun_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm disable Pump.fun", callback_data="actionconfirm:disable_pumpfun:yes"),
          InlineKeyboardButton("Cancel", callback_data="actionconfirm:disable_pumpfun:no")]]
    )
    await update.message.reply_text(
        "This pauses new Pump.fun buys only - Anoncoin keeps trading, and any open "
        "Pump.fun positions you already hold keep their automated stop loss / take "
        "profit. Confirm?",
        reply_markup=keyboard,
    )


@admin_required
async def paper_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm switch to PAPER", callback_data="actionconfirm:paper:yes"),
          InlineKeyboardButton("Cancel", callback_data="actionconfirm:paper:no")]]
    )
    await update.message.reply_text("Switch trading mode to PAPER?", reply_markup=keyboard)


@admin_required
async def live_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm switch to LIVE", callback_data="actionconfirm:live:yes"),
          InlineKeyboardButton("Cancel", callback_data="actionconfirm:live:no")]]
    )
    await update.message.reply_text(
        "*Warning:* LIVE mode places real on-chain trades using the wallet each rule's "
        "creator has connected with /connectwallet (real funds, real risk). If no wallet "
        "is connected for a rule's owner, its buys/sells will fail safely with a clear "
        "message instead of doing nothing silently. Confirm switch to LIVE?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@admin_required
async def pumpfun_snipers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show and control the independent Pump.fun Fast/Smart/Smart-Money lanes."""
    owner_id = update.effective_user.id
    state = await repo.get_or_create_bot_state(owner_id)
    rules = [r for r in await repo.get_rules_for_admin(owner_id) if getattr(r, "platform", "solana") == "solana"]
    fast_rule = next((r for r in rules if r.is_active and getattr(r, "strategy", "smart") == "fast"), None)
    smart_rule = next((r for r in rules if r.is_active and getattr(r, "strategy", "smart") == "smart"), None)
    smart_money_rule = None
    if hasattr(repo, "get_active_smart_money_rule"):
        smart_money_rule = await repo.get_active_smart_money_rule(owner_id, "solana")

    text = (
        "⚡ *Pump.fun Sniper Control*\n\n"
        f"Master Pump.fun: {'ON' if state.pumpfun_trading_enabled else 'OFF'}\n"
        f"⚡ Fast Sniper: {'ON' if getattr(state, 'pumpfun_fast_enabled', False) else 'OFF'}\n"
        f"🧠 Smart Filter: {'ON' if getattr(state, 'pumpfun_smart_enabled', True) else 'OFF'}\n"
        f"👁 Smart Money Copy: {'ON' if getattr(state, 'smart_money_copy_enabled', False) else 'OFF'}\n"
        f"⚡ Fast rule: #{fast_rule.id if fast_rule else 'none'} {fast_rule.name if fast_rule else 'none'}\n"
        f"🧠 Smart rule: #{smart_rule.id if smart_rule else 'none'} {smart_rule.name if smart_rule else 'none'}\n"
        f"👁 Smart Money rule: #{smart_money_rule.id if smart_money_rule else 'none'} {smart_money_rule.name if smart_money_rule else 'none'}\n\n"
        "Fast rules intentionally skip holder/score checks and use only the launch-time safety gate. "
        "Smart rules use the full quality/score pipeline."
    )
    buttons = [
        [InlineKeyboardButton(
            f"⚡ Fast {'OFF' if getattr(state, 'pumpfun_fast_enabled', False) else 'ON'}",
            callback_data=f"sniper:toggle:fast:{'off' if getattr(state, 'pumpfun_fast_enabled', False) else 'on'}"
        ),
         InlineKeyboardButton(
            f"🧠 Smart {'OFF' if getattr(state, 'pumpfun_smart_enabled', True) else 'ON'}",
            callback_data=f"sniper:toggle:smart:{'off' if getattr(state, 'pumpfun_smart_enabled', True) else 'on'}"
        )],
        [InlineKeyboardButton(
            f"👁 Copy Wallet {'OFF' if getattr(state, 'smart_money_copy_enabled', False) else 'ON'}",
            callback_data=f"sniper:toggle:copy:{'off' if getattr(state, 'smart_money_copy_enabled', False) else 'on'}"
        )]
    ]
    for rule in rules[:12]:
        buttons.append([
            InlineKeyboardButton(f"Use #{rule.id} as ⚡ Fast", callback_data=f"sniper:rule:fast:{rule.id}"),
            InlineKeyboardButton(f"Use #{rule.id} as 🧠 Smart", callback_data=f"sniper:rule:smart:{rule.id}"),
        ])
        if hasattr(repo, "set_smart_money_rule"):
            buttons.append([
                InlineKeyboardButton(
                    f"Use #{rule.id} as 👁 Smart Money",
                    callback_data=f"sniper:rule:smart_money:{rule.id}",
                )
            ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def pumpfun_snipers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    from app.security.allowlist import is_admin
    if not is_admin(update.effective_user.id):
        await query.edit_message_text("Restricted to admin.")
        return
    parts = query.data.split(":")
    owner_id = update.effective_user.id
    if len(parts) < 4:
        await query.edit_message_text("Invalid sniper control.")
        return
    action = parts[1]
    kind = parts[2]
    value = parts[3]
    if action == "toggle":
        enabled = value == "on"
        if kind == "copy":
            await repo.update_bot_state(owner_id, smart_money_copy_enabled=enabled)
            await repo.write_audit_log(str(owner_id), f"smart_money_copy_{value}", {
                "wallet": "HmUt3Jn46j7c7ANdURmEyjSRj8i3Em6MhjQUi37PZ219",
            })
            await query.edit_message_text(
                f"👁 Smart Money Copy: {'ON' if enabled else 'OFF'}\n\n"
                "The configured smart-money wallet is now "
                + ("being copied for new Pump.fun buys." if enabled else "ignored for new buys.")
            )
            return

        field = "pumpfun_fast_enabled" if kind == "fast" else "pumpfun_smart_enabled"
        await repo.update_bot_state(owner_id, **{field: enabled})
        await repo.write_audit_log(str(owner_id), f"pumpfun_{kind}_{value}", {})
        await query.edit_message_text(f"Pump.fun {'Fast Sniper' if kind == 'fast' else 'Smart Filter'}: {'ON' if enabled else 'OFF'}")
        return
    if action == "rule":
        try:
            rule_id = int(value)
        except ValueError:
            await query.edit_message_text("Invalid rule ID.")
            return
        if kind == "smart_money":
            if not hasattr(repo, "set_smart_money_rule"):
                await query.edit_message_text(
                    "Smart Money rule support is not deployed yet. "
                    "Deploy the matching repository patch first."
                )
                return
            rule = await repo.set_smart_money_rule(rule_id, owner_id)
            if not rule:
                await query.edit_message_text("Rule not found or does not belong to you.")
                return
            await repo.write_audit_log(
                str(owner_id),
                "assign_pumpfun_smart_money_rule",
                {"rule_id": rule.id},
            )
            await query.edit_message_text(
                f"Rule #{rule.id} ({rule.name}) is now the independent Pump.fun Smart Money rule."
            )
            return

        rule = await repo.activate_rule_for_admin_strategy(rule_id, owner_id, kind)
        if not rule:
            await query.edit_message_text("Rule not found or does not belong to you.")
            return
        await repo.write_audit_log(str(owner_id), f"activate_pumpfun_{kind}_rule", {"rule_id": rule.id})
        await query.edit_message_text(f"Rule #{rule.id} ({rule.name}) is now the active Pump.fun {kind.title()} rule.")
        return
    await query.edit_message_text("Unknown sniper control.")


@admin_required
async def setfast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """CLI shortcut: /setfast RULE_ID"""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /setfast RULE_ID")
        return
    rule = await repo.activate_rule_for_admin_strategy(int(context.args[0]), update.effective_user.id, "fast")
    await update.message.reply_text(
        f"Rule #{rule.id} is now the active Pump.fun Fast Sniper rule." if rule else "Rule not found or it is not yours."
    )


@admin_required
async def setsmart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """CLI shortcut: /setsmart RULE_ID"""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /setsmart RULE_ID")
        return
    rule = await repo.activate_rule_for_admin_strategy(int(context.args[0]), update.effective_user.id, "smart")
    await update.message.reply_text(
        f"Rule #{rule.id} is now the active Pump.fun Smart Filter rule." if rule else "Rule not found or it is not yours."
    )


@admin_required
async def setsmartmoney_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Assign an admin-owned Solana rule to the independent Smart Money copy lane."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Usage: /setsmartmoney RULE_ID\n\n"
            "Example: /setsmartmoney 1"
        )
        return

    rule_id = int(context.args[0])
    rule = await repo.set_smart_money_rule(rule_id, update.effective_user.id)

    if not rule:
        await update.message.reply_text(
            f"❌ Rule #{rule_id} was not found, does not belong to you, "
            "or is not a Solana rule."
        )
        return

    await repo.write_audit_log(
        str(update.effective_user.id),
        "assign_pumpfun_smart_money_rule",
        {"rule_id": rule.id},
    )
    await update.message.reply_text(
        f"👁 Rule #{rule.id} ({rule.name}) is now the independent "
        "Pump.fun Smart Money rule.\n\n"
        "Use /enablesmartmoney to turn Smart Money copying ON."
    )


@admin_required
async def enablesmartmoney_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enable copy-trading from the configured smart-money wallet(s)."""
    await repo.update_bot_state(
        update.effective_user.id,
        smart_money_copy_enabled=True,
    )
    await repo.write_audit_log(
        str(update.effective_user.id),
        "smart_money_copy_on",
        {},
    )
    await update.message.reply_text(
        "🧠 Smart Money Copy: ON\n\n"
        "New buys from the configured wallet will be copied using your independent Smart Money rule. "
        "Your existing position exits/TP/SL rules remain unchanged."
    )


@admin_required
async def disablesmartmoney_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disable copy-trading without changing existing positions."""
    await repo.update_bot_state(
        update.effective_user.id,
        smart_money_copy_enabled=False,
    )
    await repo.write_audit_log(
        str(update.effective_user.id),
        "smart_money_copy_off",
        {},
    )
    await update.message.reply_text(
        "🧠 Smart Money Copy: OFF\n\n"
        "No new wallet buys will be copied. Existing positions keep their normal exits."
    )


async def action_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle simple confirmations without an expiring in-memory token."""
    query = update.callback_query
    await query.answer()

    from app.security.allowlist import is_admin
    if not is_admin(update.effective_user.id):
        await query.edit_message_text("Restricted to admin.")
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.edit_message_text("Invalid confirmation.")
        return

    _, action, decision = parts
    if decision != "yes":
        await query.edit_message_text("Cancelled.")
        return

    owner_id = update.effective_user.id

    if action == "disable_all":
        await repo.update_bot_state(owner_id, trading_enabled=False)
        await repo.write_audit_log(str(owner_id), "disable_all_confirmed", {})
        await query.edit_message_text("All automated trading has been paused.")
        return

    if action == "disable_anoncoin":
        await repo.update_bot_state(owner_id, anoncoin_trading_enabled=False)
        await repo.write_audit_log(str(owner_id), "disable_anoncoin_confirmed", {})
        await query.edit_message_text("Anoncoin trading paused. Pump.fun is unaffected.")
        return

    if action == "disable_pumpfun":
        await repo.update_bot_state(owner_id, pumpfun_trading_enabled=False)
        await repo.write_audit_log(str(owner_id), "disable_pumpfun_confirmed", {})
        await query.edit_message_text("Pump.fun trading paused. Anoncoin and Four.meme are unaffected.")
        return

    if action == "disable_fourmeme":
        await repo.update_bot_state(owner_id, fourmeme_trading_enabled=False)
        await repo.write_audit_log(str(owner_id), "disable_fourmeme_confirmed", {})
        await query.edit_message_text("Four.meme trading paused. Pump.fun and Anoncoin are unaffected.")
        return

    if action in ("paper", "live"):
        await repo.update_bot_state(owner_id, mode=action)
        await repo.write_audit_log(str(owner_id), "switch_mode", {"mode": action})
        await query.edit_message_text(f"Trading mode switched to {action.upper()}.")
        return

    await query.edit_message_text("Unknown confirmation action.")


async def confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    from app.security.allowlist import is_admin

    if not is_admin(update.effective_user.id):
        await query.edit_message_text("Restricted to admin.")
        return

    _, token, decision = query.data.split(":", 2)
    entry = confirmation_store.resolve(token)
    if not entry:
        await query.edit_message_text("This confirmation has expired. Please run the command again.")
        return

    if decision in ("no", "discard"):
        await query.edit_message_text("Cancelled.")
        return

    if entry.action == "disable_all":
        await repo.update_bot_state(update.effective_user.id, trading_enabled=False)
        await repo.write_audit_log(str(update.effective_user.id), "disable_all_confirmed", {})
        await query.edit_message_text("All automated trading has been paused.")

    elif entry.action == "disable_anoncoin":
        await repo.update_bot_state(update.effective_user.id, anoncoin_trading_enabled=False)
        await repo.write_audit_log(str(update.effective_user.id), "disable_anoncoin_confirmed", {})
        await query.edit_message_text("Anoncoin trading paused. Pump.fun is unaffected.")

    elif entry.action == "disable_pumpfun":
        await repo.update_bot_state(update.effective_user.id, pumpfun_trading_enabled=False)
        await repo.write_audit_log(str(update.effective_user.id), "disable_pumpfun_confirmed", {})
        await query.edit_message_text("Pump.fun trading paused. Anoncoin and Four.meme are unaffected.")

    elif entry.action == "disable_fourmeme":
        await repo.update_bot_state(update.effective_user.id, fourmeme_trading_enabled=False)
        await repo.write_audit_log(str(update.effective_user.id), "disable_fourmeme_confirmed", {})
        await query.edit_message_text("Four.meme trading paused. Pump.fun and Anoncoin are unaffected.")

    elif entry.action == "switch_mode":
        mode = entry.payload["mode"]
        await repo.update_bot_state(update.effective_user.id, mode=mode)
        await repo.write_audit_log(str(update.effective_user.id), "switch_mode", {"mode": mode})
        await query.edit_message_text(f"Trading mode switched to {mode.upper()}.")

    elif entry.action == "save_rule":
        from app.scoring.rules import RuleParams

        params = RuleParams(**entry.payload["params"])

        # The /setrule wizard presents the lane choice at the final confirmation.
        # Fast and Smart remain independent lanes.
        if decision in ("save_fast", "save_smart", "save_smart_money"):
            strategy = (
                "fast"
                if decision == "save_fast"
                else "smart_money"
                if decision == "save_smart_money"
                else "smart"
            )
            params.strategy = strategy

            if strategy == "smart_money":
                rule = await repo.create_rule(
                    params,
                    entry.payload["user_id"],
                    activate=True,
                )
                assigned = await repo.set_smart_money_rule(
                    rule.id,
                    entry.payload["user_id"],
                )
                if not assigned:
                    await query.edit_message_text(
                        f"Rule '{rule.name}' was created, but Smart Money assignment failed. "
                        f"Use /setsmartmoney {rule.id} to assign it manually."
                    )
                    return
            else:
                rule = await repo.create_rule(
                    params,
                    entry.payload["user_id"],
                    activate=True,
                )
            await repo.write_audit_log(
                str(update.effective_user.id),
                "save_rule_strategy",
                {"rule_id": rule.id, "strategy": strategy, "activated": True},
            )
            lane = (
                "⚡ Fast Sniper"
                if strategy == "fast"
                else "🐋 Smart Money Copy"
                if strategy == "smart_money"
                else "🧠 Smart Filter"
            )
            if strategy == "smart_money":
                await query.edit_message_text(
                    f"Rule '{rule.name}' saved and activated as {lane}.\n\n"
                    "🐋 Smart Money Copy is now assigned to this independent rule.\n"
                    "Use /enablesmartmoney to turn Smart Money copying ON."
                )
            else:
                await query.edit_message_text(
                    f"Rule '{rule.name}' saved and activated as {lane}.\n\n"
                    "This changes only that sniper lane; the other lanes keep their own active rules."
                )
        else:
            # Save-only never activates either lane.
            rule = await repo.create_rule(
                params,
                entry.payload["user_id"],
                activate=False,
            )
            await repo.write_audit_log(
                str(update.effective_user.id),
                "save_rule",
                {"rule_id": rule.id, "activated": False},
            )
            await query.edit_message_text(
                f"Rule '{rule.name}' saved (not activated).\n\n"
                "Use /setfast, /setsmart, /setsmartmoney, or /pumpfunsnipers to assign it to a lane."
            )

    elif entry.action == "close_position":
        position_manager = context.application.bot_data.get("position_manager")
        position_id = entry.payload["position_id"]
        ok = await position_manager.close_position_manually(position_id) if position_manager else False
        await repo.write_audit_log(str(update.effective_user.id), "manual_close_position", {"position_id": position_id, "ok": ok})
        await query.edit_message_text(f"Position {position_id} closed." if ok else "Position not found or already closed.")

    elif entry.action == "disconnect_wallet":
        await secrets_manager.delete_wallet_private_key(entry.payload["user_id"])
        await repo.write_audit_log(str(update.effective_user.id), "disconnect_wallet", {})
        await query.edit_message_text("Wallet disconnected and key deleted.")

# ---------------------------------------------------------------------------
# Burn + close token accounts
# ---------------------------------------------------------------------------


@admin_required
async def burnclose_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the explicit admin-only full-balance burn + close flow."""
    context.user_data.pop("burn_close_accounts", None)
    context.user_data.pop("burn_close_wallet", None)
    await update.message.reply_text(
        "🔥 *Burn + Close*\n\n"
        "Send the *token-account address(es)* you want to clean. "
        "Use the token-account address, not the mint address.\n\n"
        "• Up to 20 addresses, separated by commas or new lines.\n"
        "• The bot verifies that every account belongs to the connected sniper wallet.\n"
        "• If the balance is non-zero, the *entire token balance is burned*.\n"
        "• After the balance is zero, the token account is closed and its SOL rent is returned.\n"
        "• The bot will NOT automatically burn every token in the wallet.\n\n"
        "⚠️ Burning is permanent. Only submit tokens you intentionally want to destroy.\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return BURN_CLOSE_WAITING_ACCOUNTS


async def burnclose_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text == "/cancel":
        context.user_data.pop("burn_close_accounts", None)
        context.user_data.pop("burn_close_wallet", None)
        await update.message.reply_text("Burn + close cancelled. Nothing was burned or closed.")
        return ConversationHandler.END

    try:
        keypair = await rent_recovery.resolve_wallet_keypair(
            context.application, str(update.effective_user.id)
        )
        wallet = str(keypair.pubkey())
        accounts = await rent_recovery.scan_burn_close(
            settings.solana_rpc_url, wallet, text
        )
    except Exception as exc:
        logger.warning("burn_close_scan_failed", extra={"error": str(exc)})
        await update.message.reply_text(
            "❌ Burn + close scan failed:\n"
            f"{rent_recovery.redact_text(str(exc))}\n\n"
            "Nothing was burned or closed. Try again or /cancel."
        )
        return BURN_CLOSE_WAITING_ACCOUNTS

    context.user_data["burn_close_accounts"] = text
    context.user_data["burn_close_wallet"] = wallet

    total_rent = sum(item.lamports for item in accounts)
    burn_count = sum(1 for item in accounts if item.amount > 0)
    zero_count = len(accounts) - burn_count
    rows = []
    for item in accounts[:12]:
        balance = item.ui_amount if item.ui_amount is not None else item.amount
        rows.append(
            f"• `{item.address}`\n"
            f"  Balance: `{balance}` | Rent: `{rent_recovery.format_sol(item.lamports)} SOL`"
        )
    if len(accounts) > 12:
        rows.append(f"• … and {len(accounts) - 12} more")

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🔥 Burn + Close", callback_data="burnclose:confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="burnclose:cancel"),
        ]]
    )
    message = (
        "🔎 *Burn + Close Preview*\n\n"
        f"Accounts: *{len(accounts)}*\n"
        f"Accounts with tokens to burn: *{burn_count}*\n"
        f"Already-zero accounts: *{zero_count}*\n"
        f"SOL rent to recover: *{rent_recovery.format_sol(total_rent)} SOL*\n\n"
        "*Accounts:*\n" + "\n".join(rows) + "\n\n"
        "⚠️ Confirming will permanently burn the full balance of every non-zero account, "
        "then close those accounts and return their rent."
    )
    await update.message.reply_text(
        message, parse_mode="Markdown", reply_markup=keyboard
    )
    return ConversationHandler.END


async def burnclose_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-check and execute the explicit burn + close confirmation."""
    query = update.callback_query
    await query.answer()

    from app.security.allowlist import is_admin

    if not is_admin(update.effective_user.id):
        await query.edit_message_text("Restricted to admin.")
        return

    action = query.data.split(":", 1)[1]
    account_text = context.user_data.get("burn_close_accounts")
    expected_wallet = context.user_data.get("burn_close_wallet")

    if action == "cancel":
        context.user_data.pop("burn_close_accounts", None)
        context.user_data.pop("burn_close_wallet", None)
        await query.edit_message_text("Burn + close cancelled. Nothing was burned or closed.")
        return

    if action != "confirm" or not account_text or not expected_wallet:
        await query.edit_message_text("This burn + close confirmation has expired. Run /burnclose again.")
        return

    await query.edit_message_text("⏳ Re-checking balances, burning tokens, and closing accounts…")

    try:
        keypair = await rent_recovery.resolve_wallet_keypair(
            context.application, str(update.effective_user.id)
        )
        if str(keypair.pubkey()) != expected_wallet:
            raise rent_recovery.RentRecoveryError(
                "The connected wallet changed after the scan. Nothing was burned or closed."
            )

        accounts = await rent_recovery.scan_burn_close(
            settings.solana_rpc_url, expected_wallet, account_text
        )
        result = await rent_recovery.burn_and_close(
            settings.solana_rpc_url, keypair, accounts
        )

        tx_lines = [
            f"• https://solscan.io/tx/{signature}"
            for signature in result.get("transactions", [])
        ]
        failures = result.get("failed") or []
        message = (
            "✅ *Burn + Close Finished*\n\n"
            f"Accounts closed: *{result.get('closed', 0)}*\n"
            f"Accounts burned: *{result.get('burned_accounts', 0)}*\n"
            f"SOL recovered: *{rent_recovery.format_sol(result.get('recovered_lamports', 0))} SOL*\n"
            f"Transactions: *{len(tx_lines)}*"
        )
        if tx_lines:
            message += "\n\n*Confirmed transactions:*\n" + "\n".join(tx_lines)
        if failures:
            message += "\n\n⚠️ *Some batches failed:*\n" + "\n".join(
                f"• {item}" for item in failures[:5]
            )
        await query.edit_message_text(message, parse_mode="Markdown")
        await repo.write_audit_log(
            str(update.effective_user.id),
            "burn_and_close",
            {
                "wallet": expected_wallet,
                "accounts_requested": len(accounts),
                "burned_accounts": result.get("burned_accounts", 0),
                "closed_accounts": result.get("closed", 0),
                "recovered_lamports": result.get("recovered_lamports", 0),
                "transactions": result.get("transactions", []),
                "failed_batches": len(failures),
            },
        )
    except Exception as exc:
        logger.exception("burn_close_failed")
        await query.edit_message_text(
            "❌ *Burn + close failed.*\n\n"
            f"{rent_recovery.redact_text(str(exc))}\n\n"
            "No further accounts were attempted after the failure.",
            parse_mode="Markdown",
        )
    finally:
        context.user_data.pop("burn_close_accounts", None)
        context.user_data.pop("burn_close_wallet", None)


# ---------------------------------------------------------------------------
# SOL rent recovery
# ---------------------------------------------------------------------------

@admin_required
async def recoverent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the admin-only token-account rent recovery flow."""
    context.user_data.pop("rent_recovery_scan", None)
    await update.message.reply_text(
        "🧹 *SOL Rent Recovery*\n\n"
        "Send the *BUY transaction signature(s) or SELL transaction signature(s)* from the sniper trades you want to recover rent from.\n\n"
        "• You can send multiple confirmed signatures separated by commas.\n"
        "• It must be the actual BUY or SELL transaction — not the token address, wallet address, or a recovery transaction.\n"
        "• Every signature must be an actual BUY or SELL transaction for the connected sniper wallet.\n"
        "• The bot will use those trade transactions to find token accounts belonging to the connected sniper wallet that are now empty.\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return RENT_RECOVERY_WAITING_SIGNATURES


async def recoverent_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Scan submitted transactions and present an explicit recovery confirmation."""
    text = (update.message.text or "").strip()
    if text == "/cancel":
        context.user_data.pop("rent_recovery_scan", None)
        await update.message.reply_text("Rent recovery cancelled.")
        return ConversationHandler.END

    try:
        keypair = await rent_recovery.resolve_wallet_keypair(
            context.application,
            str(update.effective_user.id),
        )
        wallet = str(keypair.pubkey())
        scan = await rent_recovery.scan_rent_recovery(
            settings.solana_rpc_url,
            wallet,
            text,
        )
    except Exception as exc:
        logger.warning(
            "rent_recovery_scan_failed",
            extra={"error": str(exc)},
        )
        await update.message.reply_text(
            "❌ Rent recovery scan failed:\n"
            f"{rent_recovery.redact_text(str(exc))}\n\n"
            "Nothing was closed.",
        )
        return RENT_RECOVERY_WAITING_SIGNATURES

    context.user_data["rent_recovery_scan"] = scan
    context.user_data["rent_recovery_wallet"] = wallet

    if not scan.eligible:
        skipped = "\n".join(f"• {item}" for item in scan.skipped[:8])
        message = (
            "🔎 *Rent Recovery Scan*\n\n"
            f"Transactions scanned: {len(scan.signatures)}\n"
            "Recoverable token accounts: *0*\n\n"
            "No eligible empty token accounts were found."
        )
        if skipped:
            message += "\n\nReasons:\n" + skipped
        await update.message.reply_text(message, parse_mode="Markdown")
        context.user_data.pop("rent_recovery_scan", None)
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("💰 Recover SOL", callback_data="rent:confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="rent:cancel"),
        ]]
    )

    skipped_count = len(scan.skipped)
    message = (
        "🔎 *Rent Recovery Scan Complete*\n\n"
        f"Transactions scanned: {len(scan.signatures)}\n"
        f"Eligible empty token accounts: *{len(scan.eligible)}*\n"
        f"Potential rent: *{rent_recovery.format_sol(scan.gross_lamports)} SOL*\n"
        f"Skipped/ignored items: {skipped_count}\n\n"
        "⚠️ The bot will only issue `CloseAccount` for accounts that are still "
        "open, belong to this connected wallet, and have an exact zero token balance.\n\n"
        "Proceed?"
    )
    await update.message.reply_text(
        message,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return ConversationHandler.END


async def rent_recovery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the rent-recovery confirmation buttons."""
    query = update.callback_query
    await query.answer()

    from app.security.allowlist import is_admin

    if not is_admin(update.effective_user.id):
        await query.edit_message_text("Restricted to admin.")
        return

    action = query.data.split(":", 1)[1]
    scan = context.user_data.get("rent_recovery_scan")
    expected_wallet = context.user_data.get("rent_recovery_wallet")

    if action == "cancel":
        context.user_data.pop("rent_recovery_scan", None)
        context.user_data.pop("rent_recovery_wallet", None)
        await query.edit_message_text("Rent recovery cancelled. No accounts were closed.")
        return

    if action != "confirm" or scan is None or not expected_wallet:
        await query.edit_message_text(
            "This rent-recovery confirmation has expired. Run /recoverent again."
        )
        return

    await query.edit_message_text("⏳ Re-checking the wallet and closing eligible accounts…")

    try:
        keypair = await rent_recovery.resolve_wallet_keypair(
            context.application,
            str(update.effective_user.id),
        )
        if str(keypair.pubkey()) != expected_wallet:
            raise rent_recovery.RentRecoveryError(
                "The connected wallet changed after the scan. Nothing was closed."
            )

        result = await rent_recovery.recover_rent(
            settings.solana_rpc_url,
            keypair,
            scan,
        )

        tx_lines = [
            f"• https://solscan.io/tx/{signature}"
            for signature in result.get("transactions", [])
        ]
        failures = result.get("failed") or []

        message = (
            "✅ *Rent Recovery Finished*\n\n"
            f"Accounts closed: *{result.get('closed', 0)}*\n"
            f"SOL recovered: *{rent_recovery.format_sol(result.get('recovered_lamports', 0))} SOL*\n"
            f"Potential scanned rent: {rent_recovery.format_sol(result.get('gross_lamports', 0))} SOL\n"
            f"Recovery transactions: {len(tx_lines)}"
        )
        if tx_lines:
            message += "\n\n*Confirmed transactions:*\n" + "\n".join(tx_lines)
        if failures:
            message += "\n\n⚠️ *Some batches failed:*\n" + "\n".join(
                f"• {item}" for item in failures[:5]
            )
        await query.edit_message_text(message, parse_mode="Markdown")
        await repo.write_audit_log(
            str(update.effective_user.id),
            "rent_recovery",
            {
                "wallet": expected_wallet,
                "eligible_accounts": len(scan.eligible),
                "closed_accounts": result.get("closed", 0),
                "recovered_lamports": result.get("recovered_lamports", 0),
                "transactions": result.get("transactions", []),
                "failed_batches": len(failures),
            },
        )
    except Exception as exc:
        logger.exception("rent_recovery_failed")
        await query.edit_message_text(
            "❌ *Rent recovery failed.*\n\n"
            f"{rent_recovery.redact_text(str(exc))}\n\n"
            "No further accounts were attempted after the failure.",
            parse_mode="Markdown",
        )
    finally:
        context.user_data.pop("rent_recovery_scan", None)
        context.user_data.pop("rent_recovery_wallet", None)



@admin_required
async def guardian_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    snap = await guardian.snapshot(update.effective_user.id)
    status = snap["status"]
    top = ", ".join(f"{name}:{count}" for name, count in snap["top_rejections"]) or "none"
    text = (
        "🧠 *GO Guardian AI*\n\n"
        f"Status: *{status}*\n"
        f"Guardian: {'ON' if snap['enabled'] else 'OFF'}\n"
        f"Auto pause: {'ON' if snap['auto_pause'] else 'OFF'}\n"
        f"Trading: {'ON' if snap['trading_enabled'] else 'PAUSED'}\n\n"
        f"Last {snap['window_seconds']}s\n"
        f"• Candidates: {snap['candidates']}\n"
        f"• Qualified: {snap['qualified']}\n"
        f"• Buy attempts: {snap['buy_attempts']}\n"
        f"• Buy success: {snap['buy_success']}\n"
        f"• Buy failed: {snap['buy_failed']}\n"
        f"• Smart Money buys: {snap['smart_money_buys']}\n"
        f"• Top rejections: {top}\n\n"
        f"*Diagnosis:* {snap['diagnosis']}"
    )
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Pause", callback_data="guardian:pause"), InlineKeyboardButton("▶ Resume", callback_data="guardian:resume")],
        [InlineKeyboardButton("Auto Pause ON/OFF", callback_data="guardian:autopause")],
    ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=buttons)


@admin_required
async def guardian_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    owner = update.effective_user.id
    action = (q.data or "").split(":", 1)[-1]
    if action == "pause":
        await guardian.pause(owner)
        await q.edit_message_text("🛑 GO Guardian paused automated trading for your account.")
        return
    if action == "resume":
        await guardian.resume(owner)
        await q.edit_message_text("▶ GO Guardian resumed automated trading for your account.")
        return
    if action == "autopause":
        state = await repo.get_or_create_bot_state(owner)
        enabled = not bool(getattr(state, "guardian_auto_pause_enabled", True))
        await repo.update_bot_state(owner, guardian_auto_pause_enabled=enabled)
        await q.edit_message_text(f"GO Guardian auto-pause is now {'ON' if enabled else 'OFF'}.")
        return
