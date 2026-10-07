import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from src.exceptions import (
    CardNotResolvedError,
    CardResolverResponseError,
    CardResolverUnavailableError,
    EmployeeDataIntegrityError,
    RepositoryError,
)
from src.models.employee import Employee
from src.models.scan import (
    CardResolution,
    HibobLookupStatus,
    NewScanEvent,
    RecordResult,
    ScanOutcome,
    ScanWarning,
)
from src.services.employee_service import EmployeeService
from src.services.scan_service import ScanService

RAW = "000012345678"
DEVICE = "FLOOR_SUPERVISOR_TABLET_01"
T0 = datetime(2026, 10, 7, 13, 45, 0, tzinfo=UTC)
TEST_EMPLOYEE = Employee("99999", "Test Employee", "Game Presenter", "Operations",
                 "Test Site", "Active", "Employed")
RESOLUTION = CardResolution("99999", "Test Employee", 99)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class FakeResolver:
    resolution: CardResolution | None = RESOLUTION
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)

    def resolve(self, raw: str) -> CardResolution:
        self.calls.append(raw)
        if self.error:
            raise self.error
        assert self.resolution is not None
        return self.resolution


@dataclass
class FakeEmployeeRepository:
    employee: Employee | None = TEST_EMPLOYEE
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)

    def get_by_employee_id(self, employee_id: str) -> Employee | None:
        self.calls.append(employee_id)
        if self.error:
            raise self.error
        return self.employee


class InMemoryScanRepository:
    """Mimics the SQL duplicate rule: same device + hibob_id with scanned_at > new - window."""

    def __init__(self, error: Exception | None = None) -> None:
        self.events: list[NewScanEvent] = []
        self.error = error

    def record_if_not_duplicate(self, event: NewScanEvent, duplicate_window_seconds: int) -> RecordResult:
        if self.error:
            raise self.error
        if duplicate_window_seconds > 0:
            threshold = event.scanned_at - timedelta(seconds=duplicate_window_seconds)
            for index in range(len(self.events) - 1, -1, -1):
                previous = self.events[index]
                if (previous.device_id == event.device_id and previous.hibob_id == event.hibob_id
                        and previous.scanned_at > threshold):
                    return RecordResult(event_id=index + 1, is_duplicate=True)
        self.events.append(event)
        return RecordResult(event_id=len(self.events), is_duplicate=False)


def _service(resolver=None, employees=None, scans=None, clock=None, window=2, device=DEVICE):
    return ScanService(
        card_resolver=resolver or FakeResolver(),
        employee_service=EmployeeService(employees or FakeEmployeeRepository()),
        scan_repository=scans if scans is not None else InMemoryScanRepository(),
        device_id=device,
        duplicate_window_seconds=window,
        clock=clock or FakeClock(),
    )


# --- happy path -------------------------------------------------------------


def test_records_scan_with_hibob_snapshot():
    scans = InMemoryScanRepository()
    result = _service(scans=scans).process_scan(RAW)

    assert result.outcome is ScanOutcome.RECORDED
    assert result.event_id == 1
    assert result.display_name == "Test Employee"
    assert result.employee == TEST_EMPLOYEE
    assert result.warnings == ()
    assert result.scanned_at == T0

    (event,) = scans.events
    assert event.hibob_id == "99999"
    assert event.hibob_lookup_status is HibobLookupStatus.FOUND
    assert event.employee == TEST_EMPLOYEE
    assert event.card_resolver_employee_name == "Test Employee"
    assert event.card_resolver_dataset_id == 99
    assert event.device_id == DEVICE
    assert event.scanned_at == T0 and event.scanned_at.tzinfo is not None


def test_input_is_stripped_before_resolving():
    resolver = FakeResolver()
    _service(resolver=resolver).process_scan(f"  {RAW}\n")
    assert resolver.calls == [RAW]


# --- HiBob NOT_FOUND / inactive ----------------------------------------------


def test_hibob_not_found_is_recorded_with_warning_and_resolver_name():
    scans = InMemoryScanRepository()
    result = _service(employees=FakeEmployeeRepository(employee=None), scans=scans).process_scan(RAW)

    assert result.outcome is ScanOutcome.RECORDED
    assert result.warnings == (ScanWarning.HIBOB_NOT_FOUND,)
    assert result.display_name == "Test Employee"
    (event,) = scans.events
    assert event.hibob_lookup_status is HibobLookupStatus.NOT_FOUND
    assert event.employee is None
    assert event.card_resolver_employee_name == "Test Employee"


