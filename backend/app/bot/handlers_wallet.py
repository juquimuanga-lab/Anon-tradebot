"""Per-admin wallet connect/disconnect for direct on-chain execution."""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from app.bot.confirmations import confirmation_store
from app.execution.onchain.wallet_keys import InvalidWalletKeyError, load_keypair
from app.execution.onchain.bsc_wallet import InvalidBscWalletKeyError, load_bsc_account
from app.security.allowlist import admin_required
from app.security.secrets_manager import secrets_manager
from app.storage import repository as repo

logger = logging.getLogger("app.bot.wallet")

CONNECT_WALLET_WAITING = 3


@admin_required
async def connectwallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "*Warning:* this wallet will be able to sign real on-chain trades. Use a "
        "dedicated/burner wallet funded only with what you're willing to risk - "
        "never your main wallet.\n\n"
        "Send your private key now: either a base58 secret key (Phantom -> Export "
        "Private Key) or a JSON byte array (Solana CLI keypair.json contents). "
        "Your message is deleted immediately after storing it encrypted. /cancel to abort.",
        parse_mode="Markdown",
    )
    return CONNECT_WALLET_WAITING


async def connectwallet_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    user_id = update.effective_user.id

    if text == "/cancel":
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    try:
        keypair = load_keypair(text)
    except InvalidWalletKeyError as exc:
        await update.message.reply_text(f"That doesn't look like a valid key ({exc}). Try again or /cancel.")
        return CONNECT_WALLET_WAITING

    await secrets_manager.set_wallet_private_key(user_id, text)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        logger.warning("could_not_delete_wallet_key_message")

    pubkey = str(keypair.pubkey())
    await repo.write_audit_log(str(user_id), "connect_wallet", {"pubkey": pubkey})
    await update.message.reply_text(
        f"Wallet connected: `{pubkey}`\n\n"
        "Fund this address with SOL to trade live. New rules you create with "
        "/setrule will execute through this wallet once you switch to /live.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


@admin_required
async def disconnectwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    token = confirmation_store.create("disconnect_wallet", {"user_id": update.effective_user.id})
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm disconnect", callback_data=f"confirm:{token}:yes"),
          InlineKeyboardButton("Cancel", callback_data=f"confirm:{token}:no")]]
    )
    await update.message.reply_text(
        "This deletes your stored wallet key. Any active rules tied to your wallet "
        "will fail safely in live mode until you reconnect. Confirm?",
        reply_markup=keyboard,
    )


BSC_CONNECT_WALLET_WAITING = 4

@admin_required
async def connectbscwallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "*BSC wallet connection:* send the private key as a 64-hex-character key (0x prefix optional). "
        "Use a dedicated trading wallet. The message is deleted immediately after validation and the key is stored encrypted. /cancel to abort.",
        parse_mode="Markdown",
    )
    return BSC_CONNECT_WALLET_WAITING

async def connectbscwallet_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text=(update.message.text or "").strip()
    if text == "/cancel":
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END
    try:
        account=load_bsc_account(text)
    except InvalidBscWalletKeyError as exc:
        await update.message.reply_text(f"Invalid BSC key ({exc}). Try again or /cancel.")
        return BSC_CONNECT_WALLET_WAITING
    await secrets_manager.set_bsc_wallet_private_key(update.effective_user.id, text)
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
    except Exception:
        logger.warning("could_not_delete_bsc_wallet_key_message")
    await repo.write_audit_log(str(update.effective_user.id), "connect_bsc_wallet", {"address": account.address})
    await update.message.reply_text(
        f"BSC wallet connected: `{account.address}`\n\nFund this address with BNB before enabling Four.meme live trading.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END

@admin_required
async def disconnectbscwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    token=confirmation_store.create("disconnect_bsc_wallet", {"user_id": update.effective_user.id})
    keyboard=InlineKeyboardMarkup([[InlineKeyboardButton("Confirm disconnect", callback_data=f"confirm:{token}:yes"),InlineKeyboardButton("Cancel", callback_data=f"confirm:{token}:no")]])
    await update.message.reply_text("Delete your encrypted BSC wallet key?", reply_markup=keyboard)
