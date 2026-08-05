"""Input validation/sanitisation for Telegram commands and wizard steps."""
import re

_MAX_LEN = 300
_SAFE_TEXT_RE = re.compile(r"^[\w\s.,\-:/@]+$")


def sanitize_text(raw: str) -> str:
    text = (raw or "").strip()[:_MAX_LEN]
    return "".join(ch for ch in text if ch.isprintable())


def parse_float(raw: str, min_value: float = 0.0, max_value: float = 1e12) -> float:
    value = float(sanitize_text(raw))
    if not (min_value <= value <= max_value):
        raise ValueError(f"must be between {min_value} and {max_value}")
    return value


def parse_int(raw: str, min_value: int = 0, max_value: int = 10_000_000) -> int:
    value = int(sanitize_text(raw))
    if not (min_value <= value <= max_value):
        raise ValueError(f"must be between {min_value} and {max_value}")
    return value


def parse_wallet_list(raw: str) -> list[str]:
    text = sanitize_text(raw)
    if text.lower() in ("", "none", "skip", "-"):
        return []
    return [w.strip() for w in text.split(",") if w.strip()]


def is_plausible_api_key(raw: str) -> bool:
    text = raw.strip()
    return 8 <= len(text) <= 400 and " " not in text and "\n" not in text
