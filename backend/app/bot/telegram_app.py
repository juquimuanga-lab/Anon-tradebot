"""Builds the python-telegram-bot Application and registers all handlers."""
from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.bot import handlers_admin, handlers_basic, handlers_wallet
from app.bot.setrule_wizard import COLLECTING, setrule_collect, setrule_start, setrule_fourmeme_start
from app.config.settings import settings


async def set_bot_commands(application: Application) -> None:
    """Populate Telegram's command menu with the currently supported commands."""
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("help", "Show help"),
        BotCommand("status", "Show bot status"),
        BotCommand("balance", "Show wallet and paper balance"),
        BotCommand("rules", "List your rules"),
        BotCommand("listrules", "List saved rules"),
        BotCommand("activaterule", "Activate a rule"),
        BotCommand("positions", "Show open positions"),
        BotCommand("history", "Show recent trades"),
        BotCommand("pumpfunsnipers", "Control Fast, Smart and Smart Money"),
        BotCommand("setfast", "Set a Pump.fun Fast rule"),
        BotCommand("setsmart", "Set a Pump.fun Smart rule"),
        BotCommand("enablesmartmoney", "Enable Smart Money copy"),
        BotCommand("disablesmartmoney", "Disable Smart Money copy"),
        BotCommand("enablepumpfun", "Enable Pump.fun trading"),
        BotCommand("disablepumpfun", "Disable Pump.fun trading"),
        BotCommand("connectwallet", "Connect your live wallet"),
        BotCommand("disconnectwallet", "Disconnect your live wallet"),
        BotCommand("paper", "Switch to paper mode"),
        BotCommand("live", "Switch to live mode"),
        BotCommand("setrule", "Create a Solana rule"),
        BotCommand("recoverent", "Recover token-account rent"),
        BotCommand("burnclose", "Burn and close token accounts"),
    ]
    await application.bot.set_my_commands(commands)


