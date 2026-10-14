"""Streamlit AppTest checks for app.py with a fake scan service (no network, no PostgreSQL)."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import src.bootstrap
from src.models.employee import Employee
from src.models.scan import ScanOutcome, ScanResult

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")
T0 = datetime(2026, 10, 7, 13, 45, 30, tzinfo=UTC)
TEST_EMPLOYEE = Employee("99999", "Test Employee", "Game Presenter", "Operations",
                 "Test Site", "Active", "Employed")


class FakeScanService:
    def __init__(self, outcome=ScanOutcome.RECORDED, error=None):
        self.calls: list[str] = []
        self.outcome = outcome
        self.error = error

    def process_scan(self, raw: str) -> ScanResult:
        self.calls.append(raw)
        if self.error:
            raise self.error
        return ScanResult(outcome=self.outcome, scanned_at=T0, hibob_id="99999",
                          display_name=TEST_EMPLOYEE.employee_name, employee=TEST_EMPLOYEE, event_id=len(self.calls))


@pytest.fixture
def fake_service(monkeypatch):
    service = FakeScanService()
    context = SimpleNamespace(
        settings=SimpleNamespace(timezone=ZoneInfo("America/Bogota"),
                                 device_id="FLOOR_SUPERVISOR_TABLET_01"),
        scan_service=service,
    )
    monkeypatch.setattr(src.bootstrap, "build_app_context", lambda settings: context)
    monkeypatch.setattr("src.config.load_settings", lambda *a, **k: None)
    st.cache_resource.clear()
    yield service
    st.cache_resource.clear()


def _html(at: AppTest) -> str:
    return " ".join(element.proto.body for element in at.get("html"))


def _scan(at: AppTest, value: str) -> AppTest:
    (scan_input,) = at.text_input
    return scan_input.input(value).run()


def test_idle_screen_renders_kiosk_without_default_alert_boxes(fake_service):
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    html = _html(at)
    assert not at.exception
    assert "Scan your card" in html and "System ready" in html
    assert 'class="fs-layer fs-result' not in html
    assert not (at.success or at.error or at.warning or at.info)


def test_scan_shows_result_and_clears_input(fake_service):
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, "000012345678")
    html = _html(at)
    assert fake_service.calls == ["000012345678"]
    assert "fs-tone-success" in html and "Scan registered" in html
    assert "Test Employee" in html
    assert "000012345678" not in html  # raw card value never rendered
    assert at.text_input[0].value == ""


def test_identical_consecutive_scans_are_both_submitted(fake_service):
    """Regression: the same card value twice must reach the service twice (fresh input key)."""
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    first_key = at.text_input[0].key
    at = _scan(at, "000012345678")
    second_key = at.text_input[0].key
    at = _scan(at, "000012345678")

    assert fake_service.calls == ["000012345678", "000012345678"]
    assert first_key != second_key != at.text_input[0].key
    assert len(at.text_input) == 1


def test_inactive_employee_renders_as_plain_success(fake_service, monkeypatch):
    from src.models.scan import ScanWarning

    inactive = Employee("99999", "Test Employee", "Game Presenter", "Operations",
                        "Test Site", "Inactive", "Terminated")
    monkeypatch.setattr(fake_service, "process_scan", lambda raw: ScanResult(
        outcome=ScanOutcome.RECORDED, scanned_at=T0, hibob_id="99999",
        display_name=inactive.employee_name, employee=inactive,
        warnings=(ScanWarning.EMPLOYEE_NOT_ACTIVE,), event_id=1))
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, "000012345678")
    # Rendered markup only (exclude the stylesheet element, which names every tone class).
    stage = " ".join(e.proto.body for e in at.get("html") if not e.proto.body.startswith("<style>"))
    assert "fs-tone-success" in stage and "Scan registered" in stage
    assert "fs-tone-warning" not in stage
    assert "Inactive" not in stage and "Terminated" not in stage


def test_unexpected_service_error_shows_generic_message(fake_service):
    fake_service.error = RuntimeError("internal detail: connection to 10.0.0.5 refused")
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, "000012345678")
    html = _html(at)
    assert not at.exception
    assert "Scan could not be completed" in html and "Code E99" in html
    assert "internal detail" not in html and "10.0.0.5" not in html


# --- manual HiBob fallback: register card & check in (AppTest) ------------------------------

from src.models.scan import (  # noqa: E402
    EntryMethod,
    FallbackTicket,
    ManualLookupResult,
    ManualLookupStatus,
)

UNKNOWN_RAW = "000000098765"


class FakeFallbackService(FakeScanService):
    """process_scan -> CARD_NOT_RESOLVED with a ticket; scripted manual lookup/registration."""

    def __init__(self, lookup_status=ManualLookupStatus.FOUND):
        super().__init__(outcome=ScanOutcome.CARD_NOT_RESOLVED)
        self.lookup_status = lookup_status
        self.lookups: list[str] = []
        self.registrations: list[tuple[str, str]] = []  # (card_raw, hibob_id)
        self.registration_outcome = ScanOutcome.RECORDED

    def process_scan(self, raw):
        self.calls.append(raw)
        if self.outcome is ScanOutcome.CARD_NOT_RESOLVED:
            return ScanResult(outcome=ScanOutcome.CARD_NOT_RESOLVED, scanned_at=T0,
                              fallback_ticket=FallbackTicket(issued_at=datetime.now(UTC),
                                                             device_id="FLOOR_SUPERVISOR_TABLET_01",
                                                             card_raw=raw))
        return super().process_scan(raw)

    def lookup_manual(self, ticket, hibob_id):
        assert isinstance(ticket, FallbackTicket)
        self.lookups.append(hibob_id)
        employee = TEST_EMPLOYEE if self.lookup_status is ManualLookupStatus.FOUND else None
        return ManualLookupResult(status=self.lookup_status, employee=employee)

    def register_card_and_confirm(self, ticket, employee):
        assert isinstance(ticket, FallbackTicket)
        self.registrations.append((ticket.card_raw, employee.employee_id))
        if self.registration_outcome is ScanOutcome.CARD_REGISTRATION_FAILED:
            return ScanResult(outcome=ScanOutcome.CARD_REGISTRATION_FAILED, scanned_at=T0,
                              hibob_id=employee.employee_id, display_name=employee.employee_name,
                              entry_method=EntryMethod.MANUAL_HIBOB_FALLBACK)
        return ScanResult(outcome=self.registration_outcome, scanned_at=T0, hibob_id=employee.employee_id,
                          display_name=employee.employee_name, employee=employee, event_id=77,
                          entry_method=EntryMethod.MANUAL_HIBOB_FALLBACK, card_registered=True)


@pytest.fixture
def fallback_service(monkeypatch):
    service = FakeFallbackService()
    context = SimpleNamespace(
        settings=SimpleNamespace(timezone=ZoneInfo("America/Bogota"), device_id="FLOOR_SUPERVISOR_TABLET_01"),
        scan_service=service,
    )
    monkeypatch.setattr(src.bootstrap, "build_app_context", lambda settings: context)
    monkeypatch.setattr("src.config.load_settings", lambda *a, **k: None)
    st.cache_resource.clear()
    yield service
    st.cache_resource.clear()


def _button(at, key):
    matches = [b for b in at.button if b.key == key]
    return matches[0] if matches else None


def _markup(at):
    return " ".join(e.proto.body for e in at.get("html") if not e.proto.body.startswith("<style>"))


def _all_rendered_text(at) -> str:
    """Markup plus every widget label/value/placeholder the browser receives."""
    parts = [_markup(at)]
    for widget in list(at.text_input) + list(at.button):
        parts += [str(getattr(widget, "label", "")), str(getattr(widget, "value", "")),
                  str(getattr(widget.proto, "placeholder", ""))]
    return " ".join(parts)


def _ticket_in_session(at):
    return at.session_state["fallback_ticket"] if "fallback_ticket" in at.session_state else None


def _to_preview(at):
    at = _scan(at, UNKNOWN_RAW)
    at = _button(at, "fs_btn_fallback").click().run()
    at.text_input[0].input("99999")
    return _button(at, "fs_btn_find").click().run()


def test_manual_fallback_not_offered_on_normal_screen(fake_service):
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    assert _button(at, "fs_btn_fallback") is None
    at = _scan(at, "000012345678")  # RECORDED
    assert _button(at, "fs_btn_fallback") is None
    assert "Enter HiBob ID" not in _markup(at)


def test_card_not_resolved_offers_registration(fallback_service):
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, UNKNOWN_RAW)
    html = _markup(at)
    assert "Card not recognized" in html
    assert "This card is not registered yet. Enter your HiBob ID to register this card." in html
    assert _button(at, "fs_btn_fallback").label == "Enter HiBob ID"


def test_raw_card_is_kept_only_in_server_session_and_never_rendered(fallback_service):
    """Requirements 1 & 2: the ticket in session state holds the raw card; no screen shows it."""
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, UNKNOWN_RAW)
    assert _ticket_in_session(at).card_raw == UNKNOWN_RAW
    assert UNKNOWN_RAW not in _all_rendered_text(at)

    at = _button(at, "fs_btn_fallback").click().run()
    assert UNKNOWN_RAW not in _all_rendered_text(at)
    at.text_input[0].input("99999")
    at = _button(at, "fs_btn_find").click().run()
    assert UNKNOWN_RAW not in _all_rendered_text(at)  # preview
    at = _button(at, "fs_btn_register").click().run()
    assert UNKNOWN_RAW not in _all_rendered_text(at)  # success


def test_full_flow_preview_then_register_and_check_in(fallback_service):
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, UNKNOWN_RAW)
    at = _button(at, "fs_btn_fallback").click().run()
    assert "HiBob ID" in _markup(at) and len(at.text_input) == 1  # scanner input not rendered
    assert _button(at, "fs_btn_find").label == "Find employee"

    at.text_input[0].input("99999")
    at = _button(at, "fs_btn_find").click().run()
    preview = _markup(at)
    assert fallback_service.lookups == ["99999"]
    assert fallback_service.registrations == []  # preview never registers or records
    for text in ("Test Employee", "Game Presenter", "Test Site",
                 "This card will be linked to this employee."):
        assert text in preview
    assert "Active" not in preview and "Employed" not in preview
    assert _button(at, "fs_btn_register").label == "Register card & check in"

    at = _button(at, "fs_btn_register").click().run()
    html = _markup(at)
    assert fallback_service.registrations == [(UNKNOWN_RAW, "99999")]
    assert "✓ Card registered" in html and "✓ Scan registered" in html and "fs-tone-success" in html
    assert len(at.text_input) == 1 and at.text_input[0].label == "Card scan"  # back to the scanner
    assert _ticket_in_session(at) is None  # raw card dropped after success
    assert _button(at, "fs_btn_fallback") is None


def test_registration_failure_stays_on_preview_and_allows_retry(fallback_service):
    fallback_service.registration_outcome = ScanOutcome.CARD_REGISTRATION_FAILED
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _to_preview(at)
    at = _button(at, "fs_btn_register").click().run()

    html = _markup(at)
    assert "Card could not be registered. Please try again." in html
    assert "HTTP" not in html and "Card Resolver" not in html  # no internal details
    assert _button(at, "fs_btn_register") is not None  # still on the preview
    assert _ticket_in_session(at).card_raw == UNKNOWN_RAW  # kept for the retry only

    fallback_service.registration_outcome = ScanOutcome.RECORDED
    at = _button(at, "fs_btn_register").click().run()
    assert fallback_service.registrations == [(UNKNOWN_RAW, "99999")] * 2
    assert "✓ Card registered" in _markup(at) and _ticket_in_session(at) is None


@pytest.mark.parametrize("cancel_key", ["fs_btn_cancel_entry", "fs_btn_cancel_preview"])
def test_cancel_clears_fallback_and_card_without_writing(fallback_service, cancel_key):
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, UNKNOWN_RAW)
    at = _button(at, "fs_btn_fallback").click().run()
    if cancel_key == "fs_btn_cancel_preview":
        at.text_input[0].input("99999")
        at = _button(at, "fs_btn_find").click().run()
    at = _button(at, cancel_key).click().run()
    assert fallback_service.registrations == []
    assert _ticket_in_session(at) is None and "manual_employee" not in at.session_state
    assert at.text_input[0].label == "Card scan" and _button(at, "fs_btn_fallback") is None


def test_new_scan_clears_previous_card(fallback_service):
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, UNKNOWN_RAW)
    assert _ticket_in_session(at) is not None
    fallback_service.outcome = ScanOutcome.RECORDED
    at = _scan(at, "000012345678")
    assert _ticket_in_session(at) is None


def test_lapsed_offer_clears_card(fallback_service):
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, UNKNOWN_RAW)
    offered_at, sequence = at.session_state["fallback_offer"]
    at.session_state["fallback_offer"] = (offered_at - 21, sequence)  # offer shown > 20 s ago
    at = at.run()
    assert _ticket_in_session(at) is None and _button(at, "fs_btn_fallback") is None


def test_employee_not_found_stays_on_manual_screen_without_registering(fallback_service):
    fallback_service.lookup_status = ManualLookupStatus.EMPLOYEE_NOT_FOUND
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, UNKNOWN_RAW)
    at = _button(at, "fs_btn_fallback").click().run()
    at.text_input[0].input("12345")
    at = _button(at, "fs_btn_find").click().run()
    assert "Employee not found. No scan was recorded." in _markup(at)
    assert fallback_service.registrations == [] and _button(at, "fs_btn_register") is None


def test_data_integrity_error_returns_to_scanner_without_registering(fallback_service):
    fallback_service.lookup_status = ManualLookupStatus.DATA_INTEGRITY_ERROR
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, UNKNOWN_RAW)
    at = _button(at, "fs_btn_fallback").click().run()
    at.text_input[0].input("99999")
    at = _button(at, "fs_btn_find").click().run()
    assert "Code E03" in _markup(at) and fallback_service.registrations == []
    assert at.text_input[0].label == "Card scan" and _ticket_in_session(at) is None
