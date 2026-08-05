"""Retry-with-backoff helpers for transient HTTP errors."""
import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


def with_backoff(max_attempts: int = 3):
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception(_is_transient),
    )
