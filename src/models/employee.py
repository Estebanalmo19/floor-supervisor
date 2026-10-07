"""Employee data as read from hibob_etl.employees (runtime subset only)."""

from __future__ import annotations

from dataclasses import dataclass

ACTIVE_EMPLOYMENT_STATUS = "Active"


@dataclass(frozen=True)
class Employee:
    employee_id: str
    employee_name: str
    job_title: str | None
    department: str | None
    site: str | None
    employment_status: str | None
    lifecycle_status: str | None

    @property
    def is_active(self) -> bool:
        return self.employment_status == ACTIVE_EMPLOYMENT_STATUS
