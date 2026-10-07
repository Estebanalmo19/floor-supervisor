"""Floor Supervisor kiosk UI (Streamlit).

Thin by design: it captures the scanner input, delegates to ScanService and
renders the result. Business rules live in src/services; copy and timings in
src/ui/presenter.py; markup in src/ui/components.py; styling in src/ui/kiosk.css.

Run:  streamlit run app.py
"""

from __future__ import annotations

import logging
import time

import streamlit as st

from src.bootstrap import AppContext, build_app_context
from src.config import load_settings
from src.exceptions import ConfigError
from src.logging_setup import configure_logging
from src.ui.brand import Brand, load_brand
from src.ui.components import (
    configuration_error_html,
    header_html,
    stage_html,
    system_status_html,
)
from src.ui.focus import render_autofocus
from src.ui.presenter import DEFAULT_TIMINGS, UNEXPECTED_ERROR_VIEW, ResultView, present
from src.ui.styles import kiosk_stylesheet

# The scan input gets a fresh key after every submission. Streamlit's text input
# does not fire on_change when the same value is submitted twice in a row, so
# reusing one key would silently drop a repeat scan of the same card.
SCAN_INPUT_KEY_PREFIX = "scan_input"
SCAN_INPUT_GENERATION_KEY = "scan_input_generation"
SCAN_INPUT_LABEL = "Card scan"
RESULT_STATE_KEY = "last_result"
RESULT_SEQUENCE_KEY = "result_sequence"

logger = logging.getLogger("src.app")

st.set_page_config(page_title="ARRISE · Floor Supervisor", layout="centered")


@st.cache_resource(show_spinner=False)
def get_app_context() -> AppContext:
    """Created once per server process and shared by all sessions."""
    return build_app_context(load_settings())


@st.cache_resource(show_spinner=False)
def get_brand() -> Brand:
    return load_brand()


def scan_input_key() -> str:
    return f"{SCAN_INPUT_KEY_PREFIX}_{st.session_state.get(SCAN_INPUT_GENERATION_KEY, 0)}"


def on_scan(context: AppContext, input_key: str) -> None:
    raw = st.session_state.get(input_key) or ""
    # Next run renders a brand-new, empty input: ready for the next card immediately.
    st.session_state[SCAN_INPUT_GENERATION_KEY] = st.session_state.get(SCAN_INPUT_GENERATION_KEY, 0) + 1
    if not raw.strip():
        return
    try:
        result = context.scan_service.process_scan(raw)
        view = present(result, context.settings.timezone)
    except Exception:  # last-resort guard: the kiosk must never show a traceback
        logger.exception("scan_unexpected_error")
        view = UNEXPECTED_ERROR_VIEW
    # Per-session counter (module globals are re-created on every Streamlit rerun).
    sequence = st.session_state.get(RESULT_SEQUENCE_KEY, 0) + 1
    st.session_state[RESULT_SEQUENCE_KEY] = sequence
    st.session_state[RESULT_STATE_KEY] = (view, time.monotonic(), sequence)


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

    view, remaining, sequence = current_result()
    st.html(stage_html(view, remaining, sequence))

    input_key = scan_input_key()
    st.text_input(
        SCAN_INPUT_LABEL,
        key=input_key,
        on_change=on_scan,
        args=(context, input_key),
        placeholder="Waiting for card…",
        label_visibility="collapsed",
        autocomplete="off",
    )
    st.html(system_status_html(context.settings.device_id))
    render_autofocus(SCAN_INPUT_LABEL)


main()
