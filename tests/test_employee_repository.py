import psycopg
import pytest

from src.exceptions import EmployeeDataIntegrityError, RepositoryError
from src.repositories.employee_repository import (
    GET_EMPLOYEE_SQL,
    SET_READ_ONLY_SQL,
    EmployeeRepository,
)
from tests.fakes import FakeDatabase

EMPLOYEE_ROW = {
    "employee_id": "99999",
    "employee_name": "Test Employee",
    "job_title": "Game Presenter",
    "department": "Operations",
    "site": "Test Site",
    "employment_status": "Active",
    "lifecycle_status": "Employed",
}


def _db_returning(rows):
    return FakeDatabase(responder=lambda sql, params: rows if sql == GET_EMPLOYEE_SQL else [])


def test_returns_employee_when_exactly_one_row():
    db = _db_returning([EMPLOYEE_ROW])
    employee = EmployeeRepository(db.connection).get_by_employee_id("99999")

    assert employee is not None
    assert employee.employee_id == "99999"
    assert employee.employee_name == "Test Employee"
    assert employee.job_title == "Game Presenter"
    assert employee.department == "Operations"
    assert employee.site == "Test Site"
    assert employee.is_active


def test_returns_none_when_no_rows():
    db = _db_returning([])
    assert EmployeeRepository(db.connection).get_by_employee_id("99999") is None


def test_more_than_one_row_is_data_integrity_error():
    db = _db_returning([EMPLOYEE_ROW, dict(EMPLOYEE_ROW)])
    with pytest.raises(EmployeeDataIntegrityError):
        EmployeeRepository(db.connection).get_by_employee_id("99999")


def test_query_is_parameterized_read_only_and_schema_qualified():
    db = _db_returning([EMPLOYEE_ROW])
    EmployeeRepository(db.connection).get_by_employee_id("99999")

    assert [s.sql for s in db.executed] == [SET_READ_ONLY_SQL, GET_EMPLOYEE_SQL]
    lookup = db.executed[1]
    assert lookup.params == {"employee_id": "99999"}
    assert "99999" not in lookup.sql
    assert "FROM hibob_etl.employees" in lookup.sql
    assert "WHERE e.raw_work_employeeidincompany = %(employee_id)s" in lookup.sql
    assert "LIMIT 2" in lookup.sql
    assert "*" not in lookup.sql


def test_query_selects_only_runtime_fields():
    selected = GET_EMPLOYEE_SQL.split("FROM")[0]
    for column in (
        "raw_work_employeeidincompany",
        "raw_root_fullname",
        "hr_work_title",
        "hr_work_department",
        "hr_work_site",
        "hr_internal_status",
        "hr_internal_lifecyclestatus",
    ):
        assert column in selected
    assert selected.count(" AS ") == 7


def test_employee_id_is_never_looked_up_by_name():
    assert "fullname" not in GET_EMPLOYEE_SQL.split("WHERE")[1]


def test_employee_id_with_leading_zeros_is_passed_as_string():
    db = _db_returning([])
    EmployeeRepository(db.connection).get_by_employee_id("00123")
    assert db.executed[1].params == {"employee_id": "00123"}


def test_non_string_employee_id_is_rejected():
    db = _db_returning([])
    with pytest.raises(TypeError):
        EmployeeRepository(db.connection).get_by_employee_id(99999)  # type: ignore[arg-type]
    assert db.executed == []


def test_database_error_is_wrapped_without_leaking_details():
    db = FakeDatabase(fail_on=lambda sql: psycopg.OperationalError("password=hunter2 failed"))
    with pytest.raises(RepositoryError) as exc_info:
        EmployeeRepository(db.connection).get_by_employee_id("99999")
    assert "hunter2" not in str(exc_info.value)
    assert "connection_rollback" in db.events
