"""ScanService: manual HiBob fallback and Power Automate mirror behaviour (fakes only)."""

import logging

import pytest

from src.exceptions import (
    CardNotResolvedError,
    CardResolverResponseError,
    CardResolverUnavailableError,
    EmployeeDataIntegrityError,
    RepositoryError,
)
from src.models.scan import (
    FALLBACK_TICKET_TTL,
    EntryMethod,
    FallbackReason,
    FallbackTicket,
    HibobLookupStatus,
    ManualLookupStatus,
    NewScanEvent,
    ScanOutcome,
)
from src.services.employee_service import EmployeeService
from src.services.scan_service import ScanService
from tests.test_scan_service import (
    DEVICE,
    RAW,
    T0,
    TEST_EMPLOYEE,
    FakeClock,
    FakeEmployeeRepository,
    FakeResolver,
    InMemoryScanRepository,
)


class FakeMirror:
    """Records mirror calls; can be told to raise (the real client never raises)."""

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[NewScanEvent, int]] = []
        self.error = error

    def send_scan_event(self, event: NewScanEvent, event_id: int) -> None:
        self.sent.append((event, event_id))
        if self.error:
            raise self.error


def _service(resolver=None, employees=None, scans=None, clock=None, mirror=None, device=DEVICE):
    return ScanService(
        card_resolver=resolver or FakeResolver(),
        employee_service=EmployeeService(employees or FakeEmployeeRepository()),
        scan_repository=scans if scans is not None else InMemoryScanRepository(),
        device_id=device,
        duplicate_window_seconds=2,
        clock=clock or FakeClock(),
        event_mirror=mirror,
    )


def _fallback_service(**kwargs):
    return _service(resolver=FakeResolver(error=CardNotResolvedError("unknown")), **kwargs)


def _not_resolved_ticket(service) -> FallbackTicket:
    result = service.process_scan(RAW)
    assert result.outcome is ScanOutcome.CARD_NOT_RESOLVED
    return result.fallback_ticket


# --- CARD entry method -------------------------------------------------------------


def test_card_scan_uses_entry_method_card():
    scans = InMemoryScanRepository()
    result = _service(scans=scans).process_scan(RAW)
    assert result.entry_method is EntryMethod.CARD and result.fallback_ticket is None
    assert scans.events[0].entry_method is EntryMethod.CARD
    assert scans.events[0].fallback_reason is None


# --- fallback access ----------------------------------------------------------------


def test_card_not_resolved_issues_fallback_ticket():
    ticket = _not_resolved_ticket(_fallback_service())
    assert isinstance(ticket, FallbackTicket) and ticket.device_id == DEVICE


@pytest.mark.parametrize("error", [CardResolverUnavailableError("timeout"),
                                   CardResolverResponseError("HTTP 500")])
def test_resolver_unavailable_or_error_never_issue_a_ticket(error):
    assert _service(resolver=FakeResolver(error=error)).process_scan(RAW).fallback_ticket is None


def test_successful_scan_never_issues_a_ticket():
    assert _service().process_scan(RAW).fallback_ticket is None


@pytest.mark.parametrize("ticket", [None, "forged", FallbackTicket(issued_at=T0, device_id="OTHER_DEVICE")])
def test_manual_fallback_cannot_be_used_without_a_valid_ticket(ticket):
    employees, scans = FakeEmployeeRepository(), InMemoryScanRepository()
    service = _service(employees=employees, scans=scans)
    assert service.lookup_manual(ticket, "99999").status is ManualLookupStatus.FALLBACK_EXPIRED
    assert service.confirm_manual(ticket, TEST_EMPLOYEE).outcome is ScanOutcome.FALLBACK_EXPIRED
    assert employees.calls == [] and scans.events == []


