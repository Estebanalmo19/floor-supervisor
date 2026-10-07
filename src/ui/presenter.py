"""Maps a ScanResult to what the kiosk shows. Pure functions; no Streamlit, no HTML.

All user-facing copy and the result display timings live here, so wording and
timing can be changed in one place. Internal exception messages are never shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import tzinfo
from typing import Literal

from src.models.scan import ScanOutcome, ScanResult, ScanWarning

Tone = Literal["success", "warning", "info", "error"]


@dataclass(frozen=True)
class ResultTimings:
    """Seconds a result stays on screen before the kiosk returns to its idle state.

    Scanning is never blocked while a result is visible; these only control the display.
    """

    success_seconds: float = 3.0
    info_seconds: float = 3.0
    warning_seconds: float = 4.0
    error_seconds: float = 5.0

    def ttl_for(self, tone: Tone) -> float:
        return {
            "success": self.success_seconds,
            "info": self.info_seconds,
            "warning": self.warning_seconds,
            "error": self.error_seconds,
        }[tone]


DEFAULT_TIMINGS = ResultTimings()
DEFAULT_TIME_ZONE_LABEL = "Colombia time"


@dataclass(frozen=True)
class ResultView:
    tone: Tone
    icon: str  # "check" | "info" | "alert" | "cross"
    title: str
    recorded: bool
    employee_name: str | None = None
    details: tuple[str, ...] = ()
    time_label: str | None = None
    message: str | None = None
    reference: str | None = None


# --- copy ----------------------------------------------------------------------

HIBOB_NOT_FOUND_MESSAGE = (
    "Employee information is currently unavailable from HiBob. "
    "The scan was recorded successfully."
)
DUPLICATE_MESSAGE = "This card was scanned moments ago. No additional record was created."
GENERIC_ERROR_TITLE = "Scan could not be completed"
GENERIC_ERROR_MESSAGE = (
    "Scan was not recorded. Please try again. "
    "If the problem continues, contact your supervisor."
)

# Short codes help support correlate a kiosk report with the logs without
# exposing internal details on screen.
_ERROR_REFERENCES: dict[ScanOutcome, str] = {
    ScanOutcome.RESOLVER_UNAVAILABLE: "E01",
    ScanOutcome.RESOLVER_ERROR: "E02",
    ScanOutcome.DATA_INTEGRITY_ERROR: "E03",
    ScanOutcome.DATABASE_ERROR: "E04",
}
UNEXPECTED_ERROR_REFERENCE = "E99"

UNEXPECTED_ERROR_VIEW = ResultView(
    tone="error",
    icon="cross",
    title=GENERIC_ERROR_TITLE,
    recorded=False,
    message=GENERIC_ERROR_MESSAGE,
    reference=UNEXPECTED_ERROR_REFERENCE,
)


def present(
    result: ScanResult,
    display_timezone: tzinfo,
    time_zone_label: str = DEFAULT_TIME_ZONE_LABEL,
) -> ResultView:
    outcome = result.outcome

    if outcome is ScanOutcome.INVALID_INPUT:
        return ResultView(
            tone="warning",
            icon="alert",
            title="Card not read correctly",
            recorded=False,
            message="Please scan your card again.",
        )
    if outcome is ScanOutcome.CARD_NOT_RESOLVED:
        return ResultView(
            tone="error",
            icon="cross",
            title="Card not recognized",
            recorded=False,
            message="Scan was not recorded.",
        )
    if outcome in _ERROR_REFERENCES:
        return ResultView(
            tone="error",
            icon="cross",
            title=GENERIC_ERROR_TITLE,
            recorded=False,
            message=GENERIC_ERROR_MESSAGE,
            reference=_ERROR_REFERENCES[outcome],
        )
    if not outcome.is_accepted:  # defensive: future outcomes default to a generic error
        return UNEXPECTED_ERROR_VIEW

    local_time = result.scanned_at.astimezone(display_timezone).strftime("%H:%M:%S")
    time_label = f"{local_time} · {time_zone_label}"
    details = _employee_details(result)

    if outcome is ScanOutcome.DUPLICATE:
        return ResultView(
            tone="info",
            icon="info",
            title="Already registered",
            recorded=True,
            employee_name=result.display_name,
            details=details,
            time_label=time_label,
            message=DUPLICATE_MESSAGE,
        )

    # RECORDED
    message: str | None = None
    tone: Tone = "success"
    if ScanWarning.HIBOB_NOT_FOUND in result.warnings:
        tone, message = "warning", HIBOB_NOT_FOUND_MESSAGE
    # EMPLOYEE_NOT_ACTIVE is a structured-log warning only (V1): the HiBob snapshot may be
    # stale, so employment/lifecycle status is never shown. Name differences are not shown either.

    return ResultView(
        tone=tone,
        icon="check",
        title="Scan registered",
        recorded=True,
        employee_name=result.display_name,
        details=details,
        time_label=time_label,
        message=message,
    )


def _employee_details(result: ScanResult) -> tuple[str, ...]:
    if result.employee is None:
        return ()
    return tuple(part for part in (result.employee.job_title, result.employee.site) if part)
