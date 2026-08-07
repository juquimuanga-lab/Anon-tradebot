"""Sends concise, secret-free Telegram alerts.

Trade-specific alerts (a token qualifying/getting skipped under a specific
rule, a buy/sell being placed or filled) go only to the admin who owns that
rule/position - other admins never see them. Only genuinely system-wide
events (a scanner-level API error, the combined daily summary) still go to
every admin.
"""
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

    async def _send_to(self, recipient_id: int, text: str) -> None:
        safe_text = redact_text(text)
        try:
            await self._bot.send_message(chat_id=recipient_id, text=safe_text, parse_mode=ParseMode.MARKDOWN)
        except TelegramError as exc:
            logger.warning("notify_failed", extra={"admin_id": recipient_id, "error": str(exc)})

    async def new_qualified_token(self, recipient_id: int, ticker: str, mint: str, score: float, source: str) -> None:
        tag = "[SIMULATED] " if source == "mock_simulated" else ""
        await self._send_to(
            recipient_id,
            f"{tag}*New qualified token* `{ticker}`\nMint: `{mint[:6]}...{mint[-4:]}`\nScore: {score:.1f}/100",
        )

    async def rule_violation(self, recipient_id: int, ticker: str, reasons: list[str]) -> None:
        await self._send_to(recipient_id, f"*Skipped* `{ticker}`\n- " + "\n- ".join(reasons[:5]))

    async def buy_placed(self, recipient_id: int, ticker: str, amount_sol: float, mode: str) -> None:
        await self._send_to(recipient_id, f"*Buy placed* `{ticker}` for {amount_sol:.3f} SOL ({mode} mode)")

    async def buy_filled(self, recipient_id: int, ticker: str, price_usd: float, mode: str, tx_signature: str | None = None) -> None:
        link = f"\n[View on Solscan](https://solscan.io/tx/{tx_signature})" if tx_signature else ""
        await self._send_to(recipient_id, f"*Buy filled* `{ticker}` @ ${price_usd:.6f} ({mode} mode){link}")

    async def buy_failed(self, recipient_id: int, ticker: str, reason: str) -> None:
        await self._send_to(recipient_id, f"*Buy failed* `{ticker}`\nReason: {reason}")

    async def sell_triggered(self, recipient_id: int, ticker: str, reason: str, sell_pct: float) -> None:
        await self._send_to(recipient_id, f"*Sell triggered* `{ticker}` ({sell_pct:.0f}%)\nReason: {reason}")

    async def sell_filled(self, recipient_id: int, ticker: str, price_usd: float, pnl_usd: float, tx_signature: str | None = None) -> None:
        sign = "+" if pnl_usd >= 0 else ""
        link = f"\n[View on Solscan](https://solscan.io/tx/{tx_signature})" if tx_signature else ""
        await self._send_to(recipient_id, f"*Sell filled* `{ticker}` @ ${price_usd:.6f}\nPnL: {sign}${pnl_usd:.2f}{link}")

    async def sell_failed(self, recipient_id: int, ticker: str, reason: str) -> None:
        await self._send_to(recipient_id, f"*Sell failed* `{ticker}`\nReason: {reason}")

    async def api_error(self, source: str, message: str) -> None:
        # System-wide (e.g. the scanner loop itself erroring) - not tied to
        # any one admin's rule or trade, so this one stays broadcast.
        await self._send_to_admins(f"*API error* ({source})\n{message}")

    async def low_balance(self, recipient_id: int, balance_sol: float) -> None:
        await self._send_to(recipient_id, f"*Low balance warning*: {balance_sol:.3f} SOL remaining")

    async def daily_summary(self, text: str) -> None:
        # Combined system-wide summary for now (see delivery notes) - still
        # broadcast to every admin rather than split per-admin.
        await self._send_to_admins(f"*Daily summary*\n{text}")
