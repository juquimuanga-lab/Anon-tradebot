"""Builds the python-telegram-bot Application and registers all handlers."""
from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, filters

from app.bot import handlers_admin, handlers_basic, handlers_wallet, rule_manager
from app.bot.setrule_wizard import COLLECTING, setrule_collect, setrule_start, setrule_fourmeme_start, setrule_pons_start
from app.config.settings import settings
from app.arbitrage import telegram as arbitrage_telegram


async def set_bot_commands(application: Application) -> None:
    """Populate Telegram's command menu with the currently supported commands."""
    commands = [
        BotCommand("start", "Start the bot"), BotCommand("help", "Show help"), BotCommand("status", "Show bot status"),
        BotCommand("balance", "Show wallet and paper balance"), BotCommand("rules", "Open rule manager"), BotCommand("rulemanager", "Open rule manager"),
        BotCommand("listrules", "List saved rules"), BotCommand("activaterule", "Activate a rule"), BotCommand("positions", "Show open positions"), BotCommand("history", "Show recent trades"),
        BotCommand("pumpfunsnipers", "Control Fast, Smart and Smart Money"), BotCommand("setfast", "Set a Pump.fun Fast rule"), BotCommand("setsmart", "Set a Pump.fun Smart rule"),
        BotCommand("setsmartmoney", "Set the Smart Money rule"), BotCommand("enablesmartmoney", "Enable Smart Money copy"), BotCommand("disablesmartmoney", "Disable Smart Money copy"),
        BotCommand("enablepumpfun", "Enable Pump.fun trading"), BotCommand("disablepumpfun", "Disable Pump.fun trading"), BotCommand("connectwallet", "Connect Solana wallet"),
        BotCommand("disconnectwallet", "Disconnect Solana wallet"), BotCommand("connectrobinhoodwallet", "Connect Robinhood Chain wallet"), BotCommand("robinhoodwallet", "Show Robinhood wallet"),
        BotCommand("disconnectrobinhoodwallet", "Disconnect Robinhood wallet"), BotCommand("paper", "Switch to paper mode"), BotCommand("live", "Switch Solana to live mode"),
        BotCommand("ponslive", "Switch Pons to live mode"), BotCommand("ponspaper", "Switch Pons to paper mode"), BotCommand("ponsstatus", "Show Pons/Robinhood status"),
        BotCommand("setrule", "Create a Solana rule"), BotCommand("setrulepons", "Create a Robinhood/Pons ETH rule"), BotCommand("recoverent", "Recover token-account rent"), BotCommand("burnclose", "Burn and close token accounts"),
        BotCommand("guardian", "GO Guardian AI dashboard"), BotCommand("arbitrage", "Show arbitrage status"), BotCommand("enablearbitrage", "Enable arbitrage scanning"), BotCommand("disablearbitrage", "Disable arbitrage scanning"),
        BotCommand("arbscan", "Scan live venue spreads"), BotCommand("arbvenues", "Show arbitrage venues"), BotCommand("arblivestatus", "Show live arbitrage gate"), BotCommand("arblive", "Submit one atomic arbitrage bundle"), BotCommand("arbhelp", "Show arbitrage commands"),
    ]
    await application.bot.set_my_commands(commands)


def build_application() -> Application:
    application = Application.builder().token(settings.telegram_bot_token).post_init(set_bot_commands).build()
    application.add_handler(CommandHandler("start", handlers_basic.start)); application.add_handler(CommandHandler("help", handlers_basic.help_cmd)); application.add_handler(CommandHandler("status", handlers_basic.status))
    application.add_handler(CommandHandler("rules", rule_manager.rule_manager_start)); application.add_handler(CommandHandler("rulemanager", rule_manager.rule_manager_start)); application.add_handler(CommandHandler("listrules", handlers_basic.listrules)); application.add_handler(CommandHandler("activaterule", handlers_basic.activaterule))
    application.add_handler(CommandHandler("balance", handlers_basic.balance)); application.add_handler(CommandHandler("positions", handlers_basic.positions_cmd)); application.add_handler(CommandHandler("history", handlers_basic.history)); application.add_handler(CommandHandler("guardian", handlers_admin.guardian_cmd))

    # Isolated Solana arbitrage controls. Live execution is separately gated.
    application.add_handler(CommandHandler("arbitrage", arbitrage_telegram.arbitrage_cmd))
    application.add_handler(CommandHandler("enablearbitrage", arbitrage_telegram.enable_arbitrage_cmd))
    application.add_handler(CommandHandler("disablearbitrage", arbitrage_telegram.disable_arbitrage_cmd))
    application.add_handler(CommandHandler("arbscan", arbitrage_telegram.arbitrage_scan_cmd))
    application.add_handler(CommandHandler("arbvenues", arbitrage_telegram.arbitrage_venues_cmd))
    application.add_handler(CommandHandler("arblivestatus", arbitrage_telegram.arbitrage_live_status_cmd))
    application.add_handler(CommandHandler("arblive", arbitrage_telegram.arbitrage_live_execute_cmd))
    application.add_handler(CommandHandler("arbhelp", arbitrage_telegram.arbitrage_help_cmd))

    application.add_handler(CommandHandler("enable", handlers_admin.enable_cmd)); application.add_handler(CommandHandler("disable", handlers_admin.disable_cmd)); application.add_handler(CommandHandler("enableanoncoin", handlers_admin.enableanoncoin_cmd)); application.add_handler(CommandHandler("disableanoncoin", handlers_admin.disableanoncoin_cmd)); application.add_handler(CommandHandler("enablepumpfun", handlers_admin.enablepumpfun_cmd)); application.add_handler(CommandHandler("disablepumpfun", handlers_admin.disablepumpfun_cmd))

    # Remaining handlers continue below in the existing application.
    application.add_handler(CommandHandler("connectwallet", handlers_wallet.connect_wallet_cmd))
    application.add_handler(CommandHandler("disconnectwallet", handlers_wallet.disconnect_wallet_cmd))
    application.add_handler(CommandHandler("connectrobinhoodwallet", handlers_wallet.connect_robinhood_wallet_cmd))
    application.add_handler(CommandHandler("robinhoodwallet", handlers_wallet.robinhood_wallet_cmd))
    application.add_handler(CommandHandler("disconnectrobinhoodwallet", handlers_wallet.disconnect_robinhood_wallet_cmd))
    application.add_handler(CommandHandler("paper", handlers_admin.paper_cmd))
    application.add_handler(CommandHandler("live", handlers_admin.live_cmd))
    application.add_handler(CommandHandler("ponslive", handlers_admin.ponslive_cmd))
    application.add_handler(CommandHandler("ponspaper", handlers_admin.ponspaper_cmd))
    application.add_handler(CommandHandler("ponsstatus", handlers_admin.ponsstatus_cmd))
    application.add_handler(CommandHandler("setrule", setrule_start))
    application.add_handler(CommandHandler("setrulepons", setrule_pons_start))
    application.add_handler(CommandHandler("recoverent", handlers_admin.recoverent_cmd))
    application.add_handler(CommandHandler("burnclose", handlers_admin.burnclose_cmd))
    application.add_handler(ConversationHandler(entry_points=[], states={COLLECTING: [MessageHandler(filters.TEXT & ~filters.COMMAND, setrule_collect)]}, fallbacks=[]))
    return application
