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

logger = logging.getLogger("app.bot.admin")

CONNECT_WAITING_KEY = 2


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
    await repo.update_bot_state(trading_enabled=True)
    await repo.write_audit_log(str(update.effective_user.id), "enable_trading", {})
    await update.message.reply_text("Automated trading resumed.")


@admin_required
async def disable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    token = confirmation_store.create("disable_all", {})
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm disable", callback_data=f"confirm:{token}:yes"),
          InlineKeyboardButton("Cancel", callback_data=f"confirm:{token}:no")]]
    )
    await update.message.reply_text(
        "This will immediately pause ALL automated trading. Confirm?", reply_markup=keyboard
    )


@admin_required
async def paper_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    token = confirmation_store.create("switch_mode", {"mode": "paper"})
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm switch to PAPER", callback_data=f"confirm:{token}:yes"),
          InlineKeyboardButton("Cancel", callback_data=f"confirm:{token}:no")]]
    )
    await update.message.reply_text("Switch trading mode to PAPER?", reply_markup=keyboard)


@admin_required
async def live_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    token = confirmation_store.create("switch_mode", {"mode": "live"})
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm switch to LIVE", callback_data=f"confirm:{token}:yes"),
          InlineKeyboardButton("Cancel", callback_data=f"confirm:{token}:no")]]
    )
    await update.message.reply_text(
        "*Warning:* LIVE mode places real trades. Anoncoin has not published a public "
        "trade-execution endpoint yet, so live buys/sells will safely fail until they do - "
        "but the mode switch itself is real. Confirm switch to LIVE?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


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
        await repo.update_bot_state(trading_enabled=False)
        await repo.write_audit_log(str(update.effective_user.id), "disable_all_confirmed", {})
        await query.edit_message_text("All automated trading has been paused.")

    elif entry.action == "switch_mode":
        mode = entry.payload["mode"]
        await repo.update_bot_state(mode=mode)
        await repo.write_audit_log(str(update.effective_user.id), "switch_mode", {"mode": mode})
        await query.edit_message_text(f"Trading mode switched to {mode.upper()}.")

    elif entry.action == "save_rule":
        from app.scoring.rules import RuleParams

        params = RuleParams(**entry.payload["params"])
        activate = decision == "activate"
        rule = await repo.create_rule(params, entry.payload["user_id"], activate=activate)
        await repo.write_audit_log(str(update.effective_user.id), "save_rule", {"rule_id": rule.id, "activated": activate})
        await query.edit_message_text(
            f"Rule '{rule.name}' saved" + (" and activated." if activate else " (not activated).")
        )

    elif entry.action == "close_position":
        position_manager = context.application.bot_data.get("position_manager")
        position_id = entry.payload["position_id"]
        ok = await position_manager.close_position_manually(position_id) if position_manager else False
        await repo.write_audit_log(str(update.effective_user.id), "manual_close_position", {"position_id": position_id, "ok": ok})
        await query.edit_message_text(f"Position {position_id} closed." if ok else "Position not found or already closed.")
