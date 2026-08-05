"""In-memory confirmation tokens for destructive actions (disable-all, mode
switch, delete config, withdraw). Tokens expire after a short window."""
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class PendingConfirmation:
    action: str
    payload: dict
    created_at: float


class ConfirmationStore:
    def __init__(self, ttl_seconds: int = 120):
        self._ttl = ttl_seconds
        self._pending: dict[str, PendingConfirmation] = {}

    def create(self, action: str, payload: Optional[dict] = None) -> str:
        token = secrets.token_urlsafe(8)
        self._pending[token] = PendingConfirmation(action=action, payload=payload or {}, created_at=time.time())
        return token

    def resolve(self, token: str) -> Optional[PendingConfirmation]:
        entry = self._pending.pop(token, None)
        if not entry:
            return None
        if time.time() - entry.created_at > self._ttl:
            return None
        return entry


confirmation_store = ConfirmationStore()
