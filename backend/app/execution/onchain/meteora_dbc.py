"""Python wrapper around the Node.js Meteora DBC transaction builder.

We shell out to Meteora's official TypeScript SDK (no private key ever
crosses this boundary - only public keys and amounts) because there is no
official Python SDK for the Dynamic Bonding Curve program. The unsigned
transaction it returns is signed locally in Python (see wallet_live.py).
"""
import asyncio
import json
import logging
import os

logger = logging.getLogger("app.execution.onchain.meteora_dbc")

_BUILDER_DIR = os.path.join(os.path.dirname(__file__), "dbc_builder")


class DbcBuildError(Exception):
    pass


async def build_unsigned_swap(
    action: str, base_mint: str, owner_pubkey: str, amount_lamports: int, slippage_bps: int, rpc_url: str
) -> dict:
    payload = {
        "action": action,
        "baseMint": base_mint,
        "ownerPubkey": owner_pubkey,
        "amountLamports": str(amount_lamports),
        "slippageBps": slippage_bps,
        "rpcUrl": rpc_url,
    }
    proc = await asyncio.create_subprocess_exec(
        "node",
        "build_tx.js",
        cwd=_BUILDER_DIR,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), timeout=30)

    if not stdout.strip():
        raise DbcBuildError(f"dbc builder produced no output (stderr: {stderr.decode()[:300]})")

    last_line = stdout.decode().strip().splitlines()[-1]
    result = json.loads(last_line)
    if not result.get("success"):
        raise DbcBuildError(result.get("error", "unknown Meteora DBC build error"))
    return result


async def get_pool_info(base_mint: str, rpc_url: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "node",
        "pool_info.js",
        cwd=_BUILDER_DIR,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    payload = json.dumps({"baseMint": base_mint, "rpcUrl": rpc_url}).encode()
    stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout=30)

    if not stdout.strip():
        raise DbcBuildError(f"pool_info produced no output (stderr: {stderr.decode()[:300]})")

    last_line = stdout.decode().strip().splitlines()[-1]
    result = json.loads(last_line)
    if not result.get("success"):
        raise DbcBuildError(result.get("error", "unknown Meteora DBC pool_info error"))
    return result
