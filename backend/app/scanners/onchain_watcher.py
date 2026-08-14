"""On-chain launch detection.

Launch sources:

1. Anoncoin/Meteora
   Watches the configured Anoncoin creator addresses and detects new SPL
   mints from token-balance changes.

2. Pump.fun
   Watches the configured Pump.fun mint-authority address and identifies
   actual Pump.fun create/create_v2 instructions.

Pump.fun remains completely separate from the Anoncoin/Meteora path.
"""

import asyncio
import base64
import logging
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solders.signature import Signature


logger = logging.getLogger("app.scanners.onchain_watcher")

SOL_MINT = "So11111111111111111111111111111111111111112"

PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPFUN_MINT_AUTHORITY = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"

PUMPFUN_CREATE_DISCRIMINATOR = bytes([24, 30, 200, 40, 5, 28, 7, 119])
PUMPFUN_CREATE_V2_DISCRIMINATOR = bytes([214, 144, 76, 236, 95, 139, 49, 180])
PUMPFUN_CREATE_DISCRIMINATORS = (
    PUMPFUN_CREATE_DISCRIMINATOR,
    PUMPFUN_CREATE_V2_DISCRIMINATOR,
)

RPC_TIMEOUT_SECONDS = 8.0
RPC_RETRIES = 2
RPC_RETRY_DELAY_SECONDS = 0.35
POLL_SIGNATURE_LIMIT = 50


def _safe_rpc_url(rpc_url: str) -> str:
    try:
        parsed = urlsplit(rpc_url)
        if parsed.query:
            return urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, "REDACTED", "")
            )
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, "", "")
        )
    except Exception:
        return "<invalid-rpc-url>"


async def _direct_rpc_request(rpc_url: str, method: str, params: list) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": "anon-tradebot",
        "method": method,
        "params": params,
    }
    last_error = None

    for attempt in range(RPC_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=RPC_TIMEOUT_SECONDS) as http_client:
                response = await http_client.post(
                    rpc_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                response_text = response.text

                if response.status_code >= 400:
                    raise RuntimeError(
                        f"HTTP {response.status_code}: {response_text[:500]}"
                    )

                try:
                    body = response.json()
                except Exception as exc:
                    raise RuntimeError(
                        f"RPC returned non-JSON response: {response_text[:500]}"
                    ) from exc

                if "error" in body:
                    raise RuntimeError(f"RPC error: {body.get('error')}")

                if "result" not in body:
                    raise RuntimeError(f"RPC response missing result: {body}")

                return body

        except Exception as exc:
            last_error = exc
            if attempt < RPC_RETRIES:
                await asyncio.sleep(RPC_RETRY_DELAY_SECONDS * (attempt + 1))
                continue

            raise RuntimeError(
                f"{method} failed after {RPC_RETRIES + 1} attempts: {exc}"
            ) from exc

    raise RuntimeError(str(last_error))


async def _get_signatures_direct(
    rpc_url: str,
    address: str,
    limit: int,
    until: Optional[str],
) -> list[dict]:
    params = [
        address,
        {"limit": int(limit), "commitment": "confirmed"},
    ]
    if until:
        params[1]["until"] = until

    body = await _direct_rpc_request(
        rpc_url,
        "getSignaturesForAddress",
        params,
    )
    result = body.get("result")

    if not isinstance(result, list):
        raise RuntimeError("getSignaturesForAddress returned an invalid result")

    return result


async def _get_transaction_direct(
    rpc_url: str,
    signature: str,
) -> Optional[dict]:
    params = [
        signature,
        {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
            "commitment": "confirmed",
        },
    ]
    body = await _direct_rpc_request(
        rpc_url,
        "getTransaction",
        params,
    )
    return body.get("result")


class WatermarkStore:
    """Per-address signature watermark."""

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
    """Extract a newly-created SPL mint from a parsed transaction."""
    try:
        meta = tx.transaction.meta

        pre_mints = {
            str(balance.mint)
            for balance in (meta.pre_token_balances or [])
        }
        post_mints = {
            str(balance.mint)
            for balance in (meta.post_token_balances or [])
        }

        new_mints = [
            mint
            for mint in (post_mints - pre_mints)
            if mint != SOL_MINT
        ]

        if new_mints:
            return new_mints[0]

    except Exception:
        logger.debug("mint_extraction_failed", exc_info=True)

    return None


async def poll_new_mints(
    rpc_url: str,
    wallet: str,
    watermarks: WatermarkStore,
    limit: int = 20,
) -> list[dict]:
    """Poll an Anoncoin creator address."""
    async with AsyncClient(rpc_url) as client:
        pubkey = Pubkey.from_string(wallet)
        until = watermarks.get(wallet)

        try:
            response = await client.get_signatures_for_address(
                pubkey,
                limit=limit,
                until=(
                    Signature.from_string(until)
                    if until
                    else None
                ),
            )
            signature_items = response.value

        except Exception as exc:
            logger.warning(
                "get_signatures_failed: "
                f"{type(exc).__name__}: {exc} | "
                f"rpc={_safe_rpc_url(rpc_url)} | wallet={wallet}"
            )
            return []

        if not signature_items:
            return []

        newest_signature = str(signature_items[0].signature)
        watermarks.set(wallet, newest_signature)

        if not watermarks.is_initialized(wallet):
            watermarks.mark_initialized(wallet)
            return []

        discovered = []

        for signature_info in reversed(signature_items):
            if signature_info.err is not None:
                continue

            try:
                transaction_response = await client.get_transaction(
                    signature_info.signature,
                    encoding="jsonParsed",
                    max_supported_transaction_version=0,
                )
            except Exception as exc:
                logger.warning(
                    "get_transaction_failed: "
                    f"{type(exc).__name__}: {exc} | "
                    f"signature={signature_info.signature}"
                )
                continue

            if not transaction_response.value:
                continue

            mint = extract_new_mint(transaction_response.value)
            if not mint:
                continue

            discovered.append(
                {
                    "mint": mint,
                    "tx_signature": str(signature_info.signature),
                    "block_time": signature_info.block_time,
                    "watched_wallet": wallet,
                    "source": "anoncoin_onchain",
                }
            )

            await asyncio.sleep(0.05)

        return discovered


def _stringify_pubkey(value) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, dict):
        for key in ("pubkey", "publicKey", "address"):
            if key in value:
                return _stringify_pubkey(value[key])
        return None

    try:
        return str(value)
    except Exception:
        return None


