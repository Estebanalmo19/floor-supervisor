from datetime import UTC, datetime

import psycopg
import pytest

from src.exceptions import RepositoryError
from src.models.employee import Employee
from src.models.scan import HibobLookupStatus, NewScanEvent
from src.repositories.scan_repository import (
    ADVISORY_LOCK_SQL,
    FIND_RECENT_SCAN_SQL,
    INSERT_SCAN_SQL,
    ScanRepository,
)
from tests.fakes import FakeDatabase

SCANNED_AT = datetime(2026, 10, 7, 15, 0, 0, tzinfo=UTC)
EMPLOYEE = Employee("99999", "Test Employee", "Game Presenter", "Operations",
                    "Test Site", "Active", "Employed")


def _event(found: bool = True) -> NewScanEvent:
    return NewScanEvent(
        hibob_id="99999",
        card_resolver_employee_name="Test Employee",
        card_resolver_dataset_id=99,
        hibob_lookup_status=HibobLookupStatus.FOUND if found else HibobLookupStatus.NOT_FOUND,
        employee=EMPLOYEE if found else None,
        device_id="FLOOR_SUPERVISOR_TABLET_01",
        scanned_at=SCANNED_AT,
    )


def _db(existing_id=None, new_id=101, fail_on=None):
    def responder(sql, params):
        if sql == FIND_RECENT_SCAN_SQL:
            return [(existing_id,)] if existing_id is not None else []
        if sql == INSERT_SCAN_SQL:
            return [(new_id,)]
        return []

    return FakeDatabase(responder=responder, fail_on=fail_on or (lambda sql: None))


def test_inserts_when_no_recent_scan():
    db = _db()
    result = ScanRepository(db.connection).record_if_not_duplicate(_event(), 2)

    assert result.event_id == 101 and not result.is_duplicate
    assert [s.sql for s in db.executed] == [ADVISORY_LOCK_SQL, FIND_RECENT_SCAN_SQL, INSERT_SCAN_SQL]
    assert db.events == ["tx_begin", "tx_commit", "connection_commit"]


def test_lock_and_duplicate_check_use_device_and_employee():
    db = _db()
    ScanRepository(db.connection).record_if_not_duplicate(_event(), 2)

    lock, find, _ = db.executed
    assert lock.params == {"lock_key": "FLOOR_SUPERVISOR_TABLET_01|99999"}
    assert find.params == {
        "device_id": "FLOOR_SUPERVISOR_TABLET_01",
        "hibob_id": "99999",
        "scanned_at": SCANNED_AT,
        "window_seconds": 2.0,
    }


def test_returns_duplicate_without_inserting():
    db = _db(existing_id=55)
    result = ScanRepository(db.connection).record_if_not_duplicate(_event(), 2)

    assert result.event_id == 55 and result.is_duplicate
    assert INSERT_SCAN_SQL not in [s.sql for s in db.executed]
    assert "tx_commit" in db.events


def test_window_zero_disables_duplicate_detection():
    db = _db(existing_id=55)
    result = ScanRepository(db.connection).record_if_not_duplicate(_event(), 0)

    assert not result.is_duplicate
    assert [s.sql for s in db.executed] == [INSERT_SCAN_SQL]


def test_negative_window_rejected():
    with pytest.raises(ValueError):
        ScanRepository(_db().connection).record_if_not_duplicate(_event(), -1)


def test_insert_params_for_found_employee():
    db = _db()
    ScanRepository(db.connection).record_if_not_duplicate(_event(found=True), 2)

    params = db.executed[-1].params
    assert params == {
        "hibob_id": "99999",
        "card_resolver_employee_name": "Test Employee",
        "card_resolver_dataset_id": 99,
        "hibob_lookup_status": "FOUND",
        "employee_name": "Test Employee",
        "job_title": "Game Presenter",
        "department": "Operations",
        "site": "Test Site",
        "employment_status": "Active",
        "lifecycle_status": "Employed",
        "device_id": "FLOOR_SUPERVISOR_TABLET_01",
        "scanned_at": SCANNED_AT,
    }


def test_insert_params_for_not_found_employee_have_null_snapshot():
    db = _db()
    ScanRepository(db.connection).record_if_not_duplicate(_event(found=False), 2)

    params = db.executed[-1].params
    assert params["hibob_lookup_status"] == "NOT_FOUND"
    assert params["card_resolver_employee_name"] == "Test Employee"
    for column in ("employee_name", "job_title", "department", "site",
                   "employment_status", "lifecycle_status"):
        assert params[column] is None


def test_sql_is_schema_qualified_and_stores_no_card_credentials():
    for sql in (FIND_RECENT_SCAN_SQL, INSERT_SCAN_SQL):
        assert "floor_supervisor.scan_event" in sql
    for forbidden in ("raw_card", "facility_code", "card_number"):
        assert forbidden not in INSERT_SCAN_SQL


def test_database_error_rolls_back_and_is_wrapped():
    db = _db(fail_on=lambda sql: psycopg.errors.CheckViolation("boom") if sql == INSERT_SCAN_SQL else None)
    with pytest.raises(RepositoryError):
        ScanRepository(db.connection).record_if_not_duplicate(_event(), 2)
    assert "tx_rollback" in db.events
    assert "tx_commit" not in db.events
