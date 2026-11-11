"""Reminder pushes: the Paid tap, the test Paid tap and the admin payment pushes (WP15).

* ``remind:<sub>:<due>`` (✅ Paid, guarded): a single reminder becomes the
  receipt '✅ <b>Paid · Netflix</b>' with no buttons; in a batch the tapped
  button greys to '☑️ Paid · Netflix · 17.09' and the push becomes
  '✅ <b>All paid</b>' once every bill is paid. A closed cycle greys its
  button to '🔒 …' with the toast 'This cycle is already closed'.
* ``testpaid:*``: the alert 'Test only: nothing recorded.' and '☑️ Paid (test)'.
* Admin pushes after a payment: '💸 <b>Ivan Petrov paid Netflix</b>' (+ the
  cycle-closed line), or '🔒 <b>Netflix cycle closed</b>' alone.
* ``sub:participants_manage_new``: the people picker as a NEW message with
  [✖️ Close] (from the 'has no participants' push).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from aiogram import Bot, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, DisabledButton, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.constants import PAYMENT_MODE_FIXED
from app.core.reminders import (
    DEFAULT_REMINDER_TIMEZONE,
    calculate_next_charge_date,
    normalize_monthly_anchor_day,
    normalize_timezone_name,
)
from app.services import calculate_share_base, normalize_payment_mode, parse_fixed_amount
from app.storage.db import Database
from app.ui.guards import guarded, pay_cycle_key
from app.ui.helpers import CYCLE_CLOSED, NO_ACCESS, SUBSCRIPTION_GONE, answer_callback_once, is_not_modified_error
from app.ui.keyboard_fit import fit_label
from app.ui.keyboards import (
    CYCLE_CLOSED_GREYED_TEXT,
    PAID_PREFIX,
    TEST_PAID_GREYED_TEXT,
    subscription_push_keyboard,
)
from app.ui.participant_screens import build_picker_screen
from app.ui.push_screens import (
    ALREADY_PAID_TOAST,
    PAID_TOAST,
    TEST_PAID_ALERT,
    batch_receipt_text,
    cycle_closed_text,
    paid_receipt_text,
    payment_recorded_text,
)
from app.ui.states import ReminderAction, SubscriptionAction, TestPaidAction
from app.ui.style import SEP
from app.ui.text import format_money

from . import admin_router, public_router

logger = logging.getLogger(__name__)

PAID_BUTTON_PREFIX = PAID_PREFIX
PAID_GREYED_PREFIX = "☑️ Paid"
CLOSED_GREYED_PREFIX = "🔒 Closed"
SINGLE_PAID_LABELS = ("✅ Paid", "Paid")
_TOTAL_RE = re.compile(r"total\s+(.+?)\s*$")


# ------------------------------------------------------------ greyed buttons


def _greyed_label(label: str, prefix: str, single_text: str) -> str:
    """``'☑️ Paid'`` for a lone Paid; ``'☑️ Paid · Netflix · 17.09'`` (name cut to fit) in a batch."""
    if label.startswith(PAID_BUTTON_PREFIX):
        label = label[len(PAID_BUTTON_PREFIX):]
    if label == "Paid":
        return single_text
    name, sep, tail = label.rpartition(SEP)
    if not sep:
        return fit_label(f"{prefix}{SEP}{label.rstrip('…').rstrip()}", 1)
    return fit_label(f"{prefix}{SEP}{name.rstrip('…').rstrip()}", 1, tail=f"{SEP}{tail}")


def grey_button(
    markup: InlineKeyboardMarkup | None,
    tapped_callback_data: str | None,
    label_for: Callable[[str], str],
) -> tuple[InlineKeyboardMarkup | None, int]:
    """Replace the tapped button with a disabled one labelled ``label_for(old_label)``.

    Returns the rebuilt markup (``None`` when the tapped button is not in the
    keyboard) and the number of callback buttons still waiting for a tap. Only
    callback buttons count: copy-text and URL rows never make a reminder look
    like it still has an unpaid item.
    """
    if markup is None or not markup.inline_keyboard or not tapped_callback_data:
        return None, 0
    rows: list[list[InlineKeyboardButton]] = []
    found = False
    remaining = 0
    for row in markup.inline_keyboard:
        new_row: list[InlineKeyboardButton] = []
        for button in row:
            if not found and button.callback_data == tapped_callback_data:
                new_row.append(InlineKeyboardButton(text=label_for(button.text), disabled=DisabledButton()))
                found = True
                continue
            if button.callback_data:
                remaining += 1
            new_row.append(button)
        rows.append(new_row)
    if not found:
        return None, remaining
    return InlineKeyboardMarkup(inline_keyboard=rows), remaining


def mark_paid_button(
    markup: InlineKeyboardMarkup | None,
    tapped_callback_data: str | None,
) -> tuple[InlineKeyboardMarkup | None, int]:
    """Grey out the tapped "Paid" button ('☑️ Paid' / '☑️ Paid · Netflix · 17.09'), keep every other row."""
    return grey_button(
        markup,
        tapped_callback_data,
        lambda label: _greyed_label(label, PAID_GREYED_PREFIX, PAID_GREYED_PREFIX),
    )


def mark_closed_button(
    markup: InlineKeyboardMarkup | None,
    tapped_callback_data: str | None,
) -> tuple[InlineKeyboardMarkup | None, int]:
    """Grey out a Paid whose cycle is closed: '🔒 Cycle closed' / '🔒 Closed · Netflix · 17.09'."""
    return grey_button(
        markup,
        tapped_callback_data,
        lambda label: _greyed_label(label, CLOSED_GREYED_PREFIX, CYCLE_CLOSED_GREYED_TEXT),
    )


def _tapped_label(markup: InlineKeyboardMarkup | None, data: str | None) -> Optional[str]:
    for row in (markup.inline_keyboard if markup else []):
        for button in row:
            if data and button.callback_data == data:
                return button.text
    return None


def _paid_labels(markup: InlineKeyboardMarkup) -> tuple[list[str], bool]:
    """Bills greyed as paid (``'Netflix · 17.09'``) and whether some bill was greyed as closed."""
    paid: list[str] = []
    closed = False
    for row in markup.inline_keyboard:
        for button in row:
            if button.disabled is None:
                continue
            if button.text.startswith(f"{PAID_GREYED_PREFIX}{SEP}"):
                paid.append(button.text[len(PAID_GREYED_PREFIX) + len(SEP):])
            elif button.text.startswith("🔒"):
                closed = True
    return paid, closed


def batch_total_from_text(text: Optional[str]) -> Optional[str]:
    """The total from a batch push's first line ('🔔 2 bills due · total 680.50 RUB')."""
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    match = _TOTAL_RE.search(first)
    return match.group(1) if match else None


