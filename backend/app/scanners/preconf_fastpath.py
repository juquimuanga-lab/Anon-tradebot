"""Helius Preconfirmations fast path for Pump.fun launches and smart-money buys.

The stream is intentionally additive: existing Helius WSS/RPC recovery remains the
source of truth and fallback. Preconfirmations only provides an earlier signal.
"""

import asyncio
import base64
import json
import logging
import os
import time
from collections import deque
from typing import Optional

import websockets
from solders.transaction import VersionedTransaction

from app.config.settings import settings

logger = logging.getLogger("app.scanners.preconf_fastpath")

PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PRECONF_WS_ENDPOINT = "wss://beta.helius-rpc.com"
PRECONF_HEADER_BYTES = 18

CREATE_DISCRIMINATORS = {
    bytes([24, 30, 200, 40, 5, 28, 7, 119]): "create",
    bytes([214, 144, 76, 236, 95, 139, 49, 180]): "create_v2",
}
BUY_DISCRIMINATORS = {
    bytes([102, 6, 61, 18, 1, 218, 235, 234]): "buy",
    bytes([56, 252, 116, 8, 158, 223, 205, 95]): "buy_exact_sol_in",
    bytes([184, 23, 238, 97, 103, 197, 211, 61]): "buy_v2",
    bytes([194, 171, 28, 70, 104, 77, 91, 47]): "buy_exact_quote_in_v2",
}

_launch_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
_smart_money_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
_task: Optional[asyncio.Task] = None
_stop = asyncio.Event()
_seen: deque[str] = deque(maxlen=10000)
_seen_set: set[str] = set()
_watched_wallets: set[str] = set()
_started = False


def _remember(signature: str) -> bool:
    if signature in _seen_set:
        return False
    if len(_seen) >= _seen.maxlen:
        old = _seen.popleft()
        _seen_set.discard(old)
    _seen.append(signature)
    _seen_set.add(signature)
    return True


def _preconf_ws_url() -> Optional[str]:
    api_key = getattr(settings, "helius_api_key", None) or os.getenv("HELIUS_API_KEY")
    if not api_key:
        return None
    return f"{PRECONF_WS_ENDPOINT}/?api-key={api_key}"


def _raw_data(ix) -> bytes:
    data = getattr(ix, "data", b"")
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        try:
            import base58
            return base58.b58decode(data)
        except Exception:
            try:
                return base64.b64decode(data)
            except Exception:
                return b""
    try:
        return bytes(data)
    except Exception:
        return b""


def _tx_instructions(raw_tx: bytes):
    try:
        tx = VersionedTransaction.from_bytes(raw_tx)
        message = tx.message
        keys = list(getattr(message, "account_keys", []) or [])
        instructions = list(getattr(message, "instructions", []) or [])
        return tx, message, keys, instructions
    except Exception:
        return None, None, [], []


def _pump_instruction(message, keys, ix):
    try:
        program_index = int(getattr(ix, "program_id_index"))
        if program_index < 0 or program_index >= len(keys):
            return None, b"", []
        program_id = str(keys[program_index])
        if program_id != PUMPFUN_PROGRAM_ID:
            return None, b"", []
        data = _raw_data(ix)
        accounts = [int(x) for x in (getattr(ix, "accounts", []) or [])]
        return program_id, data, accounts
    except Exception:
        return None, b"", []


def _signers(message, keys) -> set[str]:
    try:
        count = int(message.header.num_required_signatures)
        return {str(k) for k in keys[:count]}
    except Exception:
        return set()


def _decode_preconf_frame(frame: bytes):
    if len(frame) <= PRECONF_HEADER_BYTES:
        return None
    # Helius documents a fixed 18-byte header followed by the complete signed
    # transaction. The header fields are intentionally ignored here; the
    # transaction bytes are the hot-path payload we need.
    return frame[PRECONF_HEADER_BYTES:]


def _queue_nowait(queue: asyncio.Queue, item: dict) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass


def _classify_transaction(raw_tx: bytes, signature: str):
    tx, message, keys, instructions = _tx_instructions(raw_tx)
    if message is None:
        return [], []

    launches = []
    buy_candidates = []
    signers = _signers(message, keys)

    for ix in instructions:
        program_id, data, account_indices = _pump_instruction(message, keys, ix)
        if program_id != PUMPFUN_PROGRAM_ID or len(data) < 8:
            continue
        discriminator = data[:8]

        if discriminator in CREATE_DISCRIMINATORS and account_indices:
            try:
                mint = str(keys[account_indices[0]])
            except Exception:
                continue
            if mint and mint != PUMPFUN_PROGRAM_ID:
                launches.append({
                    "mint": mint,
                    "creator": "",
                    "source": "pumpfun",
                    "instruction": CREATE_DISCRIMINATORS[discriminator],
                    "tx_signature": signature,
                    "block_time": None,
                    "watched_program": PUMPFUN_PROGRAM_ID,
                    "discovery": "helius_preconf",
                    "rpc_transport": "preconf",
                    "preconf": True,
                    "preconf_detected_at": time.time(),
                })

        if discriminator in BUY_DISCRIMINATORS:
            buyer = next((wallet for wallet in signers if wallet in _watched_wallets), None)
            if buyer:
                mint = None
                # Legacy Pump.fun buy has mint at instruction account index 2.
                # V2 layouts can differ, so leave mint unresolved and let the
                # resolver fetch full transaction metadata without delaying the
                # preconfirmation transport.
                if len(account_indices) > 2:
                    try:
                        mint = str(keys[account_indices[2]])
                    except Exception:
                        mint = None
                buy_candidates.append({
                    "wallet": buyer,
                    "mint": mint,
                    "tx_signature": signature,
                    "transaction_type": "buy",
                    "instruction": BUY_DISCRIMINATORS[discriminator],
                    "detected_at": time.time(),
                    "discovery": "helius_preconf",
                    "source": "pumpfun",
                    "preconf": True,
                })

    return launches, buy_candidates


