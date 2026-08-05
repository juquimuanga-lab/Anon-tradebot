from types import SimpleNamespace

from app.scanners.onchain_watcher import SOL_MINT, extract_new_mint


def _make_tx(pre_mints, post_mints):
    pre = [SimpleNamespace(mint=m) for m in pre_mints]
    post = [SimpleNamespace(mint=m) for m in post_mints]
    meta = SimpleNamespace(pre_token_balances=pre, post_token_balances=post)
    return SimpleNamespace(transaction=SimpleNamespace(meta=meta))


def test_extract_new_mint_finds_freshly_created_mint():
    tx = _make_tx(pre_mints=[], post_mints=[SOL_MINT, "NewMint111111111111111111111111111111111"])
    assert extract_new_mint(tx) == "NewMint111111111111111111111111111111111"


def test_extract_new_mint_ignores_sol_mint_only_diff():
    tx = _make_tx(pre_mints=["ExistingMint1111111111111111111111111111"], post_mints=[SOL_MINT])
    assert extract_new_mint(tx) is None


def test_extract_new_mint_returns_none_when_nothing_new():
    tx = _make_tx(pre_mints=["MintA"], post_mints=["MintA"])
    assert extract_new_mint(tx) is None


def test_extract_new_mint_handles_malformed_transaction_gracefully():
    broken_tx = SimpleNamespace(transaction=None)
    assert extract_new_mint(broken_tx) is None
