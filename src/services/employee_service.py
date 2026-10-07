"""Employee lookup against the HiBob snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.models.employee import Employee
from src.models.scan import HibobLookupStatus


class EmployeeLookupRepository(Protocol):
    def get_by_employee_id(self, employee_id: str) -> Employee | None: ...


@dataclass(frozen=True)
class EmployeeLookup:
    status: HibobLookupStatus
    employee: Employee | None


class EmployeeService:
    def __init__(self, repository: EmployeeLookupRepository) -> None:
        self._repository = repository

    def lookup(self, hibob_id: str) -> EmployeeLookup:
        """Look up by HiBob ID only (never by name).

        Propagates EmployeeDataIntegrityError (>1 row) and RepositoryError.
        """
        employee = self._repository.get_by_employee_id(hibob_id)
        if employee is None:
            return EmployeeLookup(status=HibobLookupStatus.NOT_FOUND, employee=None)
        return EmployeeLookup(status=HibobLookupStatus.FOUND, employee=employee)
