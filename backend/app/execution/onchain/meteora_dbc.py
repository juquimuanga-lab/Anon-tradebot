"""Meteora DBC execution and live-price helpers.

Responsibilities:

- Build unsigned Meteora DBC transactions through the official SDK.
- Keep private keys completely outside Node.js.
- Keep transaction signing inside Python.
- Maintain a persistent read-only Node worker for fast Meteora price reads.
- Reuse the Meteora SDK and Solana connection instead of starting Node for
  every price check.
- Use processed commitment for low-latency market-price observations.
- Keep transaction construction and transaction confirmation separate from
  price monitoring.

Architecture:

TRANSACTIONS

    Python
       ↓
    build_tx.js
       ↓
    unsigned transaction
       ↓
    Python signs
       ↓
    solana_rpc.py broadcasts/confirms


LIVE PRICE

    Python
       ↓
    persistent pool_price_worker.js
       ↓
    Meteora SDK
       ↓
    Solana RPC
       ↓
    price
       ↓
    Python

The price worker is read-only. It never receives a private key, signs a
transaction, or submits a transaction.
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional


logger = logging.getLogger(
    "app.execution.onchain.meteora_dbc"
)


_BUILDER_DIR = os.path.join(
    os.path.dirname(__file__),
    "dbc_builder",
)


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------

# Transaction construction can legitimately require several RPC calls and
# SDK operations.
TRANSACTION_BUILDER_TIMEOUT_SECONDS = 30.0

# A live price observation should never hold the position monitor for a long
# time.
POOL_PRICE_REQUEST_TIMEOUT_SECONDS = 2.5

# If the persistent worker stops responding, give it a short amount of time
# to terminate before restarting it.
WORKER_SHUTDOWN_TIMEOUT_SECONDS = 1.0


class DbcBuildError(Exception):
    """Raised when Meteora DBC construction/read fails."""


# ---------------------------------------------------------------------------
# Generic subprocess helpers
# ---------------------------------------------------------------------------

async def _terminate_process(
    proc: asyncio.subprocess.Process,
) -> None:
    """Safely terminate a child process."""

    if proc.returncode is not None:
        return

    try:
        proc.kill()

    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(
            proc.wait(),
            timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS,
        )

    except asyncio.TimeoutError:

        logger.warning(
            "meteora_child_process_did_not_exit_after_kill"
        )


def _parse_builder_output(
    stdout: bytes,
    stderr: bytes,
    operation: str,
) -> dict:
    """Parse the final JSON line returned by a one-shot Node builder."""

    if not stdout.strip():

        stderr_text = (
            stderr.decode(
                errors="replace"
            )[:500]
        )

        raise DbcBuildError(
            f"{operation} produced no output "
            f"(stderr: {stderr_text})"
        )

    try:

        output = stdout.decode(
            errors="replace"
        ).strip()

        last_line = (
            output
            .splitlines()[-1]
        )

        result = json.loads(
            last_line
        )

    except Exception as exc:

        stderr_text = (
            stderr.decode(
                errors="replace"
            )[:500]
        )

        raise DbcBuildError(
            f"{operation} returned invalid JSON: "
            f"{exc}; stderr: {stderr_text}"
        ) from exc

    if not isinstance(
        result,
        dict,
    ):

        raise DbcBuildError(
            f"{operation} returned an invalid response"
        )

    if not result.get(
        "success"
    ):

        raise DbcBuildError(
            result.get(
                "error",
                f"unknown Meteora DBC {operation} error",
            )
        )

    return result


# ---------------------------------------------------------------------------
# Persistent price worker
# ---------------------------------------------------------------------------

class _PoolPriceWorker:
    """Long-lived Node worker for Meteora pool price reads.

    The worker is started lazily on the first price request.

    One Node process handles all subsequent price requests.

    Requests are correlated with requestId so multiple tokens can be
    requested concurrently without mixing responses.
    """

    def __init__(self):
        self._process: Optional[
            asyncio.subprocess.Process
        ] = None

        self._reader_task: Optional[
            asyncio.Task
        ] = None

        self._stderr_task: Optional[
            asyncio.Task
        ] = None

        self._start_lock = asyncio.Lock()

        self._write_lock = asyncio.Lock()

        self._pending: dict[
            str,
            asyncio.Future,
        ] = {}

        self._request_counter = 0


    # -----------------------------------------------------------------------
    # Request IDs
    # -----------------------------------------------------------------------

    def _next_request_id(self) -> str:
        self._request_counter += 1

        return (
            f"price-{os.getpid()}-"
            f"{self._request_counter}-"
            f"{time.monotonic_ns()}"
        )


    # -----------------------------------------------------------------------
    # Worker startup
    # -----------------------------------------------------------------------

    async def _ensure_started(self) -> None:
        """Start the persistent Node worker if necessary."""

        process = self._process

        if (
            process is not None
            and process.returncode is None
        ):
            return

        async with self._start_lock:

            process = self._process

            if (
                process is not None
                and process.returncode is None
            ):
                return

            await self._cleanup_dead_worker()

            logger.info(
                "starting_persistent_meteora_price_worker"
            )

            try:

                process = (
                    await asyncio.create_subprocess_exec(
                        "node",
                        "pool_price_worker.js",
                        cwd=_BUILDER_DIR,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                )

            except Exception as exc:

                raise DbcBuildError(
                    "failed to start persistent "
                    f"Meteora price worker: {exc}"
                ) from exc

            self._process = process

            self._reader_task = (
                asyncio.create_task(
                    self._read_stdout(process)
                )
            )

            self._stderr_task = (
                asyncio.create_task(
                    self._read_stderr(process)
                )
            )

            logger.info(
                "persistent_meteora_price_worker_started",
                extra={
                    "pid": process.pid,
                },
            )


    # -----------------------------------------------------------------------
    # Worker stdout
    # -----------------------------------------------------------------------

    async def _read_stdout(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        """Read worker responses and resolve matching futures."""

        stdout = process.stdout

        if stdout is None:
            return

        try:

            while True:

                line = await stdout.readline()

                if not line:
                    break

                line = line.strip()

                if not line:
                    continue

                try:

                    payload = json.loads(
                        line.decode(
                            errors="replace"
                        )
                    )

                except Exception as exc:

                    logger.warning(
                        "meteora_price_worker_invalid_json",
                        extra={
                            "error": str(exc),
                        },
                    )

                    continue

                request_id = payload.get(
                    "requestId"
                )

                if not request_id:
                    logger.warning(
                        "meteora_price_worker_response_missing_request_id"
                    )
                    continue

                future = self._pending.pop(
                    request_id,
                    None,
                )

                if future is None:
                    logger.debug(
                        "meteora_price_worker_response_without_pending_request",
                        extra={
                            "request_id": request_id,
                        },
                    )
                    continue

                if future.done():
                    continue

                if not payload.get(
                    "success"
                ):

                    future.set_exception(
                        DbcBuildError(
                            payload.get(
                                "error",
                                "unknown price worker error",
                            )
                        )
                    )

                    continue

                future.set_result(
                    payload
                )

        except asyncio.CancelledError:

            raise

        except Exception as exc:

            logger.exception(
                "meteora_price_worker_stdout_reader_failed",
                extra={
                    "error": str(exc),
                },
            )

        finally:

            await self._worker_died(
                process,
                "stdout closed",
            )


    # -----------------------------------------------------------------------
    # Worker stderr
    # -----------------------------------------------------------------------

    async def _read_stderr(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        """Drain worker stderr so the child cannot block on its pipe."""

        stderr = process.stderr

        if stderr is None:
            return

        try:

            while True:

                line = await stderr.readline()

                if not line:
                    break

                text = line.decode(
                    errors="replace"
                ).strip()

                if text:

                    logger.warning(
                        "meteora_price_worker_stderr",
                        extra={
                            "pid": process.pid,
                            "message": text[:1000],
                        },
                    )

        except asyncio.CancelledError:

            raise

        except Exception as exc:

            logger.debug(
                "meteora_price_worker_stderr_reader_failed",
                extra={
                    "error": str(exc),
                },
            )


    # -----------------------------------------------------------------------
    # Worker death
    # -----------------------------------------------------------------------

    async def _worker_died(
        self,
        process: asyncio.subprocess.Process,
        reason: str,
    ) -> None:
        """Fail outstanding requests if the worker disappears."""

        if (
            self._process is not process
        ):
            return

        logger.warning(
            "meteora_price_worker_stopped",
            extra={
                "pid": process.pid,
                "reason": reason,
            },
        )

        for request_id, future in list(
            self._pending.items()
        ):

            if not future.done():

                future.set_exception(
                    DbcBuildError(
                        "persistent Meteora price "
                        "worker stopped"
                    )
                )

            self._pending.pop(
                request_id,
                None,
            )

        self._process = None


    # -----------------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------------

    async def _cleanup_dead_worker(
        self,
    ) -> None:
        """Clean up a dead worker and its reader tasks."""

        process = self._process

        if process is not None:

            if process.returncode is None:

                await _terminate_process(
                    process
                )

        self._process = None

        current_task = (
            asyncio.current_task()
        )

        for task_name in (
            "_reader_task",
            "_stderr_task",
        ):

            task = getattr(
                self,
                task_name,
            )

            if (
                task is not None
                and task is not current_task
                and not task.done()
            ):

                task.cancel()

                try:
                    await task

                except (
                    asyncio.CancelledError
                ):
                    pass

                except Exception:
                    pass

            setattr(
                self,
                task_name,
                None,
            )


    # -----------------------------------------------------------------------
    # Price request
    # -----------------------------------------------------------------------

    async def request_price(
        self,
        base_mint: str,
        rpc_url: str,
        commitment: str = "processed",
    ) -> dict:
        """Request pool state from the persistent worker."""

        await self._ensure_started()

        process = self._process

        if (
            process is None
            or process.returncode is not None
            or process.stdin is None
        ):

            raise DbcBuildError(
                "Meteora price worker is not running"
            )

        request_id = (
            self._next_request_id()
        )

        loop = asyncio.get_running_loop()

        future = loop.create_future()

        self._pending[
            request_id
        ] = future

        payload = {
            "requestId": request_id,
            "baseMint": base_mint,
            "rpcUrl": rpc_url,
            "commitment": commitment,
        }

        try:

            # Multiple coroutines can request prices concurrently, so protect
            # writes to the worker's stdin.
            async with self._write_lock:

                # The worker may have died while we were waiting for the
                # write lock.
                if (
                    self._process is not process
                    or process.returncode is not None
                    or process.stdin is None
                ):

                    raise DbcBuildError(
                        "Meteora price worker stopped "
                        "before request was sent"
                    )

                process.stdin.write(
                    (
                        json.dumps(
                            payload,
                            separators=(
                                ",",
                                ":",
                            ),
                        )
                        + "\n"
                    ).encode()
                )

                await process.stdin.drain()

            try:

                result = await asyncio.wait_for(
                    future,
                    timeout=(
                        POOL_PRICE_REQUEST_TIMEOUT_SECONDS
                    ),
                )

            except asyncio.TimeoutError:

                self._pending.pop(
                    request_id,
                    None,
                )

                logger.warning(
                    "meteora_price_worker_request_timeout",
                    extra={
                        "base_mint": base_mint,
                        "request_id": request_id,
                        "timeout_seconds": (
                            POOL_PRICE_REQUEST_TIMEOUT_SECONDS
                        ),
                    },
                )

                raise DbcBuildError(
                    "persistent Meteora price worker "
                    "did not respond within "
                    f"{POOL_PRICE_REQUEST_TIMEOUT_SECONDS}s"
                )

            return result

        except asyncio.CancelledError:

            self._pending.pop(
                request_id,
                None,
            )

            raise

        except Exception:

            self._pending.pop(
                request_id,
                None,
            )

            raise


    # -----------------------------------------------------------------------
    # Shutdown
    # -----------------------------------------------------------------------

    async def close(self) -> None:
        """Stop the worker cleanly."""

        process = self._process

        if process is None:
            return

        logger.info(
            "stopping_persistent_meteora_price_worker",
            extra={
                "pid": process.pid,
            },
        )

        for request_id, future in list(
            self._pending.items()
        ):

            if not future.done():

                future.set_exception(
                    DbcBuildError(
                        "Meteora price worker shutting down"
                    )
                )

            self._pending.pop(
                request_id,
                None,
            )

        await _terminate_process(
            process
        )

        self._process = None

        current_task = (
            asyncio.current_task()
        )

        for task in (
            self._reader_task,
            self._stderr_task,
        ):

            if (
                task is not None
                and task is not current_task
                and not task.done()
            ):

                task.cancel()

        self._reader_task = None
        self._stderr_task = None


# One persistent worker for the backend process.
_POOL_PRICE_WORKER = (
    _PoolPriceWorker()
)


# ---------------------------------------------------------------------------
# Transaction builder
# ---------------------------------------------------------------------------

async def build_unsigned_swap(
    action: str,
    base_mint: str,
    owner_pubkey: str,
    amount_lamports: int,
    slippage_bps: int,
    rpc_url: str,
) -> dict:
    """Build an unsigned Meteora DBC transaction.

    The private key never crosses into Node.js.

    Node receives only:

        action
        token mint
        owner public key
        amount
        slippage
        RPC URL

    The resulting unsigned transaction is signed later by Python.
    """

    payload = {
        "action": action,
        "baseMint": base_mint,
        "ownerPubkey": owner_pubkey,
        "amountLamports": str(
            amount_lamports
        ),
        "slippageBps": int(
            slippage_bps
        ),
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

    try:

        stdout, stderr = (
            await asyncio.wait_for(
                proc.communicate(
                    json.dumps(
                        payload
                    ).encode()
                ),
                timeout=(
                    TRANSACTION_BUILDER_TIMEOUT_SECONDS
                ),
            )
        )

    except asyncio.TimeoutError:

        logger.error(
            "meteora_transaction_builder_timeout",
            extra={
                "action": action,
                "base_mint": base_mint,
                "timeout_seconds": (
                    TRANSACTION_BUILDER_TIMEOUT_SECONDS
                ),
            },
        )

        await _terminate_process(
            proc
        )

        raise DbcBuildError(
            "dbc builder timed out after "
            f"{TRANSACTION_BUILDER_TIMEOUT_SECONDS}s "
            "(node process killed)"
        )

    except asyncio.CancelledError:

        await _terminate_process(
            proc
        )

        raise

    result = _parse_builder_output(
        stdout,
        stderr,
        "dbc builder",
    )

    logger.debug(
        "meteora_unsigned_transaction_built",
        extra={
            "action": action,
            "base_mint": base_mint,
        },
    )

    return result


# ---------------------------------------------------------------------------
# Fast persistent pool-state reader
# ---------------------------------------------------------------------------

async def get_pool_info(
    base_mint: str,
    rpc_url: str,
    commitment: str = "processed",
) -> dict:
    """Read Meteora DBC pool state using the persistent price worker.

    Unlike the old implementation, this does NOT start a new Node process
    for every price check.

    The worker:

        - loads the Meteora SDK once
        - creates/reuses Solana connections
        - handles repeated price requests
        - correlates concurrent requests by requestId

    The default `processed` commitment is intentional here because this is
    a low-latency market-price observation.

    This function does NOT determine whether a transaction succeeded.
    Transaction confirmation remains handled by solana_rpc.py.
    """

    try:

        result = await (
            _POOL_PRICE_WORKER.request_price(
                base_mint=base_mint,
                rpc_url=rpc_url,
                commitment=commitment,
            )
        )

    except asyncio.TimeoutError:

        raise DbcBuildError(
            "persistent Meteora price worker "
            "timed out"
        )

    except DbcBuildError:

        raise

    except Exception as exc:

        logger.warning(
            "meteora_persistent_price_request_failed",
            extra={
                "base_mint": base_mint,
                "error": str(exc),
            },
        )

        raise DbcBuildError(
            f"Meteora persistent price request failed: "
            f"{exc}"
        ) from exc

    # Validate the response before returning it to the price source.
    price_sol = result.get(
        "price_sol_per_token"
    )

    if price_sol is None:

        raise DbcBuildError(
            "Meteora price worker returned no "
            "price_sol_per_token"
        )

    try:

        price_sol = float(
            price_sol
        )

    except Exception as exc:

        raise DbcBuildError(
            "Meteora price worker returned an "
            "invalid price_sol_per_token"
        ) from exc

    if price_sol <= 0:

        raise DbcBuildError(
            "Meteora price worker returned a "
            "non-positive token price"
        )

    result[
        "price_sol_per_token"
    ] = price_sol

    logger.debug(
        "meteora_persistent_pool_info_read",
        extra={
            "base_mint": base_mint,
            "commitment": commitment,
            "pool_address": result.get(
                "pool_address"
            ),
            "price_sol_per_token": price_sol,
        },
    )

    return result


# ---------------------------------------------------------------------------
# Optional application shutdown helper
# ---------------------------------------------------------------------------

async def shutdown_price_worker() -> None:
    """Stop the persistent Meteora price worker.

    This can be called by the application's shutdown handler.
    """

    await _POOL_PRICE_WORKER.close()