def test_expired_ticket_is_rejected():
    clock, scans = FakeClock(), InMemoryScanRepository()
    service = _fallback_service(clock=clock, scans=scans)
    ticket = _not_resolved_ticket(service)
    clock.advance(FALLBACK_TICKET_TTL.total_seconds())
    assert service.lookup_manual(ticket, "99999").status is ManualLookupStatus.FALLBACK_EXPIRED
    assert service.confirm_manual(ticket, TEST_EMPLOYEE).outcome is ScanOutcome.FALLBACK_EXPIRED
    assert scans.events == []


# --- Find employee (never writes) -----------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "abc", "99 999", "99999;", "-1", "1" * 21, "9.9"])
def test_manual_invalid_hibob_id(value):
    employees = FakeEmployeeRepository()
    service = _fallback_service(employees=employees)
    assert service.lookup_manual(_not_resolved_ticket(service), value).status is ManualLookupStatus.INVALID_INPUT
    assert employees.calls == []


def test_manual_found_previews_without_writing():
    scans, employees = InMemoryScanRepository(), FakeEmployeeRepository()
    service = _fallback_service(scans=scans, employees=employees)
    result = service.lookup_manual(_not_resolved_ticket(service), " 00123 ")
    assert result.status is ManualLookupStatus.FOUND and result.employee == TEST_EMPLOYEE
    assert employees.calls == ["00123"]  # string, trimmed, leading zeros kept
    assert scans.events == []


def test_manual_employee_not_found_does_not_write():
    scans = InMemoryScanRepository()
    service = _fallback_service(scans=scans, employees=FakeEmployeeRepository(employee=None))
    result = service.lookup_manual(_not_resolved_ticket(service), "12345")
    assert result.status is ManualLookupStatus.EMPLOYEE_NOT_FOUND and result.employee is None
    assert scans.events == []


@pytest.mark.parametrize(("error", "status"), [
    (EmployeeDataIntegrityError("2 rows"), ManualLookupStatus.DATA_INTEGRITY_ERROR),
    (RepositoryError("down"), ManualLookupStatus.DATABASE_ERROR),
])
def test_manual_lookup_errors_do_not_write(error, status):
    scans = InMemoryScanRepository()
    service = _fallback_service(scans=scans, employees=FakeEmployeeRepository(error=error))
    assert service.lookup_manual(_not_resolved_ticket(service), "99999").status is status
    assert scans.events == []


# --- Confirm scan ----------------------------------------------------------------------


def test_confirm_creates_manual_event_and_mirrors_it():
    scans, mirror = InMemoryScanRepository(), FakeMirror()
    service = _fallback_service(scans=scans, mirror=mirror)
    ticket = _not_resolved_ticket(service)
    employee = service.lookup_manual(ticket, "99999").employee
    result = service.confirm_manual(ticket, employee)

    assert result.outcome is ScanOutcome.RECORDED and result.event_id == 1
    assert result.entry_method is EntryMethod.MANUAL_HIBOB_FALLBACK
    (event,) = scans.events
    assert event.entry_method is EntryMethod.MANUAL_HIBOB_FALLBACK
    assert event.fallback_reason is FallbackReason.CARD_NOT_RESOLVED
    assert event.card_resolver_employee_name is None and event.card_resolver_dataset_id is None
    assert event.hibob_lookup_status is HibobLookupStatus.FOUND and event.employee == TEST_EMPLOYEE
    assert [eid for _, eid in mirror.sent] == [1]


def test_duplicate_detection_spans_card_and_manual():
    clock, scans = FakeClock(), InMemoryScanRepository()
    assert _service(clock=clock, scans=scans).process_scan(RAW).outcome is ScanOutcome.RECORDED

    clock.advance(1.0)  # same employee + device inside the 2 s window, via manual fallback
    manual = _fallback_service(clock=clock, scans=scans)
    result = manual.confirm_manual(FallbackTicket(issued_at=clock.now, device_id=DEVICE), TEST_EMPLOYEE)
    assert result.outcome is ScanOutcome.DUPLICATE and result.event_id == 1
    assert len(scans.events) == 1


