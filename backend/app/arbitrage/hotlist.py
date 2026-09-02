"""High-priority Solana arbitrage watchlist.

The hotlist keeps repeatedly productive mints under direct, frequent
Jupiter scrutiny without changing the sniper execution path.
"""
from __future__ import annotations

import os

DEFAULT_HOTLIST_MINTS = (
    "METvsvVRapdj9cFLzq4Tr43xK4tAjQfwX76z3n6mWQL",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn",
    "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
    "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx",
)


def configured_hotlist_mints() -> tuple[str, ...]:
    """Return configured hotlist mints, falling back to the six seed mints."""
    raw = os.getenv("ARBITRAGE_HOTLIST_MINTS", "").strip()
    if not raw:
        return DEFAULT_HOTLIST_MINTS

    values: list[str] = []
    for item in raw.split(","):
        mint = item.strip()
        if mint and mint not in values:
            values.append(mint)
    return tuple(values) or DEFAULT_HOTLIST_MINTS
