"""Reminders subtree of the admin subscription card (WP6).

* Reminders: '🔔 <b>Reminders · Netflix</b>' with Time, Days and After due
  date; [⏰ Reminder time] / [📅 Days] / [📣 After due date · 🟢] / [📬 Send now] / [◀️ To card].
* Reminder time: '⏰ <b>Reminder time · Netflix</b>', Default as a radio option,
  presets, [✏️ Other time], [◀️ To reminders].
* Send now: '📬 <b>Send now · Netflix</b>', the current cycle's progress, one
  row per person (unpaid first), [📬 Remind unpaid (2)]; every recipient opens
  the confirmation '⚠️ <b>Send the Netflix reminder to 2 people now?</b>'.

Prompts started here (Other time, Days) come back to Reminders with a result
banner, and so does the confirmed send.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from aiogram.types import InlineKeyboardMarkup, Message

from app.core.reminders import (
    DEFAULT_REMINDER_OFFSETS,
    format_offsets_for_display,
    normalize_time_string,
    parse_offsets,
    parse_time_string,
    tg_due,
)
from app.services import ReminderSendResult
from app.storage.db import Database
from app.ui.helpers import (
    SUBSCRIPTION_GONE_BANNER,
    edit_in_place,
    resolve_report_timezone,
    respond_with_markup,
    send_subscription_list,
    show_subscription_gone,
)
from app.ui.keyboards import (
    SEND_STATUS_PAID,
    SEND_STATUS_UNPAID,
    reminder_send_confirm_keyboard,
    reminder_send_empty_keyboard,
    reminder_send_targets_keyboard,
    after_due_line,
    reminder_settings_keyboard,
    subscription_reminder_time_keyboard,
)
from app.ui.states import Responder
from app.ui.style import SEP, join_blocks, title as screen_title, with_banner
from app.ui.text import escape_html, plural, plural_word

Screen = Tuple[str, InlineKeyboardMarkup]

REMINDER_TIME_NOTE = "Applied in each member's own timezone."
REMINDER_TIME_HINT = "⚠️ Need a time, for example <code>16:00</code>."
REMINDER_DAYS_HINT = "⚠️ Use whole numbers like <code>-1 0 2</code>."
MAX_REMINDER_OFFSET = 365
SEND_NOW_HINT = "People who have paid do not get it."
SEND_NOW_EMPTY_LINE = "No participants yet."
SEND_CONFIRM_CONSEQUENCE = "They get it right away, outside the schedule."
SENDING_TOAST = "⏳ Sending reminders…"
EVERYONE_PAID_TOAST = "Everyone has paid"
LIST_UPDATED_TOAST = "List updated"
NOTHING_SENT_NOT_DUE = "⚠️ Nothing sent: no one is due yet"
NOTHING_SENT_ALL_PAID = "⚠️ Nothing sent: everyone has paid"
NOTHING_SENT = "⚠️ Nothing sent"
DEFAULT_OFFSETS_TEXT = format_offsets_for_display(DEFAULT_REMINDER_OFFSETS)


def _name(subscription: Dict[str, object]) -> str:
    return escape_html(str(subscription.get("name") or ""))


def offsets_text(subscription: Dict[str, object]) -> str:
    """``'1 day before, on the day'`` (plain text)."""
    return format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))  # type: ignore[arg-type]


def reminder_override(subscription: Dict[str, object]) -> Optional[str]:
    """The subscription's own reminder time, or ``None`` when it uses the default."""
    return normalize_time_string(subscription.get("reminder_time"))  # type: ignore[arg-type]


async def base_reminder_time(db: Database) -> str:
    return parse_time_string(await db.get_effective_base_reminder_time()).strftime("%H:%M")


# ------------------------------------------------------------------ Reminders


def reminders_text(subscription: Dict[str, object], base_time: str) -> str:
    override = reminder_override(subscription)
    default_note = "" if override else " (default)"
    return join_blocks(
        screen_title("🔔", f"Reminders{SEP}{_name(subscription)}"),
        [
            f"Reminder time: <b>{escape_html(override or base_time)}</b> in each member's timezone{default_note}",
            f"Days: {escape_html(offsets_text(subscription))}",
            after_due_line(bool(subscription.get("remind_after_due"))),
        ],
    )


