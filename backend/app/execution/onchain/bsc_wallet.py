"""EVM/BSC wallet parsing. Private keys remain in Python memory only."""
from web3 import Web3
from eth_account import Account


class InvalidBscWalletKeyError(Exception):
    pass


def load_bsc_account(raw: str):
    text = raw.strip()
    if not text.startswith("0x"):
        text = "0x" + text
    if len(text) != 66:
        raise InvalidBscWalletKeyError("BSC private key must be a 32-byte hex key")
    try:
        return Account.from_key(text)
    except Exception as exc:
        raise InvalidBscWalletKeyError("invalid BSC private key") from exc


def checksum(address: str) -> str:
    return Web3.to_checksum_address(address)
