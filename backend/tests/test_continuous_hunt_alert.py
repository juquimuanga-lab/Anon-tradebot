from app.arbitrage.continuous_hunt import _has_executable


def test_has_executable_false_for_empty_result():
    from app.arbitrage.hunt import HuntResult
    assert not _has_executable(HuntResult((), ()))