async def build_reminders_screen(db: Database, subscription_id: int, *, banner: Optional[str] = None) -> Optional[Screen]:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    text = with_banner(banner, reminders_text(subscription, await base_reminder_time(db)))
    return text, reminder_settings_keyboard(subscription_id, after_due=bool(subscription.get("remind_after_due")))


async def _show(target: Responder, db: Database, screen: Optional[Screen], *, toast: Optional[str]) -> Optional[Message]:
    if screen is None:
        await show_subscription_gone(target, db)
        return None
    text, markup = screen
    return await respond_with_markup(target, text, reply_markup=markup, toast=toast)


async def send_reminders_screen(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """Show Reminders (edit on a tap); a deleted subscription redraws the list instead."""
    return await _show(target, db, await build_reminders_screen(db, subscription_id, banner=banner), toast=toast)


async def show_reminders_after_prompt(
    message: Message,
    db: Database,
    subscription_id: int,
    flow_data: Dict[str, object],
    *,
    banner: str,
) -> None:
    """A typed value was saved: edit the screen the prompt came from into Reminders with a banner.

    Call after ``prompts.finish_prompt``. When that screen cannot be edited,
    Reminders is sent as a new message; a deleted subscription shows the list.
    """
    from app.ui.prompts import origin_of  # prompts imports helpers, which import this lazily

    screen = await build_reminders_screen(db, subscription_id, banner=banner)
    if screen is None:
        await send_subscription_list(message, db, banner=SUBSCRIPTION_GONE_BANNER)
        return
    text, markup = screen
    origin = origin_of(flow_data)
    if origin is not None and message.bot is not None:
        if await edit_in_place(message.bot, origin[0], origin[1], text, markup):
            return
    await message.answer(text, reply_markup=markup)


def after_due_toast(on: bool) -> str:
    return after_due_line(on)


# -------------------------------------------------------------- Reminder time


def reminder_time_text(subscription: Dict[str, object], base_time: str) -> str:
    override = reminder_override(subscription)
    now = escape_html(override) if override else f"{escape_html(base_time)} (default)"
    return join_blocks(
        screen_title("⏰", f"Reminder time{SEP}{_name(subscription)}"),
        [f"Now: {now}", REMINDER_TIME_NOTE],
    )


async def build_reminder_time_screen(db: Database, subscription_id: int) -> Optional[Screen]:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    base_time = await base_reminder_time(db)
    override = reminder_override(subscription)
    markup = subscription_reminder_time_keyboard(
        subscription_id,
        override or base_time,
        base_time,
        has_override=override is not None,
    )
    return reminder_time_text(subscription, base_time), markup


async def send_reminder_time_screen(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    toast: Optional[str] = None,
) -> Optional[Message]:
    return await _show(target, db, await build_reminder_time_screen(db, subscription_id), toast=toast)


def reminder_time_prompt_text(subscription: Dict[str, object]) -> str:
    return f"✏️ Reminder time for {_name(subscription)}? <code>HH:MM</code>, for example <code>16:00</code>."


def reminder_time_banner(value: str, *, default: bool = False) -> str:
    """``'✅ Reminder time · <b>16:00</b>'`` (``' (default)'`` after a reset)."""
    return f"✅ Reminder time{SEP}<b>{escape_html(value)}</b>{' (default)' if default else ''}"


def reminder_time_toast(value: str, *, default: bool = False) -> str:
    return f"Reminder time: {value}{' (default)' if default else ''}"


# ----------------------------------------------------------------------- Days


def reminder_days_prompt_text(subscription: Dict[str, object]) -> str:
    return (
        f"✏️ When should {_name(subscription)} remind? Now: {escape_html(offsets_text(subscription))}.\n"
        "Send day offsets: <code>-1 0</code> = 1 day before and on the day; "
        "<code>0 2</code> = on the day and 2 days after."
    )


def parse_offsets_input(text: Optional[str]) -> Optional[List[int]]:
    """``'-1 0 2'`` / ``'-1, 0'`` / ``'−1;+2'`` → sorted unique offsets; ``None`` for a typo."""
    cleaned = (text or "").replace("−", "-").replace(";", " ").replace(",", " ")
    parts = cleaned.split()
    if not parts:
        return None
    values: List[int] = []
    for part in parts:
        try:
            value = int(part)
        except ValueError:
            return None
        if abs(value) > MAX_REMINDER_OFFSET:
            return None
        values.append(value)
    return sorted(set(values))


def reminder_days_banner(offsets: Sequence[int]) -> str:
    return f"✅ Days{SEP}<b>{escape_html(format_offsets_for_display(offsets))}</b>"


def reminder_days_toast(offsets: Sequence[int]) -> str:
    return f"Days: {format_offsets_for_display(offsets)}"


# ------------------------------------------------------------------- Send now


@dataclass
class SendNowState:
    """Who would get a reminder now: the current cycle's progress and each person's status."""

    subscription: Dict[str, object]
    people: List[Dict[str, object]] = field(default_factory=list)
    cycle_due: Optional[str] = None
    paid: int = 0
    total: int = 0
    not_signed_in: List[str] = field(default_factory=list)
    has_participants: bool = False

    @property
    def unpaid(self) -> List[Dict[str, object]]:
        return [person for person in self.people if person.get("status") == SEND_STATUS_UNPAID]

    def person(self, telegram_id: int) -> Optional[Dict[str, object]]:
        return next((p for p in self.people if int(p.get("telegram_id") or 0) == int(telegram_id)), None)


def _cycle_people(cycle_state: Dict[str, object], live: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    """The cycle's snapshot, or the live participants while no snapshot exists (as the reminder pass does)."""
    people = list(cycle_state.get("participants") or [])  # type: ignore[call-overload]
    if not people and not cycle_state.get("snapshot_ready"):
        people = list(live)
    return people


def _signed_in_ids(people: Sequence[Dict[str, object]]) -> set[int]:
    return {int(p.get("telegram_id") or 0) for p in people if int(p.get("telegram_id") or 0) > 0}  # type: ignore[call-overload]


async def load_send_now_state(db: Database, subscription: Dict[str, object]) -> SendNowState:
    subscription_id = int(subscription["id"])  # type: ignore[call-overload]
    participants = await db.list_subscription_participants(subscription_id)
    state = SendNowState(subscription=subscription, has_participants=bool(participants))
    state.not_signed_in = [
        " ".join(str(p.get("full_name") or "Unknown").split())
        for p in participants
        if int(p.get("telegram_id") or 0) <= 0
    ]
    live_ids = _signed_in_ids(participants)
    open_cycles = await db.list_open_cycles(subscription_id)
    in_cycle: set[int] = set()
    unpaid: set[int] = set()
    if open_cycles:
        state_map = await db.list_cycle_participants_for_due_dates(subscription_id, open_cycles)
        paid_by_due: Dict[str, set[int]] = {}
        for row in await db.list_payments_for_cycles(subscription_id, open_cycles):
            if row.get("paid_by_telegram_id") is not None:
                paid_by_due.setdefault(str(row.get("due_date") or ""), set()).add(int(row["paid_by_telegram_id"]))
        for index, due_value in enumerate(open_cycles):
            ids = _signed_in_ids(_cycle_people(state_map.get(due_value) or {}, participants))
            paid_ids = ids & paid_by_due.get(due_value, set())
            in_cycle |= ids
            unpaid |= ids - paid_ids
            if index == 0:
                state.cycle_due, state.paid, state.total = due_value, len(paid_ids), len(ids)
    else:
        # No open cycle yet: sending opens the next one, and nobody has paid it.
        state.cycle_due = str(subscription.get("next_charge_at") or "") or None
        state.total = len(live_ids)
        in_cycle = unpaid = set(live_ids)
    order = {SEND_STATUS_UNPAID: 0, SEND_STATUS_PAID: 1, None: 2}
    people = []
    for person in participants:
        telegram_id = int(person.get("telegram_id") or 0)
        if telegram_id <= 0:
            continue
        if telegram_id in unpaid:
            status: Optional[str] = SEND_STATUS_UNPAID
        elif telegram_id in in_cycle:
            status = SEND_STATUS_PAID
        else:
            status = None
        people.append({"full_name": person.get("full_name"), "telegram_id": telegram_id, "status": status})
    people.sort(key=lambda p: (order[p["status"]], str(p.get("full_name") or "").casefold()))  # type: ignore[index]
    state.people = people
    return state


def send_now_text(state: SendNowState, tz_name: str) -> str:
    title = screen_title("📬", f"Send now{SEP}{_name(state.subscription)}")
    if not state.has_participants:
        return join_blocks(title, SEND_NOW_EMPTY_LINE)
    lines: List[Optional[str]] = []
    if state.cycle_due:
        progress = ""
        if state.total:
            progress = SEP + ("all paid" if state.paid >= state.total else f"{state.paid} of {state.total} paid")
        lines.append(f"Cycle: {tg_due(state.cycle_due, tz_name, 'wD')}{progress}")
    if state.not_signed_in:
        lines.append(f"⏳ Not signed in yet: {escape_html(', '.join(state.not_signed_in))}")
    lines.append(SEND_NOW_HINT)
    return join_blocks(title, lines)


async def build_send_now_screen(db: Database, subscription_id: int) -> Optional[Tuple[Screen, SendNowState]]:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    state = await load_send_now_state(db, subscription)
    text = send_now_text(state, await resolve_report_timezone(db))
    if not state.has_participants:
        return (text, reminder_send_empty_keyboard(subscription_id)), state
    return (text, reminder_send_targets_keyboard(subscription_id, state.people)), state


async def send_send_now_screen(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    toast: Optional[str] = None,
) -> Optional[Message]:
    built = await build_send_now_screen(db, subscription_id)
    return await _show(target, db, built[0] if built else None, toast=toast)


def send_confirm_text(subscription: Dict[str, object], *, count: int = 0, person_name: Optional[str] = None) -> str:
    """``'⚠️ <b>Send the Netflix reminder to 2 people now?</b>'`` and the consequence line."""
    who = escape_html(person_name) if person_name else plural(count, "person", "people")
    return f"⚠️ <b>Send the {_name(subscription)} reminder to {who} now?</b>\n{SEND_CONFIRM_CONSEQUENCE}"


def send_confirm_screen(
    subscription: Dict[str, object],
    telegram_id: int,
    *,
    count: int = 0,
    person_name: Optional[str] = None,
) -> Screen:
    return (
        send_confirm_text(subscription, count=count, person_name=person_name),
        reminder_send_confirm_keyboard(int(subscription["id"]), telegram_id),  # type: ignore[call-overload]
    )


def already_paid_toast(name: str) -> str:
    return f"{name} has already paid"


def send_result_banner(result: ReminderSendResult) -> str:
    """'✅ Reminder sent to <b>2</b> people', a delivery warning, or why nothing was sent."""
    lines: List[str] = []
    if result.sent_count:
        count = result.sent_count
        lines.append(f"✅ Reminder sent to <b>{count}</b> {plural_word(count, 'person', 'people')}")
    if result.failed_count:
        failed, blocked = result.failed_count, result.blocked_count
        if blocked == failed:
            reason = " (blocked the bot)"
        elif blocked:
            reason = f" ({blocked} blocked the bot)"
        else:
            reason = ""
        lines.append(f"⚠️ {failed} could not be delivered{reason}")
    if lines:
        return "\n".join(lines)
    if result.not_due:
        return NOTHING_SENT_NOT_DUE
    if result.skipped_paid:
        return NOTHING_SENT_ALL_PAID
    return NOTHING_SENT
