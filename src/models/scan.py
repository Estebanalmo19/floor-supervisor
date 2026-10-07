"""Scan domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from src.models.employee import Employee


class HibobLookupStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"


class ScanOutcome(StrEnum):
    RECORDED = "RECORDED"
    DUPLICATE = "DUPLICATE"
    INVALID_INPUT = "INVALID_INPUT"
    CARD_NOT_RESOLVED = "CARD_NOT_RESOLVED"
    RESOLVER_UNAVAILABLE = "RESOLVER_UNAVAILABLE"
    RESOLVER_ERROR = "RESOLVER_ERROR"
    DATA_INTEGRITY_ERROR = "DATA_INTEGRITY_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"

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
class NewScanEvent:
    """A scan about to be persisted to floor_supervisor.scan_event."""

    hibob_id: str
    card_resolver_employee_name: str
    card_resolver_dataset_id: int
    hibob_lookup_status: HibobLookupStatus
    employee: Employee | None
    device_id: str
    scanned_at: datetime

    def __post_init__(self) -> None:
        if self.scanned_at.tzinfo is None or self.scanned_at.utcoffset() is None:
            raise ValueError("scanned_at must be timezone-aware")
        if (self.hibob_lookup_status is HibobLookupStatus.FOUND) != (self.employee is not None):
            raise ValueError("employee must be present if and only if lookup status is FOUND")


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
