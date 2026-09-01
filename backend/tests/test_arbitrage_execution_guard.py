import pytest

from app.arbitrage.execution_guard import UnsafeArbitrageBundle, validate_tip_layout


def test_embedded_tip_layout_is_allowed() -> None:
    validate_tip_layout(transaction_count=2, tip_is_embedded=True)


def test_standalone_tip_is_rejected() -> None:
    with pytest.raises(UnsafeArbitrageBundle):
        validate_tip_layout(transaction_count=3, tip_is_embedded=False)


def test_empty_bundle_is_rejected() -> None:
    with pytest.raises(UnsafeArbitrageBundle):
        validate_tip_layout(transaction_count=0, tip_is_embedded=True)
