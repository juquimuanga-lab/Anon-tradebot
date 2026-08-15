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

logger = logging.getLogger("app.bot.admin")

CONNECT_WAITING_KEY = 2
RENT_RECOVERY_WAITING_SIGNATURES = 3


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
        "This will immediately pause ALL automated trading - no new buys, and no "
        "automated stop loss / take profit / trailing stop on positions you already "
        "have open. Manual `/positions close <id>` still works while paused. Confirm?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@admin_required
async def enableanoncoin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await repo.update_bot_state(anoncoin_trading_enabled=True)
    await repo.write_audit_log(str(update.effective_user.id), "enable_anoncoin", {})
    await update.message.reply_text("Anoncoin trading resumed (Pump.fun unaffected).")


@admin_required
async def disableanoncoin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    token = confirmation_store.create("disable_anoncoin", {})
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm disable Anoncoin", callback_data=f"confirm:{token}:yes"),
          InlineKeyboardButton("Cancel", callback_data=f"confirm:{token}:no")]]
    )
    await update.message.reply_text(
        "This pauses new Anoncoin buys only - Pump.fun keeps trading, and any open "
        "Anoncoin positions you already hold keep their automated stop loss / take "
        "profit. Confirm?",
        reply_markup=keyboard,
    )


@admin_required
async def enablepumpfun_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await repo.update_bot_state(pumpfun_trading_enabled=True)
    await repo.write_audit_log(str(update.effective_user.id), "enable_pumpfun", {})
    await update.message.reply_text("Pump.fun trading resumed (Anoncoin unaffected).")


@admin_required
async def disablepumpfun_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    token = confirmation_store.create("disable_pumpfun", {})
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm disable Pump.fun", callback_data=f"confirm:{token}:yes"),
          InlineKeyboardButton("Cancel", callback_data=f"confirm:{token}:no")]]
    )
    await update.message.reply_text(
        "This pauses new Pump.fun buys only - Anoncoin keeps trading, and any open "
        "Pump.fun positions you already hold keep their automated stop loss / take "
        "profit. Confirm?",
        reply_markup=keyboard,
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
        "*Warning:* LIVE mode places real on-chain trades using the wallet each rule's "
        "creator has connected with /connectwallet (real funds, real risk). If no wallet "
        "is connected for a rule's owner, its buys/sells will fail safely with a clear "
        "message instead of doing nothing silently. Confirm switch to LIVE?",
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

    elif entry.action == "disable_anoncoin":
        await repo.update_bot_state(anoncoin_trading_enabled=False)
        await repo.write_audit_log(str(update.effective_user.id), "disable_anoncoin_confirmed", {})
        await query.edit_message_text("Anoncoin trading paused. Pump.fun is unaffected.")

    elif entry.action == "disable_pumpfun":
        await repo.update_bot_state(pumpfun_trading_enabled=False)
        await repo.write_audit_log(str(update.effective_user.id), "disable_pumpfun_confirmed", {})
        await query.edit_message_text("Pump.fun trading paused. Anoncoin is unaffected.")

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

    elif entry.action == "disconnect_wallet":
        await secrets_manager.delete_wallet_private_key(entry.payload["user_id"])
        await repo.write_audit_log(str(update.effective_user.id), "disconnect_wallet", {})
        await query.edit_message_text("Wallet disconnected and key deleted.")

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

