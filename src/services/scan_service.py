"""Scan orchestration: resolve card -> look up employee -> record event.

All business rules for a scan live here; the Streamlit UI only calls
``ScanService.process_scan`` and renders the returned ScanResult.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import UTC, datetime
from typing import Callable, Protocol

from src.exceptions import (
    CardNotResolvedError,
    CardResolverResponseError,
    CardResolverUnavailableError,
    EmployeeDataIntegrityError,
    RepositoryError,
)
from src.logging_setup import mask_card_value
from src.models.employee import Employee
from src.models.scan import (
    CardResolution,
    HibobLookupStatus,
    NewScanEvent,
    RecordResult,
    ScanOutcome,
    ScanResult,
    ScanWarning,
)
from src.services.employee_service import EmployeeLookup

logger = logging.getLogger(__name__)

# Accepts typical keyboard-wedge output (decimal or hex), rejects stray keystrokes.
RAW_CARD_PATTERN = re.compile(r"^[0-9A-Za-z]{4,64}$")

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class CardResolver(Protocol):
    def resolve(self, raw: str) -> CardResolution: ...


class EmployeeLookupService(Protocol):
    def lookup(self, hibob_id: str) -> EmployeeLookup: ...


class ScanRecorder(Protocol):
    def record_if_not_duplicate(
        self, event: NewScanEvent, duplicate_window_seconds: int
    ) -> RecordResult: ...


class ScanService:
    def __init__(
        self,
        card_resolver: CardResolver,
        employee_service: EmployeeLookupService,
        scan_repository: ScanRecorder,
        device_id: str,
        duplicate_window_seconds: int,
        clock: Clock = utc_now,
    ) -> None:
        if duplicate_window_seconds < 0:
            raise ValueError("duplicate_window_seconds must be >= 0")
        self._resolver = card_resolver
        self._employees = employee_service
        self._scans = scan_repository
        self._device_id = device_id
        self._window = duplicate_window_seconds
        self._clock = clock

    def process_scan(self, raw_input: str) -> ScanResult:
        scanned_at = self._clock()
        if scanned_at.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetimes")
        started = time.perf_counter()

        result = self._process(raw_input, scanned_at)

        log_level = logging.INFO if result.outcome.is_accepted else logging.WARNING
        if result.outcome.is_accepted and result.warnings:
            log_level = logging.WARNING  # e.g. EMPLOYEE_NOT_ACTIVE: logged, never shown on the kiosk
        if result.outcome in (ScanOutcome.DATABASE_ERROR, ScanOutcome.DATA_INTEGRITY_ERROR,
                              ScanOutcome.RESOLVER_ERROR):
            log_level = logging.ERROR
        logger.log(
            log_level,
            "scan_processed",
            extra={
                "outcome": result.outcome.value,
                "hibob_id": result.hibob_id,
                "device_id": self._device_id,
                "card": mask_card_value((raw_input or "").strip()),
                "warnings": [w.value for w in result.warnings],
                "name_matches_resolver": result.name_matches_resolver,  # INFO diagnostic
                "event_id": result.event_id,
                "scanned_at": scanned_at.isoformat(),
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return result

    # ------------------------------------------------------------------

    def _process(self, raw_input: str, scanned_at: datetime) -> ScanResult:
        raw = (raw_input or "").strip()
        if not RAW_CARD_PATTERN.fullmatch(raw):
            return ScanResult(outcome=ScanOutcome.INVALID_INPUT, scanned_at=scanned_at)

        # 1. Card Resolver
        try:
            resolution = self._resolver.resolve(raw)
        except CardNotResolvedError as exc:
            _log_failure("card_not_resolved", exc)
            return ScanResult(outcome=ScanOutcome.CARD_NOT_RESOLVED, scanned_at=scanned_at)
        except CardResolverUnavailableError as exc:
            _log_failure("card_resolver_unavailable", exc)
            return ScanResult(outcome=ScanOutcome.RESOLVER_UNAVAILABLE, scanned_at=scanned_at)
        except CardResolverResponseError as exc:
            _log_failure("card_resolver_bad_response", exc)
            return ScanResult(outcome=ScanOutcome.RESOLVER_ERROR, scanned_at=scanned_at)

        hibob_id = resolution.hibob_id
        base = {"scanned_at": scanned_at, "hibob_id": hibob_id,
                "display_name": resolution.employee_name}

        # 2. HiBob snapshot lookup (by ID only)
        try:
            lookup = self._employees.lookup(hibob_id)
        except EmployeeDataIntegrityError as exc:
            _log_failure("hibob_duplicate_employee_id", exc, hibob_id=hibob_id)
            return ScanResult(outcome=ScanOutcome.DATA_INTEGRITY_ERROR, **base)
        except RepositoryError as exc:
            _log_failure("hibob_lookup_failed", exc, hibob_id=hibob_id)
            return ScanResult(outcome=ScanOutcome.DATABASE_ERROR, **base)

        warnings = _warnings_for(lookup)
        employee = lookup.employee
        if employee is not None:
            base["display_name"] = employee.employee_name
            base["name_matches_resolver"] = names_match(employee, resolution.employee_name)

        # 3. Persist (duplicate-safe)
        event = NewScanEvent(
            hibob_id=hibob_id,
            card_resolver_employee_name=resolution.employee_name,
            card_resolver_dataset_id=resolution.dataset_id,
            hibob_lookup_status=lookup.status,
            employee=employee,
            device_id=self._device_id,
            scanned_at=scanned_at,
        )
        try:
            recorded = self._scans.record_if_not_duplicate(event, self._window)
        except RepositoryError as exc:
            _log_failure("scan_record_failed", exc, hibob_id=hibob_id)
            return ScanResult(outcome=ScanOutcome.DATABASE_ERROR, employee=employee,
                              warnings=warnings, **base)

        return ScanResult(
            outcome=ScanOutcome.DUPLICATE if recorded.is_duplicate else ScanOutcome.RECORDED,
            employee=employee,
            warnings=warnings,
            event_id=recorded.event_id,
            **base,
        )


def _warnings_for(lookup: EmployeeLookup) -> tuple[ScanWarning, ...]:
    if lookup.status is HibobLookupStatus.NOT_FOUND or lookup.employee is None:
        return (ScanWarning.HIBOB_NOT_FOUND,)
    if not lookup.employee.is_active:
        return (ScanWarning.EMPLOYEE_NOT_ACTIVE,)
    return ()


def _normalize_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(without_accents.casefold().split())


def names_match(employee: Employee, resolver_name: str) -> bool:
    """Exact comparison ignoring case/accents/whitespace. Diagnostic only: legitimate
    differences (middle names, surname formatting) are common, so it never warns."""
    return _normalize_name(employee.employee_name) == _normalize_name(resolver_name)


def _log_failure(event: str, exc: Exception, hibob_id: str | None = None) -> None:
    # Exception messages are constructed by this application and contain no secrets.
    logger.warning(event, extra={"error_type": type(exc).__name__, "error": str(exc),
                                 "hibob_id": hibob_id})
