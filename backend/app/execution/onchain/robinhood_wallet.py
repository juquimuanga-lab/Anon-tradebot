"""Robinhood Chain wallet parsing and safe public RPC helpers.

Private keys are accepted only through the Telegram connect flow and remain
encrypted at rest; this module never logs or persists raw key material.
"""
from __future__ import annotations

from urllib.parse import urlparse

from web3 import Web3
from eth_account import Account

ROBINHOOD_CHAIN_ID = 4663
ROBINHOOD_PUBLIC_RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
ROBINHOOD_ALCHEMY_RPC_TEMPLATE = "https://robinhood-mainnet.g.alchemy.com/v2/{api_key}"


class InvalidRobinhoodWalletKeyError(Exception):
    pass


def load_robinhood_account(raw: str):
    text = raw.strip()
    if not text.startswith("0x"):
        text = "0x" + text
    if len(text) != 66:
        raise InvalidRobinhoodWalletKeyError("Robinhood private key must be a 32-byte hex key")
    try:
        return Account.from_key(text)
    except Exception as exc:
        raise InvalidRobinhoodWalletKeyError("invalid Robinhood private key") from exc


def checksum(address: str) -> str:
    return Web3.to_checksum_address(address)


def _normalize_rpc_value(value: str) -> str:
    """Accept a complete RPC URL only when it is structurally usable.

    Railway deployments may provide either a raw Alchemy key or a complete
    Alchemy URL. A value such as
    ``https://robinhood-mainnet.g.alchemy.com/v2/`` is *not* usable and must
    not be returned as an RPC URL because Alchemy answers it with HTTP 400.
    """
    text = str(value).strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    # Alchemy Robinhood URLs require a non-empty key after /v2/.
    if parsed.netloc.lower() == "robinhood-mainnet.g.alchemy.com":
        path = parsed.path.rstrip("/")
        if not path.startswith("/v2/") or len(path.removeprefix("/v2/")) < 8:
            return ""

    return text.rstrip("/")


def _alchemy_url_from_key(value: str) -> str:
    """Build an Alchemy URL from a raw key, rejecting obvious placeholders."""
    key = str(value).strip()
    if not key or key.lower() in {"your_alchemy_key", "your_alchemy_api_key", "changeme"}:
        return ""
    return ROBINHOOD_ALCHEMY_RPC_TEMPLATE.format(api_key=key)


def resolve_robinhood_rpc_url(settings_obj) -> str:
    """Resolve Robinhood RPC safely.

    Priority:
      1. Explicit Robinhood RPC URL
      2. Explicit Alchemy RPC URL
      3. Robinhood-specific Alchemy API key
      4. Generic Alchemy API key
      5. Robinhood public RPC

    Complete URLs are used directly only when structurally valid. Raw Alchemy
    keys are expanded into the Robinhood Alchemy endpoint. Malformed/empty
    Alchemy configuration falls back to the public Robinhood RPC instead of
    constructing a guaranteed HTTP 400 URL.
    """
    explicit = (
        getattr(settings_obj, "robinhood_rpc_url", None)
        or getattr(settings_obj, "robinhood_rpc_override_url", None)
        or getattr(settings_obj, "robinhood_alchemy_rpc_url", None)
    )
    if explicit:
        normalized = _normalize_rpc_value(explicit)
        if normalized:
            return normalized

    api_key = (
        getattr(settings_obj, "robinhood_alchemy_api_key", None)
        or getattr(settings_obj, "alchemy_api_key", None)
    )
    if api_key:
        normalized = _normalize_rpc_value(api_key)
        if normalized:
            return normalized
        alchemy_url = _alchemy_url_from_key(api_key)
        if alchemy_url:
            return alchemy_url

    return ROBINHOOD_PUBLIC_RPC_URL


def build_robinhood_web3(rpc_url: str) -> Web3:
    """Build a Robinhood Web3 client with a safe public-RPC fallback.

    An older Railway environment may still contain a malformed Alchemy URL
    even after the resolver has been updated. Never let that stale value block
    the wallet/trading lane: validate it first and fall back to Robinhood's
    official public mainnet RPC when necessary.
    """
    requested = _normalize_rpc_value(rpc_url)
    candidates = []
    if requested:
        candidates.append(requested)
    if ROBINHOOD_PUBLIC_RPC_URL not in candidates:
        candidates.append(ROBINHOOD_PUBLIC_RPC_URL)

    last_error = None
    for candidate in candidates:
        w3 = Web3(Web3.HTTPProvider(candidate, request_kwargs={"timeout": 8}))
        try:
            chain_id = int(w3.eth.chain_id)
        except Exception as exc:
            last_error = exc
            continue
        if chain_id != ROBINHOOD_CHAIN_ID:
            last_error = RuntimeError(
                f"wrong chain ID {chain_id}; expected Robinhood Chain {ROBINHOOD_CHAIN_ID}"
            )
            continue
        return w3

    raise RuntimeError("could not reach Robinhood Chain RPC") from last_error


def get_native_balance_eth(rpc_url: str, address: str) -> float:
    w3 = build_robinhood_web3(rpc_url)
    balance = int(w3.eth.get_balance(Web3.to_checksum_address(address)))
    return balance / 10**18