async def _resolve_buy(candidate: dict) -> None:
    if candidate.get("mint"):
        _queue_nowait(_smart_money_queue, candidate)
        return

    # Preconf is the trigger. Full RPC metadata is used only to resolve
    # router/v2 account layouts and token direction; this does not replace the
    # preconf transport. Retry briefly because the transaction may not yet be
    # visible through RPC when the preconf arrives.
    try:
        from app.scanners import onchain_watcher
        for delay in (0.0, 0.025, 0.05, 0.10, 0.20, 0.40):
            if delay:
                await asyncio.sleep(delay)
            tx = await onchain_watcher._get_confirmed_transaction_with_fallback(
                settings.solana_rpc_url,
                candidate["tx_signature"],
                purpose="preconf_smart_money",
            )
            if tx is None:
                continue
            bought = onchain_watcher._extract_wallet_bought_mint(
                tx,
                candidate["wallet"],
            )
            if bought:
                candidate["mint"] = bought.get("mint")
                candidate["token_amount_raw"] = bought.get("token_amount_raw", 0)
                candidate["sol_spent"] = onchain_watcher._estimate_wallet_sol_spent(
                    tx,
                    candidate["wallet"],
                )
                _queue_nowait(_smart_money_queue, candidate)
                return
    except Exception:
        logger.debug("preconf_smart_money_resolution_failed", exc_info=True)

    logger.debug(
        "preconf_smart_money_unresolved",
        extra={"signature": candidate.get("tx_signature")},
    )


async def _worker() -> None:
    backoff = 1.0
    while not _stop.is_set():
        url = _preconf_ws_url()
        if not url:
            logger.warning("helius_preconf_disabled reason=missing_HELIUS_API_KEY")
            return
        try:
            async with websockets.connect(
                url,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=4 * 1024 * 1024,
            ) as ws:
                request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "preconfSubscribe",
                    "params": [{
                        "failed": False,
                        "accountRequired": [PUMPFUN_PROGRAM_ID],
                    }],
                }
                await ws.send(json.dumps(request))
                ack = await asyncio.wait_for(ws.recv(), timeout=10)
                if isinstance(ack, bytes):
                    raise RuntimeError("preconfSubscribe returned binary ack unexpectedly")
                response = json.loads(ack)
                if response.get("error") or "result" not in response:
                    raise RuntimeError(f"preconfSubscribe failed: {response}")
                logger.info(
                    "helius_preconf_connected",
                    extra={"subscription_id": response.get("result")},
                )
                backoff = 1.0

                while not _stop.is_set():
                    frame = await ws.recv()
                    if isinstance(frame, str):
                        continue
                    raw_tx = _decode_preconf_frame(frame)
                    if not raw_tx:
                        continue

                    try:
                        tx = VersionedTransaction.from_bytes(raw_tx)
                        signature = str(tx.signatures[0])
                    except Exception:
                        continue
                    if not _remember(signature):
                        continue

                    launches, buys = _classify_transaction(raw_tx, signature)
                    for launch in launches:
                        _queue_nowait(_launch_queue, launch)
                    for candidate in buys:
                        asyncio.create_task(_resolve_buy(candidate))

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "helius_preconf_disconnected",
                extra={
                    "error": f"{type(exc).__name__}: {exc}",
                    "retry_seconds": backoff,
                },
            )
            try:
                await asyncio.wait_for(_stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2.0, 15.0)


def ensure_started(wallets: Optional[list[str]] = None) -> None:
    global _task, _started
    if wallets:
        _watched_wallets.update(str(w).strip() for w in wallets if str(w).strip())
    if _started:
        return
    _started = True
    try:
        _task = asyncio.create_task(_worker(), name="helius-preconf-pumpfun")
    except RuntimeError:
        _started = False
        logger.debug("helius_preconf_start_deferred", exc_info=True)


def drain_launches() -> list[dict]:
    items = []
    while True:
        try:
            items.append(_launch_queue.get_nowait())
        except asyncio.QueueEmpty:
            return items


def drain_smart_money(wallets: list[str]) -> list[dict]:
    _watched_wallets.update(str(w).strip() for w in wallets if str(w).strip())
    items = []
    while True:
        try:
            item = _smart_money_queue.get_nowait()
        except asyncio.QueueEmpty:
            return items
        if item.get("wallet") in _watched_wallets:
            items.append(item)


def reset_for_tests() -> None:
    global _task, _started
    _stop.set()
    _task = None
    _started = False
