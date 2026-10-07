"""Tests for logging helpers, the focus script and model invariants."""

import json
import logging
from datetime import UTC, datetime

import pytest

from src.logging_setup import JsonFormatter, mask_card_value
from src.models.employee import Employee
from src.models.scan import HibobLookupStatus, NewScanEvent
from src.ui.focus import build_autofocus_script

T0 = datetime(2026, 10, 7, 13, 45, 30, tzinfo=UTC)  # 08:45:30 in Bogota
TEST_EMPLOYEE = Employee("99999", "Test Employee", "Game Presenter", "Operations",
                 "Test Site", "Active", "Employed")


# --- logging -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "masked"),
    [("000012345678", "****5678"), ("12345678", "****5678"), ("1234567", "****"), ("", "")],
)
def test_mask_card_value(raw, masked):
    assert mask_card_value(raw) == masked


def test_json_formatter_includes_extra_fields():
    record = logging.LogRecord("src.test", logging.INFO, __file__, 1, "scan_processed", None, None)
    record.outcome = "RECORDED"
    record.hibob_id = "99999"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "scan_processed"
    assert payload["level"] == "INFO"
    assert payload["outcome"] == "RECORDED"
    assert payload["hibob_id"] == "99999"


# --- focus script ------------------------------------------------------------


def test_autofocus_script_targets_label():
    script = build_autofocus_script("Card scan")
    assert 'const LABEL = "Card scan";' in script
    assert "input.focus" in script


def test_autofocus_script_cannot_break_out_of_script_tag():
    script = build_autofocus_script('x</script><script>alert(1)</script>')
    assert script.count("</script>") == 1


@pytest.mark.parametrize(("label", "interval"), [("", 300), ("Card scan", 10)])
def test_autofocus_script_validates_arguments(label, interval):
    with pytest.raises(ValueError):
        build_autofocus_script(label, interval)


# --- model invariants ----------------------------------------------------------


def _event(**overrides):
    values = dict(hibob_id="99999", card_resolver_employee_name="Test Employee", card_resolver_dataset_id=99,
                  hibob_lookup_status=HibobLookupStatus.FOUND, employee=TEST_EMPLOYEE,
                  device_id="TABLET", scanned_at=T0)
    values.update(overrides)
    return NewScanEvent(**values)


def test_scan_event_requires_aware_timestamp():
    with pytest.raises(ValueError):
        _event(scanned_at=datetime(2026, 10, 7, 8, 0, 0))


@pytest.mark.parametrize(
    ("status", "employee"),
    [(HibobLookupStatus.FOUND, None), (HibobLookupStatus.NOT_FOUND, TEST_EMPLOYEE)],
)
def test_scan_event_status_must_match_employee_presence(status, employee):
    with pytest.raises(ValueError):
        _event(hibob_lookup_status=status, employee=employee)
