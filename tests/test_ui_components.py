import re

from src.ui.brand import Brand, load_brand
from src.ui.components import (
    configuration_error_html,
    header_html,
    stage_html,
    system_status_html,
)
from src.ui.focus import build_autofocus_script
from src.ui.presenter import ResultView
from src.ui.styles import ARRISE_TOKENS, KIOSK_CSS_PATH, kiosk_stylesheet, tokens_css

OFFICIAL = {
    "--color-primary": "#35106a", "--color-primary-hover": "#2a0c55",
    "--color-primary-active": "#200941", "--color-primary-soft": "#f4eefc",
    "--color-ink": "#1c0f38", "--color-ink-deep": "#120826",
    "--color-violet-bright": "#7f5af0", "--color-secondary": "#06284a",
    "--color-accent": "#0e9f6e", "--color-bg": "#f7f5fb", "--color-surface": "#ffffff",
    "--color-surface-secondary": "#f4f1f9", "--color-border": "#e5e1ee",
    "--color-border-strong": "#d3cce2", "--color-text-primary": "#1a1424",
    "--color-text-secondary": "#6b6475", "--color-text-tertiary": "#7d7689",
    "--color-success-fg": "#1f9d55", "--color-success-bg": "#e8f7ee",
    "--color-warning-fg": "#b7791f", "--color-warning-bg": "#fdf3dd",
    "--color-error-fg": "#c0392b", "--color-error-bg": "#fbeae8",
    "--color-info-fg": "#2f6fd6", "--color-info-bg": "#e8f0fd",
}

RECORDED = ResultView(tone="success", icon="check", title="Scan registered", recorded=True,
                      employee_name="Test Employee",
                      details=("Game Presenter", "Test Site"),
                      time_label="08:45:30 · Colombia time")


# --- styles --------------------------------------------------------------------


def test_tokens_are_exactly_the_official_palette():
    assert ARRISE_TOKENS == OFFICIAL
    css = tokens_css()
    for name, value in OFFICIAL.items():
        assert f"{name}: {value};" in css


def test_component_css_uses_tokens_only_no_raw_colours():
    css = KIOSK_CSS_PATH.read_text(encoding="utf-8")
    assert re.findall(r"#[0-9a-fA-F]{3,8}\b", css) == []
    assert re.findall(r"\brgba?\(", css) == []


def test_component_css_only_references_defined_tokens():
    css = KIOSK_CSS_PATH.read_text(encoding="utf-8")
    used = set(re.findall(r"var\((--color-[a-z-]+)", css))
    assert used and used <= set(ARRISE_TOKENS)


def test_stylesheet_hides_streamlit_chrome_and_hides_scanner_digits():
    sheet = kiosk_stylesheet()
    assert sheet.startswith("<style>") and sheet.endswith("</style>")
    for selector in ('header[data-testid="stHeader"]', '[data-testid="stToolbar"]', "#MainMenu"):
        assert selector in sheet
    assert '[class*="st-key-scan_input_"] input {' in sheet
    assert "color: transparent !important;" in sheet


# --- components ------------------------------------------------------------------


def test_header_uses_wordmark_without_logo_asset():
    html = header_html(Brand())
    assert '<span class="fs-brand-wordmark">ARRISE</span>' in html
    assert "Floor Supervisor" in html
    assert "<img" not in html
    assert "Scanner ready" in html


def test_header_uses_logo_when_supplied():
    html = header_html(Brand(logo_data_uri="data:image/png;base64,AAAA"))
    assert '<img class="fs-brand-logo" src="data:image/png;base64,AAAA" alt="ARRISE">' in html


def test_idle_stage():
    html = stage_html()
    assert "Scan your card" in html
    assert "Hold your card near the reader" in html
    assert "fs-result" not in html


def test_result_stage_renders_view_with_ttl_and_sequence():
    html = stage_html(RECORDED, ttl_seconds=3, sequence=7)
    assert "Scan your card" in html  # idle layer remains underneath
    assert 'class="fs-layer fs-result fs-expire-a fs-tone-success"' in html
    assert "--fs-ttl: 3.00s" in html
    assert 'data-sequence="7"' in html
    assert 'data-recorded="true"' in html
    for text in ("Scan registered", "Test Employee",
                 "Game Presenter · Test Site", "08:45:30 · Colombia time"):
        assert text in html


def test_expired_result_is_not_rendered():
    assert "fs-result" not in stage_html(RECORDED, ttl_seconds=0, sequence=1)


