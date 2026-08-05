"""Watches specific wallets on-chain (via the free public Solana RPC) for new
SPL token mint creation - used because Anoncoin's own discovery API and the
user's current Solscan plan are both unavailable. Anoncoin builds on Meteora's
Dynamic Bonding Curve program, so any transaction from a watched wallet that
introduces a brand-new token mint (via preTokenBalances/postTokenBalances
diff) is treated as a new launch."""
import asyncio
import logging
from typing import Optional

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solders.signature import Signature

logger = logging.getLogger("app.scanners.onchain_watcher")

SOL_MINT = "So11111111111111111111111111111111111111112"


class WatermarkStore:
    """Per-wallet signature watermark so each poll only looks at new activity."""

    def __init__(self):
        self._last_seen: dict[str, str] = {}
        self._initialized: set[str] = set()

    def get(self, wallet: str) -> Optional[str]:
        return self._last_seen.get(wallet)

    def set(self, wallet: str, signature: str) -> None:
        self._last_seen[wallet] = signature

    def is_initialized(self, wallet: str) -> bool:
        return wallet in self._initialized

    def mark_initialized(self, wallet: str) -> None:
        self._initialized.add(wallet)


def extract_new_mint(tx) -> Optional[str]:
    """Given a jsonParsed getTransaction result, returns a newly-created SPL
    mint address (present in postTokenBalances but not preTokenBalances),
    ignoring the wrapped-SOL mint. Returns None if nothing new was minted."""
    try:
        meta = tx.transaction.meta
        pre_mints = {str(b.mint) for b in (meta.pre_token_balances or [])}
        post_mints = {str(b.mint) for b in (meta.post_token_balances or [])}
        new_mints = [m for m in (post_mints - pre_mints) if m != SOL_MINT]
        if new_mints:
            return new_mints[0]
    except Exception:
        logger.debug("mint_extraction_failed", exc_info=True)
    return None


async def poll_new_mints(rpc_url: str, wallet: str, watermarks: WatermarkStore, limit: int = 20) -> list[dict]:
    async with AsyncClient(rpc_url) as client:
        pubkey = Pubkey.from_string(wallet)
        until = watermarks.get(wallet)
        try:
            resp = await client.get_signatures_for_address(
                pubkey, limit=limit, until=Signature.from_string(until) if until else None
            )
        except Exception as exc:
            logger.warning("get_signatures_failed", extra={"wallet": wallet, "error": str(exc)})
            return []

        sig_infos = resp.value
        if not sig_infos:
            return []

        watermarks.set(wallet, str(sig_infos[0].signature))

        if not watermarks.is_initialized(wallet):
            # First poll for this wallet: just establish the watermark so we
            # only alert on launches that happen after the bot starts watching.
            watermarks.mark_initialized(wallet)
            return []

        discovered = []
        for sig_info in reversed(sig_infos):  # oldest to newest
            if sig_info.err is not None:
                continue
            try:
                tx_resp = await client.get_transaction(
                    sig_info.signature, encoding="jsonParsed", max_supported_transaction_version=0
                )
            except Exception as exc:
                logger.warning("get_transaction_failed", extra={"error": str(exc)})
                continue
            if not tx_resp.value:
                continue
            mint = extract_new_mint(tx_resp.value)
            if mint:
                discovered.append(
                    {
                        "mint": mint,
                        "tx_signature": str(sig_info.signature),
                        "block_time": sig_info.block_time,
                        "watched_wallet": wallet,
                    }
                )
            await asyncio.sleep(0.15)  # be gentle with the free public RPC's rate limit
        return discovered
