"""Member settings (WP12): the ⚙️ Settings screen of a member and its two pickers.

The screen texts, prompts, hints, toasts and banners are pure functions of a
``MemberSettings`` snapshot, so they can be tested without a bot. A member's
reminder time is a wall-clock time in the member's own timezone (that is how
the reminder worker applies it), so the default is stated in their timezone
too, never as "the admin's" time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.core.config import Settings
from app.core.reminders import (
    DEFAULT_REMINDER_TIMEZONE,
    normalize_time_string,
    normalize_timezone_name,
    parse_time_string,
)
from app.storage.db import Database
from app.ui.keyboards import MENU_SUBSCRIPTIONS, REMINDER_TIME_TEXT, TIMEZONE_TEXT
from app.ui.style import SEP, title
from app.ui.text import escape_html, utc_offset_label

SETTINGS_TITLE = title("⚙️", "Settings")
REMINDER_TIME_TITLE = title("⏰", "Reminder time")
TIMEZONE_TITLE = title("🌍", "Timezone")
SETTINGS_HINT = f"Each subscription can have its own time: open it in “{MENU_SUBSCRIPTIONS}”."
TIMEZONE_HINT = "Reminders arrive at your reminder time in this timezone."

TIMEZONE_PROMPT = (
    "✏️ Your timezone? A city zone like <code>Europe/Paris</code> or an offset like <code>UTC+3</code>."
)
TIME_TYPO_HINT = "⚠️ Need a time like <code>16:00</code>."
TIME_NOT_TEXT_HINT = "⚠️ Send text, for example <code>16:00</code>."
TIMEZONE_TYPO_HINT = "⚠️ Unknown timezone. Try <code>Europe/Paris</code> or <code>UTC+3</code>."
TIMEZONE_NOT_TEXT_HINT = "⚠️ Send text, for example <code>Europe/Paris</code>."
# A picker button on a message too old to edit.
SETTINGS_REOPEN_ALERT = "Open “⚙️ Settings” again."

# The "what changed" words of toasts and banners: the button names without emoji.
_TIME_NAME = REMINDER_TIME_TEXT.split(" ", 1)[1]
_TIMEZONE_NAME = TIMEZONE_TEXT.split(" ", 1)[1]


@dataclass(frozen=True)
class MemberSettings:
    time: str
    default_time: str
    has_time_override: bool
    timezone: str
    default_timezone: str
    has_timezone_override: bool


async def member_settings_snapshot(db: Database, settings: Settings, telegram_id: int) -> MemberSettings:
    """The member's effective time and timezone, the defaults, and whether they set their own."""
    default_time = parse_time_string(await db.get_setting("base_reminder_time"), settings.base_reminder_time).strftime(
        "%H:%M"
    )
    own_time = await db.get_user_setting(telegram_id, "base_reminder_time")
    default_timezone = (
        normalize_timezone_name(await db.get_setting("base_timezone"), settings.base_timezone)
        or DEFAULT_REMINDER_TIMEZONE
    )
    own_timezone = await db.get_user_setting(telegram_id, "timezone")
    return MemberSettings(
        time=parse_time_string(own_time, default_time).strftime("%H:%M"),
        default_time=default_time,
        has_time_override=bool(normalize_time_string(own_time)),
        timezone=normalize_timezone_name(own_timezone, default_timezone) or default_timezone,
        default_timezone=default_timezone,
        has_timezone_override=bool(normalize_timezone_name(own_timezone)),
    )


def zone_with_offset(zone: str, now: Optional[datetime] = None) -> str:
    """``'Asia/Dubai (UTC+4)'``; plain ``'UTC'`` for UTC (plain text, not escaped)."""
    offset = utc_offset_label(zone, now)
    return zone if offset == zone else f"{zone} ({offset})"


def member_settings_text(snapshot: MemberSettings, *, now: Optional[datetime] = None) -> str:
    """'⚙️ <b>Settings</b>' with the reminder time and timezone, defaults marked."""
    time_line = f"⏰ Reminder time: <b>{snapshot.time}</b> your time"
    if not snapshot.has_time_override:
        time_line += " (default)"
    details = []
    offset = utc_offset_label(snapshot.timezone, now)
    if offset != snapshot.timezone:
        details.append(offset)
    if not snapshot.has_timezone_override:
        details.append("default")
    timezone_line = f"🌍 Timezone: {escape_html(snapshot.timezone)}"
    if details:
        timezone_line += f" ({', '.join(details)})"
    return f"{SETTINGS_TITLE}\n\n{time_line}\n{timezone_line}\n\n{SETTINGS_HINT}"


def reminder_time_text(snapshot: MemberSettings) -> str:
    """The current value once; the default only when it differs (an own time is set)."""
    now = f"Now: <b>{snapshot.time}</b> in your timezone ({escape_html(snapshot.timezone)})"
    if not snapshot.has_time_override:
        return f"{REMINDER_TIME_TITLE}\n\n{now} (default)"
    return f"{REMINDER_TIME_TITLE}\n\n{now}\nDefault: {snapshot.default_time} in your timezone"


def timezone_text(snapshot: MemberSettings, *, now: Optional[datetime] = None) -> str:
    """The current zone once; the default only when an own zone is set."""
    current = f"Now: {escape_html(zone_with_offset(snapshot.timezone, now))}"
    if not snapshot.has_timezone_override:
        return f"{TIMEZONE_TITLE}\n\n{current} (default)\n{TIMEZONE_HINT}"
    return f"{TIMEZONE_TITLE}\n\n{current}\nDefault: {escape_html(snapshot.default_timezone)}\n{TIMEZONE_HINT}"


def reminder_time_prompt(zone: str) -> str:
    return (
        f"✏️ Your reminder time? <code>HH:MM</code> in {escape_html(zone)}, "
        "for example <code>16:00</code>."
    )


# --------------------------------------------------------- toasts and banners


def time_toast(value: Optional[str]) -> str:
    """'Reminder time: 12:00', or 'Reminder time: default' (``None``)."""
    return f"{_TIME_NAME}: {value or 'default'}"


def timezone_toast(zone: Optional[str], *, now: Optional[datetime] = None) -> str:
    """'Timezone: Asia/Dubai (UTC+4)', or 'Timezone: default' (``None``)."""
    return f"{_TIMEZONE_NAME}: {zone_with_offset(zone, now) if zone else 'default'}"


def time_banner(value: str, *, default: bool = False) -> str:
    """'✅ Reminder time · <b>16:30</b>' (' (default)' after the value when it is the default)."""
    return f"✅ {_TIME_NAME}{SEP}<b>{escape_html(value)}</b>" + (" (default)" if default else "")


def timezone_banner(zone: str, *, default: bool = False, now: Optional[datetime] = None) -> str:
    """'✅ Timezone · <b>Asia/Bangkok (UTC+7)</b>' (' (default)' when it is the default)."""
    return f"✅ {_TIMEZONE_NAME}{SEP}<b>{escape_html(zone_with_offset(zone, now))}</b>" + (
        " (default)" if default else ""
    )
