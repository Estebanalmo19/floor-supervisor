"""Structured (JSON lines) logging for the application.

Usage:
    logger = logging.getLogger(__name__)
    logger.info("scan_processed", extra={"outcome": "RECORDED", "hibob_id": "99999"})

Rules: never log secrets, passwords, bearer tokens or full card values. Use ``mask_card_value``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

APP_LOGGER_NAME = "src"

# Attributes present on every LogRecord; anything else came from ``extra=``.
_STANDARD_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None)).keys()
) | {"message", "asctime", "taskName"}


def mask_card_value(raw: str | None) -> str:
    """Mask a card value, revealing at most its last 4 characters.

    Short values (< 8 chars) are fully masked so that the visible part never
    amounts to half or more of the credential.
    """
    if not raw:
        return ""
    if len(raw) < 8:
        return "****"
    return "****" + raw[-4:]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure the application logger tree (idempotent; safe across Streamlit reruns)."""
    logger = logging.getLogger(APP_LOGGER_NAME)
    logger.setLevel(level)
    if not any(getattr(h, "_floor_supervisor", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler._floor_supervisor = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.propagate = False
    # httpx logs "HTTP Request: POST <full url>" at INFO; the Power Automate URL is signed.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logger
