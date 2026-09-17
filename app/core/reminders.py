from __future__ import annotations

import html
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, List, Sequence
from zoneinfo import ZoneInfo

from app.core.constants import DATE_INPUT_FORMAT, MONTHLY_PERIOD_SENTINEL

DEFAULT_REMINDER_TIME = "16:00"
DEFAULT_REMINDER_TIMEZONE = "Europe/Moscow"
DEFAULT_REMINDER_OFFSETS = [-1, 0]
# Calendar dates are anchored at noon so that the day never flips for a
# reader whose clock is within eleven hours of the subscription timezone.
DUE_DATE_ANCHOR = time(12, 0)


def normalize_monthly_anchor_day(value: object | None) -> int | None:
    if value is None:
        return None
    try:
        day = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= day <= 31:
        return day
    return None


def _add_month_same_day(current: date, anchor_day: int | None = None) -> date:
    year = current.year + (1 if current.month == 12 else 0)
    month = 1 if current.month == 12 else current.month + 1
    day = normalize_monthly_anchor_day(anchor_day) or current.day
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1
            if day < 1:
                return date(year, month, 1)


def calculate_next_charge_date(
    current: date,
    period_days: int,
    monthly_anchor_day: int | None = None,
) -> date:
    if period_days == MONTHLY_PERIOD_SENTINEL:
        return _add_month_same_day(current, monthly_anchor_day)
    period = period_days or 30
    return current + timedelta(days=period)


def normalize_time_string(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%H:%M")
    except ValueError:
        return None
    return parsed.strftime("%H:%M")


def normalize_timezone_name(value: str | None, default: str | None = None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return default
    try:
        ZoneInfo(raw)
    except Exception:  # noqa: BLE001
        return default
    return raw


def parse_timezone(value: str | None, default: str = DEFAULT_REMINDER_TIMEZONE) -> ZoneInfo:
    name = normalize_timezone_name(value, default) or DEFAULT_REMINDER_TIMEZONE
    return ZoneInfo(name)


def parse_time_string(value: str | None, default: str = DEFAULT_REMINDER_TIME) -> time:
    normalized = normalize_time_string(value) or normalize_time_string(default) or DEFAULT_REMINDER_TIME
    try:
        parsed = datetime.strptime(normalized, "%H:%M")
    except ValueError:
        parsed = datetime.strptime(DEFAULT_REMINDER_TIME, "%H:%M")
    return parsed.time()


def parse_offsets(raw: str | None) -> List[int]:
    try:
        data = json.loads(raw) if raw else DEFAULT_REMINDER_OFFSETS
    except json.JSONDecodeError:
        data = DEFAULT_REMINDER_OFFSETS
    values: List[int] = []
    for item in data:
        try:
            values.append(int(item))
        except (TypeError, ValueError):
            continue
    if not values:
        values = DEFAULT_REMINDER_OFFSETS.copy()
    return sorted(set(values))


def serialize_offsets(offsets: Sequence[int]) -> str:
    unique = sorted(set(int(value) for value in offsets))
    return json.dumps(unique)


def format_offsets_for_display(offsets: Iterable[int]) -> str:
    normalized = sorted(set(offsets))
    if not normalized:
        return "none"
    parts = []
    for value in normalized:
        if value == 0:
            parts.append("same day")
        elif value < 0:
            parts.append(f"{abs(value)} day(s) before")
        else:
            parts.append(f"{value} day(s) after")
    return ", ".join(parts)


def format_due_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return value
    return parsed.strftime(DATE_INPUT_FORMAT)


def tg_time(moment: datetime, fmt: str, fallback: str) -> str:
    """Render a Telegram date_time entity for the HTML parse mode.

    Clients show the moment in the reader's own timezone and language; ``fmt``
    follows the Bot API (``r`` relative, ``w`` weekday, ``d``/``D`` date,
    ``t``/``T`` time). ``fallback`` is what older clients and notifications
    display, so it must read well on its own. Naive datetimes are UTC.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (
        f'<tg-time unix="{int(moment.timestamp())}" format="{fmt}">'
        f"{html.escape(fallback, quote=False)}</tg-time>"
    )


def tg_due(due_value: str, tz_name: str | None, fmt: str = "wD", fallback: str | None = None) -> str:
    """Render a ``YYYY-MM-DD`` due date as a localized date entity."""
    try:
        due = datetime.strptime(due_value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return html.escape(str(due_value), quote=False)
    moment = datetime.combine(due, DUE_DATE_ANCHOR, tzinfo=parse_timezone(tz_name))
    return tg_time(moment, fmt, fallback if fallback is not None else format_due_date(due_value))


def tg_clock(
    time_value: str | None,
    tz_name: str | None,
    fallback: str | None = None,
    now: datetime | None = None,
) -> str:
    """Render a ``HH:MM`` wall-clock time as a localized time entity.

    The entity is anchored at the next occurrence of that time in
    ``tz_name`` (today if still ahead, otherwise tomorrow), so a reader in
    another timezone sees the same instant on their own clock. ``now`` is
    only meant for tests; naive values are UTC.
    """
    zone = parse_timezone(tz_name)
    wall = parse_time_string(time_value)
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(zone)
    moment = datetime.combine(current.date(), wall, tzinfo=zone)
    if moment <= current:
        moment = datetime.combine(current.date() + timedelta(days=1), wall, tzinfo=zone)
    label = fallback if fallback is not None else f"{wall.strftime('%H:%M')} ({zone.key})"
    return tg_time(moment, "t", label)


def due_status_html(due_value: str, tz_name: str | None, today: date) -> str:
    """Render ``Due in N day(s)`` / ``Due today`` / ``Overdue by N day(s)``.

    The relative part is a live entity on new clients (``in 3 days``,
    ``2 days ago``) while the status word stays outside it so the meaning is
    never lost; the due day itself is static text because a relative entity
    would read as ``in 5 hours``.
    """
    try:
        due = datetime.strptime(due_value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return "Unknown date"
    if due == today:
        return "Due today"
    days = abs((due - today).days)
    if due > today:
        return "Due " + tg_due(due_value, tz_name, "r", f"in {days} day(s)")
    return "Overdue " + tg_due(due_value, tz_name, "r", f"by {days} day(s)")
