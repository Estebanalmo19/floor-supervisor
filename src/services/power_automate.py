"""Mirror of newly recorded scan events to a Power Automate HTTP trigger (-> Excel).

PostgreSQL is the system of record. Delivery here is best effort: it runs after the
scan_event COMMIT and never raises, so a Power Automate failure can never fail a scan.

The trigger URL carries a signed ``sig`` query parameter and is a secret: it is never
logged, never put in exception messages and never included in reprs.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, tzinfo
from typing import Any

import httpx

from src.models.scan import NewScanEvent

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SOURCE_SYSTEM = "floor_supervisor"
EVENT_TYPE = "scan_recorded"
SUCCESS_STATUSES = frozenset({200, 201, 202})
# One short retry only for failures that are typically instantaneous and transient.
RETRYABLE_STATUSES = frozenset({502, 503, 504})
RETRY_DELAY_SECONDS = 0.5


def build_scan_payload(event: NewScanEvent, event_id: int, display_timezone: tzinfo) -> dict[str, Any]:
    """The exact JSON object sent to Power Automate. Contains no card or secret data."""
    employee = event.employee
    scanned_utc = event.scanned_at.astimezone(UTC)
    scanned_local = event.scanned_at.astimezone(display_timezone)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_system": SOURCE_SYSTEM,
        "event_type": EVENT_TYPE,
        "event_id": str(event_id),
        "hibob_id": event.hibob_id,
        "hibob_lookup_status": event.hibob_lookup_status.value,
        "employee_name": event.display_name,
        "job_title": employee.job_title if employee else None,
        "department": employee.department if employee else None,
        "site": employee.site if employee else None,
        "entry_method": event.entry_method.value,
        "fallback_reason": event.fallback_reason.value if event.fallback_reason else None,
        "device_id": event.device_id,
        "scanned_at_utc": scanned_utc.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "scanned_at_colombia": scanned_local.isoformat(timespec="milliseconds"),
        "scanned_date_colombia": scanned_local.strftime("%Y-%m-%d"),
        "scanned_time_colombia": scanned_local.strftime("%H:%M:%S"),
    }


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    attempts: int
    http_status: int | None = None
    error_kind: str | None = None  # TIMEOUT | TRANSPORT | HTTP_<status> | UNEXPECTED


class PowerAutomateClient:
    def __init__(
        self,
        url: str,
        timeout_seconds: float,
        display_timezone: tzinfo,
        http_client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        self._url = url
        # httpx logs "HTTP Request: POST <full url>" at INFO, which would leak the signed
        # URL; enforce this here rather than relying on how logging was configured.
        for noisy in ("httpx", "httpcore"):
            if logging.getLogger(noisy).getEffectiveLevel() < logging.WARNING:
                logging.getLogger(noisy).setLevel(logging.WARNING)
        self._timeout = httpx.Timeout(timeout_seconds)
        self._tz = display_timezone
        self._http = http_client or httpx.Client(timeout=self._timeout)
        self._sleep = sleep

    def __repr__(self) -> str:  # never expose the signed URL
        return "PowerAutomateClient(url=<redacted>)"

    def close(self) -> None:
        self._http.close()

    def send_scan_event(self, event: NewScanEvent, event_id: int) -> DeliveryResult:
        """POST one newly recorded event. Never raises; logs the outcome safely."""
        started = time.perf_counter()
        try:
            payload = build_scan_payload(event, event_id, self._tz)
            result = self._post_with_one_retry(payload)
        except Exception as exc:  # defensive: delivery must never break a recorded scan
            result = DeliveryResult(delivered=False, attempts=1, error_kind="UNEXPECTED")
            logger.error("power_automate_unexpected_error",
                         extra={"event_id": event_id, "error_type": type(exc).__name__})

        log_fields = {
            "event_id": event_id,
            "entry_method": event.entry_method.value,
            "http_status": result.http_status,
            "attempts": result.attempts,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        if result.delivered:
            logger.info("power_automate_delivered", extra=log_fields)
        else:
            logger.error("power_automate_delivery_failed",
                         extra={**log_fields, "error_kind": result.error_kind})
        return result

    def _post_with_one_retry(self, payload: dict[str, Any]) -> DeliveryResult:
        result = self._post_once(payload, attempt=1)
        if not result.delivered and _is_retryable(result):
            self._sleep(RETRY_DELAY_SECONDS)
            result = self._post_once(payload, attempt=2)
        return result

    def _post_once(self, payload: dict[str, Any], attempt: int) -> DeliveryResult:
        try:
            response = self._http.post(
                self._url,
                json=payload,
                headers={"Accept": "application/json"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            return DeliveryResult(delivered=False, attempts=attempt, error_kind="TIMEOUT")
        except httpx.TransportError:
            # httpx exception messages can contain the URL: only the kind is kept.
            return DeliveryResult(delivered=False, attempts=attempt, error_kind="TRANSPORT")
        status = response.status_code
        if status in SUCCESS_STATUSES:
            return DeliveryResult(delivered=True, attempts=attempt, http_status=status)
        return DeliveryResult(delivered=False, attempts=attempt, http_status=status,
                              error_kind=f"HTTP_{status}")


def _is_retryable(result: DeliveryResult) -> bool:
    return result.error_kind == "TRANSPORT" or result.http_status in RETRYABLE_STATUSES
