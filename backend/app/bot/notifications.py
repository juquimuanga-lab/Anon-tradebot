"""Sends concise, secret-free Telegram alerts to all admins."""
import logging

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from app.config.settings import settings
from app.security.redact import redact_text

logger = logging.getLogger("app.bot.notifications")


class Notifier:
    def __init__(self, bot: Bot):
        self._bot = bot

    async def _send_to_admins(self, text: str) -> None:
        safe_text = redact_text(text)
        for admin_id in settings.telegram_admin_ids:
            try:
                await self._bot.send_message(chat_id=admin_id, text=safe_text, parse_mode=ParseMode.MARKDOWN)
            except TelegramError as exc:
                logger.warning("notify_failed", extra={"admin_id": admin_id, "error": str(exc)})

    async def new_qualified_token(self, ticker: str, mint: str, score: float, source: str) -> None:
        tag = "[SIMULATED] " if source == "mock_simulated" else ""
        await self._send_to_admins(
            f"{tag}*New qualified token* `{ticker}`\nMint: `{mint[:6]}...{mint[-4:]}`\nScore: {score:.1f}/100"
        )

    async def rule_violation(self, ticker: str, reasons: list[str]) -> None:
        await self._send_to_admins(f"*Skipped* `{ticker}`\n- " + "\n- ".join(reasons[:5]))

    async def buy_placed(self, ticker: str, amount_sol: float, mode: str) -> None:
        await self._send_to_admins(f"*Buy placed* `{ticker}` for {amount_sol:.3f} SOL ({mode} mode)")

    async def buy_filled(self, ticker: str, price_usd: float, mode: str) -> None:
        await self._send_to_admins(f"*Buy filled* `{ticker}` @ ${price_usd:.6f} ({mode} mode)")

    async def buy_failed(self, ticker: str, reason: str) -> None:
        await self._send_to_admins(f"*Buy failed* `{ticker}`\nReason: {reason}")

    async def sell_triggered(self, ticker: str, reason: str, sell_pct: float) -> None:
        await self._send_to_admins(f"*Sell triggered* `{ticker}` ({sell_pct:.0f}%)\nReason: {reason}")

    async def sell_filled(self, ticker: str, price_usd: float, pnl_usd: float) -> None:
        sign = "+" if pnl_usd >= 0 else ""
        await self._send_to_admins(f"*Sell filled* `{ticker}` @ ${price_usd:.6f}\nPnL: {sign}${pnl_usd:.2f}")

    async def api_error(self, source: str, message: str) -> None:
        await self._send_to_admins(f"*API error* ({source})\n{message}")

    async def low_balance(self, balance_sol: float) -> None:
        await self._send_to_admins(f"*Low balance warning*: {balance_sol:.3f} SOL remaining")

    async def daily_summary(self, text: str) -> None:
        await self._send_to_admins(f"*Daily summary*\n{text}")
