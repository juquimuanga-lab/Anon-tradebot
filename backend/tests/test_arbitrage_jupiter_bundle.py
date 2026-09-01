import base64

from app.arbitrage.jupiter_bundle import _instruction


def test_jupiter_instruction_parser_preserves_accounts_and_data():
    raw = {
        "programId": "11111111111111111111111111111111",
        "accounts": [
            {
                "pubkey": "11111111111111111111111111111111",
                "isSigner": False,
                "isWritable": True,
            }
        ],
        "data": base64.b64encode(b"arb").decode(),
    }

    instruction = _instruction(raw)

    assert str(instruction.program_id) == raw["programId"]
    assert instruction.data == b"arb"
    assert len(instruction.accounts) == 1
    assert instruction.accounts[0].is_writable is True
    assert instruction.accounts[0].is_signer is False
