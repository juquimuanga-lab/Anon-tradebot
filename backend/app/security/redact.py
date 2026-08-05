"""Redaction helpers so secrets never leak into logs or Telegram messages."""
import re
from typing import Iterable

_TOKEN_PATTERNS = [
    re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"),  # telegram bot token
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),  # JWT
    re.compile(r"\b(?=[A-Za-z0-9_-]{28,}\b)(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{28,}\b"),  # long token/key-like strings (must contain a digit to avoid matching plain log identifiers)
]

REDACTED = "***REDACTED***"


def redact_text(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Mask known secret values and anything that looks like a token/key."""
    if not text:
        return text
    out = text
    for secret in extra_secrets:
        if secret and secret in out:
            out = out.replace(secret, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def mask_secret(value: str, keep_last: int = 4) -> str:
    """Show only the last few characters, for user-facing confirmation only."""
    if not value:
        return ""
    if len(value) <= keep_last:
        return "*" * len(value)
    return "*" * (len(value) - keep_last) + value[-keep_last:]
