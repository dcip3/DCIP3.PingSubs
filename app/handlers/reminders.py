from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, DisabledButton, InlineKeyboardButton, InlineKeyboardMarkup

from app.core.constants import PAYMENT_MODE_FIXED
from app.core.reminders import (
    DEFAULT_REMINDER_TIMEZONE,
    calculate_next_charge_date,
    normalize_monthly_anchor_day,
    normalize_timezone_name,
)
from app.services import calculate_share_base, normalize_payment_mode, parse_fixed_amount
from app.ui.guards import guarded, pay_cycle_key
from app.ui.keyboards import subscription_push_keyboard
from app.ui.push_screens import cycle_closed_text, payment_recorded_text
from app.ui.states import ReminderAction, TestPaidAction
from app.storage.db import Database
from app.ui.text import format_money

from . import public_router

logger = logging.getLogger(__name__)

PAID_BUTTON_PREFIX = "✅ "


def mark_paid_button(
    markup: InlineKeyboardMarkup | None,
    tapped_callback_data: str | None,
) -> tuple[InlineKeyboardMarkup | None, int]:
    """Grey out the tapped "Paid" button and keep every other row as it is.

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
                label = button.text
                if label.startswith(PAID_BUTTON_PREFIX):
                    label = label[len(PAID_BUTTON_PREFIX):]
                paid_text = "☑️ Paid" if label == "Paid" else f"☑️ Paid · {label}"
                new_row.append(InlineKeyboardButton(text=paid_text, disabled=DisabledButton()))
                found = True
                continue
            if button.callback_data:
                remaining += 1
            new_row.append(button)
        rows.append(new_row)
    if not found:
        return None, remaining
    return InlineKeyboardMarkup(inline_keyboard=rows), remaining


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


@public_router.callback_query(ReminderAction.filter())
async def handle_reminder_paid(callback: CallbackQuery, callback_data: ReminderAction, db: Database) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    subscription = await db.get_subscription(callback_data.subscription_id)
    if not subscription or user_id is None:
        await callback.answer("You don't have access to this subscription.", show_alert=True)
        return

    due_value = callback_data.due_date
    # Authorize before touching cycle state so that a forged callback can neither
    # freeze snapshots nor probe which subscriptions and cycles exist.
    known_state = (await db.list_cycle_participants_for_due_dates(subscription["id"], [due_value])).get(
        due_value
    ) or {}
    known_participants = list(known_state.get("participants") or [])
    if not known_participants and not bool(known_state.get("snapshot_ready")):
        known_participants = await db.list_subscription_participants(subscription["id"])
    known_ids = {
        int(person["telegram_id"]) for person in known_participants if person.get("telegram_id") is not None
    }
    if user_id not in known_ids and not await db.is_admin(user_id):
        await callback.answer("You don't have access to this subscription.", show_alert=True)
        return

    async with guarded(callback, pay_cycle_key(subscription["id"], due_value, user_id)) as acquired:
        if not acquired:
            return
        # Do not recreate the cycle here: a stale "Paid" button (after the cycle was
        # moved, reset, or closed) must not resurrect a ghost cycle at the old date.
        open_cycles = await db.list_open_cycles(subscription["id"])
        if due_value not in open_cycles:
            await callback.answer("This payment cycle is already closed.", show_alert=True)
            return
        payment = await record_cycle_payment(db, subscription, due_value, user_id)
        await callback.answer("Payment recorded. Thank you!")
        if callback.message:
            updated_markup, remaining_buttons = mark_paid_button(
                getattr(callback.message, "reply_markup", None),
                callback.data,
            )
            if updated_markup is not None:
                try:
                    await callback.message.edit_reply_markup(reply_markup=updated_markup)
                except TelegramAPIError as exc:
                    logger.warning(
                        "Cannot grey out the Paid button for user %s (message %s): %s",
                        user_id,
                        callback.message.message_id,
                        exc,
                    )
                else:
                    logger.debug(
                        "Paid button greyed out for user %s; %s callback button(s) left",
                        user_id,
                        remaining_buttons,
                    )

    if callback.bot is not None:
        payer = callback.from_user.full_name if callback.from_user else "Someone"
        await notify_admins_about_payment(
            callback.bot,
            db,
            payment,
            user_id,
            payer,
            reminder_message_id=callback.message.message_id if callback.message else None,
        )


@public_router.callback_query(TestPaidAction.filter())
async def handle_test_reminder_paid(callback: CallbackQuery, callback_data: TestPaidAction) -> None:
    await callback.answer("Test only: no payment recorded.", show_alert=True)
