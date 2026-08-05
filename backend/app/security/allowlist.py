"""Telegram admin allowlist enforcement."""
import functools
from typing import Callable

from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import settings


def is_admin(user_id: int) -> bool:
    return user_id in settings.telegram_admin_ids


def admin_required(func: Callable):
    """Decorator for handlers that mutate config, secrets, or place trades."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or not is_admin(user.id):
            if update.message:
                await update.message.reply_text(
                    "This action is restricted to the bot admin(s)."
                )
            elif update.callback_query:
                await update.callback_query.answer(
                    "Restricted to admin.", show_alert=True
                )
            return None
        return await func(update, context, *args, **kwargs)

    return wrapper
