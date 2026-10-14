"""CARD_NOT_RESOLVED -> register the card in Card Resolver -> check in (fakes only)."""

import json
import logging
from zoneinfo import ZoneInfo

import httpx
import pytest

from src.exceptions import (
    CardNotResolvedError,
    CardRegistrationRejectedError,
    CardResolverResponseError,
    CardResolverUnavailableError,
    EmployeeDataIntegrityError,
)
from src.logging_setup import JsonFormatter
from src.models.scan import (
    EntryMethod,
    FALLBACK_TICKET_TTL,
    FallbackReason,
    ManualLookupStatus,
    ScanOutcome,
)
from src.repositories.scan_repository import INSERT_SCAN_SQL, ScanRepository
from src.services.card_resolver import CardResolverClient
from src.services.power_automate import build_scan_payload
from tests.fakes import FakeDatabase
from tests.test_manual_fallback_and_mirror import FakeMirror, _service
from tests.test_scan_service import (
    RAW,
    TEST_EMPLOYEE,
    FakeClock,
    FakeEmployeeRepository,
    FakeResolver,
    InMemoryScanRepository,
)


def _unknown_card_resolver(**kwargs) -> FakeResolver:
    return FakeResolver(error=CardNotResolvedError("unknown"), **kwargs)


def _setup(resolver=None, **kwargs):
    resolver = resolver or _unknown_card_resolver()
    scans, mirror = InMemoryScanRepository(), FakeMirror()
    kwargs.setdefault("scans", scans)
    kwargs.setdefault("mirror", mirror)
    service = _service(resolver=resolver, **kwargs)
    return service, resolver, kwargs["scans"], kwargs["mirror"]


def _ticket(service):
    result = service.process_scan(RAW)
    assert result.outcome is ScanOutcome.CARD_NOT_RESOLVED
    return result.fallback_ticket


# --- 1. raw card retained only in the fallback ticket ------------------------------------


def test_card_not_resolved_ticket_carries_raw_card_but_never_reveals_it():
    service, *_ = _setup()
    result = service.process_scan(RAW)
    assert result.fallback_ticket.card_raw == RAW
    assert RAW not in repr(result.fallback_ticket) and RAW not in repr(result)


def test_no_ticket_when_card_registration_is_not_configured():
    service, *_ = _setup(resolver=_unknown_card_resolver(registration_enabled=False))
    result = service.process_scan(RAW)
    assert result.outcome is ScanOutcome.CARD_NOT_RESOLVED and result.fallback_ticket is None


def test_no_ticket_for_a_card_value_card_resolver_cannot_store():
    service, *_ = _setup()
    assert service.process_scan("1" * 21).fallback_ticket is None  # API max length is 20


# --- 6/7/8. nothing reaches /cards/update before explicit confirmation --------------------


@pytest.mark.parametrize("value", ["", "abc", "99 999", "1" * 21])
def test_invalid_hibob_id_does_not_call_update(value):
    service, resolver, scans, _ = _setup()
    assert service.lookup_manual(_ticket(service), value).status is ManualLookupStatus.INVALID_INPUT
    assert resolver.updates == [] and scans.events == []


def test_employee_not_found_does_not_call_update():
    service, resolver, scans, _ = _setup(employees=FakeEmployeeRepository(employee=None))
    assert service.lookup_manual(_ticket(service), "12345").status is ManualLookupStatus.EMPLOYEE_NOT_FOUND
    assert resolver.updates == [] and scans.events == []


def test_duplicate_hibob_rows_do_not_call_update():
    service, resolver, scans, _ = _setup(
        employees=FakeEmployeeRepository(error=EmployeeDataIntegrityError("2 rows")))
    assert service.lookup_manual(_ticket(service), "99999").status is ManualLookupStatus.DATA_INTEGRITY_ERROR
    assert resolver.updates == [] and scans.events == []


def test_employee_preview_does_not_call_update():
    service, resolver, scans, mirror = _setup()
    lookup = service.lookup_manual(_ticket(service), "99999")
    assert lookup.status is ManualLookupStatus.FOUND and lookup.employee == TEST_EMPLOYEE
    assert resolver.updates == [] and scans.events == [] and mirror.sent == []


