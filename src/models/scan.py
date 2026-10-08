"""Scan domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from src.models.employee import Employee

# How long the manual HiBob fallback stays available after CARD_NOT_RESOLVED.
FALLBACK_TICKET_TTL = timedelta(seconds=90)


class HibobLookupStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"


class EntryMethod(StrEnum):
    CARD = "CARD"
    MANUAL_HIBOB_FALLBACK = "MANUAL_HIBOB_FALLBACK"


class FallbackReason(StrEnum):
    CARD_NOT_RESOLVED = "CARD_NOT_RESOLVED"


class ScanOutcome(StrEnum):
    RECORDED = "RECORDED"
    DUPLICATE = "DUPLICATE"
    INVALID_INPUT = "INVALID_INPUT"
    CARD_NOT_RESOLVED = "CARD_NOT_RESOLVED"
    RESOLVER_UNAVAILABLE = "RESOLVER_UNAVAILABLE"
    RESOLVER_ERROR = "RESOLVER_ERROR"
    DATA_INTEGRITY_ERROR = "DATA_INTEGRITY_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    FALLBACK_EXPIRED = "FALLBACK_EXPIRED"

    @property
    def is_accepted(self) -> bool:
        return self in (ScanOutcome.RECORDED, ScanOutcome.DUPLICATE)


class ScanWarning(StrEnum):
    HIBOB_NOT_FOUND = "HIBOB_NOT_FOUND"
    EMPLOYEE_NOT_ACTIVE = "EMPLOYEE_NOT_ACTIVE"


@dataclass(frozen=True)
class CardResolution:
    """Successful Card Resolver answer (only the fields Floor Supervisor uses)."""

    hibob_id: str
    employee_name: str
    dataset_id: int


@dataclass(frozen=True)
class FallbackTicket:
    """Permission to use the manual HiBob fallback.

    Issued only by ScanService when a card scan ends in CARD_NOT_RESOLVED. It lives
    in server-side session state, so the browser cannot create or alter one.
    """

    issued_at: datetime
    device_id: str

    def is_valid_at(self, now: datetime) -> bool:
        return self.issued_at <= now < self.issued_at + FALLBACK_TICKET_TTL


@dataclass(frozen=True)
class NewScanEvent:
    """A scan about to be persisted to floor_supervisor.scan_event."""

    hibob_id: str
    hibob_lookup_status: HibobLookupStatus
    employee: Employee | None
    device_id: str
    scanned_at: datetime
    card_resolver_employee_name: str | None = None
    card_resolver_dataset_id: int | None = None
    entry_method: EntryMethod = EntryMethod.CARD
    fallback_reason: FallbackReason | None = None

    def __post_init__(self) -> None:
        if self.scanned_at.tzinfo is None or self.scanned_at.utcoffset() is None:
            raise ValueError("scanned_at must be timezone-aware")
        if (self.hibob_lookup_status is HibobLookupStatus.FOUND) != (self.employee is not None):
            raise ValueError("employee must be present if and only if lookup status is FOUND")
        # Mirrors scan_event_entry_method_consistency_chk (migration 002).
        if self.entry_method is EntryMethod.CARD:
            if (self.fallback_reason is not None or self.card_resolver_employee_name is None
                    or self.card_resolver_dataset_id is None):
                raise ValueError("CARD events need Card Resolver data and no fallback reason")
        else:
            if (self.fallback_reason is not FallbackReason.CARD_NOT_RESOLVED
                    or self.card_resolver_employee_name is not None
                    or self.card_resolver_dataset_id is not None
                    or self.hibob_lookup_status is not HibobLookupStatus.FOUND):
                raise ValueError("manual fallback events need CARD_NOT_RESOLVED, a HiBob row "
                                 "and no Card Resolver data")

    @property
    def display_name(self) -> str:
        """HiBob name when available, otherwise the Card Resolver name."""
        if self.employee is not None:
            return self.employee.employee_name
        return self.card_resolver_employee_name or ""


@dataclass(frozen=True)
class RecordResult:
    event_id: int
    is_duplicate: bool


@dataclass(frozen=True)
class ScanResult:
    outcome: ScanOutcome
    scanned_at: datetime
    hibob_id: str | None = None
    display_name: str | None = None
    employee: Employee | None = None
    warnings: tuple[ScanWarning, ...] = ()
    event_id: int | None = None
    # Diagnostic only (never a warning, never shown): exact normalized name equality between
    # Card Resolver and HiBob. None when there is no HiBob row to compare.
    name_matches_resolver: bool | None = None
    entry_method: EntryMethod = EntryMethod.CARD
    # Present only for CARD_NOT_RESOLVED: unlocks the manual HiBob fallback.
    fallback_ticket: FallbackTicket | None = None


class ManualLookupStatus(StrEnum):
    FOUND = "FOUND"
    INVALID_INPUT = "INVALID_INPUT"
    EMPLOYEE_NOT_FOUND = "EMPLOYEE_NOT_FOUND"
    DATA_INTEGRITY_ERROR = "DATA_INTEGRITY_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    FALLBACK_EXPIRED = "FALLBACK_EXPIRED"


@dataclass(frozen=True)
class ManualLookupResult:
    """Outcome of 'Find employee' in the manual fallback. Never writes anything."""

    status: ManualLookupStatus
    employee: Employee | None = None
