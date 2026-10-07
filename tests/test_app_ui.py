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