def test_inactive_employee_is_recorded_with_warning():
    inactive = Employee("99999", "Test Employee", "Game Presenter", "Operations",
                        "Test Site", "Inactive", "Terminated")
    scans = InMemoryScanRepository()
    result = _service(employees=FakeEmployeeRepository(employee=inactive), scans=scans).process_scan(RAW)

    assert result.outcome is ScanOutcome.RECORDED
    assert ScanWarning.EMPLOYEE_NOT_ACTIVE in result.warnings
    assert scans.events[0].employee.employment_status == "Inactive"


def test_inactive_employee_is_a_structured_log_warning(caplog):
    inactive = Employee("99999", "Test Employee", "Game Presenter", "Operations",
                        "Test Site", "Inactive", "Terminated")
    caplog.set_level(logging.DEBUG, logger="src")
    _service(employees=FakeEmployeeRepository(employee=inactive)).process_scan(RAW)

    (record,) = [r for r in caplog.records if r.getMessage() == "scan_processed"]
    assert record.levelno == logging.WARNING
    assert record.outcome == "RECORDED"
    assert record.warnings == ["EMPLOYEE_NOT_ACTIVE"]


def test_clean_recorded_scan_logs_at_info(caplog):
    caplog.set_level(logging.DEBUG, logger="src")
    _service().process_scan(RAW)
    (record,) = [r for r in caplog.records if r.getMessage() == "scan_processed"]
    assert record.levelno == logging.INFO


def test_name_comparison_ignores_accents_and_case():
    accented = Employee("99999", "Tést Émployee", None, None, None, "Active", "Employed")
    result = _service(employees=FakeEmployeeRepository(employee=accented)).process_scan(RAW)
    assert result.name_matches_resolver is True


def test_name_mismatch_is_info_diagnostic_never_a_warning(caplog):
    # HiBob omits a middle name that Card Resolver includes (a common, legitimate difference)
    resolver = FakeResolver(resolution=CardResolution("99999", "Test Middle Employee", 99))
    caplog.set_level(logging.DEBUG, logger="src")
    scans = InMemoryScanRepository()
    result = _service(resolver=resolver, scans=scans).process_scan(RAW)  # HiBob: "Test Employee"

    assert result.outcome is ScanOutcome.RECORDED and len(scans.events) == 1  # never blocks
    assert result.warnings == ()
    assert result.name_matches_resolver is False
    assert result.display_name == "Test Employee"  # HiBob is the display authority
    (record,) = [r for r in caplog.records if r.getMessage() == "scan_processed"]
    assert record.levelno == logging.INFO
    assert record.warnings == []
    assert record.name_matches_resolver is False
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_name_comparison_absent_when_hibob_not_found():
    result = _service(employees=FakeEmployeeRepository(employee=None)).process_scan(RAW)
    assert result.name_matches_resolver is None


def test_lookup_uses_resolver_hibob_id_not_name():
    employees = FakeEmployeeRepository()
    _service(employees=employees).process_scan(RAW)
    assert employees.calls == ["99999"]


# --- rejections (nothing recorded) -----------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", "12", "0000 1234", "000012345678;", "x" * 65])
def test_invalid_input_rejected_without_calling_resolver(raw):
    resolver, scans = FakeResolver(), InMemoryScanRepository()
    result = _service(resolver=resolver, scans=scans).process_scan(raw)

    assert result.outcome is ScanOutcome.INVALID_INPUT
    assert resolver.calls == [] and scans.events == []


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (CardNotResolvedError("unknown"), ScanOutcome.CARD_NOT_RESOLVED),
        (CardResolverUnavailableError("timeout"), ScanOutcome.RESOLVER_UNAVAILABLE),
        (CardResolverResponseError("bad body"), ScanOutcome.RESOLVER_ERROR),
    ],
)
def test_resolver_failures_reject_scan(error, outcome):
    employees, scans = FakeEmployeeRepository(), InMemoryScanRepository()
    result = _service(resolver=FakeResolver(error=error), employees=employees, scans=scans).process_scan(RAW)

    assert result.outcome is outcome
    assert employees.calls == [] and scans.events == []


def test_duplicate_hibob_rows_is_data_integrity_error_and_not_recorded():
    scans = InMemoryScanRepository()
    employees = FakeEmployeeRepository(error=EmployeeDataIntegrityError("2 rows"))
    result = _service(employees=employees, scans=scans).process_scan(RAW)

    assert result.outcome is ScanOutcome.DATA_INTEGRITY_ERROR
    assert scans.events == []


