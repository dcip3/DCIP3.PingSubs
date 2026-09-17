from __future__ import annotations

import logging
from datetime import datetime

from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, DisabledButton, InlineKeyboardButton, InlineKeyboardMarkup

from app.core.constants import PAYMENT_MODE_FIXED
from app.core.reminders import format_due_date, normalize_monthly_anchor_day
from app.services import calculate_share_base, normalize_payment_mode, parse_fixed_amount
from app.ui.states import ReminderAction, TestPaidAction
from app.storage.db import Database
from app.ui.text import escape_html
from app.core.reminders import calculate_next_charge_date

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

    # Do not recreate the cycle here: a stale "Paid" button (after the cycle was
    # moved, reset, or closed) must not resurrect a ghost cycle at the old date.
    open_cycles = await db.list_open_cycles(subscription["id"])
    if due_value not in open_cycles:
        await callback.answer("This payment cycle is already closed.", show_alert=True)
        return

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
    participant_ids = {int(p["telegram_id"]) for p in cycle_participants}
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

    notify_paid = await db.get_setting_bool("notify_admin_paid", True)
    notify_closed = await db.get_setting_bool("notify_admin_closed", True)
    admin_ids = await db.list_admin_ids()
    if admin_ids and notify_paid:
        payer_name = callback.from_user.full_name if callback.from_user else "Someone"
        reminder_snapshot = None
        if callback.message and user_id is not None:
            reminder_snapshot = await db.get_reminder_message(user_id, callback.message.message_id)

        if reminder_snapshot:
            paid_amount = float(reminder_snapshot["share_amount"])
            paid_currency = escape_html(reminder_snapshot["share_currency"])
            amount_line = f"Amount: {paid_amount:.2f} {paid_currency}"
            converted_display = reminder_snapshot.get("converted_display")
            if converted_display:
                amount_line += f" (≈ {escape_html(converted_display)})"
        else:
            share_base = calculate_share_base({"share_limit": cycle_share_limit}, cycle_participants)
            weight = next(
                (
                    int(p.get("share_weight") or 1)
                    for p in cycle_participants
                    if int(p["telegram_id"]) == user_id
                ),
                share_base,
            )
            cycle_payment_mode = normalize_payment_mode(
                cycle_state.get("payment_mode") if settings_snapshot_ready else subscription.get("payment_mode")
            )
            fixed_amount = parse_fixed_amount(
                next(
                    (
                        p.get("fixed_amount")
                        for p in cycle_participants
                        if int(p["telegram_id"]) == user_id
                    ),
                    None,
                )
            )
            if cycle_payment_mode == PAYMENT_MODE_FIXED:
                base_amount = fixed_amount if fixed_amount is not None else cycle_amount
                paid_amount = base_amount * weight
            else:
                paid_amount = cycle_amount * weight / share_base
            amount_line = f"Amount: {paid_amount:.2f} {escape_html(cycle_currency)}"
        admin_note = (
            f"✅ Payment recorded: {escape_html(subscription['name'])} ({format_due_date(due_value)})\n"
            f"Payer: {escape_html(payer_name)}\n"
            f"{amount_line}"
        )
        for admin_id in admin_ids:
            if user_id is not None and admin_id == user_id:
                continue
            try:
                await callback.bot.send_message(admin_id, admin_note)
            except TelegramAPIError as exc:
                logger.warning("Cannot notify admin %s about a payment: %s", admin_id, exc)

    if admin_ids and notify_closed and cycle_closed:
        close_note = (
            f"✅ Cycle closed: {escape_html(subscription['name'])} "
            f"({format_due_date(due_value)})"
        )
        for admin_id in admin_ids:
            try:
                await callback.bot.send_message(admin_id, close_note)
            except TelegramAPIError as exc:
                logger.warning("Cannot notify admin %s about a closed cycle: %s", admin_id, exc)


@public_router.callback_query(TestPaidAction.filter())
async def handle_test_reminder_paid(callback: CallbackQuery, callback_data: TestPaidAction) -> None:
    await callback.answer("Test only: no payment recorded.", show_alert=True)
