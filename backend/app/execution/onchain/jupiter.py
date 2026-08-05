"""Jupiter aggregator client for post-graduation (migrated) tokens. Pure
Python/HTTP - Jupiter indexes Meteora DAMM pools once a token migrates, so
this covers the case Meteora's DBC no longer applies to."""
import logging
from typing import Optional

import httpx

from app.execution.retry import with_backoff

logger = logging.getLogger("app.execution.onchain.jupiter")

SOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterError(Exception):
    pass


class JupiterClient:
    def __init__(self, base_url: str):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=20.0)

    async def aclose(self):
        await self._client.aclose()

    @with_backoff()
    async def _get_quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
        resp = await self._client.get(
            "/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
                "slippageBps": slippage_bps,
            },
        )
        if resp.status_code != 200:
            raise JupiterError(f"quote failed with status {resp.status_code}")
        return resp.json()

    @with_backoff()
    async def _post_swap(self, quote_response: dict, user_pubkey: str) -> dict:
        resp = await self._client.post(
            "/swap",
            json={
                "quoteResponse": quote_response,
                "userPublicKey": user_pubkey,
                "wrapAndUnwrapSol": True,
            },
        )
        if resp.status_code != 200:
            raise JupiterError(f"swap build failed with status {resp.status_code}")
        return resp.json()
    async def build_unsigned_swap(
        self, input_mint: str, output_mint: str, amount: int, slippage_bps: int, user_pubkey: str
    ) -> dict:
        quote = await self._get_quote(input_mint, output_mint, amount, slippage_bps)
        if "error" in quote:
            raise JupiterError(str(quote["error"]))
        swap = await self._post_swap(quote, user_pubkey)
        if "swapTransaction" not in swap:
            raise JupiterError("swap response missing swapTransaction")
        return {
            "transaction_b64": swap["swapTransaction"],
            "quoted_output_amount": quote.get("outAmount"),
        }

    async def buy_quote_tx(self, token_mint: str, amount_lamports: int, slippage_bps: int, user_pubkey: str) -> dict:
        return await self.build_unsigned_swap(SOL_MINT, token_mint, amount_lamports, slippage_bps, user_pubkey)

    async def sell_quote_tx(self, token_mint: str, amount_raw: int, slippage_bps: int, user_pubkey: str) -> dict:
        return await self.build_unsigned_swap(token_mint, SOL_MINT, amount_raw, slippage_bps, user_pubkey)