# ------------------------------------------------------------ the mark-paid service


@dataclass(frozen=True)
class RecordedPayment:
    """What ``record_cycle_payment`` did, and the cycle it did it on (for the admin notes)."""

    subscription: Dict[str, Any]
    due_value: str
    cycle_closed: bool
    cycle_state: Dict[str, Any]
    cycle_participants: List[Dict[str, Any]]
    cycle_amount: float
    cycle_currency: str
    cycle_share_limit: Any
    settings_snapshot_ready: bool


async def record_cycle_payment(
    db: Database,
    subscription: Dict[str, Any],
    due_value: str,
    user_id: int,
) -> RecordedPayment:
    """The mark-paid service: record ``user_id``'s payment of the open cycle ``due_value``.

    Freezes the cycle snapshot, logs the payment, and closes the cycle (moving
    the next charge on) once every cycle participant has paid. The caller has
    already checked access and that the cycle is still open.
    """
    await db.freeze_cycle_snapshot(subscription["id"], due_value)

    cycle_state_map = await db.list_cycle_participants_for_due_dates(subscription["id"], [due_value])
    cycle_state = cycle_state_map.get(due_value) or {}
    cycle_participants = list(cycle_state.get("participants") or [])
    snapshot_ready = bool(cycle_state.get("snapshot_ready"))
    settings_snapshot_ready = bool(cycle_state.get("settings_snapshot_ready"))
    if not cycle_participants and not snapshot_ready:
        cycle_participants = await db.list_subscription_participants(subscription["id"])
    cycle_currency = str(cycle_state.get("currency") or subscription["currency"])
    cycle_share_limit = cycle_state.get("share_limit")
    try:
        cycle_amount = float(cycle_state.get("amount") or subscription["amount"])
    except (TypeError, ValueError):
        cycle_amount = float(subscription["amount"])
    if not settings_snapshot_ready:
        cycle_share_limit = subscription.get("share_limit")

    try:
        due_date = datetime.strptime(due_value, "%Y-%m-%d").date()
    except ValueError:
        due_date = datetime.today().date()

    await db.log_payment(subscription["id"], due_date, user_id)
    participant_ids = {int(p["telegram_id"]) for p in cycle_participants if p.get("telegram_id") is not None}
    cycle_closed = False
    if participant_ids:
        payments = await db.list_payments_for_cycles(subscription["id"], [due_value])
        paid_ids = {row["paid_by_telegram_id"] for row in payments if row.get("paid_by_telegram_id")}
        if participant_ids.issubset(paid_ids):
            await db.close_cycle(subscription["id"], due_value)
            cycle_closed = True
            if str(subscription.get("next_charge_at")) == str(due_value):
                period_days = int(subscription.get("period_days") or 30)
                monthly_anchor_day = normalize_monthly_anchor_day(subscription.get("monthly_anchor_day"))
                next_due = calculate_next_charge_date(due_date, period_days, monthly_anchor_day)
                await db.update_subscription_fields(subscription["id"], next_charge_at=next_due)
    return RecordedPayment(
        subscription=subscription,
        due_value=due_value,
        cycle_closed=cycle_closed,
        cycle_state=cycle_state,
        cycle_participants=cycle_participants,
        cycle_amount=cycle_amount,
        cycle_currency=cycle_currency,
        cycle_share_limit=cycle_share_limit,
        settings_snapshot_ready=settings_snapshot_ready,
    )


