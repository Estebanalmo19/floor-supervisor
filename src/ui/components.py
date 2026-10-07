"""HTML fragments for the kiosk. Pure functions returning markup strings.

Every dynamic value is HTML-escaped here; callers pass plain text only.
Class names are styled by ``kiosk.css``.
"""

from __future__ import annotations

from html import escape

from src.ui.brand import Brand
from src.ui.presenter import ResultView

# Icons are CSS/text glyphs: st.html sanitizes markup and strips inline SVG.
_SCAN_ICON = '<span class="fs-card-glyph" aria-hidden="true"></span>'

_RESULT_ICONS = {
    "check": "✓",  # ✓
    "cross": "✕",  # ✕
    "info": "i",
    "alert": "!",
}


def header_html(brand: Brand) -> str:
    if brand.logo_data_uri:
        mark = (
            f'<img class="fs-brand-logo" src="{escape(brand.logo_data_uri)}" '
            f'alt="{escape(brand.wordmark)}">'
        )
    else:
        mark = f'<span class="fs-brand-wordmark">{escape(brand.wordmark)}</span>'
    return (
        '<header class="fs-header">'
        f'<div class="fs-brand">{mark}<span class="fs-brand-divider" aria-hidden="true"></span>'
        f'<span class="fs-product">{escape(brand.product_name)}</span></div>'
        '<div class="fs-scanner-pill" role="status">'
        '<span class="fs-dot" aria-hidden="true"></span>'
        '<span class="fs-when-ready">Scanner ready</span>'
        '<span class="fs-when-idle">Tap screen to activate scanner</span>'
        "</div>"
        "</header>"
    )


def _idle_layer() -> str:
    return (
        '<section class="fs-layer fs-idle" aria-label="Scan your card">'
        f'<div class="fs-scan-icon">{_SCAN_ICON}</div>'
        '<h1 class="fs-title">Scan your card</h1>'
        '<p class="fs-subtitle">Hold your card near the reader</p>'
        "</section>"
    )


def _result_layer(view: ResultView, ttl_seconds: float, sequence: int) -> str:
    parts = [
        f'<div class="fs-result-icon" aria-hidden="true">{_RESULT_ICONS.get(view.icon, "!")}</div>',
        f'<h1 class="fs-title">{escape(view.title)}</h1>',
    ]
    if view.employee_name:
        parts.append(f'<p class="fs-employee">{escape(view.employee_name)}</p>')
    if view.details:
        parts.append(f'<p class="fs-details">{" · ".join(escape(d) for d in view.details)}</p>')
    if view.time_label:
        parts.append(f'<span class="fs-time">{escape(view.time_label)}</span>')
    if view.message:
        parts.append(f'<p class="fs-message">{escape(view.message)}</p>')
    if view.reference:
        parts.append(f'<p class="fs-reference">Code {escape(view.reference)}</p>')

    # Alternating the expiry animation name guarantees every result its full TTL,
    # even if the element were reused (see kiosk.css). data-sequence aids debugging.
    expiry = "fs-expire-a" if sequence % 2 else "fs-expire-b"
    return (
        f'<section class="fs-layer fs-result {expiry} fs-tone-{escape(view.tone)}" role="alert" '
        f'aria-live="assertive" data-sequence="{int(sequence)}" '
        f'data-recorded="{"true" if view.recorded else "false"}" '
        f'style="--fs-ttl: {max(ttl_seconds, 0.0):.2f}s">'
        + "".join(parts)
        + "</section>"
    )


def stage_html(view: ResultView | None = None, ttl_seconds: float = 0.0, sequence: int = 0) -> str:
    """Idle prompt, with the result layered on top while it is still fresh."""
    result = _result_layer(view, ttl_seconds, sequence) if view is not None and ttl_seconds > 0 else ""
    return f'<div class="fs-stage">{_idle_layer()}{result}</div>'


def system_status_html(device_id: str, system_ready: bool = True) -> str:
    state = "System ready" if system_ready else "System unavailable"
    css = "fs-system" if system_ready else "fs-system fs-system-down"
    return (
        f'<div class="{css}" role="status"><span class="fs-dot" aria-hidden="true"></span>'
        f'<span>{state}</span><span class="fs-device">· {escape(device_id)}</span></div>'
    )


def configuration_error_html() -> str:
    return (
        '<div class="fs-stage"><section class="fs-layer fs-tone-error" role="alert">'
        f'<div class="fs-result-icon" aria-hidden="true">{_RESULT_ICONS["cross"]}</div>'
        '<h1 class="fs-title">Scanner unavailable</h1>'
        '<p class="fs-message">The kiosk is not configured correctly. '
        "Please contact your supervisor.</p>"
        "</section></div>"
    )
