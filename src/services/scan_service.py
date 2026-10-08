"""Scan orchestration: resolve card -> look up employee -> record event -> mirror.

All business rules for a scan live here; the Streamlit UI only calls the public
methods and renders the returned results.

Card flow:            process_scan(raw)
Manual HiBob fallback (only after CARD_NOT_RESOLVED, which issues a FallbackTicket):
                      lookup_manual(ticket, hibob_id)   -> preview, never writes
                      confirm_manual(ticket, employee)  -> records the event
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

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
    EntryMethod,
    FallbackReason,
    FallbackTicket,
    HibobLookupStatus,
    ManualLookupResult,
    ManualLookupStatus,
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
# HiBob employee IDs are numeric strings (leading zeros are preserved).
MANUAL_HIBOB_ID_PATTERN = re.compile(r"^[0-9]{1,20}$")

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


class ScanEventMirror(Protocol):
    """Best-effort mirror of newly recorded events (Power Automate). Must never raise."""

    def send_scan_event(self, event: NewScanEvent, event_id: int) -> Any: ...


class ScanService:
    def __init__(
        self,
        card_resolver: CardResolver,
        employee_service: EmployeeLookupService,
        scan_repository: ScanRecorder,
        device_id: str,
        duplicate_window_seconds: int,
        clock: Clock = utc_now,
        event_mirror: ScanEventMirror | None = None,
    ) -> None:
        if duplicate_window_seconds < 0:
            raise ValueError("duplicate_window_seconds must be >= 0")
        self._resolver = card_resolver
        self._employees = employee_service
        self._scans = scan_repository
        self._device_id = device_id
        self._window = duplicate_window_seconds
        self._clock = clock
        self._mirror = event_mirror

    # ------------------------------------------------------------------ card flow

    def process_scan(self, raw_input: str) -> ScanResult:
        scanned_at = self._now()
        started = time.perf_counter()
        result = self._process(raw_input, scanned_at)
        self._log_processed(result, scanned_at, started, card=mask_card_value((raw_input or "").strip()))
        return result

    def _process(self, raw_input: str, scanned_at: datetime) -> ScanResult:
        raw = (raw_input or "").strip()
        if not RAW_CARD_PATTERN.fullmatch(raw):
            return ScanResult(outcome=ScanOutcome.INVALID_INPUT, scanned_at=scanned_at)

        # 1. Card Resolver
        try:
            resolution = self._resolver.resolve(raw)
        except CardNotResolvedError as exc:
            _log_failure("card_not_resolved", exc)
            # Only this outcome unlocks the manual HiBob fallback.
            return ScanResult(outcome=ScanOutcome.CARD_NOT_RESOLVED, scanned_at=scanned_at,
                              fallback_ticket=FallbackTicket(issued_at=scanned_at,
                                                             device_id=self._device_id))
        except CardResolverUnavailableError as exc:
            _log_failure("card_resolver_unavailable", exc)
            return ScanResult(outcome=ScanOutcome.RESOLVER_UNAVAILABLE, scanned_at=scanned_at)
        except CardResolverResponseError as exc:
            _log_failure("card_resolver_bad_response", exc)
            return ScanResult(outcome=ScanOutcome.RESOLVER_ERROR, scanned_at=scanned_at)

        hibob_id = resolution.hibob_id
        base: dict[str, Any] = {"scanned_at": scanned_at, "hibob_id": hibob_id,
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

        # 3. Persist (duplicate-safe), then mirror
        event = NewScanEvent(
            hibob_id=hibob_id,
            card_resolver_employee_name=resolution.employee_name,
            card_resolver_dataset_id=resolution.dataset_id,
            hibob_lookup_status=lookup.status,
            employee=employee,
            device_id=self._device_id,
            scanned_at=scanned_at,
            entry_method=EntryMethod.CARD,
        )
        return self._record(event, base, warnings)

    # ------------------------------------------------------- manual HiBob fallback

    def lookup_manual(self, ticket: FallbackTicket | None, hibob_id_input: str) -> ManualLookupResult:
        """'Find employee': validate and look up by HiBob ID. Never writes."""
        if not self._ticket_valid(ticket):
            return ManualLookupResult(status=ManualLookupStatus.FALLBACK_EXPIRED)
        hibob_id = (hibob_id_input or "").strip()
        if not MANUAL_HIBOB_ID_PATTERN.fullmatch(hibob_id):
            result = ManualLookupResult(status=ManualLookupStatus.INVALID_INPUT)
        else:
            try:
                lookup = self._employees.lookup(hibob_id)
            except EmployeeDataIntegrityError as exc:
                _log_failure("hibob_duplicate_employee_id", exc, hibob_id=hibob_id)
                result = ManualLookupResult(status=ManualLookupStatus.DATA_INTEGRITY_ERROR)
            except RepositoryError as exc:
                _log_failure("hibob_lookup_failed", exc, hibob_id=hibob_id)
                result = ManualLookupResult(status=ManualLookupStatus.DATABASE_ERROR)
            else:
                result = (ManualLookupResult(status=ManualLookupStatus.FOUND, employee=lookup.employee)
                          if lookup.employee is not None
                          else ManualLookupResult(status=ManualLookupStatus.EMPLOYEE_NOT_FOUND))
        logger.log(logging.INFO if result.status is ManualLookupStatus.FOUND else logging.WARNING,
                   "manual_fallback_lookup",
                   extra={"status": result.status.value, "device_id": self._device_id,
                          "hibob_id": hibob_id if MANUAL_HIBOB_ID_PATTERN.fullmatch(hibob_id) else None})
        return result

    def confirm_manual(self, ticket: FallbackTicket | None, employee: Employee) -> ScanResult:
        """'Confirm scan': the only manual-fallback action that writes a scan_event."""
        scanned_at = self._now()
        started = time.perf_counter()
        if not self._ticket_valid(ticket):
            result = ScanResult(outcome=ScanOutcome.FALLBACK_EXPIRED, scanned_at=scanned_at,
                                entry_method=EntryMethod.MANUAL_HIBOB_FALLBACK)
        else:
            event = NewScanEvent(
                hibob_id=employee.employee_id,
                hibob_lookup_status=HibobLookupStatus.FOUND,
                employee=employee,
                device_id=self._device_id,
                scanned_at=scanned_at,
                entry_method=EntryMethod.MANUAL_HIBOB_FALLBACK,
                fallback_reason=FallbackReason.CARD_NOT_RESOLVED,
            )
            base = {"scanned_at": scanned_at, "hibob_id": employee.employee_id,
                    "display_name": employee.employee_name}
            result = self._record(event, base, _warnings_for(
                EmployeeLookup(status=HibobLookupStatus.FOUND, employee=employee)))
        self._log_processed(result, scanned_at, started, card=None)
        return result

    # ----------------------------------------------------------------- internals

    def _record(self, event: NewScanEvent, base: dict[str, Any],
                warnings: tuple[ScanWarning, ...]) -> ScanResult:
        common = {"employee": event.employee, "warnings": warnings,
                  "entry_method": event.entry_method, **base}
        try:
            recorded = self._scans.record_if_not_duplicate(event, self._window)
        except RepositoryError as exc:
            _log_failure("scan_record_failed", exc, hibob_id=event.hibob_id)
            return ScanResult(outcome=ScanOutcome.DATABASE_ERROR, **common)

        if recorded.is_duplicate:
            return ScanResult(outcome=ScanOutcome.DUPLICATE, event_id=recorded.event_id, **common)

        # Committed. Mirror only newly recorded events; a mirror failure never fails the scan.
        if self._mirror is not None:
            try:
                self._mirror.send_scan_event(event, recorded.event_id)
            except Exception as exc:  # the mirror must not raise; guard anyway
                logger.error("scan_mirror_unexpected_error",
                             extra={"event_id": recorded.event_id, "error_type": type(exc).__name__})
        return ScanResult(outcome=ScanOutcome.RECORDED, event_id=recorded.event_id, **common)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetimes")
        return now

    def _ticket_valid(self, ticket: FallbackTicket | None) -> bool:
        return (isinstance(ticket, FallbackTicket)
                and ticket.device_id == self._device_id
                and ticket.is_valid_at(self._now()))

    def _log_processed(self, result: ScanResult, scanned_at: datetime, started: float,
                       card: str | None) -> None:
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
                "entry_method": result.entry_method.value,
                "hibob_id": result.hibob_id,
                "device_id": self._device_id,
                "card": card,
                "warnings": [w.value for w in result.warnings],
                "name_matches_resolver": result.name_matches_resolver,  # INFO diagnostic
                "event_id": result.event_id,
                "scanned_at": scanned_at.isoformat(),
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
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
