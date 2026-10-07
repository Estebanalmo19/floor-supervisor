"""ARRISE brand mark for the kiosk header.

No logo image is shipped in the repository. When the official asset is supplied,
place it at ``assets/brand/arrise-logo.png`` (or ``.svg``) and the header uses it
automatically with the same treatment as ARRISE Appearance (white on the dark
header). Until then a typographic ARRISE wordmark is rendered.

The asset is embedded as a data URI, so the kiosk never hotlinks an external URL.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOGO_CANDIDATES = (
    PROJECT_ROOT / "assets" / "brand" / "arrise-logo.svg",
    PROJECT_ROOT / "assets" / "brand" / "arrise-logo.png",
)
MAX_LOGO_BYTES = 512 * 1024
_MIME_TYPES = {".png": "image/png", ".svg": "image/svg+xml"}


@dataclass(frozen=True)
class Brand:
    wordmark: str = "ARRISE"
    product_name: str = "Floor Supervisor"
    logo_data_uri: str | None = None


def load_brand(logo_candidates: tuple[Path, ...] = DEFAULT_LOGO_CANDIDATES) -> Brand:
    return Brand(logo_data_uri=_load_logo(logo_candidates))


def _load_logo(candidates: tuple[Path, ...]) -> str | None:
    for path in candidates:
        mime = _MIME_TYPES.get(path.suffix.lower())
        if mime is None or not path.is_file():
            continue
        data = path.read_bytes()
        if not data or len(data) > MAX_LOGO_BYTES:
            continue
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    return None
