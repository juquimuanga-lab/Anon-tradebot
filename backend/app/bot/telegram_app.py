"""Builds the python-telegram-bot Application and registers all handlers."""
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.bot import handlers_admin, handlers_basic, handlers_wallet
from app.bot.setrule_wizard import COLLECTING, setrule_collect, setrule_start
from app.config.settings import settings


def build_application() -> Application:
    application = Application.builder().token(settings.telegram_bot_token).build()

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
    application.add_handler(CommandHandler("paper", handlers_admin.paper_cmd))
    application.add_handler(CommandHandler("live", handlers_admin.live_cmd))
    application.add_handler(CommandHandler("disconnectwallet", handlers_wallet.disconnectwallet_cmd))

    connect_conv = ConversationHandler(
        entry_points=[CommandHandler("connect", handlers_admin.connect_start)],
        states={
            handlers_admin.CONNECT_WAITING_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND | filters.Regex("^/cancel$"), handlers_admin.connect_receive)
            ]
        },
        fallbacks=[CommandHandler("cancel", handlers_admin.connect_receive)],
        name="connect_conversation",
    )
    application.add_handler(connect_conv)

    connectwallet_conv = ConversationHandler(
        entry_points=[CommandHandler("connectwallet", handlers_wallet.connectwallet_start)],
        states={
            handlers_wallet.CONNECT_WALLET_WAITING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND | filters.Regex("^/cancel$"), handlers_wallet.connectwallet_receive)
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

    # SOL rent recovery: recover rent from empty token accounts discovered from
    # confirmed BUY/SELL transaction signatures.
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

    setrule_conv = ConversationHandler(
        entry_points=[CommandHandler("setrule", setrule_start)],
        states={
            COLLECTING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND | filters.Regex("^/(skip|cancel)$"), setrule_collect)
            ]
        },
        fallbacks=[CommandHandler("cancel", setrule_collect)],
        name="setrule_conversation",
    )
    application.add_handler(setrule_conv)

    application.add_handler(CallbackQueryHandler(handlers_admin.confirmation_callback, pattern=r"^confirm:"))
    application.add_handler(CallbackQueryHandler(handlers_admin.burnclose_callback, pattern=r"^burnclose:"))
    application.add_handler(CallbackQueryHandler(handlers_admin.rent_recovery_callback, pattern=r"^rent:"))

    return application
