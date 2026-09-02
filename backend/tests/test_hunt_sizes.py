from app.arbitrage.hunt import DEFAULT_HUNT_DISCOVERY_SIZES_SOL


def test_default_hunt_discovery_sizes_include_040_sol():
    assert DEFAULT_HUNT_DISCOVERY_SIZES_SOL == (0.02, 0.04, 0.10, 0.50)
