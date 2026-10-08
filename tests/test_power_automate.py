import json
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from src.models.employee import Employee
from src.models.scan import EntryMethod, FallbackReason, HibobLookupStatus, NewScanEvent
from src.services.power_automate import PowerAutomateClient, build_scan_payload

SIGNED_URL = "https://flow.example.test/workflows/abc/triggers/manual/paths/invoke?api-version=1&sig=SUPERSECRETSIG123"
BOGOTA = ZoneInfo("America/Bogota")
T0 = datetime(2026, 10, 7, 17, 13, 5, 976000, tzinfo=UTC)  # 12:13:05.976 in Bogota
EMPLOYEE = Employee("99999", "Test Employee", "Game Presenter", "Operations", "Test Site", "Active", "Employed")

PAYLOAD_KEYS = {
    "schema_version", "source_system", "event_type", "event_id", "hibob_id", "hibob_lookup_status",
    "employee_name", "job_title", "department", "site", "entry_method", "fallback_reason", "device_id",
    "scanned_at_utc", "scanned_at_colombia", "scanned_date_colombia", "scanned_time_colombia",
}


def _card_event(found: bool = True) -> NewScanEvent:
    return NewScanEvent(
        hibob_id="99999", hibob_lookup_status=HibobLookupStatus.FOUND if found else HibobLookupStatus.NOT_FOUND,
        employee=EMPLOYEE if found else None, device_id="FLOOR_SUPERVISOR_TABLET_01", scanned_at=T0,
        card_resolver_employee_name="Test Middle Employee", card_resolver_dataset_id=99,
    )


def _manual_event() -> NewScanEvent:
    return NewScanEvent(
        hibob_id="99999", hibob_lookup_status=HibobLookupStatus.FOUND, employee=EMPLOYEE,
        device_id="FLOOR_SUPERVISOR_TABLET_01", scanned_at=T0,
        entry_method=EntryMethod.MANUAL_HIBOB_FALLBACK, fallback_reason=FallbackReason.CARD_NOT_RESOLVED,
    )


def _client(handler, sleeps=None):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return PowerAutomateClient(SIGNED_URL, timeout_seconds=10, display_timezone=BOGOTA, http_client=http,
                               sleep=(sleeps.append if sleeps is not None else lambda s: None))


# --- payload -----------------------------------------------------------------------


def test_card_payload_exact_fields_and_values():
    payload = build_scan_payload(_card_event(), 123, BOGOTA)
    assert set(payload) == PAYLOAD_KEYS
    assert payload == {
        "schema_version": 1,
        "source_system": "floor_supervisor",
        "event_type": "scan_recorded",
        "event_id": "123",
        "hibob_id": "99999",
        "hibob_lookup_status": "FOUND",
        "employee_name": "Test Employee",
        "job_title": "Game Presenter",
        "department": "Operations",
        "site": "Test Site",
        "entry_method": "CARD",
        "fallback_reason": None,
        "device_id": "FLOOR_SUPERVISOR_TABLET_01",
        "scanned_at_utc": "2026-10-07T17:13:05.976Z",
        "scanned_at_colombia": "2026-10-07T12:13:05.976-05:00",
        "scanned_date_colombia": "2026-10-07",
        "scanned_time_colombia": "12:13:05",
    }


def test_manual_payload_marks_fallback():
    payload = build_scan_payload(_manual_event(), 124, BOGOTA)
    assert payload["entry_method"] == "MANUAL_HIBOB_FALLBACK"
    assert payload["fallback_reason"] == "CARD_NOT_RESOLVED"
    assert payload["employee_name"] == "Test Employee"


def test_not_found_payload_uses_resolver_name_and_nulls():
    payload = build_scan_payload(_card_event(found=False), 125, BOGOTA)
    assert payload["hibob_lookup_status"] == "NOT_FOUND"
    assert payload["employee_name"] == "Test Middle Employee"
    assert payload["job_title"] is None and payload["department"] is None and payload["site"] is None


