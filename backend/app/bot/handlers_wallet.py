"""Per-admin wallet connect/disconnect for direct on-chain execution."""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from app.bot.confirmations import confirmation_store
from app.execution.onchain.wallet_keys import InvalidWalletKeyError, load_keypair
from app.execution.onchain.bsc_wallet import InvalidBscWalletKeyError, load_bsc_account
from app.execution.onchain.robinhood_wallet import (
    InvalidRobinhoodWalletKeyError,
    build_robinhood_web3,
    get_native_balance_eth,
    resolve_robinhood_rpc_url,
    load_robinhood_account,
)
from app.config.settings import settings
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


ROBINHOOD_CONNECT_WALLET_WAITING = 5


@admin_required
async def connectrobinhoodwallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "*Robinhood Chain wallet connection:* send the private key as a 64-hex-character EVM key (0x prefix optional). "
        "Use a dedicated trading wallet. The message is deleted immediately after validation and the key is stored encrypted. "
        "The wallet is checked against Robinhood Chain (chain ID 4663). /cancel to abort.",
        parse_mode="Markdown",
    )
    return ROBINHOOD_CONNECT_WALLET_WAITING


async def connectrobinhoodwallet_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text == "/cancel":
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    try:
        account = load_robinhood_account(text)
        # Resolve the public/Alchemy RPC from the configured app settings.
        # The wallet private key itself is never taken from an environment variable.
        rpc_url = resolve_robinhood_rpc_url(settings)
        # Validate the live RPC network and the wallet balance before storing.
        w3 = build_robinhood_web3(rpc_url)
        balance_eth = int(w3.eth.get_balance(account.address)) / 10**18
    except (InvalidRobinhoodWalletKeyError, RuntimeError, ValueError) as exc:
        await update.message.reply_text(f"Robinhood wallet validation failed: {exc}. Try again or /cancel.")
        return ROBINHOOD_CONNECT_WALLET_WAITING
    except Exception as exc:
        await update.message.reply_text(f"Robinhood wallet validation failed: {type(exc).__name__}. Try again or /cancel.")
        return ROBINHOOD_CONNECT_WALLET_WAITING

    user_id = update.effective_user.id
    await secrets_manager.set_robinhood_wallet_private_key(user_id, text)
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
    except Exception:
        logger.warning("could_not_delete_robinhood_wallet_key_message")

    await repo.write_audit_log(str(user_id), "connect_robinhood_wallet", {"address": account.address, "chain_id": 4663})
    await update.message.reply_text(
        f"✅ Robinhood Chain wallet connected: `{account.address}`\n\n"
        f"Network: Robinhood Chain (4663)\n"
        f"ETH balance: `{balance_eth:.6f} ETH`\n\n"
        "Fund this address with ETH on Robinhood Chain before enabling live Pons trading.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


@admin_required
async def disconnectrobinhoodwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    token = confirmation_store.create("disconnect_robinhood_wallet", {"user_id": update.effective_user.id})
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Confirm disconnect", callback_data=f"rhdisconnect:{token}:yes"),
            InlineKeyboardButton("Cancel", callback_data=f"rhdisconnect:{token}:no"),
        ]]
    )
    await update.message.reply_text(
        "Delete your encrypted Robinhood Chain wallet key? Your BSC wallet will remain untouched.",
        reply_markup=keyboard,
    )


async def disconnectrobinhoodwallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    from app.security.allowlist import is_admin
    if not is_admin(update.effective_user.id):
        await query.edit_message_text("Restricted to admin.")
        return

    try:
        _, token, decision = query.data.split(":", 2)
    except ValueError:
        await query.edit_message_text("Invalid disconnect confirmation.")
        return

    entry = confirmation_store.resolve(token)
    if not entry or entry.action != "disconnect_robinhood_wallet":
        await query.edit_message_text("This confirmation has expired. Run /disconnectrobinhoodwallet again.")
        return
    if decision != "yes":
        await query.edit_message_text("Robinhood Chain wallet disconnect cancelled.")
        return

    user_id = update.effective_user.id
    await secrets_manager.delete_robinhood_wallet_private_key(user_id)
    await repo.write_audit_log(str(user_id), "disconnect_robinhood_wallet", {})
    await query.edit_message_text(
        "✅ Robinhood Chain wallet disconnected and its encrypted private key deleted. "
        "Your BSC wallet remains untouched."
    )


@admin_required
async def robinhoodwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    raw_key = await secrets_manager.get_robinhood_wallet_private_key(user_id)
    rpc_url = getattr(settings, "robinhood_rpc_url", None) or getattr(settings, "robinhood_rpc_override_url", None)
    if not raw_key:
        await update.message.reply_text("No Robinhood Chain wallet is connected. Use /connectrobinhoodwallet.")
        return
    if not rpc_url:
        await update.message.reply_text("Robinhood Chain RPC is not configured.")
        return
    try:
        account = load_robinhood_account(raw_key)
        w3 = build_robinhood_web3(rpc_url)
        balance_eth = int(w3.eth.get_balance(account.address)) / 10**18
        trading = bool(getattr(settings, "robinhood_pons_trading_enabled", False))
        await update.message.reply_text(
            "*Robinhood Chain Wallet*\n\n"
            f"Address: `{account.address}`\n"
            f"Chain ID: `{int(w3.eth.chain_id)}`\n"
            f"ETH balance: `{balance_eth:.6f} ETH`\n"
            f"Pons live trading: `{'ON' if trading else 'OFF'}`",
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.warning("robinhood_wallet_status_failed", extra={"owner_user_id": user_id, "error": str(exc)})
        await update.message.reply_text("Could not read the Robinhood wallet status. Check the RPC configuration.")