def test_manual_confirm_logs_entry_method(caplog):
    caplog.set_level(logging.DEBUG, logger="src")
    service = _fallback_service()
    ticket = _not_resolved_ticket(service)
    service.confirm_manual(ticket, TEST_EMPLOYEE)
    records = [r for r in caplog.records if r.getMessage() == "scan_processed"]
    assert records[-1].entry_method == "MANUAL_HIBOB_FALLBACK" and records[-1].card is None


# --- Power Automate mirror ---------------------------------------------------------------


def test_recorded_scan_is_mirrored_once_after_persisting():
    scans, mirror = InMemoryScanRepository(), FakeMirror()
    result = _service(scans=scans, mirror=mirror).process_scan(RAW)
    ((event, event_id),) = mirror.sent
    assert result.outcome is ScanOutcome.RECORDED
    assert event_id == result.event_id == 1 and event is scans.events[0]


def test_duplicate_is_not_mirrored():
    clock, scans, mirror = FakeClock(), InMemoryScanRepository(), FakeMirror()
    service = _service(clock=clock, scans=scans, mirror=mirror)
    service.process_scan(RAW)
    clock.advance(0.5)
    assert service.process_scan(RAW).outcome is ScanOutcome.DUPLICATE
    assert len(mirror.sent) == 1


@pytest.mark.parametrize("error", [CardNotResolvedError("x"), CardResolverUnavailableError("x")])
def test_rejected_scans_are_not_mirrored(error):
    mirror = FakeMirror()
    _service(resolver=FakeResolver(error=error), mirror=mirror).process_scan(RAW)
    assert mirror.sent == []


def test_database_failure_is_not_mirrored():
    mirror = FakeMirror()
    _service(scans=InMemoryScanRepository(error=RepositoryError("down")), mirror=mirror).process_scan(RAW)
    assert mirror.sent == []


def test_mirror_failure_never_fails_the_scan(caplog):
    caplog.set_level(logging.DEBUG, logger="src")
    scans = InMemoryScanRepository()
    result = _service(scans=scans, mirror=FakeMirror(error=RuntimeError("flow down"))).process_scan(RAW)
    assert result.outcome is ScanOutcome.RECORDED and len(scans.events) == 1
    assert any(r.getMessage() == "scan_mirror_unexpected_error" and r.levelno == logging.ERROR
               for r in caplog.records)


def test_no_mirror_configured_is_fine():
    assert _service(mirror=None).process_scan(RAW).outcome is ScanOutcome.RECORDED


# --- model invariants (mirror migration 002 CHECK constraints) ---------------------------


@pytest.mark.parametrize("kwargs", [
    dict(entry_method=EntryMethod.CARD, fallback_reason=FallbackReason.CARD_NOT_RESOLVED,
         card_resolver_employee_name="X", card_resolver_dataset_id=1),
    dict(entry_method=EntryMethod.CARD),  # missing Card Resolver data
    dict(entry_method=EntryMethod.MANUAL_HIBOB_FALLBACK),  # missing fallback reason
    dict(entry_method=EntryMethod.MANUAL_HIBOB_FALLBACK, fallback_reason=FallbackReason.CARD_NOT_RESOLVED,
         card_resolver_employee_name="X", card_resolver_dataset_id=1),
])
def test_new_scan_event_enforces_entry_method_rules(kwargs):
    with pytest.raises(ValueError):
        NewScanEvent(hibob_id="99999", hibob_lookup_status=HibobLookupStatus.FOUND, employee=TEST_EMPLOYEE,
                     device_id=DEVICE, scanned_at=T0, **kwargs)


def test_manual_event_requires_found_employee():
    with pytest.raises(ValueError):
        NewScanEvent(hibob_id="99999", hibob_lookup_status=HibobLookupStatus.NOT_FOUND, employee=None,
                     device_id=DEVICE, scanned_at=T0, entry_method=EntryMethod.MANUAL_HIBOB_FALLBACK,
                     fallback_reason=FallbackReason.CARD_NOT_RESOLVED)