def _decode_instruction_data(value) -> Optional[bytes]:
    if value is None:
        return None

    if isinstance(value, bytes):
        return value

    if isinstance(value, bytearray):
        return bytes(value)

    if isinstance(value, list):
        try:
            return bytes(value)
        except Exception:
            return None

    if isinstance(value, str):
        try:
            import base58
            return base58.b58decode(value)
        except Exception:
            pass

        try:
            return base64.b64decode(value)
        except Exception:
            return None

    return None


def _instruction_program_id(
    instruction,
    account_keys=None,
) -> Optional[str]:
    for attribute in ("program_id", "programId"):
        value = getattr(instruction, attribute, None)
        if value is not None:
            return _stringify_pubkey(value)

    if isinstance(instruction, dict):
        if instruction.get("programId"):
            return _stringify_pubkey(instruction["programId"])

        program_index = instruction.get("programIdIndex")
        if (
            program_index is not None
            and account_keys
            and 0 <= int(program_index) < len(account_keys)
        ):
            return _stringify_pubkey(account_keys[int(program_index)])

    return None


def _instruction_data(instruction) -> Optional[bytes]:
    if isinstance(instruction, dict):
        return _decode_instruction_data(instruction.get("data"))

    return _decode_instruction_data(
        getattr(instruction, "data", None)
    )


def _instruction_accounts(
    instruction,
    account_keys=None,
) -> list[str]:
    if isinstance(instruction, dict):
        raw_accounts = instruction.get("accounts")
    else:
        raw_accounts = getattr(instruction, "accounts", None)

    if not raw_accounts:
        return []

    result = []

    for account in raw_accounts:
        if not isinstance(account, int):
            value = _stringify_pubkey(account)
            if value:
                result.append(value)
            continue

        if (
            account_keys
            and 0 <= int(account) < len(account_keys)
        ):
            value = _stringify_pubkey(account_keys[int(account)])
            if value:
                result.append(value)

    return result


