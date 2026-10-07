"""ARRISE design tokens and kiosk stylesheet assembly.

The official tokens are the single source of colour. ``kiosk.css`` must reference
them through ``var(--…)`` only (enforced by tests), so the palette cannot drift.
Semantic colours are reserved for operational state.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

ARRISE_TOKENS: dict[str, str] = {
    # Primary
    "--color-primary": "#35106a",
    "--color-primary-hover": "#2a0c55",
    "--color-primary-active": "#200941",
    "--color-primary-soft": "#f4eefc",
    # Ink
    "--color-ink": "#1c0f38",
    "--color-ink-deep": "#120826",
    # Violet
    "--color-violet-bright": "#7f5af0",
    # Secondary / accent
    "--color-secondary": "#06284a",
    "--color-accent": "#0e9f6e",
    # Neutrals
    "--color-bg": "#f7f5fb",
    "--color-surface": "#ffffff",
    "--color-surface-secondary": "#f4f1f9",
    "--color-border": "#e5e1ee",
    "--color-border-strong": "#d3cce2",
    # Text
    "--color-text-primary": "#1a1424",
    "--color-text-secondary": "#6b6475",
    "--color-text-tertiary": "#7d7689",
    # Semantic (operational state only)
    "--color-success-fg": "#1f9d55",
    "--color-success-bg": "#e8f7ee",
    "--color-warning-fg": "#b7791f",
    "--color-warning-bg": "#fdf3dd",
    "--color-error-fg": "#c0392b",
    "--color-error-bg": "#fbeae8",
    "--color-info-fg": "#2f6fd6",
    "--color-info-bg": "#e8f0fd",
}

KIOSK_CSS_PATH = Path(__file__).with_name("kiosk.css")


def tokens_css() -> str:
    lines = "\n".join(f"    {name}: {value};" for name, value in ARRISE_TOKENS.items())
    return f":root {{\n{lines}\n}}"


@lru_cache(maxsize=1)
def kiosk_stylesheet() -> str:
    """Full ``<style>`` block: tokens followed by the kiosk component styles."""
    component_css = KIOSK_CSS_PATH.read_text(encoding="utf-8")
    return f"<style>\n{tokens_css()}\n\n{component_css}\n</style>"
