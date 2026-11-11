"""Admin settings (WP11): ⚙️ Settings, its pickers and prompts, and 🧪 Send a test reminder.

The texts, prompts, toasts and banners are pure functions of an
``AdminSettings`` snapshot, so they can be tested without a bot. The admin's
reminder time is the default for everyone: a wall-clock time in the admin
timezone, shown to the viewer on their own clock through a tg-time entity.
The labels come from the keyboard constants, so a button, the screen title it
opens and the toast after a pick always use the same words.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Sequence

from aiogram.types import InlineKeyboardMarkup, Message

from app.core.config import Settings
from app.core.reminders import DEFAULT_REMINDER_TIMEZONE, normalize_timezone_name, parse_time_string, tg_clock
from app.storage.db import Database
from app.ui.helpers import respond_with_markup
from app.ui.keyboards import (
    NOTIFY_OPTIONS,
    REMINDER_TIME_TEXT,
    ROUNDING_OPTIONS,
    ROUNDING_TEXT,
    SEND_TEST_TEXT,
    TIMEZONE_TEXT,
    admin_settings_keyboard,
    settings_tests_keyboard,
)
# The typo hints, toasts and banners of time and timezone are the member's
# (app.ui.member_settings): one wording for the same setting everywhere.
from app.ui.member_settings import zone_with_offset
from app.ui.states import Responder
from app.ui.style import SEP, title, with_banner
from app.ui.text import escape_html


def _title_of(label: str) -> str:
    """``'🔢 <b>Rounding</b>'`` from the button label ``'🔢 Rounding'``."""
    emoji, name = label.split(" ", 1)
    return title(emoji, name)


SETTINGS_TITLE = title("⚙️", "Settings")
REMINDER_TIME_TITLE = _title_of(REMINDER_TIME_TEXT)
TIMEZONE_TITLE = _title_of(TIMEZONE_TEXT)
ROUNDING_TITLE = _title_of(ROUNDING_TEXT)
NOTIFICATIONS_TITLE = title("🔔", "Admin notifications")
TESTS_TITLE = _title_of(SEND_TEST_TEXT)

SETTINGS_HINT = "Members can set their own time and timezone."
TIME_HINT = "The default for all reminders; members and subscriptions can override it."
TIMEZONE_HINT = "Used for the default reminder time and dates."
ROUNDING_HINT = "Applies to converted amounts in reminders."
NOTIFICATIONS_SCREEN_TEXT = f"{NOTIFICATIONS_TITLE}\n\nWhat the bot tells admins. Each toggle saves at once."
TESTS_HINT = "Pick a person. They get a sample reminder; tapping “Paid” records nothing."
TESTS_EMPTY = "No signed-in users yet. Add people in “👥 Users” first."

TIMEZONE_PROMPT = "✏️ Timezone? A city zone like <code>Europe/Paris</code> or an offset like <code>UTC+3</code>."

ROUNDING_MODES = tuple(mode for mode, _label in ROUNDING_OPTIONS)
DEFAULT_ROUNDING = "precise"
TESTS_PER_PAGE = 6

_ROUNDING_LABELS: Dict[str, str] = dict(ROUNDING_OPTIONS)
_ROUNDING_NAME = ROUNDING_TEXT.split(" ", 1)[1]
# settings_notify:<value> -> (settings key, toggle label without the emoji)
NOTIFY_KEYS: Dict[str, str] = {value: key for value, key, _label in NOTIFY_OPTIONS}
_NOTIFY_NAMES: Dict[str, str] = {value: label.split(" ", 1)[1] for value, _key, label in NOTIFY_OPTIONS}


@dataclass(frozen=True)
class AdminSettings:
    time: str
    timezone: str
    rounding: str
    notifications: tuple[bool, ...]  # in NOTIFY_OPTIONS order


async def admin_settings_snapshot(db: Database, settings: Settings) -> AdminSettings:
    """The stored defaults (the database wins over the in-memory ``settings``)."""
    time_value = parse_time_string(await db.get_setting("base_reminder_time"), settings.base_reminder_time).strftime(
        "%H:%M"
    )
    zone = (
        normalize_timezone_name(await db.get_setting("base_timezone"), settings.base_timezone)
        or DEFAULT_REMINDER_TIMEZONE
    )
    rounding = (await db.get_setting("currency_rounding") or settings.currency_rounding or "").strip().lower()
    if rounding not in _ROUNDING_LABELS:
        rounding = DEFAULT_ROUNDING
    notifications = tuple([await db.get_setting_bool(key, True) for _value, key, _label in NOTIFY_OPTIONS])
    return AdminSettings(time=time_value, timezone=zone, rounding=rounding, notifications=notifications)


# ------------------------------------------------------------------- texts


def rounding_name(mode: str) -> str:
    """'with cents', 'round to nearest': the button label in running text."""
    return _ROUNDING_LABELS.get(mode, _ROUNDING_LABELS[DEFAULT_ROUNDING]).lower()


def notifications_summary(states: Sequence[bool]) -> str:
    """'2 of 3 on'; 'all on' / 'all off' when every toggle agrees."""
    on = sum(1 for state in states if state)
    if on == len(states):
        return "all on"
    if on == 0:
        return "all off"
    return f"{on} of {len(states)} on"


def _your_clock(snapshot: AdminSettings, now: Optional[datetime]) -> str:
    return f"{tg_clock(snapshot.time, snapshot.timezone, snapshot.time, now)} your time"


def admin_settings_text(snapshot: AdminSettings, *, now: Optional[datetime] = None) -> str:
    """'⚙️ <b>Settings</b>' with one line per setting and the hint last."""
    return (
        f"{SETTINGS_TITLE}\n\n"
        f"⏰ Reminder time: <b>{snapshot.time}</b> ({_your_clock(snapshot, now)})\n"
        f"🌍 Timezone: {escape_html(zone_with_offset(snapshot.timezone, now))}\n"
        f"🔢 Rounding: {rounding_name(snapshot.rounding)}\n"
        f"🔔 Admin notifications: {notifications_summary(snapshot.notifications)}\n\n"
        f"{SETTINGS_HINT}"
    )


def reminder_time_text(snapshot: AdminSettings, *, now: Optional[datetime] = None) -> str:
    return (
        f"{REMINDER_TIME_TITLE}\n\n"
        f"Now: <b>{snapshot.time}</b> ({escape_html(snapshot.timezone)}){SEP}{_your_clock(snapshot, now)}\n"
        f"{TIME_HINT}"
    )


def timezone_text(snapshot: AdminSettings, *, now: Optional[datetime] = None) -> str:
    return f"{TIMEZONE_TITLE}\n\nNow: {escape_html(zone_with_offset(snapshot.timezone, now))}\n{TIMEZONE_HINT}"


def rounding_text(snapshot: AdminSettings) -> str:
    return f"{ROUNDING_TITLE}\n\nNow: {rounding_name(snapshot.rounding)}\n{ROUNDING_HINT}"


def time_prompt(zone: str) -> str:
    return (
        f"✏️ Default reminder time? <code>HH:MM</code> in {escape_html(zone)}, "
        "for example <code>16:00</code>."
    )


def rounding_toast(mode: str) -> str:
    """'Rounding: round down'."""
    return f"{_ROUNDING_NAME}: {rounding_name(mode)}"


def rounding_banner(mode: str) -> str:
    """'✅ Rounding · <b>round down</b>', as the other pickers' result banners."""
    return f"✅ {_ROUNDING_NAME}{SEP}<b>{escape_html(rounding_name(mode))}</b>"


