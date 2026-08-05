"""Execution adapter contract. Trade execution is isolated here so the real
Anoncoin trade endpoint (not yet published) can be plugged in later without
touching scanning, scoring or Telegram control code."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from app.scoring.rules import TokenSnapshot


@dataclass
class OrderResult:
    success: bool
    status: str  # filled | failed | pending
    price_usd: float = 0.0
    tx_signature: Optional[str] = None
    error_message: Optional[str] = None


class ExecutionNotAvailableError(Exception):
    pass


class ExecutionAdapter(ABC):
    mode: str

    @abstractmethod
    async def buy(self, token: TokenSnapshot, amount_sol: float) -> OrderResult:
        ...

    @abstractmethod
    async def sell(self, token: TokenSnapshot, amount_tokens: float, sell_pct: float) -> OrderResult:
        ...
