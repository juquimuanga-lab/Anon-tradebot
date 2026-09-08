from datetime import datetime, timezone

from app.scoring.rules import RuleParams, TokenSnapshot
from app.scanners.trader_brain import TraderBrain


def _token(mint: str, mc: float, pressure: float, ratio: float, velocity: float) -> TokenSnapshot:
    return TokenSnapshot(
        mint=mint,
        created_on=datetime.now(timezone.utc),
        price_usd=mc / 1_000_000,
        market_cap_usd=mc,
        liquidity_usd=5_000,
        holders=40,
        source="pumpfun",
        raw_enrichment={
            "pumpfun_launch_safety": {
                "signals": {
                    "buy_pressure": pressure,
                    "buy_sell_ratio": ratio,
                    "buy_velocity_sol_per_sec": velocity,
                }
            }
        },
    )


def _rule() -> RuleParams:
    return RuleParams(strategy="smart", platform="solana")


def test_vertical_move_with_weakening_flow_is_not_chased():
    brain = TraderBrain()
    rule = _rule()

    brain.observe(_token("mint-a", 8_000, 0.82, 3.0, 0.04), rule)
    brain.observe(_token("mint-a", 10_000, 0.84, 3.2, 0.05), rule)
    result = brain.observe(_token("mint-a", 12_000, 0.72, 2.0, 0.03), rule)

    assert result["phase"] in {"extended", "watch"}
    assert result["decision"] == "wait"


def test_pullback_then_reclaim_can_trigger_entry():
    brain = TraderBrain()
    rule = _rule()

    brain.observe(_token("mint-b", 10_000, 0.80, 2.5, 0.03), rule)
    brain.observe(_token("mint-b", 12_000, 0.82, 2.8, 0.04), rule)
    brain.observe(_token("mint-b", 14_000, 0.84, 3.0, 0.05), rule)
    pullback = brain.observe(_token("mint-b", 12_600, 0.55, 0.95, 0.004), rule)
    assert pullback["decision"] == "wait"

    reclaim = brain.observe(_token("mint-b", 13_150, 0.75, 1.8, 0.02), rule)
    assert reclaim["phase"] == "reclaim"
    assert reclaim["decision"] == "enter"


def test_controlled_breakout_can_trigger_entry():
    brain = TraderBrain()
    rule = _rule()

    brain.observe(_token("mint-c", 10_000, 0.70, 1.8, 0.02), rule)
    brain.observe(_token("mint-c", 10_100, 0.72, 1.9, 0.02), rule)
    result = brain.observe(_token("mint-c", 10_400, 0.78, 2.2, 0.025), rule)

    assert result["phase"] == "breakout"
    assert result["decision"] == "enter"