def test_expired_ticket_does_not_call_update():
    clock = FakeClock()
    service, resolver, scans, _ = _setup(clock=clock)
    ticket = _ticket(service)
    clock.advance(FALLBACK_TICKET_TTL.total_seconds())
    assert service.register_card_and_confirm(ticket, TEST_EMPLOYEE).outcome is ScanOutcome.FALLBACK_EXPIRED
    assert resolver.updates == [] and scans.events == []


# --- 10/11/12. confirm: update, then record, then Power Automate --------------------------


def test_confirm_registers_card_then_records_and_mirrors():
    service, resolver, scans, mirror = _setup()
    ticket = _ticket(service)
    employee = service.lookup_manual(ticket, "99999").employee
    result = service.register_card_and_confirm(ticket, employee)

    assert resolver.updates == [(RAW, "99999")]
    assert result.outcome is ScanOutcome.RECORDED and result.card_registered is True
    assert result.entry_method is EntryMethod.MANUAL_HIBOB_FALLBACK
    (event,) = scans.events
    assert event.entry_method is EntryMethod.MANUAL_HIBOB_FALLBACK
    assert event.fallback_reason is FallbackReason.CARD_NOT_RESOLVED
    assert event.hibob_id == "99999" and event.employee == TEST_EMPLOYEE
    assert [eid for _, eid in mirror.sent] == [result.event_id]


def test_update_happens_before_the_scan_event_is_written():
    order: list[str] = []
    resolver = _unknown_card_resolver()
    original_update = resolver.update_card
    resolver.update_card = lambda raw, hibob: (order.append("update"), original_update(raw, hibob))[1]

    class OrderedScans(InMemoryScanRepository):
        def record_if_not_duplicate(self, event, window):
            order.append("record")
            return super().record_if_not_duplicate(event, window)

    service, *_ = _setup(resolver=resolver, scans=OrderedScans())
    service.register_card_and_confirm(_ticket(service), TEST_EMPLOYEE)
    assert order == ["update", "record"]


# --- 3/4. raw card never reaches scan_event or Power Automate ------------------------------


def test_raw_card_is_not_written_to_scan_event():
    service, _, scans, _ = _setup()
    service.register_card_and_confirm(_ticket(service), TEST_EMPLOYEE)
    (event,) = scans.events
    assert RAW not in repr(event) and RAW not in json.dumps(vars(event), default=str)

    db = FakeDatabase(responder=lambda sql, params: [(1,)] if sql == INSERT_SCAN_SQL else [])
    ScanRepository(db.connection).record_if_not_duplicate(event, 2)
    assert all(RAW not in json.dumps(s.params, default=str) for s in db.executed)


def test_raw_card_is_not_in_power_automate_payload():
    service, _, _, mirror = _setup()
    service.register_card_and_confirm(_ticket(service), TEST_EMPLOYEE)
    ((event, event_id),) = mirror.sent
    payload = build_scan_payload(event, event_id, ZoneInfo("America/Bogota"))
    assert RAW not in json.dumps(payload)
    assert payload["entry_method"] == "MANUAL_HIBOB_FALLBACK"
    assert payload["fallback_reason"] == "CARD_NOT_RESOLVED"


# --- 5. logs: masked only -------------------------------------------------------------------


@pytest.mark.parametrize("update_error", [None, CardRegistrationRejectedError("HTTP 409"),
                                          CardResolverUnavailableError("timeout")])
def test_raw_card_is_never_logged_unmasked(caplog, update_error):
    caplog.set_level(logging.DEBUG)
    service, *_ = _setup(resolver=_unknown_card_resolver(update_error=update_error))
    ticket = _ticket(service)
    service.lookup_manual(ticket, "99999")
    service.register_card_and_confirm(ticket, TEST_EMPLOYEE)

    formatted = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert RAW not in caplog.text and RAW not in formatted
    events = {r.getMessage(): r for r in caplog.records}
    key = "card_registered" if update_error is None else "card_registration_failed"
    assert events[key].card == "****5678"


# --- 13/14. failed registration: nothing written, nothing mirrored --------------------------


