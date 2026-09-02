from app.arbitrage.continuous_hunt import ContinuousArbitrageHunt
from app.arbitrage.hunt import HuntResult, HuntStats


def test_status_keeps_hotlist_result_separate_from_global_result():
    hunter = ContinuousArbitrageHunt()
    hotlist = HuntResult((), (), stats=HuntStats(final_candidates=6, jupiter_round_trips=6))
    global_result = HuntResult((), (), stats=HuntStats(final_candidates=8, jupiter_round_trips=8))

    hunter._last_hotlist_result = hotlist
    hunter._last_global_result = global_result

    status = hunter.status

    assert status.last_hotlist_result is hotlist
    assert status.last_global_result is global_result
    assert status.last_hotlist_result.stats.final_candidates == 6
    assert status.last_global_result.stats.final_candidates == 8
    assert status.last_result is global_result