def test_consecutive_results_alternate_expiry_animation():
    # A new animation-name restarts the TTL even if the element were reused.
    first, second, third = (stage_html(RECORDED, 3, n) for n in (1, 2, 3))
    assert "fs-expire-a" in first and "fs-expire-b" in second and "fs-expire-a" in third
    css = KIOSK_CSS_PATH.read_text(encoding="utf-8")
    assert "@keyframes fs-expire-a" in css and "@keyframes fs-expire-b" in css


def test_dynamic_text_is_escaped():
    view = ResultView(tone="info", icon="info", title="Already registered", recorded=True,
                      employee_name='<img src=x onerror="alert(1)">', details=("<b>x</b>",),
                      message="a & b")
    html = stage_html(view, ttl_seconds=3, sequence=1)
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "a &amp; b" in html
    assert "<script>" not in html


def test_error_reference_and_tone():
    view = ResultView(tone="error", icon="cross", title="Scan could not be completed",
                      recorded=False, message="Scan was not recorded.", reference="E04")
    html = stage_html(view, ttl_seconds=5, sequence=1)
    assert "fs-tone-error" in html and "Code E04" in html and 'data-recorded="false"' in html


def test_system_status_and_configuration_error():
    assert "System ready" in system_status_html("TABLET_01")
    assert "fs-system-down" in system_status_html("TABLET_01", system_ready=False)
    assert "Scanner unavailable" in configuration_error_html()


# --- brand ---------------------------------------------------------------------


def test_brand_falls_back_to_wordmark_when_no_asset(tmp_path):
    brand = load_brand((tmp_path / "arrise-logo.svg", tmp_path / "arrise-logo.png"))
    assert brand.logo_data_uri is None and brand.wordmark == "ARRISE"


def test_brand_embeds_local_asset_as_data_uri(tmp_path):
    logo = tmp_path / "arrise-logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    brand = load_brand((logo,))
    assert brand.logo_data_uri is not None
    assert brand.logo_data_uri.startswith("data:image/png;base64,")


def test_brand_ignores_unsupported_or_oversized_assets(tmp_path):
    other = tmp_path / "arrise-logo.gif"
    other.write_bytes(b"GIF89a")
    huge = tmp_path / "arrise-logo.png"
    huge.write_bytes(b"0" * (512 * 1024 + 1))
    assert load_brand((other, huge)).logo_data_uri is None


def test_repository_ships_no_logo_asset():
    assert load_brand().logo_data_uri is None


# --- focus -----------------------------------------------------------------------


def test_focus_script_suppresses_on_screen_keyboard():
    script = build_autofocus_script("Card scan")
    assert 'const INPUT_MODE = "none";' in script
    assert 'input.setAttribute("inputmode", INPUT_MODE)' in script


def test_focus_script_numeric_keypad_for_manual_entry():
    assert 'const INPUT_MODE = "numeric";' in build_autofocus_script("HiBob ID", input_mode="numeric")


def test_focus_script_rejects_unknown_input_mode():
    import pytest
    with pytest.raises(ValueError):
        build_autofocus_script("HiBob ID", input_mode="text")


# --- manual HiBob fallback screens -------------------------------------------------------


def test_manual_entry_screen():
    from src.ui.components import manual_entry_html

    html = manual_entry_html()
    assert "Enter HiBob ID" in html and "fs-inline-message" not in html
    assert "Employee not found" in manual_entry_html("Employee not found. No scan was recorded.")
    assert "&lt;b&gt;" in manual_entry_html("<b>x</b>")


def test_manual_preview_shows_name_title_site_only():
    from src.models.employee import Employee
    from src.ui.components import manual_preview_html

    employee = Employee("99999", "Test <Employee>", "Game Presenter", "Operations", "Test Site",
                        "Inactive", "Terminated")
    html = manual_preview_html(employee)
    for text in ("Name", "Job title", "Site", "Test &lt;Employee&gt;", "Game Presenter", "Test Site"):
        assert text in html
    for hidden in ("Inactive", "Terminated", "Operations", "99999", "<Employee>"):
        assert hidden not in html


def test_manual_entry_badge_rendered_on_result():
    view = ResultView(tone="success", icon="check", title="Scan registered", recorded=True,
                      employee_name="Test Employee", badge="Manual entry")
    assert '<span class="fs-badge">Manual entry</span>' in stage_html(view, ttl_seconds=3, sequence=1)
