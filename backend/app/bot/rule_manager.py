"""Admin rule manager: button-driven rule switching and JSON imports."""
from __future__ import annotations

import json
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.security.allowlist import admin_required
from app.scoring.rules import RuleParams
from app.storage import repository as repo

logger = logging.getLogger("app.bot.rule_manager")

MAX_RULE_FILE_BYTES = 256 * 1024


def _platform_label(platform: str) -> str:
    return {
        "solana": "SOLANA / Pump.fun",
        "pons": "ROBINHOOD / PONS (ETH)",
        "fourmeme": "FOUR.MEME / BSC",
    }.get(platform, platform.upper())


def _strategy_label(strategy: str) -> str:
    return {
        "fast": "⚡ Fast Sniper",
        "smart": "🧠 Smart Filter",
        "smart_money": "🐋 Smart Money Copy",
    }.get(strategy, strategy)


def _rule_line(rule) -> str:
    active = "🟢 " if rule.is_active else "⚪ "
    return f"{active}{rule.name}  ·  #{rule.id}  ·  {_strategy_label(getattr(rule, 'strategy', 'smart'))}"


def _manager_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ SOL Fast", callback_data="rulemgr:lane:solana:fast"),
            InlineKeyboardButton("🧠 SOL Smart", callback_data="rulemgr:lane:solana:smart"),
        ],
        [
            InlineKeyboardButton("🐋 SOL Smart Money", callback_data="rulemgr:lane:solana:smart_money"),
            InlineKeyboardButton("🧪 Pons", callback_data="rulemgr:lane:pons:smart"),
        ],
        [
            InlineKeyboardButton("📋 All Saved Rules", callback_data="rulemgr:all"),
            InlineKeyboardButton("📥 Import JSON", callback_data="rulemgr:import"),
        ],
    ])


@admin_required
async def rule_manager_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_manager(update, context, edit=False)


async def _show_manager(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = True) -> None:
    text = (
        "⚙️ *RULE MANAGER*\n\n"
        "Switch saved rules instantly — no redeploy required.\n\n"
        "Choose a trading lane below, or import a JSON rule file."
    )
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=_manager_keyboard())
    else:
        await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=_manager_keyboard())


async def rule_manager_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    from app.security.allowlist import is_admin
    if not is_admin(update.effective_user.id):
        await query.edit_message_text("Restricted to admin.")
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "home":
        await _show_manager(update, context)
        return

    if action == "import":
        context.user_data["rule_manager_waiting_file"] = True
        await query.edit_message_text(
            "📥 *Import rule file*\n\n"
            "Upload a `.json` file containing either one rule object or a `rules` array.\n\n"
            "The file must contain rule settings only — never private keys or wallet secrets.\n\n"
            "Example: `{\"name\":\"graduation-safe\",\"platform\":\"solana\",\"strategy\":\"smart\"}`\n\n"
            "Send /cancel to stop.",
            parse_mode="Markdown",
        )
        return

    if action == "all":
        rules = await repo.get_rules_for_admin(update.effective_user.id)
        if not rules:
            await query.edit_message_text(
                "📋 *Saved Rules*\n\nNo rules saved yet. Use Import JSON or /setrule.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="rulemgr:home")]]),
            )
            return
        lines = ["📋 *SAVED RULES*\n"]
        for rule in rules[:40]:
            lines.append(f"{_rule_line(rule)}  ·  {_platform_label(rule.platform)}")
        if len(rules) > 40:
            lines.append(f"\n…and {len(rules) - 40} more")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="rulemgr:home")]]),
        )
        return

    if action == "lane" and len(parts) == 4:
        platform, strategy = parts[2], parts[3]
        await _show_lane(update, platform, strategy)
        return

    if action == "activate" and len(parts) == 3 and parts[2].isdigit():
        rule_id = int(parts[2])
        rule = await repo.get_rule_for_admin(rule_id, update.effective_user.id)
        if not rule:
            await query.edit_message_text("❌ That rule does not belong to this admin.")
            return
        strategy = getattr(rule, "strategy", "smart") or "smart"
        activated = await repo.activate_rule_for_admin_strategy(rule_id, update.effective_user.id, strategy)
        if not activated:
            await query.edit_message_text("❌ Could not activate that rule.")
            return
        if strategy == "smart_money":
            assigned = await repo.set_smart_money_rule(rule_id, update.effective_user.id)
            if not assigned:
                await query.edit_message_text("⚠️ Rule activated, but Smart Money assignment failed. Check the rule ID.")
                return
        await repo.write_audit_log(
            str(update.effective_user.id),
            "rule_manager_activate",
            {"rule_id": rule_id, "platform": rule.platform, "strategy": strategy},
        )
        await _show_lane(update, rule.platform, strategy)
        return


