"""Persistence for floor_supervisor.scan_event (append-only)."""

from __future__ import annotations

import psycopg

from src.db import ConnectionFactory
from src.exceptions import RepositoryError
from src.models.scan import NewScanEvent, RecordResult

# Serializes concurrent scans of the same (device, employee) pair for the
# duration of the transaction. device_id cannot contain '|', so the key is unambiguous.
ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))"

FIND_RECENT_SCAN_SQL = """
SELECT id
FROM floor_supervisor.scan_event
WHERE device_id = %(device_id)s
  AND hibob_id = %(hibob_id)s
  AND scanned_at > %(scanned_at)s - make_interval(secs => %(window_seconds)s)
ORDER BY scanned_at DESC
LIMIT 1
"""

INSERT_SCAN_SQL = """
INSERT INTO floor_supervisor.scan_event (
    hibob_id,
    card_resolver_employee_name,
    card_resolver_dataset_id,
    hibob_lookup_status,
    employee_name,
    job_title,
    department,
    site,
    employment_status,
    lifecycle_status,
    device_id,
    scanned_at
) VALUES (
    %(hibob_id)s,
    %(card_resolver_employee_name)s,
    %(card_resolver_dataset_id)s,
    %(hibob_lookup_status)s,
    %(employee_name)s,
    %(job_title)s,
    %(department)s,
    %(site)s,
    %(employment_status)s,
    %(lifecycle_status)s,
    %(device_id)s,
    %(scanned_at)s
)
RETURNING id
"""


def advisory_lock_key(device_id: str, hibob_id: str) -> str:
    return f"{device_id}|{hibob_id}"


class ScanRepository:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def record_if_not_duplicate(
        self, event: NewScanEvent, duplicate_window_seconds: int
    ) -> RecordResult:
        """Insert the event unless the same device/employee was recorded within the window.

        Lock, duplicate check and insert run in a single transaction. A window of 0
        disables duplicate detection. Raises RepositoryError on database failure
        (the transaction is rolled back).
        """
        if duplicate_window_seconds < 0:
            raise ValueError("duplicate_window_seconds must be >= 0")

        try:
            with self._connection_factory() as conn:
                with conn.transaction(), conn.cursor() as cur:
                    if duplicate_window_seconds > 0:
                        cur.execute(
                            ADVISORY_LOCK_SQL,
                            {"lock_key": advisory_lock_key(event.device_id, event.hibob_id)},
                        )
                        cur.execute(
                            FIND_RECENT_SCAN_SQL,
                            {
                                "device_id": event.device_id,
                                "hibob_id": event.hibob_id,
                                "scanned_at": event.scanned_at,
                                # make_interval(secs => ...) takes double precision
                                "window_seconds": float(duplicate_window_seconds),
                            },
                        )
                        existing = cur.fetchone()
                        if existing is not None:
                            return RecordResult(event_id=existing[0], is_duplicate=True)

                    cur.execute(INSERT_SCAN_SQL, _insert_params(event))
                    inserted = cur.fetchone()
        except psycopg.Error as exc:
            raise RepositoryError(f"Recording scan event failed: {type(exc).__name__}") from exc

        if inserted is None:  # pragma: no cover - INSERT ... RETURNING always returns a row
            raise RepositoryError("INSERT did not return an id")
        return RecordResult(event_id=inserted[0], is_duplicate=False)


def _insert_params(event: NewScanEvent) -> dict[str, object]:
    employee = event.employee
    return {
        "hibob_id": event.hibob_id,
        "card_resolver_employee_name": event.card_resolver_employee_name,
        "card_resolver_dataset_id": event.card_resolver_dataset_id,
        "hibob_lookup_status": event.hibob_lookup_status.value,
        "employee_name": employee.employee_name if employee else None,
        "job_title": employee.job_title if employee else None,
        "department": employee.department if employee else None,
        "site": employee.site if employee else None,
        "employment_status": employee.employment_status if employee else None,
        "lifecycle_status": employee.lifecycle_status if employee else None,
        "device_id": event.device_id,
        "scanned_at": event.scanned_at,
    }