def _is_pumpfun_create_instruction(
    instruction,
    account_keys=None,
) -> bool:
    program_id = _instruction_program_id(
        instruction,
        account_keys,
    )

    if program_id != PUMPFUN_PROGRAM_ID:
        return False

    data = _instruction_data(instruction)
    if not data:
        return False

    return any(
        data.startswith(discriminator)
        for discriminator in PUMPFUN_CREATE_DISCRIMINATORS
    )


def _pumpfun_create_version(instruction) -> Optional[str]:
    data = _instruction_data(instruction)
    if not data:
        return None

    if data.startswith(PUMPFUN_CREATE_V2_DISCRIMINATOR):
        return "create_v2"

    if data.startswith(PUMPFUN_CREATE_DISCRIMINATOR):
        return "create"

    return None


def _extract_pumpfun_from_native_tx(tx) -> Optional[dict]:
    try:
        message = tx.transaction.transaction.message
    except Exception:
        try:
            message = tx.transaction.message
        except Exception:
            return None

    instructions = getattr(message, "instructions", None)
    if not instructions:
        return None

    for instruction in instructions:
        if not _is_pumpfun_create_instruction(instruction):
            continue

        accounts = _instruction_accounts(instruction)
        if not accounts:
            continue

        mint = accounts[0]
        if not mint or mint == SOL_MINT:
            continue

        creator = accounts[7] if len(accounts) > 7 else None

        return {
            "mint": mint,
            "creator": creator,
            "source": "pumpfun",
            "instruction": _pumpfun_create_version(instruction),
        }

    return None


def _raw_message_account_keys(transaction: dict) -> list:
    return (
        transaction
        .get("transaction", {})
        .get("message", {})
        .get("accountKeys", [])
    )


def _raw_message_instructions(transaction: dict) -> list:
    return (
        transaction
        .get("transaction", {})
        .get("message", {})
        .get("instructions", [])
    )


def _extract_pumpfun_from_raw_tx(
    transaction: dict,
) -> Optional[dict]:
    if not isinstance(transaction, dict):
        return None

    account_keys = _raw_message_account_keys(transaction)
    instructions = _raw_message_instructions(transaction)

    if not instructions:
        return None

    for instruction in instructions:
        if not _is_pumpfun_create_instruction(
            instruction,
            account_keys,
        ):
            continue

        accounts = _instruction_accounts(
            instruction,
            account_keys,
        )
        if not accounts:
            continue

        mint = accounts[0]
        if not mint or mint == SOL_MINT:
            continue

        creator = accounts[7] if len(accounts) > 7 else None

        return {
            "mint": mint,
            "creator": creator,
            "source": "pumpfun",
            "instruction": _pumpfun_create_version(instruction),
        }

    return None


def extract_pumpfun_create(tx) -> Optional[dict]:
    """Extract a Pump.fun create/create_v2 launch."""
    if isinstance(tx, dict):
        return _extract_pumpfun_from_raw_tx(tx)

    return _extract_pumpfun_from_native_tx(tx)