def computed_share(payment: RecordedPayment, user_id: int) -> float:
    """``user_id``'s share of the cycle when no reminder snapshot says otherwise."""
    participants = payment.cycle_participants
    share_base = calculate_share_base({"share_limit": payment.cycle_share_limit}, participants)
    person = next((p for p in participants if int(p.get("telegram_id") or 0) == user_id), None)
    weight = int(person.get("share_weight") or 1) if person else share_base
    mode = normalize_payment_mode(
        payment.cycle_state.get("payment_mode") if payment.settings_snapshot_ready else payment.subscription.get("payment_mode")
    )
    if mode == PAYMENT_MODE_FIXED:
        fixed = parse_fixed_amount(person.get("fixed_amount") if person else None)
        return (fixed if fixed is not None else payment.cycle_amount) * weight
    return payment.cycle_amount * weight / share_base


async def paid_amount_texts(
    db: Database,
    payment: RecordedPayment,
    user_id: int,
    reminder_message_id: Optional[int] = None,
) -> tuple[str, Optional[str]]:
    """``('4.17 USD', '380.50 RUB')``: the stored reminder share when it is this bill, else the computed share."""
    snapshot = None
    if reminder_message_id is not None:
        snapshot = await db.get_reminder_message(user_id, reminder_message_id)
    if snapshot and int(snapshot.get("subscription_id") or 0) == int(payment.subscription["id"]) and str(
        snapshot.get("due_date")
    ) == str(payment.due_value):
        amount = format_money(float(snapshot["share_amount"]), str(snapshot["share_currency"]))
        converted = snapshot.get("converted_display") or None
        return amount, str(converted) if converted else None
    full_share = computed_share(payment, user_id)
    # Paid from the card (no reminder message): when a reminder of this cycle
    # already spent part of the balance, the person paid only what was left.
    left = await db.get_reminder_share_left(payment.subscription["id"], payment.due_value, user_id)
    if left and str(left.get("share_currency") or "").strip().upper() == str(payment.cycle_currency).upper():
        try:
            left_value = float(left["share_amount"])
        except (TypeError, ValueError):
            left_value = full_share
        if 0 <= left_value < full_share - 0.005:
            return format_money(left_value, payment.cycle_currency), None
    return format_money(full_share, payment.cycle_currency), None