def build_application() -> Application:
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(set_bot_commands)
        .build()
    )

    application.add_handler(CommandHandler("start", handlers_basic.start))
    application.add_handler(CommandHandler("help", handlers_basic.help_cmd))
    application.add_handler(CommandHandler("status", handlers_basic.status))
    application.add_handler(CommandHandler("rules", handlers_basic.rules))
    application.add_handler(CommandHandler("listrules", handlers_basic.listrules))
    application.add_handler(CommandHandler("activaterule", handlers_basic.activaterule))
    application.add_handler(CommandHandler("balance", handlers_basic.balance))
    application.add_handler(CommandHandler("positions", handlers_basic.positions_cmd))
    application.add_handler(CommandHandler("history", handlers_basic.history))

    application.add_handler(CommandHandler("enable", handlers_admin.enable_cmd))
    application.add_handler(CommandHandler("disable", handlers_admin.disable_cmd))
    application.add_handler(CommandHandler("enableanoncoin", handlers_admin.enableanoncoin_cmd))
    application.add_handler(CommandHandler("disableanoncoin", handlers_admin.disableanoncoin_cmd))
    application.add_handler(CommandHandler("enablepumpfun", handlers_admin.enablepumpfun_cmd))
    application.add_handler(CommandHandler("disablepumpfun", handlers_admin.disablepumpfun_cmd))

    # Pump.fun sniper lanes
    application.add_handler(CommandHandler("pumpfunsnipers", handlers_admin.pumpfun_snipers_cmd))
    application.add_handler(CommandHandler("setfast", handlers_admin.setfast_cmd))
    application.add_handler(CommandHandler("setsmart", handlers_admin.setsmart_cmd))
    application.add_handler(CommandHandler("enablesmartmoney", handlers_admin.enablesmartmoney_cmd))
    application.add_handler(CommandHandler("disablesmartmoney", handlers_admin.disablesmartmoney_cmd))

    application.add_handler(CommandHandler("enablefourmeme", handlers_admin.enablefourmeme_cmd))
    application.add_handler(CommandHandler("disablefourmeme", handlers_admin.disablefourmeme_cmd))
    application.add_handler(CommandHandler("paper", handlers_admin.paper_cmd))
    application.add_handler(CommandHandler("live", handlers_admin.live_cmd))
    application.add_handler(CommandHandler("disconnectwallet", handlers_wallet.disconnectwallet_cmd))
    application.add_handler(CommandHandler("disconnectbscwallet", handlers_wallet.disconnectbscwallet_cmd))

    connect_conv = ConversationHandler(
        entry_points=[CommandHandler("connect", handlers_admin.connect_start)],
        states={
            handlers_admin.CONNECT_WAITING_KEY: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND | filters.Regex("^/cancel$"),
                    handlers_admin.connect_receive,
                )
            ]
        },
        fallbacks=[CommandHandler("cancel", handlers_admin.connect_receive)],
        name="connect_conversation",
    )
    application.add_handler(connect_conv)

    connectbscwallet_conv = ConversationHandler(
        entry_points=[CommandHandler("connectbscwallet", handlers_wallet.connectbscwallet_start)],
        states={
            handlers_wallet.BSC_CONNECT_WALLET_WAITING: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND | filters.Regex("^/cancel$"),
                    handlers_wallet.connectbscwallet_receive,
                )
            ]
        },
        fallbacks=[CommandHandler("cancel", handlers_wallet.connectbscwallet_receive)],
        name="connect_bsc_wallet_conversation",
    )
    application.add_handler(connectbscwallet_conv)

    connectwallet_conv = ConversationHandler(
        entry_points=[CommandHandler("connectwallet", handlers_wallet.connectwallet_start)],
        states={
            handlers_wallet.CONNECT_WALLET_WAITING: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND | filters.Regex("^/cancel$"),
                    handlers_wallet.connectwallet_receive,
                )
            ]
        },
        fallbacks=[CommandHandler("cancel", handlers_wallet.connectwallet_receive)],
        name="connectwallet_conversation",
    )
    application.add_handler(connectwallet_conv)

    burnclose_conv = ConversationHandler(
        entry_points=[CommandHandler("burnclose", handlers_admin.burnclose_cmd)],
        states={
            handlers_admin.BURN_CLOSE_WAITING_ACCOUNTS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND | filters.Regex("^/cancel$"),
                    handlers_admin.burnclose_receive,
                )
            ]
        },
        fallbacks=[CommandHandler("cancel", handlers_admin.burnclose_receive)],
        name="burnclose_conversation",
    )
    application.add_handler(burnclose_conv)

    rent_recovery_conv = ConversationHandler(
        entry_points=[CommandHandler("recoverent", handlers_admin.recoverent_cmd)],
        states={
            handlers_admin.RENT_RECOVERY_WAITING_SIGNATURES: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND | filters.Regex("^/cancel$"),
                    handlers_admin.recoverent_receive,
                )
            ]
        },
        fallbacks=[CommandHandler("cancel", handlers_admin.recoverent_receive)],
        name="rent_recovery_conversation",
    )
    application.add_handler(rent_recovery_conv)

    setrule_fourmeme_conv = ConversationHandler(
        entry_points=[CommandHandler("setrulefourmeme", setrule_fourmeme_start)],
        states={
            COLLECTING: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND | filters.Regex("^/(skip|cancel)$"),
                    setrule_collect,
                )
            ]
        },
        fallbacks=[CommandHandler("cancel", setrule_collect)],
        name="setrule_fourmeme_conversation",
    )
    application.add_handler(setrule_fourmeme_conv)

    setrule_conv = ConversationHandler(
        entry_points=[CommandHandler("setrule", setrule_start)],
        states={
            COLLECTING: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND | filters.Regex("^/(skip|cancel)$"),
                    setrule_collect,
                )
            ]
        },
        fallbacks=[CommandHandler("cancel", setrule_collect)],
        name="setrule_conversation",
    )
    application.add_handler(setrule_conv)

    application.add_handler(CallbackQueryHandler(handlers_admin.action_confirmation_callback, pattern=r"^actionconfirm:"))
    application.add_handler(CallbackQueryHandler(handlers_admin.confirmation_callback, pattern=r"^confirm:"))
    application.add_handler(CallbackQueryHandler(handlers_admin.pumpfun_snipers_callback, pattern=r"^sniper:"))
    application.add_handler(CallbackQueryHandler(handlers_admin.burnclose_callback, pattern=r"^burnclose:"))
    application.add_handler(CallbackQueryHandler(handlers_admin.rent_recovery_callback, pattern=r"^rent:"))

    return application
