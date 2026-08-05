"""Structured logging with automatic secret redaction."""
import logging
import sys

from app.config.settings import settings
from app.security.redact import redact_text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_text(str(record.msg))
            if record.args:
                record.args = tuple(
                    redact_text(str(a)) if isinstance(a, str) else a for a in record.args
                )
        except Exception:
            pass
        return True


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
        )
    )
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    # Quiet noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
