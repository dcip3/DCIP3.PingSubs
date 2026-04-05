from __future__ import annotations

import html
import unicodedata
from typing import Any


def escape_html(value: Any) -> str:
    return html.escape(str(value), quote=False)


def format_display_name(full_name: str | None) -> str:
    name = (full_name or "Unknown").strip()
    parts = [part for part in name.split() if part]
    first = parts[0] if parts else "Unknown"
    last_initial = f" {parts[1][0].upper()}." if len(parts) > 1 else ""
    return f"{first}{last_initial}"


def validate_person_name(raw_name: str | None) -> tuple[str | None, str | None]:
    normalized = " ".join((raw_name or "").strip().split())
    if not normalized:
        return None, "Name cannot be empty. Please try again."

    allowed_punctuation = {"-", "'", "’", ".", " "}
    has_letter = False
    for char in normalized:
        if char in allowed_punctuation:
            continue
        category = unicodedata.category(char)
        if category.startswith("L") or category in {"Mn", "Mc"}:
            has_letter = True
            continue
        return None, (
            "Name can contain only letters, spaces, and standard separators "
            "(-, ', .). Please try again."
        )

    if not has_letter:
        return None, "Name must contain at least one letter. Please try again."

    if normalized[0] in allowed_punctuation or normalized[-1] in allowed_punctuation:
        return None, "Name must start and end with a letter. Please try again."

    return normalized, None
