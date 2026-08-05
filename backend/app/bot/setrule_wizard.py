"""Step-by-step /setrule wizard. A small generic engine avoids repeating
near-identical handler code for each of the ~18 rule parameters."""
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from app.bot.confirmations import confirmation_store
from app.bot.validation import parse_float, parse_int, parse_wallet_list, sanitize_text
from app.scoring.rules import RuleParams, TakeProfitLevel
from app.security.allowlist import admin_required
from app.storage import repository as repo

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


STEPS = [
    Step("name", "Step 1/18 - Name this rule set (e.g. `sniper-default`):", sanitize_text, default="default"),
    Step("max_buy_size_sol", "Step 2/18 - Max buy size per trade, in SOL (e.g. `0.1`):", lambda r: parse_float(r, 0.001, 1000)),
    Step("min_liquidity_usd", "Step 3/18 - Minimum liquidity in USD (e.g. `1000`):", lambda r: parse_float(r, 0, 1e9)),
    Step("min_holders", "Step 4/18 - Minimum holder count (e.g. `10`):", lambda r: parse_int(r, 0, 1_000_000)),
    Step("max_age_seconds", "Step 5/18 - Max token age since creation, in seconds (e.g. `600`):", lambda r: parse_int(r, 1, 86400 * 30)),
    Step("creator_allowlist", "Step 6/18 - Creator wallet ALLOWLIST, comma separated, or /skip for none:", parse_wallet_list, default=[], optional=True),
    Step("creator_denylist", "Step 7/18 - Creator wallet DENYLIST, comma separated, or /skip for none:", parse_wallet_list, default=[], optional=True),
    Step("bonding_curve_phase", "Step 8/18 - Bonding curve phase requirement: `any`, `pre_graduation` or `post_graduation`:", _parse_phase, default="any"),
    Step("min_market_cap_usd", "Step 9/18 - Min market cap in USD, or /skip for none:", lambda r: parse_float(r, 0, 1e12), default=None, optional=True),
    Step("max_market_cap_usd", "Step 10/18 - Max market cap in USD, or /skip for none:", lambda r: parse_float(r, 0, 1e12), default=None, optional=True),
    Step("max_slippage_pct", "Step 11/18 - Max slippage percent (e.g. `5`):", lambda r: parse_float(r, 0, 100)),
    Step("max_trades_per_hour", "Step 12/18 - Max trades per hour (e.g. `5`):", lambda r: parse_int(r, 1, 1000)),
    Step("cooldown_seconds", "Step 13/18 - Cooldown between buys, in seconds (e.g. `120`):", lambda r: parse_int(r, 0, 86400)),
    Step("take_profit_levels", "Step 14/18 - Take profit levels as `gain:sell%,gain:sell%` (e.g. `50:50,100:50`), or /skip:", _parse_take_profit, default=[], optional=True),
    Step("stop_loss_pct", "Step 15/18 - Stop loss percent (e.g. `20`):", lambda r: parse_float(r, 0, 100)),
    Step("trailing_stop_pct", "Step 16/18 - Trailing stop percent, or /skip for none:", lambda r: parse_float(r, 0, 100), default=None, optional=True),
    Step("sell_on_volume_drop_pct", "Step 17/18 - Sell on volume drop percent, or /skip for none:", lambda r: parse_float(r, 0, 100), default=None, optional=True),
    Step("time_based_exit_seconds", "Step 18/18 - Time-based exit, in seconds, or /skip for none:", lambda r: parse_int(r, 1, 86400 * 30), default=None, optional=True),
]


@admin_required
async def setrule_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["setrule"] = {"index": 0, "answers": {}}
    await update.message.reply_text(
        "Let's build a new rule set. Send /cancel anytime to stop.\n\n" + STEPS[0].prompt
    )
    return COLLECTING


async def setrule_collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("setrule")
    if not state:
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if text == "/cancel":
        context.user_data.pop("setrule", None)
        await update.message.reply_text("Rule creation cancelled.")
        return ConversationHandler.END

    step = STEPS[state["index"]]
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
    if state["index"] >= len(STEPS):
        return await _finish_wizard(update, context)

    await update.message.reply_text(STEPS[state["index"]].prompt)
    return COLLECTING


async def _finish_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answers = context.user_data.pop("setrule")["answers"]
    params = RuleParams(**answers)
    token = confirmation_store.create("save_rule", {"params": params.model_dump(), "user_id": update.effective_user.id})
    summary = (
        f"*Rule '{params.name}' ready*\n"
        f"Max buy: {params.max_buy_size_sol} SOL | Min liq: ${params.min_liquidity_usd:,.0f} | "
        f"Min holders: {params.min_holders} | Max age: {params.max_age_seconds}s\n"
        f"SL: {params.stop_loss_pct}% | TP levels: {len(params.take_profit_levels)} | "
        f"Max trades/hr: {params.max_trades_per_hour}\n\nActivate this rule set now?"
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Save & Activate", callback_data=f"confirm:{token}:activate"),
          InlineKeyboardButton("Save only", callback_data=f"confirm:{token}:save"),
          InlineKeyboardButton("Discard", callback_data=f"confirm:{token}:discard")]]
    )
    await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=keyboard)
    return ConversationHandler.END


setrule_fallback = MessageHandler(filters.COMMAND, lambda u, c: ConversationHandler.END)
