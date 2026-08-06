"""Normalises raw Anoncoin coin payloads into TokenSnapshot objects."""
from datetime import datetime, timezone
from typing import Optional

from app.scoring.rules import TokenSnapshot


def _parse_money(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace("$", "").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def from_anoncoin_coin(raw: dict) -> TokenSnapshot:
    creator = raw.get("devWalletAddress") or (raw.get("creator") or {}).get("userName", "")
    return TokenSnapshot(
        mint=raw["mint"],
        ticker_name=raw.get("tickerName", ""),
        ticker_symbol=raw.get("tickerSymbol", ""),
        creator_wallet=creator or "",
        created_on=_parse_dt(raw.get("createdOn")) or datetime.now(timezone.utc),
        price_usd=_parse_money(raw.get("priceUsd")),
        market_cap_usd=_parse_money(raw.get("marketCapUsd")),
        liquidity_usd=_parse_money(raw.get("liquidityUsd")),
        holders=int(raw.get("holders") or 0),
        volume_24h_usd=_parse_money(raw.get("volume24HrsUsd")),
        is_migrated=bool(raw.get("isMigrated", False)),
        source="anoncoin",
        raw_anoncoin=raw,
    )


def apply_solscan_enrichment(
    token: TokenSnapshot,
    meta: Optional[dict],
    holders: Optional[dict],
) -> TokenSnapshot:

    if meta and isinstance(meta.get("data"), dict):
        data = meta["data"]

        token.raw_solscan["meta"] = data

        decimals = data.get("decimals")
        if isinstance(decimals, int):
            token.decimals = decimals

    if holders and isinstance(holders.get("data"), dict):
        data = holders["data"]

        token.raw_solscan["holders"] = data

        total = data.get("total")

        if isinstance(total, int):
            if token.holders is None:
                token.holders = total
            else:
                token.holders = max(token.holders, total)

    return token
