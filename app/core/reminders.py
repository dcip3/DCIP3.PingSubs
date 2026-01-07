from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Iterable, List, Sequence
from zoneinfo import ZoneInfo

from app.core.constants import DATE_INPUT_FORMAT, MONTHLY_PERIOD_SENTINEL

DEFAULT_REMINDER_TIME = "16:00"
DEFAULT_REMINDER_OFFSETS = [-1, 0]
REMINDER_TIMEZONE = ZoneInfo("Europe/Moscow")


def _add_month_same_day(current: date) -> date:
    year = current.year + (1 if current.month == 12 else 0)
    month = 1 if current.month == 12 else current.month + 1
    day = current.day
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1
            if day < 1:
                return date(year, month, 1)


def calculate_next_charge_date(current: date, period_days: int) -> date:
    if period_days == MONTHLY_PERIOD_SENTINEL:
        return _add_month_same_day(current)
    period = period_days or 30
    return current + timedelta(days=period)


def parse_time_string(value: str | None) -> time:
    raw = (value or DEFAULT_REMINDER_TIME).strip() or DEFAULT_REMINDER_TIME
    try:
        parsed = datetime.strptime(raw, "%H:%M")
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