def test_hibob_database_error():
    result = _service(employees=FakeEmployeeRepository(error=RepositoryError("down"))).process_scan(RAW)
    assert result.outcome is ScanOutcome.DATABASE_ERROR


def test_record_database_error():
    result = _service(scans=InMemoryScanRepository(error=RepositoryError("down"))).process_scan(RAW)
    assert result.outcome is ScanOutcome.DATABASE_ERROR
    assert result.event_id is None


# --- duplicate window --------------------------------------------------------


def test_scan_within_window_is_duplicate():
    clock, scans = FakeClock(), InMemoryScanRepository()
    service = _service(clock=clock, scans=scans)

    first = service.process_scan(RAW)
    clock.advance(1.9)
    second = service.process_scan(RAW)

    assert first.outcome is ScanOutcome.RECORDED
    assert second.outcome is ScanOutcome.DUPLICATE
    assert second.event_id == first.event_id
    assert len(scans.events) == 1


def test_scan_exactly_at_window_boundary_is_recorded():
    clock, scans = FakeClock(), InMemoryScanRepository()
    service = _service(clock=clock, scans=scans)

    service.process_scan(RAW)
    clock.advance(2.0)
    assert service.process_scan(RAW).outcome is ScanOutcome.RECORDED
    assert len(scans.events) == 2


def test_scans_minutes_apart_are_never_duplicates():
    clock, scans = FakeClock(), InMemoryScanRepository()
    service = _service(clock=clock, scans=scans)

    service.process_scan(RAW)
    clock.advance(180)
    assert service.process_scan(RAW).outcome is ScanOutcome.RECORDED


def test_same_employee_on_other_device_is_not_duplicate():
    clock, scans = FakeClock(), InMemoryScanRepository()
    _service(clock=clock, scans=scans, device="TABLET_A").process_scan(RAW)
    clock.advance(0.5)
    result = _service(clock=clock, scans=scans, device="TABLET_B").process_scan(RAW)
    assert result.outcome is ScanOutcome.RECORDED


def test_different_employee_same_device_is_not_duplicate():
    clock, scans = FakeClock(), InMemoryScanRepository()
    _service(clock=clock, scans=scans).process_scan(RAW)
    clock.advance(0.5)
    other = FakeResolver(resolution=CardResolution("99998", "Second Test Employee", 99))
    result = _service(clock=clock, scans=scans, resolver=other,
                      employees=FakeEmployeeRepository(employee=None)).process_scan("000012349999")
    assert result.outcome is ScanOutcome.RECORDED


def test_failed_scan_does_not_make_next_scan_a_duplicate():
    clock = FakeClock()
    scans = InMemoryScanRepository(error=RepositoryError("down"))
    service = _service(clock=clock, scans=scans)
    assert service.process_scan(RAW).outcome is ScanOutcome.DATABASE_ERROR

    scans.error = None
    clock.advance(0.5)
    assert service.process_scan(RAW).outcome is ScanOutcome.RECORDED


def test_window_zero_disables_duplicates():
    clock, scans = FakeClock(), InMemoryScanRepository()
    service = _service(clock=clock, scans=scans, window=0)
    service.process_scan(RAW)
    assert service.process_scan(RAW).outcome is ScanOutcome.RECORDED


def test_negative_window_rejected():
    with pytest.raises(ValueError):
        _service(window=-1)


def test_naive_clock_rejected():
    service = _service(clock=lambda: datetime(2026, 10, 7, 8, 0, 0))
    with pytest.raises(ValueError):
        service.process_scan(RAW)


# --- logging -----------------------------------------------------------------


@pytest.mark.parametrize(
    "resolver",
    [FakeResolver(), FakeResolver(error=CardNotResolvedError("unknown")),
     FakeResolver(error=CardResolverUnavailableError("timeout"))],
)
def test_logs_never_contain_full_card_value(caplog, resolver):
    caplog.set_level(logging.DEBUG, logger="src")
    _service(resolver=resolver).process_scan(RAW)

    assert caplog.records, "expected structured log records"
    for record in caplog.records:
        assert RAW not in record.getMessage()
        assert all(RAW not in str(value) for value in vars(record).values())
    processed = [r for r in caplog.records if r.getMessage() == "scan_processed"]
    assert processed and processed[0].card == "****5678"
    assert processed[0].device_id == DEVICE
