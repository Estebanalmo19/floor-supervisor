"""Read-only access to hibob_etl.employees.

Floor Supervisor never writes to hibob_etl. Besides the role having SELECT-only
privileges, every lookup runs inside a READ ONLY transaction.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg.rows import dict_row

from src.db import ConnectionFactory
from src.exceptions import EmployeeDataIntegrityError, RepositoryError
from src.models.employee import Employee

logger = logging.getLogger(__name__)

SET_READ_ONLY_SQL = "SET TRANSACTION READ ONLY"

# LIMIT 2 is enough to distinguish 0 / 1 / "more than one" rows.
GET_EMPLOYEE_SQL = """
SELECT e.raw_work_employeeidincompany AS employee_id,
       e.raw_root_fullname            AS employee_name,
       e.hr_work_title                AS job_title,
       e.hr_work_department           AS department,
       e.hr_work_site                 AS site,
       e.hr_internal_status           AS employment_status,
       e.hr_internal_lifecyclestatus  AS lifecycle_status
FROM hibob_etl.employees AS e
WHERE e.raw_work_employeeidincompany = %(employee_id)s
LIMIT 2
"""


class EmployeeRepository:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def get_by_employee_id(self, employee_id: str) -> Employee | None:
        """Return the employee, None when absent.

        Raises EmployeeDataIntegrityError when more than one row matches and
        RepositoryError on any database failure.
        """
        if not isinstance(employee_id, str):
            raise TypeError("employee_id must be a str")

        try:
            with self._connection_factory() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(SET_READ_ONLY_SQL)
                    cur.execute(GET_EMPLOYEE_SQL, {"employee_id": employee_id})
                    rows = cur.fetchall()
        except psycopg.Error as exc:
            raise RepositoryError(f"HiBob employee lookup failed: {type(exc).__name__}") from exc

        if not rows:
            return None
        if len(rows) > 1:
            raise EmployeeDataIntegrityError(
                f"More than one hibob_etl.employees row for employee_id={employee_id}"
            )

        row = rows[0]
        return Employee(
            employee_id=row["employee_id"],
            employee_name=row["employee_name"],
            job_title=row["job_title"],
            department=row["department"],
            site=row["site"],
            employment_status=row["employment_status"],
            lifecycle_status=row["lifecycle_status"],
        )
