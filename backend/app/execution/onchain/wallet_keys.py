"""Parses a user-supplied private key (base58 secret key or JSON byte array)
into a solders Keypair. Never logs or echoes the raw value."""
import json

import base58
from solders.keypair import Keypair

from app.security.redact import redact_text


class InvalidWalletKeyError(Exception):
    pass


def load_keypair(raw: str) -> Keypair:
    text = raw.strip()

    if text.startswith("["):
        try:
            byte_list = json.loads(text)
            key_bytes = bytes(byte_list)
        except (ValueError, TypeError) as exc:
            raise InvalidWalletKeyError(redact_text(f"invalid JSON byte array: {exc}"))
    else:
        try:
            key_bytes = base58.b58decode(text)
        except ValueError as exc:
            raise InvalidWalletKeyError(redact_text(f"invalid base58 secret key: {exc}"))

    try:
        if len(key_bytes) == 64:
            return Keypair.from_bytes(key_bytes)
        if len(key_bytes) == 32:
            return Keypair.from_seed(key_bytes)
        raise InvalidWalletKeyError(f"unexpected key length {len(key_bytes)} (expected 32 or 64 bytes)")
    except InvalidWalletKeyError:
        raise
    except Exception as exc:
        raise InvalidWalletKeyError(redact_text(f"could not construct keypair: {exc}"))