async def paid_progress(db: Database, payment: RecordedPayment) -> tuple[int, int]:
    """``(paid, total)`` among the cycle's participants after this payment."""
    participant_ids = {
        int(p["telegram_id"]) for p in payment.cycle_participants if p.get("telegram_id") is not None
    }
    rows = await db.list_payments_for_cycles(payment.subscription["id"], [payment.due_value])
    paid_ids = {int(row["paid_by_telegram_id"]) for row in rows if row.get("paid_by_telegram_id") is not None}
    return len(participant_ids & paid_ids), len(participant_ids)


async def _admin_timezone(db: Database) -> str:
    return normalize_timezone_name(
        await db.get_effective_base_timezone(DEFAULT_REMINDER_TIMEZONE),
        DEFAULT_REMINDER_TIMEZONE,
    ) or DEFAULT_REMINDER_TIMEZONE


async def _user_timezone(db: Database, user_id: int) -> str:
    return normalize_timezone_name(
        await db.get_effective_user_timezone(user_id, DEFAULT_REMINDER_TIMEZONE),
        DEFAULT_REMINDER_TIMEZONE,
    ) or DEFAULT_REMINDER_TIMEZONE


async def _payer_name(db: Database, user_id: int, fallback: str) -> str:
    """The stored user name (as admins know them), not the Telegram profile name."""
    friend = await db.get_friend_by_telegram(user_id)
    if friend and str(friend.get("full_name") or "").strip():
        return str(friend["full_name"]).strip()
    return (await db.get_admin_name(user_id)) or fallback or "Someone"


async def notify_admins_about_payment(
    bot: Bot,
    db: Database,
    payment: RecordedPayment,
    user_id: int,
    payer_name: str,
    *,
    reminder_message_id: Optional[int] = None,
) -> None:
    """Best-effort admin pushes after ``record_cycle_payment``.

    With the 'paid' toggle every admin but the payer gets '💸 <b>Ivan Petrov
    paid Netflix</b>' with the progress bar; when this payment closed the
    cycle and the 'closed' toggle is on too, the line '🔒 Cycle closed — …'
    is appended. An admin who gets no payment push (the toggle is off, or
    they paid themselves) gets '🔒 <b>Netflix cycle closed</b>' on its own.
    ``reminder_message_id`` is the reminder the Paid was tapped on, if any:
    its stored share (and conversion) is quoted instead of recomputing it.
    """
    subscription = payment.subscription
    notify_paid = await db.get_setting_bool("notify_admin_paid", True)
    notify_closed = await db.get_setting_bool("notify_admin_closed", True) and payment.cycle_closed
    admin_ids = await db.list_admin_ids()
    if not admin_ids or not (notify_paid or notify_closed):
        return
    tz_name = await _admin_timezone(db)
    fresh = await db.get_subscription(int(subscription["id"])) or subscription
    next_due = str(fresh.get("next_charge_at") or "") or None
    name = str(subscription.get("name") or "")
    markup = subscription_push_keyboard(int(subscription["id"]))

    paid_note: Optional[str] = None
    if notify_paid:
        amount_text, converted_text = await paid_amount_texts(db, payment, user_id, reminder_message_id)
        paid, total = await paid_progress(db, payment)
        paid_note = payment_recorded_text(
            payer_name=await _payer_name(db, user_id, payer_name),
            payer_id=user_id,
            subscription_name=name,
            due_value=payment.due_value,
            tz_name=tz_name,
            amount_text=amount_text,
            converted_text=converted_text,
            paid=paid,
            total=total,
            closed=notify_closed,
            next_due=next_due,
        )
    closed_note = (
        cycle_closed_text(name, format_money(payment.cycle_amount, payment.cycle_currency), next_due, tz_name)
        if notify_closed
        else None
    )
    for admin_id in admin_ids:
        text = paid_note if paid_note is not None and admin_id != user_id else closed_note
        if text is None:
            continue
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
        except TelegramAPIError as exc:
            logger.warning("Cannot notify admin %s about a payment: %s", admin_id, exc)