@pytest.mark.parametrize("error", [
    CardResolverUnavailableError("Card Resolver update timed out"),
    CardResolverUnavailableError("Card Resolver update connection failed: ConnectError"),
    CardRegistrationRejectedError("HTTP 409"),
    CardRegistrationRejectedError("HTTP 422"),
    CardResolverUnavailableError("HTTP 503"),
    CardResolverResponseError("non-JSON body"),
    CardResolverResponseError("returned a different hibob_id"),
], ids=["timeout", "network", "4xx-conflict", "4xx-validation", "5xx", "invalid-body", "id-mismatch"])
def test_failed_update_creates_no_event_and_no_mirror(error):
    service, resolver, scans, mirror = _setup(resolver=_unknown_card_resolver(update_error=error))
    result = service.register_card_and_confirm(_ticket(service), TEST_EMPLOYEE)
    assert result.outcome is ScanOutcome.CARD_REGISTRATION_FAILED and result.card_registered is False
    assert result.event_id is None
    assert resolver.updates == [(RAW, "99999")]
    assert scans.events == [] and mirror.sent == []


# --- 15. hibob_id mismatch, through the real HTTP client -----------------------------------


def _http_resolver(update_handler) -> CardResolverClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/resolve"):
            return httpx.Response(404, json={"detail": "not found"})
        return update_handler(request)

    return CardResolverClient(
        "https://resolver.test/card-resolver/api/v1/cards/resolve", "token", timeout_seconds=5,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        update_url="https://resolver.test/card-resolver/api/v1/cards/update",
        update_credentials=("kiosk", "pw"))


def test_response_hibob_id_mismatch_is_rejected_end_to_end():
    client = _http_resolver(lambda r: httpx.Response(200, json={
        "status": "unchanged", "card_number_raw": RAW, "hibob_id": "11111"}))
    service, _, scans, mirror = _setup(resolver=client)
    result = service.register_card_and_confirm(_ticket(service), TEST_EMPLOYEE)
    assert result.outcome is ScanOutcome.CARD_REGISTRATION_FAILED
    assert scans.events == [] and mirror.sent == []


def test_successful_update_end_to_end_with_real_client():
    seen = {}

    def update(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"status": "unchanged", "card_number_raw": RAW,
                                         "decoder": {"format": "h10304-reversed"}, "hibob_id": "99999"})

    service, _, scans, mirror = _setup(resolver=_http_resolver(update))
    result = service.register_card_and_confirm(_ticket(service), TEST_EMPLOYEE)
    assert result.outcome is ScanOutcome.RECORDED and len(scans.events) == 1 and len(mirror.sent) == 1
    assert seen == {"card_number_raw": RAW, "hibob_id": "99999", "overwrite": False}


# --- 16. duplicate after a successful registration -----------------------------------------


def test_duplicate_after_registration_creates_no_second_event():
    clock = FakeClock()
    scans, mirror = InMemoryScanRepository(), FakeMirror()
    # The employee checked in moments ago with another (known) card on this device.
    _service(clock=clock, scans=scans, mirror=mirror).process_scan("000011112222")
    clock.advance(1.0)

    service, resolver, _, _ = _setup(clock=clock, scans=scans, mirror=mirror)
    result = service.register_card_and_confirm(_ticket(service), TEST_EMPLOYEE)
    assert resolver.updates == [(RAW, "99999")]  # the card mapping itself still happened
    assert result.outcome is ScanOutcome.DUPLICATE and result.card_registered is True
    assert len(scans.events) == 1 and len(mirror.sent) == 1


# --- 17/18. afterwards the card follows the normal CARD route -----------------------------


def test_registered_card_then_resolves_through_normal_card_flow():
    clock = FakeClock()
    service, resolver, scans, mirror = _setup(clock=clock)
    service.register_card_and_confirm(_ticket(service), TEST_EMPLOYEE)
    clock.advance(10)  # outside the duplicate window

    result = service.process_scan(RAW)
    assert result.outcome is ScanOutcome.RECORDED and result.fallback_ticket is None
    assert result.entry_method is EntryMethod.CARD and result.card_registered is False
    second = scans.events[-1]
    assert second.entry_method is EntryMethod.CARD and second.fallback_reason is None
    # Card Resolver has no name for this mapping; HiBob supplies it (migration 003).
    assert second.card_resolver_employee_name is None and second.card_resolver_dataset_id == 99
    assert second.employee == TEST_EMPLOYEE and result.display_name == "Test Employee"
    assert result.name_matches_resolver is None
    assert len(resolver.updates) == 1  # no re-registration on the normal route
    payload = build_scan_payload(second, 2, ZoneInfo("America/Bogota"))
    assert payload["entry_method"] == "CARD" and payload["fallback_reason"] is None


