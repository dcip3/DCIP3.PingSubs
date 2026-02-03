from __future__ import annotations

import contextlib
from datetime import datetime

from aiogram.types import CallbackQuery

from app.core.reminders import format_due_date
from app.services import calculate_share_base
from app.ui.states import ReminderAction, TestPaidAction
from app.storage.db import Database
from app.ui.text import escape_html
from app.core.reminders import calculate_next_charge_date

from . import public_router


@public_router.callback_query(ReminderAction.filter())
async def handle_reminder_paid(callback: CallbackQuery, callback_data: ReminderAction, db: Database) -> None:
    subscription = await db.get_subscription(callback_data.subscription_id)
    if not subscription:
        await callback.answer("Subscription not found.", show_alert=True)
        return

    due_value = callback_data.due_date
    await db.ensure_cycle(subscription["id"], due_value)
    open_cycles = await db.list_open_cycles(subscription["id"])
    if due_value not in open_cycles:
        await callback.answer("This payment cycle is already closed.", show_alert=True)
        return

    user_id = callback.from_user.id if callback.from_user else None
    is_authorized = False
    if user_id is not None:
        participants = await db.list_subscription_participants(subscription["id"])
        participant_ids = {person["telegram_id"] for person in participants}
        if user_id in participant_ids or await db.is_admin(user_id):
            is_authorized = True
    if not is_authorized:
        await callback.answer("Only users or admins can confirm payments.", show_alert=True)
        return

    try:
        due_date = datetime.strptime(due_value, "%Y-%m-%d").date()
    except ValueError:
        due_date = datetime.today().date()

    await db.log_payment(subscription["id"], due_date, user_id)
    participants = await db.list_subscription_participants(subscription["id"])
    participant_ids = {p["telegram_id"] for p in participants}
    cycle_closed = False
    if participant_ids:
        payments = await db.list_payments_for_cycles(subscription["id"], [due_value])
        paid_ids = {row["paid_by_telegram_id"] for row in payments if row.get("paid_by_telegram_id")}
        if participant_ids.issubset(paid_ids):
            await db.close_cycle(subscription["id"], due_value)
            cycle_closed = True
            if str(subscription.get("next_charge_at")) == str(due_value):
                period_days = int(subscription.get("period_days") or 30)
                next_due = calculate_next_charge_date(due_date, period_days)
                await db.update_subscription_fields(subscription["id"], next_charge_at=next_due)
    await callback.answer("Payment recorded. Thank you!")
    if callback.message:
        await callback.message.edit_text(
            "Payment recorded."
            f" Payment for {format_due_date(due_value)} confirmed."
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
            share_base = calculate_share_base(subscription, participants)
            weight = next(
                (int(p.get("share_weight") or 1) for p in participants if p["telegram_id"] == user_id),
                share_base,
            )
            paid_amount = float(subscription["amount"]) * weight / share_base
            amount_line = f"Amount: {paid_amount:.2f} {escape_html(subscription['currency'])}"
        admin_note = (
            f"✅ Payment recorded: {escape_html(subscription['name'])} ({format_due_date(due_value)})\n"
            f"Payer: {escape_html(payer_name)}\n"
            f"{amount_line}"
        )
        for admin_id in admin_ids:
            if user_id is not None and admin_id == user_id:
                continue
            with contextlib.suppress(Exception):
                await callback.bot.send_message(admin_id, admin_note)

    if admin_ids and notify_closed and cycle_closed:
        close_note = (
            f"✅ Cycle closed: {escape_html(subscription['name'])} "
            f"({format_due_date(due_value)})"
        )
        for admin_id in admin_ids:
            with contextlib.suppress(Exception):
                    await callback.bot.send_message(admin_id, close_note)


@public_router.callback_query(TestPaidAction.filter())
async def handle_test_reminder_paid(callback: CallbackQuery, callback_data: TestPaidAction) -> None:
    await callback.answer("Test only: no payment recorded.", show_alert=True)