def test_payload_contains_no_card_or_status_data():
    text = json.dumps(build_scan_payload(_card_event(), 123, BOGOTA))
    for forbidden in ("raw", "facility", "card_number", "decoder", "dataset", "employment_status",
                      "lifecycle_status", "Active", "Employed", "sig", "http"):
        assert forbidden not in text


# --- delivery ------------------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 201, 202])
def test_success_statuses(status):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["method"] = request.method
        return httpx.Response(status, json={"result": "inserted"})

    result = _client(handler).send_scan_event(_card_event(), 123)
    assert result.delivered and result.attempts == 1 and result.http_status == status
    assert seen["method"] == "POST" and seen["body"]["event_id"] == "123"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_non_retryable_failures_are_reported_not_raised(status):
    calls = []
    result = _client(lambda r: calls.append(1) or httpx.Response(status)).send_scan_event(_card_event(), 1)
    assert not result.delivered and result.error_kind == f"HTTP_{status}" and len(calls) == 1


@pytest.mark.parametrize("status", [502, 503, 504])
def test_one_short_retry_for_gateway_errors(status):
    responses = iter([httpx.Response(status), httpx.Response(200)])
    sleeps = []
    result = _client(lambda r: next(responses), sleeps).send_scan_event(_card_event(), 1)
    assert result.delivered and result.attempts == 2 and sleeps == [0.5]


def test_retry_happens_only_once():
    calls = []
    result = _client(lambda r: calls.append(1) or httpx.Response(503)).send_scan_event(_card_event(), 1)
    assert not result.delivered and result.attempts == 2 and len(calls) == 2


def test_connection_error_retried_once_then_reported():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

    result = _client(handler).send_scan_event(_card_event(), 1)
    assert not result.delivered and result.error_kind == "TRANSPORT" and len(calls) == 2


def test_timeout_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout(f"timed out {request.url}", request=request)

    result = _client(handler).send_scan_event(_card_event(), 1)
    assert not result.delivered and result.error_kind == "TIMEOUT" and len(calls) == 1


def test_unexpected_exception_never_escapes():
    def handler(request):
        raise RuntimeError(f"boom {request.url}")

    result = _client(handler).send_scan_event(_card_event(), 1)
    assert not result.delivered and result.error_kind == "UNEXPECTED"


# --- secrecy -------------------------------------------------------------------------


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(200),
    lambda r: httpx.Response(500),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError(f"cannot connect to {r.url}", request=r)),
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout(f"timeout {r.url}", request=r)),
    lambda r: (_ for _ in ()).throw(RuntimeError(f"boom {r.url}")),
])
def test_signed_url_never_in_logs_result_or_repr(caplog, handler):
    caplog.set_level(logging.DEBUG)
    client = _client(handler)
    result = client.send_scan_event(_card_event(), 1)
    assert "SUPERSECRETSIG123" not in caplog.text
    assert "flow.example.test" not in caplog.text
    assert "SUPERSECRETSIG123" not in repr(result) and "SUPERSECRETSIG123" not in repr(client)
    for record in caplog.records:
        assert all("SUPERSECRETSIG123" not in str(v) for v in vars(record).values())


def test_failure_is_logged_as_structured_error(caplog):
    caplog.set_level(logging.DEBUG, logger="src")
    _client(lambda r: httpx.Response(500)).send_scan_event(_card_event(), 42)
    (record,) = [r for r in caplog.records if r.getMessage() == "power_automate_delivery_failed"]
    assert record.levelno == logging.ERROR
    assert record.event_id == 42 and record.http_status == 500 and record.error_kind == "HTTP_500"


def test_success_is_logged_at_info(caplog):
    caplog.set_level(logging.DEBUG, logger="src")
    _client(lambda r: httpx.Response(200)).send_scan_event(_card_event(), 43)
    (record,) = [r for r in caplog.records if r.getMessage() == "power_automate_delivered"]
    assert record.levelno == logging.INFO and record.event_id == 43


def test_httpx_request_logging_is_suppressed_by_app_logging():
    from src.logging_setup import configure_logging

    root_propagate = logging.getLogger("src").propagate
    try:
        configure_logging("DEBUG")
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    finally:
        logging.getLogger("src").propagate = root_propagate