# ------------------------------------------------------------------ Paid tap


async def _edit_markup(message: Message, markup: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_reply_markup(reply_markup=markup)
    except TelegramAPIError as exc:
        if not (isinstance(exc, TelegramBadRequest) and is_not_modified_error(exc)):
            logger.warning("Cannot update the buttons of reminder %s: %s", message.message_id, exc)


async def _edit_text(message: Message, text: str) -> bool:
    """Turn the push into its final text with no buttons; False when it cannot be edited."""
    try:
        await message.edit_text(text, reply_markup=None)
    except TelegramAPIError as exc:
        if isinstance(exc, TelegramBadRequest) and is_not_modified_error(exc):
            return True
        logger.warning("Cannot turn reminder %s into a receipt: %s", message.message_id, exc)
        return False
    return True


async def _show_paid(
    callback: CallbackQuery,
    db: Database,
    payment: RecordedPayment,
    user_id: int,
) -> None:
    """Close the done screen: the single reminder becomes a receipt, a batch greys the bill."""
    message = callback.message
    if not isinstance(message, Message):
        return
    markup = message.reply_markup
    label = _tapped_label(markup, callback.data)
    if label is None:
        return
    if label in SINGLE_PAID_LABELS:
        amount_text, _ = await paid_amount_texts(db, payment, user_id, message.message_id)
        receipt = paid_receipt_text(
            str(payment.subscription.get("name") or ""),
            payment.due_value,
            await _user_timezone(db, user_id),
            amount_text,
            datetime.now(timezone.utc),
        )
        if await _edit_text(message, receipt):
            return
    updated, remaining = mark_paid_button(markup, callback.data)
    if updated is None:
        return
    paid_labels, some_closed = _paid_labels(updated)
    if label not in SINGLE_PAID_LABELS and remaining == 0 and not some_closed:
        if await _edit_text(message, batch_receipt_text(batch_total_from_text(message.text), paid_labels)):
            return
    await _edit_markup(message, updated)


async def _can_pay(db: Database, subscription: Dict[str, Any], due_value: str, user_id: int) -> bool:
    """A participant of that cycle (or an admin). Checked before touching any cycle state."""
    known_state = (await db.list_cycle_participants_for_due_dates(subscription["id"], [due_value])).get(
        due_value
    ) or {}
    known_participants = list(known_state.get("participants") or [])
    if not known_participants and not bool(known_state.get("snapshot_ready")):
        known_participants = await db.list_subscription_participants(subscription["id"])
    known_ids = {
        int(person["telegram_id"]) for person in known_participants if person.get("telegram_id") is not None
    }
    return user_id in known_ids or await db.is_admin(user_id)


async def _already_paid(db: Database, subscription_id: int, due_value: str, user_id: int) -> bool:
    rows = await db.list_payments_for_cycles(subscription_id, [due_value])
    return any(int(row.get("paid_by_telegram_id") or 0) == user_id for row in rows)


@public_router.callback_query(ReminderAction.filter())
async def handle_reminder_paid(callback: CallbackQuery, callback_data: ReminderAction, db: Database) -> None:
    """✅ Paid under a reminder (guarded): record, close the done screen, then tell the admins."""
    user_id = callback.from_user.id if callback.from_user else None
    subscription = await db.get_subscription(callback_data.subscription_id)
    due_value = callback_data.due_date
    # Authorize before touching cycle state so that a forged callback can neither
    # freeze snapshots nor probe which subscriptions and cycles exist.
    if not subscription:
        await answer_callback_once(callback, SUBSCRIPTION_GONE)
        return
    if user_id is None or not await _can_pay(db, subscription, due_value, user_id):
        await answer_callback_once(callback, NO_ACCESS)
        return

    payment: Optional[RecordedPayment] = None
    async with guarded(callback, pay_cycle_key(subscription["id"], due_value, user_id)) as acquired:
        if not acquired:
            return
        message = callback.message if isinstance(callback.message, Message) else None
        if await _already_paid(db, subscription["id"], due_value, user_id):
            # Paid meanwhile (member card, another tap): close the screen, record nothing.
            settled = await _settled_payment(db, subscription, due_value)
            await _show_paid(callback, db, settled, user_id)
            await answer_callback_once(callback, ALREADY_PAID_TOAST)
            return
        # Do not recreate the cycle here: a stale "Paid" button (after the cycle was
        # moved, reset, or closed) must not resurrect a ghost cycle at the old date.
        if due_value not in await db.list_open_cycles(subscription["id"]):
            if message is not None:
                updated, _ = mark_closed_button(message.reply_markup, callback.data)
                if updated is not None:
                    await _edit_markup(message, updated)
            await answer_callback_once(callback, CYCLE_CLOSED)
            return
        payment = await record_cycle_payment(db, subscription, due_value, user_id)
        await _show_paid(callback, db, payment, user_id)
        await answer_callback_once(callback, PAID_TOAST)

    if payment is not None and callback.bot is not None:
        payer = callback.from_user.full_name if callback.from_user else "Someone"
        await notify_admins_about_payment(
            callback.bot,
            db,
            payment,
            user_id,
            payer,
            reminder_message_id=callback.message.message_id if callback.message else None,
        )


async def _settled_payment(db: Database, subscription: Dict[str, Any], due_value: str) -> RecordedPayment:
    """The cycle as it is, for the receipt of a bill that was already paid (nothing is recorded)."""
    state = (await db.list_cycle_participants_for_due_dates(subscription["id"], [due_value])).get(due_value) or {}
    participants = list(state.get("participants") or [])
    if not participants and not bool(state.get("snapshot_ready")):
        participants = await db.list_subscription_participants(subscription["id"])
    snapshot_ready = bool(state.get("settings_snapshot_ready"))
    try:
        amount = float(state.get("amount") or subscription["amount"])
    except (TypeError, ValueError):
        amount = float(subscription["amount"])
    return RecordedPayment(
        subscription=subscription,
        due_value=due_value,
        cycle_closed=False,
        cycle_state=state,
        cycle_participants=participants,
        cycle_amount=amount,
        cycle_currency=str(state.get("currency") or subscription["currency"]),
        cycle_share_limit=state.get("share_limit") if snapshot_ready else subscription.get("share_limit"),
        settings_snapshot_ready=snapshot_ready,
    )


@public_router.callback_query(TestPaidAction.filter())
async def handle_test_reminder_paid(callback: CallbackQuery, callback_data: TestPaidAction) -> None:
    """✅ Paid under a test reminder: records nothing; the button greys to '☑️ Paid (test)'."""
    message = callback.message
    if isinstance(message, Message):
        updated, _ = grey_button(message.reply_markup, callback.data, lambda _label: TEST_PAID_GREYED_TEXT)
        if updated is not None:
            await _edit_markup(message, updated)
    await answer_callback_once(callback, TEST_PAID_ALERT, alert=True)


# ------------------------------------------------ picker from the admin push


@admin_router.callback_query(SubscriptionAction.filter(F.action == "participants_manage_new"))
async def handle_participants_manage_new(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    """[👥 Add participants] under 'Netflix has no participants': the picker as a NEW message with [✖️ Close]."""
    screen = await build_picker_screen(db, callback_data.subscription_id, close=True)
    if screen is None:
        await answer_callback_once(callback, SUBSCRIPTION_GONE)
        return
    text, new_markup = screen
    if isinstance(callback.message, Message):
        await callback.message.answer(text, reply_markup=new_markup)
    elif callback.bot is not None:
        await callback.bot.send_message(callback.from_user.id, text, reply_markup=new_markup)
    await answer_callback_once(callback)
