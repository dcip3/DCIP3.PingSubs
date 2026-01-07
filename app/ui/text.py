from __future__ import annotations

import html
from typing import Any


def escape_html(value: Any) -> str:
    return html.escape(str(value), quote=False)


def format_display_name(full_name: str | None) -> str:
    name = (full_name or "Unknown").strip()
    parts = [part for part in name.split() if part]
    first = parts[0] if parts else "Unknown"
    last_initial = f" {parts[1][0].upper()}." if len(parts) > 1 else ""
    return f"{first}{last_initial}"
