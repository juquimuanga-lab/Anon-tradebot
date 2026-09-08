from app.execution.onchain.solana_rpc import extract_wallet_trade_execution


OWNER = "11111111111111111111111111111111"
TOKEN_MINT = "So11111111111111111111111111111111111111112"
SENDER_TIP = "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"


def _buy_transaction(*, include_sender_tip: bool) -> dict:
    instructions = [
        {
            "parsed": {
                "type": "transfer",
                "info": {
                    "source": OWNER,
                    "destination": "BondingCurve111111111111111111111111111111",
                    "lamports": 1_000_000,
                },
            }
        }
    ]

    if include_sender_tip:
        instructions.append(
            {
                "parsed": {
                    "type": "transfer",
                    "info": {
                        "source": OWNER,
                        "destination": SENDER_TIP,
                        "lamports": 1_000_000,
                    },
                }
            }
        )

    return {
        "meta": {
            "fee": 5_000,
            "preBalances": [10_005_000],
            "postBalances": [8_000_000 if include_sender_tip else 9_000_000],
            "preTokenBalances": [],
            "postTokenBalances": [
                {
                    "accountIndex": 0,
                    "mint": TOKEN_MINT,
                    "owner": OWNER,
                    "uiTokenAmount": {
                        "amount": "1000",
                        "decimals": 0,
                    },
                }
            ],
            "innerInstructions": [
                {
                    "index": 0,
                    "instructions": instructions,
                }
            ],
        },
        "transaction": {
            "message": {
                "accountKeys": [OWNER],
                "instructions": [],
            }
        },
    }


def test_sender_tip_is_not_counted_as_pumpfun_buy_cost():
    execution = extract_wallet_trade_execution(
        _buy_transaction(include_sender_tip=True),
        OWNER,
        TOKEN_MINT,
    )

    assert execution is not None
    assert execution["sol_transfer_lamports"] == 1_000_000
    assert execution["sol_spent_excluding_fee_lamports"] == 1_000_000
    assert execution["sender_tip_lamports"] == 1_000_000
    assert execution["token_received_raw"] == 1000


def test_normal_buy_transfer_is_unchanged_without_sender_tip():
    execution = extract_wallet_trade_execution(
        _buy_transaction(include_sender_tip=False),
        OWNER,
        TOKEN_MINT,
    )

    assert execution is not None
    assert execution["sol_transfer_lamports"] == 1_000_000
    assert execution["sol_spent_excluding_fee_lamports"] == 1_000_000
    assert execution["token_received_raw"] == 1000
