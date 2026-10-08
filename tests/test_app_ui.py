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


# --- manual HiBob fallback (AppTest) -------------------------------------------------------

from src.models.scan import (  # noqa: E402
    EntryMethod,
    FallbackTicket,
    ManualLookupResult,
    ManualLookupStatus,
)


class FakeFallbackService(FakeScanService):
    """process_scan -> CARD_NOT_RESOLVED with a ticket; scripted manual lookup/confirm."""

    def __init__(self, lookup_status=ManualLookupStatus.FOUND):
        super().__init__(outcome=ScanOutcome.CARD_NOT_RESOLVED)
        self.lookup_status = lookup_status
        self.lookups: list[str] = []
        self.confirms: list[str] = []

    def process_scan(self, raw):
        self.calls.append(raw)
        if self.outcome is ScanOutcome.CARD_NOT_RESOLVED:
            return ScanResult(outcome=ScanOutcome.CARD_NOT_RESOLVED, scanned_at=T0,
                              fallback_ticket=FallbackTicket(issued_at=datetime.now(UTC),
                                                             device_id="FLOOR_SUPERVISOR_TABLET_01"))
        return super().process_scan(raw)

    def lookup_manual(self, ticket, hibob_id):
        assert isinstance(ticket, FallbackTicket)
        self.lookups.append(hibob_id)
        employee = TEST_EMPLOYEE if self.lookup_status is ManualLookupStatus.FOUND else None
        return ManualLookupResult(status=self.lookup_status, employee=employee)

    def confirm_manual(self, ticket, employee):
        assert isinstance(ticket, FallbackTicket)
        self.confirms.append(employee.employee_id)
        return ScanResult(outcome=ScanOutcome.RECORDED, scanned_at=T0, hibob_id=employee.employee_id,
                          display_name=employee.employee_name, employee=employee, event_id=77,
                          entry_method=EntryMethod.MANUAL_HIBOB_FALLBACK)


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


def test_manual_fallback_not_offered_on_normal_screen(fake_service):
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    assert _button(at, "fs_btn_fallback") is None
    at = _scan(at, "000012345678")  # RECORDED
    assert _button(at, "fs_btn_fallback") is None
    assert "Enter HiBob ID" not in _markup(at)


def test_card_not_resolved_offers_manual_fallback(fallback_service):
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, "000000000001")
    assert "Card not recognized" in _markup(at)
    assert "Try scanning the card again or enter the employee HiBob ID." in _markup(at)
    assert _button(at, "fs_btn_fallback").label == "Enter HiBob ID manually"


def test_full_manual_flow_confirm_records(fallback_service):
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, "000000000001")
    at = _button(at, "fs_btn_fallback").click().run()
    assert "Enter HiBob ID" in _markup(at) and len(at.text_input) == 1  # scanner input not rendered

    at.text_input[0].input("99999")
    at = _button(at, "fs_btn_find").click().run()
    preview = _markup(at)
    assert fallback_service.lookups == ["99999"] and fallback_service.confirms == []  # preview never writes
    for text in ("Test Employee", "Game Presenter", "Test Site"):
        assert text in preview
    assert "Active" not in preview and "Employed" not in preview

    at = _button(at, "fs_btn_confirm").click().run()
    html = _markup(at)
    assert fallback_service.confirms == ["99999"]
    assert "Scan registered" in html and "Manual entry" in html and "fs-tone-success" in html
    assert len(at.text_input) == 1 and at.text_input[0].label == "Card scan"  # back to the scanner
    assert _button(at, "fs_btn_fallback") is None  # ticket consumed


@pytest.mark.parametrize("cancel_key", ["fs_btn_cancel_entry", "fs_btn_cancel_preview"])
def test_cancel_does_not_write(fallback_service, cancel_key):
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, "000000000001")
    at = _button(at, "fs_btn_fallback").click().run()
    if cancel_key == "fs_btn_cancel_preview":
        at.text_input[0].input("99999")
        at = _button(at, "fs_btn_find").click().run()
    at = _button(at, cancel_key).click().run()
    assert fallback_service.confirms == []
    assert at.text_input[0].label == "Card scan" and _button(at, "fs_btn_fallback") is None


def test_employee_not_found_stays_on_manual_screen_without_writing(fallback_service):
    fallback_service.lookup_status = ManualLookupStatus.EMPLOYEE_NOT_FOUND
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, "000000000001")
    at = _button(at, "fs_btn_fallback").click().run()
    at.text_input[0].input("12345")
    at = _button(at, "fs_btn_find").click().run()
    assert "Employee not found. No scan was recorded." in _markup(at)
    assert fallback_service.confirms == [] and _button(at, "fs_btn_confirm") is None


def test_data_integrity_error_returns_to_scanner_without_writing(fallback_service):
    fallback_service.lookup_status = ManualLookupStatus.DATA_INTEGRITY_ERROR
    at = AppTest.from_file(APP_PATH, default_timeout=10).run()
    at = _scan(at, "000000000001")
    at = _button(at, "fs_btn_fallback").click().run()
    at.text_input[0].input("99999")
    at = _button(at, "fs_btn_find").click().run()
    assert "Code E03" in _markup(at) and fallback_service.confirms == []
    assert at.text_input[0].label == "Card scan"
