"""Platform-scoped /setrule wizards for Solana and Four.meme."""
import logging
from dataclasses import dataclass
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from app.bot.confirmations import confirmation_store
from app.bot.validation import parse_float, parse_int, parse_wallet_list, sanitize_text
from app.scoring.rules import RuleParams, TakeProfitLevel
from app.security.allowlist import admin_required

logger = logging.getLogger("app.bot.setrule")
COLLECTING = 1

@dataclass
class Step:
    key: str
    prompt: str
    parser: Callable[[str], Any]
    default: Any = None
    optional: bool = False


def _parse_phase(raw: str) -> str:
    value = sanitize_text(raw).lower()
    if value not in ("any", "pre_graduation", "post_graduation"):
        raise ValueError("must be one of: any, pre_graduation, post_graduation")
    return value


def _parse_take_profit(raw: str) -> list[TakeProfitLevel]:
    text = sanitize_text(raw)
    if text.lower() in ("", "none", "skip", "-"):
        return []
    levels = []
    for part in text.split(","):
        gain_str, sell_str = part.split(":")
        levels.append(TakeProfitLevel(gain_pct=float(gain_str), sell_pct=float(sell_str)))
    return levels



def _parse_strategy(raw: str) -> str:
    value = sanitize_text(raw).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "fast": "fast",
        "fast_sniper": "fast",
        "smart": "smart",
        "smart_filter": "smart",
        "smart_money": "smart_money",
        "smart_money_copy": "smart_money",
        "copy": "smart_money",
    }
    if value not in aliases:
        raise ValueError("must be one of: fast, smart, smart_money")
    return aliases[value]


def _steps(platform: str) -> list[Step]:
    buy_key = "max_buy_size_bnb" if platform == "fourmeme" else "max_buy_size_sol"
    buy_unit = "BNB" if platform == "fourmeme" else "SOL"
    buy_default = 0.01 if platform == "fourmeme" else 0.1
    return [
        Step("name", "Step 1/20 - Name this rule set (e.g. `sniper-default`):", sanitize_text, default="default"),
        Step(
            "strategy",
            "Step 2/20 - Entry strategy: `fast` = ⚡ Fast Sniper, `smart` = 🧠 Smart Filter, `smart_money` = 🐋 Smart Money Copy:",
            _parse_strategy,
            default="smart",
        ),
        Step(buy_key, f"Step 3/20 - Max buy size per trade, in {buy_unit} (e.g. `0.01`):", lambda r: parse_float(r, 0.000001, 1000), default=buy_default),
        Step("min_liquidity_usd", "Step 4/20 - Minimum liquidity in USD (e.g. `1000`):", lambda r: parse_float(r, 0, 1e9)),
        Step("min_holders", "Step 5/20 - Minimum holder count (e.g. `10`):", lambda r: parse_int(r, 0, 1_000_000)),
        Step("max_age_seconds", "Step 6/20 - Max token age since creation, in seconds (e.g. `600`):", lambda r: parse_int(r, 1, 86400 * 30)),
        Step("creator_allowlist", "Step 7/20 - Creator wallet ALLOWLIST, comma separated, or /skip for none:", parse_wallet_list, default=[], optional=True),
        Step("creator_denylist", "Step 8/20 - Creator wallet DENYLIST, comma separated, or /skip for none:", parse_wallet_list, default=[], optional=True),
        Step("bonding_curve_phase", "Step 9/20 - Bonding curve phase requirement: `any`, `pre_graduation` or `post_graduation`:", _parse_phase, default="any"),
        Step("min_market_cap_usd", "Step 10/20 - Min market cap in USD, or /skip for none:", lambda r: parse_float(r, 0, 1e12), default=None, optional=True),
        Step("max_market_cap_usd", "Step 11/20 - Max market cap in USD, or /skip for none:", lambda r: parse_float(r, 0, 1e12), default=None, optional=True),
        Step("max_slippage_pct", "Step 12/20 - Max slippage percent (e.g. `5`):", lambda r: parse_float(r, 0, 100)),
        Step("qualify_score_threshold", "Step 13/20 - Minimum qualification score (0-100, e.g. `50`):", lambda r: parse_float(r, 0, 100), default=52.0),
        Step("max_trades_per_hour", "Step 14/20 - Max trades per hour (e.g. `5`):", lambda r: parse_int(r, 1, 1000)),
        Step("cooldown_seconds", "Step 15/20 - Cooldown between buys, in seconds (e.g. `120`):", lambda r: parse_int(r, 0, 86400)),
        Step("take_profit_levels", "Step 16/20 - Take profit levels as `gain:sell%,gain:sell%` (e.g. `50:50,100:50`), or /skip:", _parse_take_profit, default=[], optional=True),
        Step("stop_loss_pct", "Step 17/20 - Stop loss percent (e.g. `20`):", lambda r: parse_float(r, 0, 100)),
        Step("trailing_stop_pct", "Step 18/20 - Trailing stop percent, or /skip for none:", lambda r: parse_float(r, 0, 100), default=None, optional=True),
        Step("sell_on_volume_drop_pct", "Step 19/20 - Sell on volume drop percent, or /skip for none:", lambda r: parse_float(r, 0, 100), default=None, optional=True),
        Step("time_based_exit_seconds", "Step 20/20 - Time-based exit, in seconds, or /skip for none:", lambda r: parse_int(r, 1, 86400 * 30), default=None, optional=True),
    ]


