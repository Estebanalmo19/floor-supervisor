from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from src.models.employee import Employee
from src.models.scan import ScanOutcome, ScanResult, ScanWarning
from src.ui.presenter import (
    DEFAULT_TIMINGS,
    DUPLICATE_MESSAGE,
    GENERIC_ERROR_MESSAGE,
    GENERIC_ERROR_TITLE,
    HIBOB_NOT_FOUND_MESSAGE,
    UNEXPECTED_ERROR_VIEW,
    ResultTimings,
    present,
)

BOGOTA = ZoneInfo("America/Bogota")
T0 = datetime(2026, 10, 7, 13, 45, 30, tzinfo=UTC)  # 08:45:30 in Bogota
TEST_EMPLOYEE = Employee("99999", "Test Employee", "Game Presenter", "Operations",
                 "Test Site", "Active", "Employed")


def _result(outcome, warnings=(), employee=TEST_EMPLOYEE, name="Test Employee"):
    return ScanResult(outcome=outcome, scanned_at=T0, hibob_id="99999", display_name=name,
                      employee=employee, warnings=warnings, event_id=1)


def test_recorded_is_success_with_name_title_site_and_colombia_time():
    view = present(_result(ScanOutcome.RECORDED), BOGOTA)
    assert view.tone == "success"
    assert view.icon == "check"
    assert view.title == "Scan registered"
    assert view.recorded is True
    assert view.employee_name == "Test Employee"
    assert view.details == ("Game Presenter", "Test Site")
    assert view.time_label == "08:45:30 · Colombia time"
    assert view.message is None


def test_hibob_not_found_is_recorded_with_warning_styling():
    view = present(
        _result(ScanOutcome.RECORDED, (ScanWarning.HIBOB_NOT_FOUND,), employee=None), BOGOTA
    )
    assert view.tone == "warning"
    assert view.icon == "check"  # unambiguously successful
    assert view.title == "Scan registered"
    assert view.recorded is True
    assert view.employee_name == "Test Employee"  # Card Resolver name
    assert view.details == ()
    assert view.message == HIBOB_NOT_FOUND_MESSAGE
    assert "recorded successfully" in view.message


def test_employee_not_active_shows_normal_success_without_status():
    """V1: the HiBob snapshot may be stale, so inactive status is log-only."""
    inactive = Employee("99999", "Test Employee", "Game Presenter", "Operations",
                        "Test Site", "Inactive", "Terminated")
    view = present(
        _result(ScanOutcome.RECORDED, (ScanWarning.EMPLOYEE_NOT_ACTIVE,), employee=inactive), BOGOTA
    )
    active_view = present(_result(ScanOutcome.RECORDED), BOGOTA)

    assert view == active_view  # identical to a normal RECORDED experience
    assert view.tone == "success" and view.message is None
    shown = " ".join(filter(None, (view.title, view.employee_name, *view.details,
                                   view.time_label, view.message, view.reference)))
    for hidden in ("Inactive", "Terminated", "inactive", "status"):
        assert hidden not in shown


def test_duplicate_is_info_and_explains_no_new_record():
    view = present(_result(ScanOutcome.DUPLICATE), BOGOTA)
    assert view.tone == "info"
    assert view.title == "Already registered"
    assert view.message == DUPLICATE_MESSAGE
    assert view.employee_name == "Test Employee"


def test_name_mismatch_is_not_shown():
    mismatch = ScanResult(outcome=ScanOutcome.RECORDED, scanned_at=T0, hibob_id="99999",
                          display_name=TEST_EMPLOYEE.employee_name, employee=TEST_EMPLOYEE, event_id=1,
                          name_matches_resolver=False)
    view = present(mismatch, BOGOTA)
    assert view == present(_result(ScanOutcome.RECORDED), BOGOTA)
    assert view.tone == "success" and view.message is None


def test_name_mismatch_is_not_a_warning_type():
    assert "NAME_MISMATCH" not in {w.value for w in ScanWarning}


def test_card_not_resolved_is_error_not_recorded():
    view = present(ScanResult(outcome=ScanOutcome.CARD_NOT_RESOLVED, scanned_at=T0), BOGOTA)
    assert view.tone == "error"
    assert view.title == "Card not recognized"
    assert view.message == "Scan was not recorded."
    assert view.recorded is False


@pytest.mark.parametrize(
    ("outcome", "reference"),
    [
        (ScanOutcome.RESOLVER_UNAVAILABLE, "E01"),
        (ScanOutcome.RESOLVER_ERROR, "E02"),
        (ScanOutcome.DATA_INTEGRITY_ERROR, "E03"),
        (ScanOutcome.DATABASE_ERROR, "E04"),
    ],
)
def test_operational_errors_are_generic(outcome, reference):
    view = present(ScanResult(outcome=outcome, scanned_at=T0, hibob_id="99999",
                              display_name="Test Employee"), BOGOTA)
    assert view.tone == "error"
    assert view.title == GENERIC_ERROR_TITLE
    assert view.message == GENERIC_ERROR_MESSAGE
    assert view.reference == reference
    assert view.recorded is False
    assert view.employee_name is None  # nothing that suggests the scan went through


def test_invalid_input_asks_to_rescan():
    view = present(ScanResult(outcome=ScanOutcome.INVALID_INPUT, scanned_at=T0), BOGOTA)
    assert view.tone == "warning" and view.recorded is False


def test_every_outcome_has_a_view_and_failures_are_not_recorded():
    for outcome in ScanOutcome:
        view = present(_result(outcome), BOGOTA)
        assert view.recorded is outcome.is_accepted


def test_unexpected_error_view_is_generic():
    assert UNEXPECTED_ERROR_VIEW.tone == "error"
    assert UNEXPECTED_ERROR_VIEW.message == GENERIC_ERROR_MESSAGE
    assert UNEXPECTED_ERROR_VIEW.reference == "E99"


def test_default_timings_match_spec():
    assert DEFAULT_TIMINGS.ttl_for("success") == 3
    assert DEFAULT_TIMINGS.ttl_for("info") == 3
    assert DEFAULT_TIMINGS.ttl_for("warning") == 4
    assert DEFAULT_TIMINGS.ttl_for("error") == 5


def test_timings_are_configurable():
    timings = ResultTimings(success_seconds=1.5, error_seconds=8)
    assert timings.ttl_for("success") == 1.5
    assert timings.ttl_for("error") == 8
