"""Text helpers: escaping, names, plurals, money, relative time and loose input parsers.

Every count with a noun goes through ``plural``; every amount through
``format_money``. The parsers accept input the way people type it and return
``None`` when they cannot make sense of it.
"""
from __future__ import annotations

import html
import math
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

from app.ui.style import NBSP

UNICODE_MINUS = "−"
NAME_EXAMPLE = "<code>Anna Petrova</code>"


def escape_html(value: Any) -> str:
    return html.escape(str(value), quote=False)


def format_display_name(full_name: str | None) -> str:
    name = (full_name or "Unknown").strip()
    parts = [part for part in name.split() if part]
    first = parts[0] if parts else "Unknown"
    last_initial = f" {parts[1][0].upper()}." if len(parts) > 1 else ""
    return f"{first}{last_initial}"


def validate_person_name(raw_name: str | None) -> tuple[str | None, str | None]:
    """Normalize a typed person name, or return a one-line HTML hint with an example."""
    normalized = " ".join((raw_name or "").strip().split())
    if not normalized:
        return None, f"⚠️ Send a name, for example {NAME_EXAMPLE}."

    allowed_punctuation = {"-", "'", "’", ".", " "}
    has_letter = False
    for char in normalized:
        if char in allowed_punctuation:
            continue
        category = unicodedata.category(char)
        if category.startswith("L") or category in {"Mn", "Mc"}:
            has_letter = True
            continue
        return None, f"⚠️ Use only letters, spaces and - ' . in a name, for example {NAME_EXAMPLE}."

    if not has_letter:
        return None, f"⚠️ A name needs at least one letter, for example {NAME_EXAMPLE}."

    if normalized[0] in allowed_punctuation or normalized[-1] in allowed_punctuation:
        return None, f"⚠️ A name starts and ends with a letter, for example {NAME_EXAMPLE}."

    return normalized, None


# ------------------------------------------------------------------ plurals


def plural_word(n: float, singular: str, plural: str | None = None) -> str:
    """The noun form for ``n``: ``singular`` for 1 (or -1), else ``plural`` (default ``singular + 's'``)."""
    if abs(n) == 1:
        return singular
    return plural if plural is not None else f"{singular}s"


def plural(n: float, singular: str, plural: str | None = None) -> str:
    """``'1 day'`` / ``'2 days'`` / ``'0 users'``: the count with its noun."""
    number = int(n) if float(n).is_integer() else n
    return f"{number} {plural_word(n, singular, plural)}"


def format_days(n: int) -> str:
    return plural(n, "day")


def format_users(n: int) -> str:
    return plural(n, "user")


def format_shares(n: float) -> str:
    return plural(n, "share")


def format_cycles(n: int) -> str:
    return plural(n, "cycle")


def format_payments(n: int) -> str:
    return plural(n, "payment")


# -------------------------------------------------------------------- money


def _group_thousands(digits: str) -> str:
    groups = []
    while len(digits) > 3:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    groups.insert(0, digits)
    return NBSP.join(groups)


def format_number(amount: float, decimals: int = 2) -> str:
    """``12500`` -> ``'12 500.00'`` with NBSP thousands grouping."""
    text = f"{abs(amount):.{decimals}f}"
    whole, _, fraction = text.partition(".")
    sign = "-" if amount < 0 and float(text) != 0 else ""
    return sign + _group_thousands(whole) + (f".{fraction}" if fraction else "")


def format_money(
    amount: float | None,
    currency: str,
    *,
    approx: bool = False,
    bold: bool = False,
    decimals: int = 2,
) -> str:
    """``'1 200.00 RUB'``: NBSP thousands, NBSP before the currency, ``'≈ '`` when approximate.

    An unknown amount (``None``, NaN, infinity) renders as ``'?'``, never as zero.
    """
    try:
        number = float(amount) if amount is not None else math.nan
    except (TypeError, ValueError):
        number = math.nan
    shown = format_number(number, decimals) if math.isfinite(number) else "?"
    value = f"{shown}{NBSP}{escape_html(currency)}"
    if bold:
        value = f"<b>{value}</b>"
    return f"≈ {value}" if approx else value


# ------------------------------------------------------------ days and time


