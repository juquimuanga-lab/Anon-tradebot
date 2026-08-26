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
    """Accept either a raw Alchemy key or a complete Robinhood RPC URL.

    Railway deployments sometimes receive the full Alchemy app URL in the
    API-key variable. The old resolver blindly appended that URL to `/v2/`,
    producing an invalid address such as:
      .../v2/https://robinhood-mainnet.g.alchemy.com/v2/<key>
    """
    text = str(value).strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return text.rstrip("/")
    return ""


def resolve_robinhood_rpc_url(settings_obj) -> str:
    """Resolve Robinhood RPC without requiring a wallet private key in env.

    Priority:
      1. Explicit Robinhood RPC URL
      2. Explicit Alchemy RPC URL
      3. Robinhood-specific Alchemy API key
      4. Generic Alchemy API key
      5. Robinhood public RPC

    A setting containing a complete HTTP(S) URL is used directly. A raw
    Alchemy key is expanded into the Robinhood Alchemy endpoint.
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
        return ROBINHOOD_ALCHEMY_RPC_TEMPLATE.format(api_key=str(api_key).strip())

    return ROBINHOOD_PUBLIC_RPC_URL


def build_robinhood_web3(rpc_url: str) -> Web3:
    if not rpc_url:
        raise RuntimeError("Robinhood Chain RPC is not configured")
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
    try:
        chain_id = int(w3.eth.chain_id)
    except Exception as exc:
        raise RuntimeError("could not reach Robinhood Chain RPC") from exc
    if chain_id != ROBINHOOD_CHAIN_ID:
        raise RuntimeError(f"wrong chain ID {chain_id}; expected Robinhood Chain {ROBINHOOD_CHAIN_ID}")
    return w3


def get_native_balance_eth(rpc_url: str, address: str) -> float:
    w3 = build_robinhood_web3(rpc_url)
    balance = int(w3.eth.get_balance(Web3.to_checksum_address(address)))
    return balance / 10**18