@admin_required
async def setrule_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _start(update, context, "solana")


@admin_required
async def setrule_fourmeme_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _start(update, context, "fourmeme")


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE, platform: str) -> int:
    context.user_data["setrule"] = {"index": 0, "answers": {}, "platform": platform}
    label = "FOUR.MEME / BSC" if platform == "fourmeme" else "SOLANA (Anoncoin + Pump.fun)"
    await update.message.reply_text(
        f"Let's build a new *{label}* rule set. Send /cancel anytime to stop.\n\n"
        + _steps(platform)[0].prompt,
        parse_mode="Markdown",
    )
    return COLLECTING


async def setrule_collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("setrule")
    if not state:
        return ConversationHandler.END

    platform = state.get("platform", "solana")
    steps = _steps(platform)
    text = (update.message.text or "").strip()
    if text == "/cancel":
        context.user_data.pop("setrule", None)
        await update.message.reply_text("Rule creation cancelled.")
        return ConversationHandler.END

    step = steps[state["index"]]
    if text == "/skip":
        if not step.optional:
            await update.message.reply_text(f"This field is required.\n\n{step.prompt}")
            return COLLECTING
        state["answers"][step.key] = step.default
    else:
        try:
            state["answers"][step.key] = step.parser(text)
        except (ValueError, ZeroDivisionError) as exc:
            await update.message.reply_text(f"Invalid value ({exc}). Try again.\n\n{step.prompt}")
            return COLLECTING

    state["index"] += 1
    if state["index"] >= len(steps):
        return await _finish_wizard(update, context)

    await update.message.reply_text(steps[state["index"]].prompt)
    return COLLECTING


async def _finish_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.pop("setrule")
    answers = state["answers"]
    platform = state.get("platform", "solana")
    answers["platform"] = platform
    # Ensure the unused currency field remains valid for the shared model/storage.
    if platform == "fourmeme":
        answers.setdefault("max_buy_size_sol", 0.1)
    else:
        answers.setdefault("max_buy_size_bnb", 0.01)

    params = RuleParams(**answers)
    token = confirmation_store.create("save_rule", {"params": params.model_dump(), "user_id": update.effective_user.id})
    unit = "BNB" if platform == "fourmeme" else "SOL"
    label = "FOUR.MEME" if platform == "fourmeme" else "SOLANA"
    buy = params.max_buy_size_bnb if platform == "fourmeme" else params.max_buy_size_sol
    summary = (
        f"*{label} rule '{params.name}' ready*\n"
        f"Max buy: {buy} {unit} | Min liq: ${params.min_liquidity_usd:,.0f} | "
        f"Min holders: {params.min_holders} | Max age: {params.max_age_seconds}s\n"
        f"Market cap: ${params.min_market_cap_usd or 0:,.0f} - ${params.max_market_cap_usd:,.0f} | "
        f"SL: {params.stop_loss_pct}% | TP levels: {len(params.take_profit_levels)}\n"
        f"Entry strategy: {('⚡ FAST SNIPER' if params.strategy == 'fast' else '🐋 SMART MONEY COPY' if params.strategy == 'smart_money' else '🧠 SMART FILTER')}\n\n"
        f"Activate this *{label}* rule set now?"
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Save & Activate", callback_data=f"confirm:{token}:activate"),
          InlineKeyboardButton("Save only", callback_data=f"confirm:{token}:save"),
          InlineKeyboardButton("Discard", callback_data=f"confirm:{token}:discard")]]
    )
    await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=keyboard)
    return ConversationHandler.END


setrule_fallback = MessageHandler(filters.COMMAND, lambda u, c: ConversationHandler.END)
