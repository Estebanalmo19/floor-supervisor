"""Keyboard-focus restoration for the scanner input (kiosk mode).

Streamlit does not keep focus on a text input across reruns. The card scanner
behaves like a keyboard, so if the input is not focused the scan is lost.

This module is intentionally isolated from business logic: it only injects a
small script that periodically re-focuses the scan input when nothing else
(another form control) holds focus, and sets ``inputmode="none"`` so a tablet
does not open its on-screen keyboard (the scanner is a hardware keyboard). It
can be replaced or removed without touching the scan flow. Validate on the real
tablet/browser before production.
"""

from __future__ import annotations

import json

DEFAULT_INTERVAL_MS = 300

_SCRIPT_TEMPLATE = """<script>
(function () {
  const LABEL = __LABEL__;
  const INTERVAL_MS = __INTERVAL__;
  const INPUT_MODE = __INPUT_MODE__;
  const parentWindow = window.parent;
  const doc = parentWindow.document;

  function focusScanInput() {
    const selector = 'input[aria-label="' + CSS.escape(LABEL) + '"]';
    const input = doc.querySelector(selector);
    if (!input || input.disabled) { return; }
    if (input.getAttribute("inputmode") !== INPUT_MODE) {
      // "none": hardware scanner only, never open the tablet's on-screen keyboard.
      // "numeric": manual HiBob ID entry opens the number pad.
      input.setAttribute("inputmode", INPUT_MODE);
      input.setAttribute("autocorrect", "off");
      input.setAttribute("autocapitalize", "off");
      input.setAttribute("spellcheck", "false");
    }
    const active = doc.activeElement;
    if (active === input) { return; }
    const otherControlFocused = active && active !== doc.body && active.tagName !== "IFRAME"
      && active.matches("input, textarea, select, button, [contenteditable='true']");
    if (otherControlFocused) { return; }
    input.focus({ preventScroll: true });
  }

  if (parentWindow.__floorSupervisorFocusTimer) {
    parentWindow.clearInterval(parentWindow.__floorSupervisorFocusTimer);
  }
  parentWindow.__floorSupervisorFocusTimer = parentWindow.setInterval(focusScanInput, INTERVAL_MS);
  focusScanInput();
})();
</script>"""


def build_autofocus_script(input_label: str, interval_ms: int = DEFAULT_INTERVAL_MS,
                           input_mode: str = "none") -> str:
    """Return the HTML/JS snippet that keeps ``input_label`` focused.

    The label is JSON-encoded and ``</`` is escaped so the value cannot break
    out of the script element. ``input_mode`` is "none" for the hardware scanner
    and "numeric" for manual HiBob ID entry.
    """
    if not input_label:
        raise ValueError("input_label must not be empty")
    if not 50 <= interval_ms <= 5000:
        raise ValueError("interval_ms must be between 50 and 5000")
    if input_mode not in ("none", "numeric"):
        raise ValueError("input_mode must be 'none' or 'numeric'")
    label_js = json.dumps(input_label).replace("</", "<\\/")
    return (_SCRIPT_TEMPLATE.replace("__LABEL__", label_js)
            .replace("__INTERVAL__", str(interval_ms))
            .replace("__INPUT_MODE__", json.dumps(input_mode)))


def render_autofocus(input_label: str, interval_ms: int = DEFAULT_INTERVAL_MS,
                     input_mode: str = "none") -> None:
    """Inject the focus script into the current Streamlit page.

    ``st.iframe`` runs HTML strings with same-origin access to the app, which the
    script needs to reach the input. Only the constant script built above is passed
    (never user or database content). The iframe is removed from the tab order.
    """
    import streamlit as st

    st.iframe(
        build_autofocus_script(input_label, interval_ms, input_mode),
        height=1,
        tab_index=-1,
        alt="Scanner focus helper",
    )