async def _show_lane(update: Update, platform: str, strategy: str) -> None:
    query = update.callback_query
    active = await repo.get_active_rule_for_strategy(update.effective_user.id, platform, strategy)
    rules = await repo.get_rules_for_admin(update.effective_user.id)
    lane_rules = [
        rule for rule in rules
        if getattr(rule, "platform", "solana") == platform
        and getattr(rule, "strategy", "smart") == strategy
    ]

    lines = [
        f"{_platform_label(platform)}\n",
        f"{_strategy_label(strategy)}\n",
        f"Active: *{active.name}* (#{active.id})" if active else "Active: *NONE*",
        "\nSaved rules:",
    ]
    buttons = []
    if lane_rules:
        for rule in lane_rules[:15]:
            label = f"🟢 {rule.name}" if rule.is_active else f"▶️ {rule.name}"
            buttons.append([InlineKeyboardButton(label[:60], callback_data=f"rulemgr:activate:{rule.id}")])
            lines.append(f"• #{rule.id} — {rule.name}")
    else:
        lines.append("• No saved rules in this lane yet.")

    buttons.append([
        InlineKeyboardButton("⬅️ Manager", callback_data="rulemgr:home"),
        InlineKeyboardButton("📥 Import", callback_data="rulemgr:import"),
    ])
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def rule_manager_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("rule_manager_waiting_file"):
        return

    from app.security.allowlist import is_admin
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Restricted to admin.")
        return

    document = update.message.document
    if not document:
        return
    if document.file_size and document.file_size > MAX_RULE_FILE_BYTES:
        await update.message.reply_text("❌ Rule file is too large. Maximum size is 256 KB.")
        return
    if document.file_name and not document.file_name.lower().endswith(".json"):
        await update.message.reply_text("❌ Please upload a JSON rule file.")
        return

    context.user_data["rule_manager_waiting_file"] = False
    try:
        tg_file = await document.get_file()
        raw_bytes = await tg_file.download_as_bytearray()
        payload = json.loads(bytes(raw_bytes).decode("utf-8"))
        items: list[dict[str, Any]]
        if isinstance(payload, dict) and isinstance(payload.get("rules"), list):
            items = payload["rules"]
        elif isinstance(payload, dict):
            items = [payload]
        else:
            raise ValueError("JSON root must be an object or an object containing a rules array")

        if not items or len(items) > 20:
            raise ValueError("file must contain between 1 and 20 rules")

        imported = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("each rule must be a JSON object")
            unknown = set(item) - set(RuleParams.model_fields)
            if unknown:
                raise ValueError(f"unknown rule fields: {', '.join(sorted(unknown))}")
            params = RuleParams.model_validate(item)
            rule = await repo.create_rule(params, update.effective_user.id, activate=False)
            imported.append(rule)

        await repo.write_audit_log(
            str(update.effective_user.id),
            "rule_manager_import",
            {"count": len(imported), "rule_ids": [r.id for r in imported], "file": document.file_name or "upload.json"},
        )

        lines = ["✅ *Rules imported successfully*\n"]
        for rule in imported:
            lines.append(f"• #{rule.id} — {rule.name} — {_platform_label(rule.platform)} — {_strategy_label(rule.strategy)}")
        lines.append("\nNothing was activated automatically. Use the buttons below to switch a lane.")
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Open Rule Manager", callback_data="rulemgr:home")],
                [InlineKeyboardButton("📋 View Saved Rules", callback_data="rulemgr:all")],
            ]),
        )
    except Exception as exc:
        logger.exception("rule_manager_import_failed")
        await update.message.reply_text(
            "❌ *Rule import failed*\n\n"
            f"`{type(exc).__name__}: {exc}`\n\n"
            "No rule is activated automatically. Fix the JSON and upload it again.",
            parse_mode="Markdown",
        )


async def rule_manager_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.pop("rule_manager_waiting_file", False):
        await update.message.reply_text("Rule import cancelled.")