def notification_toast(value: str, on: bool) -> str:
    """'Payment recorded: on'."""
    return f"{_NOTIFY_NAMES[value]}: {'on' if on else 'off'}"


def admin_settings_screen_of(
    snapshot: AdminSettings,
    *,
    banner: Optional[str] = None,
    now: Optional[datetime] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    return with_banner(banner, admin_settings_text(snapshot, now=now)), admin_settings_keyboard()


async def admin_settings_screen(
    db: Database,
    settings: Settings,
    *,
    banner: Optional[str] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    return admin_settings_screen_of(await admin_settings_snapshot(db, settings), banner=banner)


# ------------------------------------------------------- 🧪 Send a test reminder


def test_picker_text(*, empty: bool = False, banner: Optional[str] = None) -> str:
    return with_banner(banner, f"{TESTS_TITLE}\n\n{TESTS_EMPTY if empty else TESTS_HINT}")


def test_sent_banner(name: str) -> str:
    """'✅ Test sent to <b>Anna</b>'; its toast is the same line without tags."""
    return f"✅ Test sent to <b>{escape_html(name)}</b>"


def test_failed_alert(name: str) -> str:
    """'Could not send the test to Anna. Try again.' (an alert: plain text)."""
    return f"Could not send the test to {name}. Try again."


def test_people(friends: Sequence[dict]) -> list[dict]:
    """Signed-in people (they have a Telegram id), by name."""
    people = [friend for friend in friends if friend.get("telegram_id") is not None]
    return sorted(
        people,
        key=lambda item: (str(item.get("full_name") or "").casefold(), int(item.get("id") or 0)),
    )


def test_page_of(people: Sequence[dict], telegram_id: int, *, per_page: int = TESTS_PER_PAGE) -> Optional[int]:
    """The page of the picker that shows ``telegram_id``, or None when they are not listed."""
    for index, person in enumerate(people):
        if int(person["telegram_id"]) == telegram_id:
            return index // per_page + 1
    return None


def shown_page(message: object) -> int:
    """The page a picker message shows, read from its ``n / N`` counter (1 when unknown)."""
    markup = message.reply_markup if isinstance(message, Message) else None
    for row in getattr(markup, "inline_keyboard", None) or ():
        for button in row:
            if button.disabled is None or button.callback_data:
                continue
            current, _, total = button.text.partition(" / ")
            if current.isdigit() and total.isdigit():
                return int(current)
    return 1


async def send_test_picker(
    target: Responder,
    db: Database,
    *,
    page: int = 1,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> None:
    """🧪 Send a test reminder: one person per row, paged, [◀️ Back] to ⚙️ Settings.

    A tap edits the message in place (a typed tap sends it) and is answered
    once, with ``toast``; ``banner`` goes above the title after a send.
    """
    people = test_people(await db.list_friends())
    if not people:
        await respond_with_markup(
            target,
            test_picker_text(empty=True, banner=banner),
            reply_markup=settings_tests_keyboard([], page=1, total_pages=1),
            toast=toast,
        )
        return
    total_pages = max(1, (len(people) + TESTS_PER_PAGE - 1) // TESTS_PER_PAGE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * TESTS_PER_PAGE
    await respond_with_markup(
        target,
        test_picker_text(banner=banner),
        reply_markup=settings_tests_keyboard(
            people[start : start + TESTS_PER_PAGE],
            page=page,
            total_pages=total_pages,
            total_users=len(people),
        ),
        toast=toast,
    )