def test_normal_card_flow_never_calls_update():
    service, resolver, scans, mirror = _setup(resolver=FakeResolver())
    result = service.process_scan(RAW)
    assert result.outcome is ScanOutcome.RECORDED and result.entry_method is EntryMethod.CARD
    assert resolver.updates == [] and len(scans.events) == 1 and len(mirror.sent) == 1


# --- migration 003: CARD resolution without a Card Resolver name ----------------------------


def test_card_event_without_resolver_name_is_valid_but_needs_dataset():
    from src.models.scan import HibobLookupStatus, NewScanEvent
    from tests.test_scan_service import DEVICE, T0

    event = NewScanEvent(hibob_id="99999", hibob_lookup_status=HibobLookupStatus.FOUND,
                         employee=TEST_EMPLOYEE, device_id=DEVICE, scanned_at=T0,
                         card_resolver_employee_name=None, card_resolver_dataset_id=99)
    assert event.display_name == "Test Employee"
    with pytest.raises(ValueError):
        NewScanEvent(hibob_id="99999", hibob_lookup_status=HibobLookupStatus.FOUND,
                     employee=TEST_EMPLOYEE, device_id=DEVICE, scanned_at=T0,
                     card_resolver_employee_name=None, card_resolver_dataset_id=None)


def test_nameless_card_scan_persists_null_resolver_name_and_mirrors_hibob_name():
    from src.models.scan import CardResolution

    resolver = FakeResolver(resolution=CardResolution("99999", None, 99))
    service, _, scans, mirror = _setup(resolver=resolver)
    result = service.process_scan(RAW)
    assert result.outcome is ScanOutcome.RECORDED and result.entry_method is EntryMethod.CARD
    (event,) = scans.events

    db = FakeDatabase(responder=lambda sql, params: [(1,)] if sql == INSERT_SCAN_SQL else [])
    ScanRepository(db.connection).record_if_not_duplicate(event, 2)
    insert = next(s for s in db.executed if s.sql == INSERT_SCAN_SQL)
    assert insert.params["card_resolver_employee_name"] is None
    assert insert.params["employee_name"] == "Test Employee"

    ((mirrored, event_id),) = mirror.sent
    payload = build_scan_payload(mirrored, event_id, ZoneInfo("America/Bogota"))
    assert payload["employee_name"] == "Test Employee" and payload["entry_method"] == "CARD"


def test_nameless_card_scan_without_hibob_row_is_still_recorded():
    from src.models.scan import CardResolution, ScanWarning

    service, _, scans, _ = _setup(resolver=FakeResolver(resolution=CardResolution("99999", None, 99)),
                                  employees=FakeEmployeeRepository(employee=None))
    result = service.process_scan(RAW)
    assert result.outcome is ScanOutcome.RECORDED and result.warnings == (ScanWarning.HIBOB_NOT_FOUND,)
    assert scans.events[0].card_resolver_employee_name is None


def test_migration_003_only_relaxes_the_card_name_condition():
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "sql" /
           "003_allow_resolved_card_without_resolver_name.sql").read_text(encoding="utf-8")
    body = chr(10).join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    card_branch = body.split("entry_method = 'CARD'")[1].split("OR (")[0]
    assert "card_resolver_employee_name" not in card_branch
    assert "card_resolver_dataset_id IS NOT NULL" in card_branch and "fallback_reason IS NULL" in card_branch
    manual_branch = body.split("entry_method = 'MANUAL_HIBOB_FALLBACK'")[1]
    for condition in ("fallback_reason = 'CARD_NOT_RESOLVED'", "card_resolver_employee_name IS NULL",
                      "card_resolver_dataset_id IS NULL", "hibob_lookup_status = 'FOUND'"):
        assert condition in manual_branch
    assert body.count("BEGIN;") == 1 and body.count("COMMIT;") == 1