async def poll_new_pumpfun_mints(
    rpc_url: str,
    mint_authority: str,
    watermarks: WatermarkStore,
    limit: int = POLL_SIGNATURE_LIMIT,
) -> list[dict]:
    """Poll Pump.fun for newly-created tokens.

    Detection requires:
        watched mint-authority address
        +
        official Pump.fun program
        +
        create OR create_v2 discriminator
    """

    watermark_key = f"pumpfun:{mint_authority}"
    until = watermarks.get(watermark_key)

    logger.debug(
        "pumpfun_poll_started",
        extra={
            "mint_authority": mint_authority,
            "has_watermark": until is not None,
        },
    )

    async with AsyncClient(rpc_url) as client:
        normalized = []

        try:
            authority_pubkey = Pubkey.from_string(mint_authority)

            response = await client.get_signatures_for_address(
                authority_pubkey,
                limit=limit,
                until=(
                    Signature.from_string(until)
                    if until
                    else None
                ),
            )

            for item in response.value:
                normalized.append(
                    {
                        "signature": str(item.signature),
                        "err": item.err,
                        "block_time": item.block_time,
                    }
                )

            logger.debug(
                "pumpfun_signatures_fetched",
                extra={
                    "mint_authority": mint_authority,
                    "count": len(normalized),
                },
            )

        except Exception as exc:
            logger.warning(
                "pumpfun_get_signatures_failed: "
                f"{type(exc).__name__}: {exc} | "
                f"rpc={_safe_rpc_url(rpc_url)} | "
                f"mint_authority={mint_authority}"
            )

            try:
                normalized = await _get_signatures_direct(
                    rpc_url,
                    mint_authority,
                    limit,
                    until,
                )

                logger.info(
                    "pumpfun_get_signatures_direct_rpc_success",
                    extra={
                        "mint_authority": mint_authority,
                        "count": len(normalized),
                    },
                )

            except Exception as direct_exc:
                logger.error(
                    "pumpfun_get_signatures_failed: "
                    "direct rpc fallback: "
                    f"{type(direct_exc).__name__}: {direct_exc} | "
                    f"rpc={_safe_rpc_url(rpc_url)} | "
                    f"mint_authority={mint_authority}"
                )
                return []

        if not normalized:
            logger.debug(
                "pumpfun_no_signatures",
                extra={"mint_authority": mint_authority},
            )
            return []

        newest_signature = normalized[0].get("signature")
        if not newest_signature:
            logger.warning(
                "pumpfun_signature_response_missing_signature"
            )
            return []

        watermarks.set(
            watermark_key,
            newest_signature,
        )

        if not watermarks.is_initialized(
            watermark_key
        ):
            watermarks.mark_initialized(
                watermark_key
            )

            logger.info(
                "pumpfun_watermark_initialized",
                extra={
                    "mint_authority": mint_authority,
                    "signature": newest_signature,
                    "signature_count": len(normalized),
                },
            )

            # Historical launches are intentionally ignored.
            return []

        discovered = []

        for signature_info in reversed(normalized):
            if signature_info.get("err") is not None:
                continue

            signature = signature_info.get("signature")
            if not signature:
                continue

            tx_value = None

            try:
                signature_object = Signature.from_string(signature)

                transaction_response = await client.get_transaction(
                    signature_object,
                    encoding="jsonParsed",
                    max_supported_transaction_version=0,
                )

                tx_value = transaction_response.value

            except Exception as exc:
                logger.warning(
                    "pumpfun_get_transaction_failed: "
                    "solana client: "
                    f"{type(exc).__name__}: {exc} | "
                    f"signature={signature}"
                )

            if tx_value is None:
                try:
                    tx_value = await _get_transaction_direct(
                        rpc_url,
                        signature,
                    )

                    logger.debug(
                        "pumpfun_get_transaction_direct_rpc_success",
                        extra={"signature": signature},
                    )

                except Exception as direct_exc:
                    logger.warning(
                        "pumpfun_get_transaction_failed: "
                        "direct rpc fallback: "
                        f"{type(direct_exc).__name__}: {direct_exc} | "
                        f"signature={signature}"
                    )
                    continue

            if not tx_value:
                logger.debug(
                    "pumpfun_transaction_empty",
                    extra={"signature": signature},
                )
                continue

            try:
                launch = extract_pumpfun_create(tx_value)
            except Exception as exc:
                logger.warning(
                    "pumpfun_create_parse_failed: "
                    f"{type(exc).__name__}: {exc} | "
                    f"signature={signature}",
                    exc_info=True,
                )
                continue

            if not launch:
                logger.debug(
                    "pumpfun_transaction_not_create",
                    extra={"signature": signature},
                )
                continue

            launch["tx_signature"] = signature
            launch["block_time"] = signature_info.get("block_time")
            launch["watched_wallet"] = mint_authority

            discovered.append(launch)

            logger.debug(
                "pumpfun_launch_detected",
                extra={
                    "mint": launch.get("mint"),
                    "creator": launch.get("creator"),
                    "instruction": launch.get("instruction"),
                    "tx_signature": signature,
                },
            )

            await asyncio.sleep(0.05)

        if discovered:
            logger.info(
                "pumpfun_discovery_batch_complete",
                extra={
                    "count": len(discovered),
                    "mint_authority": mint_authority,
                },
            )

        return discovered