def days_left_phrase(days: int) -> str:
    """``'today'`` | ``'tomorrow'`` | ``'in 3 days'`` | ``'overdue by 5 days'``."""
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days > 1:
        return f"in {format_days(days)}"
    return f"overdue by {format_days(-days)}"


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def format_relative_past(moment: datetime, now: datetime | None = None) -> str:
    """``'just now'`` (≤5 min), ``'12 minutes ago'``, ``'2 hours ago'``, ``'yesterday'``,
    ``'3 days ago'``, then the date ``'05.11.2026'`` after 7 days. Naive datetimes are UTC."""
    moment = _as_utc(moment)
    current = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    elapsed = current - moment
    if elapsed <= timedelta(minutes=5):
        return "just now"
    if elapsed < timedelta(hours=1):
        return f"{plural(int(elapsed.total_seconds() // 60), 'minute')} ago"
    if elapsed < timedelta(days=1):
        return f"{plural(int(elapsed.total_seconds() // 3600), 'hour')} ago"
    if elapsed < timedelta(days=2):
        return "yesterday"
    if elapsed < timedelta(days=8):
        return f"{format_days(elapsed.days)} ago"
    return moment.strftime("%d.%m.%Y")


def short_relative(moment: datetime, now: datetime | None = None) -> str:
    """Button-sized age: ``'5m'``, ``'2h'``, ``'3d'``, then ``'05.11'`` after 7 days."""
    moment = _as_utc(moment)
    current = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    seconds = max(0, int((current - moment).total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 8 * 86400:
        return f"{seconds // 86400}d"
    return moment.strftime("%d.%m")


# ------------------------------------------------------------ input parsing

DEFAULT_WORDS = frozenset({"default", "-", "reset", "none", "clear"})

_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?")
_GROUPING_RE = re.compile(r"[\s   '’_]")


def _normalize_minus(text: str) -> str:
    return text.replace(UNICODE_MINUS, "-").replace("–", "-")


def is_default_word(text: str | None) -> bool:
    """True for ``default``, ``-``, ``reset``, ``none``, ``clear`` (any case, Unicode minus too)."""
    return _normalize_minus((text or "").strip()).lower() in DEFAULT_WORDS


def _ungroup(whole: str, separator: str) -> str | None:
    """``'1,200,000'`` -> ``'1200000'``; ``None`` unless groups are 1–3 digits then threes."""
    parts = whole.split(separator)
    if len(parts) > 1 and (not 1 <= len(parts[0]) <= 3 or any(len(part) != 3 for part in parts[1:])):
        return None
    return "".join(parts)


def parse_amount(text: str | None, allow_signed: bool = False) -> tuple[float, bool] | None:
    """Parse ``'1 200'``, ``'1,200.50'``, ``'149,99'`` into ``(value, is_relative)``.

    With ``allow_signed``, ``'+500'`` / ``'-200'`` (Unicode minus too) are
    relative changes: ``(500.0, True)`` / ``(-200.0, True)``. Without it a sign
    is rejected. Returns ``None`` for anything that is not a plain amount.
    """
    raw = _GROUPING_RE.sub("", _normalize_minus((text or "").strip()))
    if not raw:
        return None
    relative = raw[0] in "+-"
    negative = raw[0] == "-"
    if relative:
        if not allow_signed:
            return None
        raw = raw[1:]
    if "," in raw and "." in raw:
        # The later separator is the decimal one: '1,200.50' and '1.200,50'.
        decimal_sep = "." if raw.rfind(".") > raw.rfind(",") else ","
        whole, _, fraction = raw.rpartition(decimal_sep)
        whole = _ungroup(whole, "," if decimal_sep == "." else ".")
        raw = f"{whole}.{fraction}" if whole is not None else ""
    elif raw.count(",") > 1 or raw.count(".") > 1:
        raw = _ungroup(raw, "," if raw.count(",") > 1 else ".") or ""
    elif "," in raw or "." in raw:
        # One separator: followed by exactly three digits it groups thousands,
        # the same for ',' and '.': '1,200' and '1.200' are both 1200 (a RUB
        # user types '1.000' for a thousand). Otherwise it is the decimal point.
        separator = "," if "," in raw else "."
        whole, _, fraction = raw.partition(separator)
        grouped = _ungroup(raw, separator)
        if len(fraction) == 3 and whole.strip("0") and grouped is not None:
            raw = grouped
        else:
            raw = f"{whole}.{fraction}"
    if not _AMOUNT_RE.fullmatch(raw):
        return None
    value = float(raw)
    if not math.isfinite(value):
        return None
    return (-value if negative else value), relative


def parse_currency(text: str | None) -> str | None:
    """A three-letter currency code in any case -> ``'EUR'``."""
    raw = (text or "").strip()
    if len(raw) == 3 and raw.isascii() and raw.isalpha():
        return raw.upper()
    return None


_TIME_SPLIT_RE = re.compile(r"^(\d{1,2})\s*[:.\sh]\s*(\d{1,2})$")


def parse_time_loose(text: str | None) -> str | None:
    """``'9'``, ``'9:5'``, ``'09.30'``, ``'0930'``, ``'930'`` -> ``'HH:MM'``."""
    raw = (text or "").strip().lower()
    if not raw:
        return None
    match = _TIME_SPLIT_RE.match(raw)
    if match:
        hours, minutes = int(match.group(1)), int(match.group(2))
    elif raw.isdigit() and len(raw) <= 2:
        hours, minutes = int(raw), 0
    elif raw.isdigit() and len(raw) in (3, 4):
        hours, minutes = int(raw[:-2]), int(raw[-2:])
    else:
        return None
    if 0 <= hours <= 23 and 0 <= minutes <= 59:
        return f"{hours:02d}:{minutes:02d}"
    return None


# A 'DD.MM' that passed at most this many days ago stays in this year (a past date).
RECENT_PAST_DAYS = 183

_DATE_RE = re.compile(r"^(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}|\d{2}))?$")


def parse_date_loose(text: str | None, today: date) -> date | None:
    """``'DD.MM.YYYY'``, ``'D.M.YYYY'``, ``'DD.MM.YY'``, ``'DD.MM'``, ``'today'``, ``'tomorrow'``.

    ``'DD.MM'`` is the nearest occurrence. A day that passed at most
    ``RECENT_PAST_DAYS`` ago and is nearer than its next occurrence stays in
    the past (yesterday's charge date stays yesterday, so the caller's
    past-date warning can catch it); otherwise it is the next occurrence
    (29.02 waits for a leap year).
    """
    raw = (text or "").strip().lower()
    if raw == "today":
        return today
    if raw == "tomorrow":
        return today + timedelta(days=1)
    match = _DATE_RE.match(raw)
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    year_text = match.group(3)
    if year_text:
        year = int(year_text) + (2000 if len(year_text) == 2 else 0)
        try:
            return date(year, month, day)
        except ValueError:
            return None
    candidates = []
    for year in range(today.year - 1, today.year + 5):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    upcoming = next((candidate for candidate in candidates if candidate >= today), None)
    passed = [candidate for candidate in candidates if candidate < today]
    recent = passed[-1] if passed else None
    if recent is not None and (today - recent).days <= RECENT_PAST_DAYS:
        if upcoming is None or (today - recent) < (upcoming - today):
            return recent  # just passed: never silently a year ahead
    return upcoming


@lru_cache(maxsize=1)
def _timezones_by_lower_name() -> dict[str, str]:
    return {name.lower(): name for name in available_timezones()}


_OFFSET_RE = re.compile(r"^(?:utc|gmt)([+-])(\d{1,2})(?::?00)?$")


def parse_timezone_loose(text: str | None) -> str | None:
    """An IANA id in any case (``'asia/dubai'``), ``'UTC'``, or ``'UTC+3'`` / ``'GMT-5'``.

    Whole-hour offsets map to ``Etc/GMT∓N`` (the POSIX sign is inverted:
    ``UTC+3`` is ``Etc/GMT-3``). Returns ``None`` for anything else.
    """
    raw = _normalize_minus((text or "").strip())
    if not raw:
        return None
    compact = raw.lower().replace(" ", "")
    if compact in {"utc", "gmt", "z"}:
        return "UTC"
    match = _OFFSET_RE.match(compact)
    if match:
        sign, hours = match.group(1), int(match.group(2))
        if hours == 0:
            return "UTC"
        if (sign == "+" and hours > 14) or (sign == "-" and hours > 12):
            return None
        return f"Etc/GMT{'-' if sign == '+' else '+'}{hours}"
    name = _timezones_by_lower_name().get(raw.lower().replace(" ", "_"))
    if name:
        return name
    if "/" not in raw:
        return None
    try:
        ZoneInfo(raw)
    except Exception:  # noqa: BLE001 - any lookup failure means "not a timezone"
        return None
    return raw


def utc_offset_label(tz: str | tzinfo | None, now: datetime | None = None) -> str:
    """``'UTC+3'``, ``'UTC-5'``, ``'UTC+5:30'`` or ``'UTC'`` for a timezone at ``now``."""
    if tz is None:
        return "UTC"
    try:
        zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    except Exception:  # noqa: BLE001
        return "UTC"
    moment = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    offset = moment.astimezone(zone).utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    if minutes == 0:
        return "UTC"
    sign = "+" if minutes > 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")
