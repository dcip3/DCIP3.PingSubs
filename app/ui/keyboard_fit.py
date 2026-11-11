"""Estimate whether button labels fit on a phone without being cut with "…".

Telegram gives every button in a row the same width and truncates a label that
does not fit. The budget below is in "units" (about one average letter at the
button font). It is ported from the owner's other bot, where it was calibrated
on an iPhone screenshot and scaled down to a common 390 pt phone, and the
character table is recalibrated for Latin script: narrow letters and
punctuation count half, ``m``/``w``/``M``/``W`` count 1.4, other lowercase
letters 0.85, other capitals 1.15, digits 0.9 and emoji 2.

Use ``keyboard_problems`` in tests: every keyboard the bot sends should come
back empty. Labels built from user data (member names, subscription names,
payment-method titles, timezone ids) should be shortened with ``fit_label``.

``misplaced_back_buttons`` checks the other layout rule: a way back
(``◀️ …``, ``⬅️ …``, ``✖️ Close``, ``✖️ Cancel``, ``Keep``) that shares a row
with other buttons is the leftmost one.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

# Units available for one label when the row holds 1, 2, 3, 4... buttons.
INLINE_ROW_BUDGET = {1: 27.0, 2: 12.5, 3: 7.5, 4: 5.0}
# Reply-keyboard buttons span the whole screen width.
REPLY_ROW_BUDGET = {1: 30.0, 2: 14.0, 3: 8.5, 4: 6.0}

ELLIPSIS = "…"

# NBSP and narrow NBSP are spaces too (money amounts group thousands with NBSP).
_NARROW = set(" \u00a0\u202f.,:;!|·'’()[]-–/ilIjtf1ʼ")
_WIDE = set("MWmw@%")
_ZERO = {"️", "︎", "‍"}


def _char_units(char: str) -> float:
    if char in _ZERO or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0.0
    code = ord(char)
    if 0x1F1E6 <= code <= 0x1F1FF:
        return 1.0  # half of a flag pair
    if code >= 0x1F000 or 0x2190 <= code <= 0x2BFF or 0x2300 <= code <= 0x23FF:
        return 2.0  # emoji and pictographs (📋 ⏰ ♻ ✅ ⏳ ◀ ✖ ...)
    if char in _NARROW:
        return 0.5
    if char in _WIDE:
        return 1.4
    if char.isdigit():
        return 0.9
    if char.isupper():
        return 1.15
    if "a" <= char <= "z":
        return 0.85
    return 1.0


def label_units(text: str) -> float:
    return round(sum(_char_units(char) for char in text), 2)


def row_budget(buttons_in_row: int, *, reply: bool = False) -> float:
    table = REPLY_ROW_BUDGET if reply else INLINE_ROW_BUDGET
    if buttons_in_row in table:
        return table[buttons_in_row]
    return table[max(table)] * max(table) / max(buttons_in_row, 1)


def fit_label(text: str, buttons_in_row: int = 1, *, reply: bool = False, tail: str = "") -> str:
    """Shorten ``text`` with "…" so ``text + tail`` fits the row.

    For labels made of user data. ``tail`` is the distinguishing part that must
    survive whole (``" · 12 500 RUB"``, ``" · 20.09"``); only ``text`` is cut.
    """
    budget = row_budget(buttons_in_row, reply=reply)
    if label_units(text + tail) <= budget:
        return text + tail
    result = ""
    for char in text:
        if label_units(result + char + ELLIPSIS + tail) > budget:
            break
        result += char
    return result.rstrip() + ELLIPSIS + tail


@dataclass(frozen=True)
class Overflow:
    row: int
    text: str
    units: float
    budget: float

    def __str__(self) -> str:
        return f"row {self.row}: {self.text!r} needs {self.units} > {self.budget}"


def _rows(markup: InlineKeyboardMarkup | ReplyKeyboardMarkup) -> list[list]:
    if isinstance(markup, ReplyKeyboardMarkup):
        return markup.keyboard
    return markup.inline_keyboard


def _is_disabled(button: object) -> bool:
    return getattr(button, "disabled", None) is not None


def keyboard_overflows(markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None) -> list[Overflow]:
    """Every button whose label would be cut on a common phone."""
    if markup is None:
        return []
    reply = isinstance(markup, ReplyKeyboardMarkup)
    found: list[Overflow] = []
    for index, row in enumerate(_rows(markup)):
        budget = row_budget(len(row), reply=reply)
        for button in row:
            units = label_units(button.text)
            if units > budget:
                found.append(Overflow(index, button.text, units, budget))
    return found


# ---------------------------------------------------------------- the way back

_EXIT_LABELS = {"✖ Close", "Keep"}


def is_back_button(text: str) -> bool:
    """A button that leaves the screen instead of acting on it.

    ``◀️ …`` / ``⬅️ …`` go back (Back, To card, To list, To users, the bare
    pagination ``◀️``), ``✖️ Close`` removes the message, ``Cancel`` (bare or
    with ``✖️`` / ``❌``) drops a confirmation or an input, and ``Keep`` is the
    safe side of a turn-off or undo question ([Keep][❌ Reject]).
    ``❌ Reject`` and ``✅ …`` are actions, not ways back.
    """
    plain = text.replace("️", "").strip()
    if plain.startswith(("◀", "⬅")):
        return True
    if plain in _EXIT_LABELS:
        return True
    return plain.lstrip("✖❌ ") == "Cancel"


@dataclass(frozen=True)
class MisplacedBack:
    row: int
    column: int
    text: str

    def __str__(self) -> str:
        return f"row {self.row}: {self.text!r} is button {self.column + 1}; the way back goes first (left)"


def misplaced_back_buttons(markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None) -> list[MisplacedBack]:
    """Every way-back button that sits to the right of an action in its row.

    In a row that holds a way back together with other buttons, the way back is
    the first (leftmost) button: [◀️ To list][🔄 Refresh], [✖️ Cancel][🗑 Delete],
    [Keep][❌ Reject]. The way back is then in the same corner on every screen.
    A disabled button (the "n / N" page counter) is not an action and is ignored.
    """
    if markup is None:
        return []
    found: list[MisplacedBack] = []
    for index, row in enumerate(_rows(markup)):
        seen_action = False
        for column, button in enumerate(row):
            if _is_disabled(button):
                continue
            if not is_back_button(button.text):
                seen_action = True
            elif seen_action:
                found.append(MisplacedBack(index, column, button.text))
    return found


def keyboard_problems(markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None) -> list[str]:
    """Labels cut on a phone and ways back out of place: what keyboard tests check."""
    return [str(item) for item in keyboard_overflows(markup)] + [
        str(item) for item in misplaced_back_buttons(markup)
    ]
