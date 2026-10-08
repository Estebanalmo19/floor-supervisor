"""Floor Supervisor kiosk UI (Streamlit).

Thin by design: it captures input, delegates to ScanService and renders results.
Business rules live in src/services; copy and timings in src/ui/presenter.py; markup
in src/ui/components.py; styling in src/ui/kiosk.css.

Screens:
  scan            default card scanner (unchanged behaviour)
  manual_entry    HiBob ID entry   - only reachable after CARD_NOT_RESOLVED
  manual_preview  employee preview - "Confirm scan" is the only action that records

Run:  streamlit run app.py
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import streamlit as st

from src.bootstrap import AppContext, build_app_context
from src.config import load_settings
from src.exceptions import ConfigError
from src.logging_setup import configure_logging
from src.models.scan import FallbackTicket, ManualLookupStatus, ScanOutcome
from src.ui.brand import Brand, load_brand
from src.ui.components import (
    configuration_error_html,
    header_html,
    manual_entry_html,
    manual_preview_html,
    stage_html,
    system_status_html,
)
from src.ui.focus import render_autofocus
from src.ui.presenter import (
    DEFAULT_TIMINGS,
    FALLBACK_EXPIRED_VIEW,
    UNEXPECTED_ERROR_VIEW,
    ResultView,
    present,
    present_manual_lookup,
)
from src.ui.styles import kiosk_stylesheet

# The scan input gets a fresh key after every submission. Streamlit's text input
# does not fire on_change when the same value is submitted twice in a row, so
# reusing one key would silently drop a repeat scan of the same card.
SCAN_INPUT_KEY_PREFIX = "scan_input"
SCAN_INPUT_GENERATION_KEY = "scan_input_generation"
SCAN_INPUT_LABEL = "Card scan"
RESULT_STATE_KEY = "last_result"
RESULT_SEQUENCE_KEY = "result_sequence"

# Manual HiBob fallback state (server-side session state; not reachable from the browser).
MODE_KEY = "mode"
FALLBACK_TICKET_KEY = "fallback_ticket"
FALLBACK_OFFER_KEY = "fallback_offer"  # (offered_at monotonic, offer sequence)
MANUAL_EMPLOYEE_KEY = "manual_employee"
MANUAL_MESSAGE_KEY = "manual_message"
MANUAL_INPUT_KEY_PREFIX = "manual_hibob_id"
MANUAL_INPUT_GENERATION_KEY = "manual_input_generation"
MANUAL_INPUT_LABEL = "HiBob ID"
MODE_SCAN, MODE_MANUAL_ENTRY, MODE_MANUAL_PREVIEW = "scan", "manual_entry", "manual_preview"
# How long the "Enter HiBob ID manually" offer stays visible after CARD_NOT_RESOLVED.
FALLBACK_OFFER_SECONDS = 20

logger = logging.getLogger("src.app")

st.set_page_config(page_title="ARRISE · Floor Supervisor", layout="centered")


@st.cache_resource(show_spinner=False)
def get_app_context() -> AppContext:
    """Created once per server process and shared by all sessions."""
    return build_app_context(load_settings())


@st.cache_resource(show_spinner=False)
def get_brand() -> Brand:
    return load_brand()


# --------------------------------------------------------------------------- state helpers


def _rotating_key(prefix: str, generation_key: str) -> str:
    return f"{prefix}_{st.session_state.get(generation_key, 0)}"


def _rotate(generation_key: str) -> None:
    st.session_state[generation_key] = st.session_state.get(generation_key, 0) + 1


def show_result(view: ResultView) -> None:
    # Per-session counter (module globals are re-created on every Streamlit rerun).
    sequence = st.session_state.get(RESULT_SEQUENCE_KEY, 0) + 1
    st.session_state[RESULT_SEQUENCE_KEY] = sequence
    st.session_state[RESULT_STATE_KEY] = (view, time.monotonic(), sequence)


def back_to_scanner(clear_ticket: bool = True) -> None:
    st.session_state[MODE_KEY] = MODE_SCAN
    st.session_state.pop(MANUAL_EMPLOYEE_KEY, None)
    st.session_state.pop(MANUAL_MESSAGE_KEY, None)
    if clear_ticket:
        st.session_state.pop(FALLBACK_TICKET_KEY, None)
        st.session_state.pop(FALLBACK_OFFER_KEY, None)


def current_result() -> tuple[ResultView | None, float, int]:
    """The last result and its remaining display time (expired results are dropped)."""
    entry = st.session_state.get(RESULT_STATE_KEY)
    if entry is None:
        return None, 0.0, 0
    view, shown_at, sequence = entry
    remaining = DEFAULT_TIMINGS.ttl_for(view.tone) - (time.monotonic() - shown_at)
    if remaining <= 0:
        st.session_state.pop(RESULT_STATE_KEY, None)
        return None, 0.0, 0
    return view, remaining, sequence


def _ticket() -> FallbackTicket | None:
    ticket = st.session_state.get(FALLBACK_TICKET_KEY)
    return ticket if isinstance(ticket, FallbackTicket) else None


def _ticket_seconds_left(ticket: FallbackTicket | None) -> float:
    """Display-only helper; ScanService re-validates the ticket on every action."""
    if ticket is None:
        return 0.0
    from src.models.scan import FALLBACK_TICKET_TTL

    return (ticket.issued_at + FALLBACK_TICKET_TTL - datetime.now(UTC)).total_seconds()


# ------------------------------------------------------------------------------ callbacks


def on_scan(context: AppContext, input_key: str) -> None:
    raw = st.session_state.get(input_key) or ""
    # Next run renders a brand-new, empty input: ready for the next card immediately.
    _rotate(SCAN_INPUT_GENERATION_KEY)
    if not raw.strip():
        return
    try:
        result = context.scan_service.process_scan(raw)
        view = present(result, context.settings.timezone)
        # Only CARD_NOT_RESOLVED carries a ticket; any other scan withdraws the offer.
        if result.outcome is ScanOutcome.CARD_NOT_RESOLVED and result.fallback_ticket is not None:
            st.session_state[FALLBACK_TICKET_KEY] = result.fallback_ticket
            previous_sequence = st.session_state.get(FALLBACK_OFFER_KEY, (0.0, 0))[1]
            st.session_state[FALLBACK_OFFER_KEY] = (time.monotonic(), previous_sequence + 1)
        else:
            st.session_state.pop(FALLBACK_TICKET_KEY, None)
    except Exception:  # last-resort guard: the kiosk must never show a traceback
        logger.exception("scan_unexpected_error")
        st.session_state.pop(FALLBACK_TICKET_KEY, None)
        view = UNEXPECTED_ERROR_VIEW
    show_result(view)


def on_open_manual() -> None:
    if _ticket() is None:  # the offer is only rendered with a ticket; guard anyway
        return
    st.session_state.pop(RESULT_STATE_KEY, None)
    st.session_state.pop(MANUAL_MESSAGE_KEY, None)
    _rotate(MANUAL_INPUT_GENERATION_KEY)
    st.session_state[MODE_KEY] = MODE_MANUAL_ENTRY


def on_find_employee(context: AppContext, input_key: str) -> None:
    if st.session_state.get(MODE_KEY) != MODE_MANUAL_ENTRY:
        return
    value = st.session_state.get(input_key) or ""
    try:
        lookup = context.scan_service.lookup_manual(_ticket(), value)
    except Exception:
        logger.exception("manual_lookup_unexpected_error")
        back_to_scanner()
        show_result(UNEXPECTED_ERROR_VIEW)
        return
    if lookup.status is ManualLookupStatus.FOUND and lookup.employee is not None:
        st.session_state[MANUAL_EMPLOYEE_KEY] = lookup.employee
        st.session_state.pop(MANUAL_MESSAGE_KEY, None)
        st.session_state[MODE_KEY] = MODE_MANUAL_PREVIEW
        return
    outcome = present_manual_lookup(lookup.status)
    if outcome.result_view is not None:
        back_to_scanner()
        show_result(outcome.result_view)
    else:  # stay on the manual screen with an inline message, fresh empty input
        st.session_state[MANUAL_MESSAGE_KEY] = outcome.inline_message
        _rotate(MANUAL_INPUT_GENERATION_KEY)


def on_confirm_manual(context: AppContext) -> None:
    if st.session_state.get(MODE_KEY) != MODE_MANUAL_PREVIEW:
        return
    employee = st.session_state.get(MANUAL_EMPLOYEE_KEY)
    ticket = st.session_state.pop(FALLBACK_TICKET_KEY, None)  # single use: a double tap cannot re-record
    back_to_scanner()
    if employee is None:
        show_result(FALLBACK_EXPIRED_VIEW)
        return
    try:
        result = context.scan_service.confirm_manual(ticket, employee)
        view = present(result, context.settings.timezone)
    except Exception:
        logger.exception("manual_confirm_unexpected_error")
        view = UNEXPECTED_ERROR_VIEW
    show_result(view)


def on_cancel_manual() -> None:
    back_to_scanner()  # nothing is written


# -------------------------------------------------------------------------------- screens


def render_scan_screen(context: AppContext) -> None:
    view, remaining, sequence = current_result()
    st.html(stage_html(view, remaining, sequence))

    input_key = _rotating_key(SCAN_INPUT_KEY_PREFIX, SCAN_INPUT_GENERATION_KEY)
    st.text_input(
        SCAN_INPUT_LABEL,
        key=input_key,
        on_change=on_scan,
        args=(context, input_key),
        placeholder="Waiting for card…",
        label_visibility="collapsed",
        autocomplete="off",
    )

    ticket = _ticket()
    offered_at, offer_sequence = st.session_state.get(FALLBACK_OFFER_KEY, (0.0, 0))
    offer_left = min(_ticket_seconds_left(ticket),
                     FALLBACK_OFFER_SECONDS - (time.monotonic() - offered_at))
    if ticket is not None and offer_left > 0:
        # Hide the offer client-side when it lapses (same mechanism as result expiry).
        expiry = "fs-expire-a" if offer_sequence % 2 else "fs-expire-b"
        st.html(f"<style>.st-key-fs_btn_fallback {{ animation: {expiry} 0.2s ease-in forwards; "
                f"animation-delay: {offer_left:.2f}s; }}</style>")
        st.button("Enter HiBob ID manually", key="fs_btn_fallback", on_click=on_open_manual,
                  type="secondary", width="stretch")

    st.html(system_status_html(context.settings.device_id))
    render_autofocus(SCAN_INPUT_LABEL)


def render_manual_entry_screen(context: AppContext) -> None:
    st.html(manual_entry_html(st.session_state.get(MANUAL_MESSAGE_KEY)))
    input_key = _rotating_key(MANUAL_INPUT_KEY_PREFIX, MANUAL_INPUT_GENERATION_KEY)
    with st.form("manual_entry_form", clear_on_submit=False, border=False):
        st.text_input(MANUAL_INPUT_LABEL, key=input_key, placeholder="HiBob ID",
                      label_visibility="collapsed", autocomplete="off", max_chars=20)
        find_col, cancel_col = st.columns(2)
        with find_col:
            st.form_submit_button("Find employee", key="fs_btn_find", type="primary",
                                  on_click=on_find_employee, args=(context, input_key),
                                  width="stretch")
        with cancel_col:
            st.form_submit_button("Cancel", key="fs_btn_cancel_entry", type="secondary",
                                  on_click=on_cancel_manual, width="stretch")
    render_autofocus(MANUAL_INPUT_LABEL, input_mode="numeric")
    manual_timeout_watch()


def render_manual_preview_screen(context: AppContext) -> None:
    employee = st.session_state.get(MANUAL_EMPLOYEE_KEY)
    if employee is None:
        back_to_scanner()
        st.rerun()
    st.html(manual_preview_html(employee))
    confirm_col, cancel_col = st.columns(2)
    with confirm_col:
        st.button("Confirm scan", key="fs_btn_confirm", type="primary",
                  on_click=on_confirm_manual, args=(context,), width="stretch")
    with cancel_col:
        st.button("Cancel", key="fs_btn_cancel_preview", type="secondary",
                  on_click=on_cancel_manual, width="stretch")
    manual_timeout_watch()


@st.fragment(run_every=5)
def manual_timeout_watch() -> None:
    """Return an abandoned manual screen to the scanner once the fallback window lapses."""
    if st.session_state.get(MODE_KEY, MODE_SCAN) == MODE_SCAN:
        return
    if _ticket_seconds_left(_ticket()) <= 0:
        back_to_scanner()
        show_result(FALLBACK_EXPIRED_VIEW)
        st.rerun(scope="app")


# ----------------------------------------------------------------------------------- main


def main() -> None:
    configure_logging()
    st.html(kiosk_stylesheet())
    st.html(header_html(get_brand()))

    try:
        context = get_app_context()
    except ConfigError as exc:
        logger.error("configuration_error", extra={"error": str(exc)})
        st.html(configuration_error_html())
        st.stop()

    mode = st.session_state.get(MODE_KEY, MODE_SCAN)
    if mode == MODE_MANUAL_ENTRY and _ticket() is not None:
        render_manual_entry_screen(context)
    elif mode == MODE_MANUAL_PREVIEW and _ticket() is not None:
        render_manual_preview_screen(context)
    else:
        if mode != MODE_SCAN:  # manual state without a ticket can never be shown
            back_to_scanner()
        render_scan_screen(context)


main()
