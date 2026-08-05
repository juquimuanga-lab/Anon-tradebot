import json

import base58
import pytest
from solders.keypair import Keypair

from app.execution.onchain.wallet_keys import InvalidWalletKeyError, load_keypair


def test_load_keypair_from_base58_secret_key():
    original = Keypair()
    encoded = base58.b58encode(bytes(original)).decode()

    loaded = load_keypair(encoded)

    assert str(loaded.pubkey()) == str(original.pubkey())


def test_load_keypair_from_json_byte_array():
    original = Keypair()
    encoded = json.dumps(list(bytes(original)))

    loaded = load_keypair(encoded)

    assert str(loaded.pubkey()) == str(original.pubkey())


def test_load_keypair_rejects_garbage_base58():
    with pytest.raises(InvalidWalletKeyError):
        load_keypair("not-a-valid-key-!!!")


def test_load_keypair_rejects_wrong_length_json_array():
    with pytest.raises(InvalidWalletKeyError):
        load_keypair(json.dumps([1, 2, 3]))


def test_load_keypair_error_never_echoes_raw_input():
    bad_key = "SuperSecretRawInput12345"
    with pytest.raises(InvalidWalletKeyError) as exc_info:
        load_keypair(bad_key + "!!!not-base58!!!")
    # the raw secret-looking text should not be echoed verbatim into the error
    assert "SuperSecretRawInput12345!!!not-base58!!!" not in str(exc_info.value)
