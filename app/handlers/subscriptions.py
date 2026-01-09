from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core.constants import DATE_INPUT_FORMAT, MONTHLY_PERIOD_SENTINEL
from app.storage.db import Database
from app.ui.helpers import (
    currency_prompt,
    get_edit_subscription_id,
    period_prompt,
    require_edit_subscription_id,
    send_pricing_settings,
    send_subscription_payment_report,
    send_reminder_send_menu,
    send_reminder_settings,
    send_subscription_detail,
    send_subscription_list,
    share_limit_prompt,
    start_subscription_edit_flow,
)
from app.ui.keyboards import admin_reply_keyboard, dialog_keyboard
from app.ui.states import ReminderSendAction, Responder, SubscriptionAction, SubscriptionEditForm, SubscriptionForm
from app.core.reminders import (
    DEFAULT_REMINDER_OFFSETS,
    format_offsets_for_display,
    parse_offsets,
    serialize_offsets,
)
from app.core.config import Settings
from app.services import CurrencyConverter, send_reminders_now
from app.ui.text import escape_html

from . import admin_router


def _response_target(responder: Responder) -> Message:
    return responder.message if isinstance(responder, CallbackQuery) else responder


def _parse_offset_input(text: str) -> Optional[List[int]]:
    cleaned = text.replace(";", " ").replace(",", " ")
    parts = [part for part in cleaned.split() if part]
    if not parts:
        return None
    values: List[int] = []
    for part in parts:
        try:
            values.append(int(part))
        except ValueError:
            return None
    return values


async def _load_subscription(
    callback: CallbackQuery,
    db: Database,
    subscription_id: int,
) -> Optional[Dict[str, object]]:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await callback.answer("Subscription not found.", show_alert=True)
        return None
    return subscription


async def start_subscription_creation(responder: Responder, state: FSMContext) -> None:
    target = _response_target(responder)
    await state.clear()
    await state.set_state(SubscriptionForm.name)
    await target.answer(
        "Enter the subscription name (for example, Netflix):",
        reply_markup=dialog_keyboard(),
    )


async def _finalize_new_subscription(
    responder: Responder,
    state: FSMContext,
    db: Database,
    share_limit: Optional[int],
) -> None:
    data = await state.get_data()
    sub_id = await db.create_subscription(
        name=data["subscription_name"],
        amount=float(data["amount"]),
        currency=data["currency"],
        due_date=data["due_date"],
        period_days=int(data["period_days"]),
        share_limit=share_limit,
    )
    await state.clear()

    target = _response_target(responder)
    period_days = int(data["period_days"])
    cadence = "monthly" if period_days == MONTHLY_PERIOD_SENTINEL else f"every {period_days} days"

    await target.answer(
        "Subscription saved.",
        reply_markup=admin_reply_keyboard(),
    )
    await send_subscription_detail(target, db, sub_id)
    if isinstance(responder, CallbackQuery):
        await responder.answer("Subscription saved")


@admin_router.message(F.text == "➕ Add subscription")
async def subscription_button(message: Message, state: FSMContext) -> None:
    await start_subscription_creation(message, state)


@admin_router.message(F.text == "📋 Subscriptions")
async def subs_button(message: Message, db: Database) -> None:
    await send_subscription_list(message, db)


@admin_router.message(SubscriptionForm.name)
async def subscription_form_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("Name cannot be empty.")
        return

    await state.update_data(subscription_name=title)
    await state.set_state(SubscriptionForm.amount)
    await message.answer("Charge amount (e.g. 149.99):")


@admin_router.message(SubscriptionForm.amount)
async def subscription_form_amount(message: Message, state: FSMContext) -> None:
    try:
        raw = (message.text or "").strip().replace(",", ".")
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Amount must be a positive number. Try again.")
        return

    await state.update_data(amount=amount)
    await state.set_state(SubscriptionForm.currency)
    text, markup = currency_prompt()
    await message.answer(text, reply_markup=markup)


@admin_router.message(SubscriptionForm.currency)
async def subscription_form_currency(message: Message, state: FSMContext) -> None:
    currency = (message.text or "").strip().upper()
    if len(currency) != 3:
        await message.answer("Currency must contain 3 letters, e.g. TRY.")
        return

    await state.update_data(currency=currency)
    await state.set_state(SubscriptionForm.due_date)
    await message.answer("Next charge date (DD.MM.YYYY):")


@admin_router.callback_query(F.data.startswith("currency:"))
async def handle_currency_quick_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    raw_value = callback.data.split(":", 1)[1].upper()
    current_state = await state.get_state()
    if current_state == SubscriptionForm.currency.state:
        await state.update_data(currency=raw_value)
        await state.set_state(SubscriptionForm.due_date)
        await callback.message.answer("Next charge date (DD.MM.YYYY):")
        await callback.answer(f"Currency set to {raw_value}")
        return

    if current_state == SubscriptionEditForm.currency.state:
        sub_id = await get_edit_subscription_id(state)
        if sub_id is None:
            await callback.answer("Session expired. Reopen the subscription.", show_alert=True)
            return
        await db.update_subscription_fields(sub_id, currency=raw_value)
        await state.clear()
        await callback.answer(f"Currency set to {raw_value}")
        await send_subscription_detail(callback, db, sub_id)
        return

    await callback.answer()


@admin_router.message(SubscriptionForm.due_date)
async def subscription_form_due(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    normalized = raw.replace("-", ".")
    try:
        due = datetime.strptime(normalized, DATE_INPUT_FORMAT).date()
    except ValueError:
        await message.answer("Date must be in DD.MM.YYYY format. Try again.")
        return

    await state.update_data(due_date=due)
    await state.set_state(SubscriptionForm.period)
    text_prompt, markup_prompt = period_prompt()
    await message.answer(text_prompt, reply_markup=markup_prompt)


@admin_router.message(SubscriptionForm.period)
async def subscription_form_period(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text:
        lowered = text.lower()
        if lowered in {"month", "monthly"}:
            period = MONTHLY_PERIOD_SENTINEL
        else:
            try:
                period = int(text)
                if period <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("Period must be a positive integer or 'monthly'. Try again.")
                return
    else:
        period = 30

    await state.update_data(period_days=period)
    await state.set_state(SubscriptionForm.share_limit)
    text_prompt, markup_prompt = share_limit_prompt()
    await message.answer(text_prompt, reply_markup=markup_prompt)


@admin_router.callback_query(F.data.startswith("sharelimit:"))
async def handle_share_limit_quick_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    value = callback.data.split(":", 1)[1].lower()
    if value == "all":
        share_limit: Optional[int] = None
    else:
        await callback.answer("Unsupported option.", show_alert=True)
        return
    current_state = await state.get_state()
    if current_state == SubscriptionForm.share_limit.state:
        await _finalize_new_subscription(callback, state, db, share_limit)
        return

    if current_state == SubscriptionEditForm.share_limit.state:
        sub_id = await get_edit_subscription_id(state)
        if sub_id is None:
            await callback.answer("Session expired. Reopen the subscription.", show_alert=True)
            return
        await db.update_subscription_fields(sub_id, share_limit=share_limit)
        await state.clear()
        await callback.answer("Split limit updated.")
        await send_subscription_detail(callback, db, sub_id)
        return

    await callback.answer()


@admin_router.callback_query(F.data.startswith("period:"))
async def handle_period_quick_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    value = callback.data.split(":", 1)[1]
    if value.lower() == "month":
        days = MONTHLY_PERIOD_SENTINEL
    else:
        try:
            days = int(value)
            if days <= 0:
                raise ValueError
        except ValueError:
            await callback.answer("Invalid period value.", show_alert=True)
            return

    current_state = await state.get_state()
    if current_state == SubscriptionForm.period.state:
        await state.update_data(period_days=days)
        await state.set_state(SubscriptionForm.share_limit)
        text_prompt, markup_prompt = share_limit_prompt()
        await callback.message.answer(text_prompt, reply_markup=markup_prompt)
        label = "monthly" if days == MONTHLY_PERIOD_SENTINEL else f"{days} day(s)"
        await callback.answer(f"Period set to {label}")
        return

    if current_state == SubscriptionEditForm.period.state:
        sub_id = await get_edit_subscription_id(state)
        if sub_id is None:
            await callback.answer("Session expired. Reopen the subscription.", show_alert=True)
            return
        await db.update_subscription_fields(sub_id, period_days=days)
        await state.clear()
        label = "monthly" if days == MONTHLY_PERIOD_SENTINEL else f"{days} day(s)"
        await callback.answer(f"Period set to {label}")
        await send_subscription_detail(callback, db, sub_id)
        return

    await callback.answer()


@admin_router.message(SubscriptionForm.share_limit)
async def subscription_form_share_limit(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    share_limit: Optional[int]
    if text:
        try:
            share_limit = int(text)
            if share_limit <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Share count must be a positive integer or empty. Try again.")
            return
    else:
        share_limit = None

    await _finalize_new_subscription(message, state, db, share_limit)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "open"))
async def handle_subscription_open(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    await send_subscription_detail(callback, db, callback_data.subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "create"))
async def handle_subscription_create(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await start_subscription_creation(callback, state)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "back"))
async def handle_subscription_back(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    await send_subscription_list(callback, db)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "rename"))
async def handle_subscription_rename_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    prompt = f"Send the new subscription name (current: {escape_html(subscription['name'])})."
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.rename,
        prompt,
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "pricing"))
async def handle_subscription_pricing_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    await send_pricing_settings(callback, db, callback_data.subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "amount"))
async def handle_subscription_amount_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    prompt = (
        f"Send the new amount (e.g. 149.99). "
        f"Current value: {subscription['amount']:.2f} {escape_html(subscription['currency'])}."
    )
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.amount,
        prompt,
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "currency"))
async def handle_subscription_currency_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    _, prompt_markup = currency_prompt()
    prompt_text = (
        "Choose a currency or type your own (3 letters).\n"
        f"Current value: {escape_html(subscription['currency'])}."
    )
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.currency,
        prompt_text,
        reply_markup=prompt_markup,
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "duedate"))
async def handle_subscription_due_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    try:
        current = datetime.strptime(subscription["next_charge_at"], "%Y-%m-%d").strftime(DATE_INPUT_FORMAT)
    except (KeyError, ValueError):
        current = subscription.get("next_charge_at", "unknown")
    prompt = f"Send the next charge date (DD.MM.YYYY). Current date: {escape_html(current)}."
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.due_date,
        prompt,
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "period"))
async def handle_subscription_period_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    period_text, period_markup = period_prompt()
    current_period = int(subscription.get("period_days") or 30)
    if current_period == MONTHLY_PERIOD_SENTINEL:
        current_label = "monthly"
    else:
        current_label = f"{current_period} day(s)"
    prompt = f"{period_text}\nCurrent value: {current_label}."
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.period,
        prompt,
        reply_markup=period_markup,
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "reminders"))
async def handle_subscription_reminders_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    await send_reminder_settings(callback, db, callback_data.subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "report"))
async def handle_subscription_report_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    await send_subscription_payment_report(callback, db, callback_data.subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "reminders_send"))
async def handle_subscription_reminders_send(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    await send_reminder_send_menu(callback, db, callback_data.subscription_id)


@admin_router.callback_query(ReminderSendAction.filter())
async def handle_subscription_reminders_send_target(
    callback: CallbackQuery,
    callback_data: ReminderSendAction,
    bot: Bot,
    settings: Settings,
    db: Database,
    converter: CurrencyConverter,
    state: FSMContext,
) -> None:
    await state.clear()
    target_id = callback_data.telegram_id or None
    sent_count = await send_reminders_now(
        bot,
        db,
        converter,
        subscription_id=callback_data.subscription_id,
        rounding_mode=settings.currency_rounding,
        target_telegram_id=target_id,
    )
    await callback.answer(f"Sent {sent_count} notification(s).")
    await send_reminder_settings(callback, db, callback_data.subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "share"))
async def handle_subscription_share_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    _, prompt_markup = share_limit_prompt()
    current_share = subscription.get("share_limit")
    current_label = f"{current_share} user(s)" if current_share else "all users"
    prompt_text = (
        "Send the number of users who split this subscription or tap “Split across all”.\n"
        f"Current setting: {escape_html(current_label)}."
    )
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.share_limit,
        prompt_text,
        reply_markup=prompt_markup,
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "remindertime"))
async def handle_subscription_reminder_time_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    current_time = (subscription.get("reminder_time") or "16:00").strip() or "16:00"
    prompt = f"Send the reminder time in HH:MM (Moscow time). Current value: {escape_html(current_time)}."
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.reminder_time,
        prompt,
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "reminderdays"))
async def handle_subscription_reminder_days_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    current_offsets = format_offsets_for_display(
        parse_offsets(subscription.get("reminder_offsets"))
    )
    prompt = (
        "Send reminder offsets as numbers separated by commas or spaces "
        "(e.g. -1 0 1). Negative = before, positive = after.\n"
        f"Current schedule: {escape_html(current_offsets)}."
    )
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.reminder_offsets,
        prompt,
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "reminderloop"))
async def handle_subscription_reminder_loop_toggle(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    subscription = await db.get_subscription(callback_data.subscription_id)
    if not subscription:
        await callback.answer("Subscription not found.", show_alert=True)
        return

    new_value = 0 if subscription.get("remind_after_due") else 1
    await db.update_subscription_fields(callback_data.subscription_id, remind_after_due=new_value)
    status = "enabled" if new_value else "disabled"
    await callback.answer(f"Post-due reminders {status}.")
    await send_reminder_settings(callback, db, callback_data.subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "delete"))
async def handle_subscription_delete_prompt(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
) -> None:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Yes, delete",
        callback_data=SubscriptionAction(
            action="confirm_delete",
            subscription_id=callback_data.subscription_id,
        ).pack(),
    )
    builder.button(
        text="⬅️ Back",
        callback_data=SubscriptionAction(action="open", subscription_id=callback_data.subscription_id).pack(),
    )
    await callback.message.edit_text(
        "Are you sure you want to delete this subscription?",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@admin_router.callback_query(SubscriptionAction.filter(F.action == "confirm_delete"))
async def handle_subscription_delete_confirm(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    await db.delete_subscription(callback_data.subscription_id)
    await callback.answer("Subscription deleted.")
    await send_subscription_list(callback, db)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "cancel_edit"))
async def handle_subscription_edit_cancel(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    await callback.answer("Canceled.")
    await send_subscription_detail(callback, db, callback_data.subscription_id)


@admin_router.message(SubscriptionEditForm.rename)
async def edit_subscription_rename(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    new_name = (message.text or "").strip()
    if not new_name:
        await message.answer("Name cannot be empty.")
        return

    await db.update_subscription_fields(sub_id, name=new_name)
    await state.clear()
    await message.answer("Name updated.", reply_markup=admin_reply_keyboard())
    await send_subscription_detail(message, db, sub_id)


@admin_router.message(SubscriptionEditForm.amount)
async def edit_subscription_amount(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    try:
        raw = (message.text or "").strip().replace(",", ".")
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Amount must be a positive number. Try again.")
        return

    await db.update_subscription_fields(sub_id, amount=amount)
    await state.clear()
    await message.answer("Amount updated.", reply_markup=admin_reply_keyboard())
    await send_subscription_detail(message, db, sub_id)


@admin_router.message(SubscriptionEditForm.currency)
async def edit_subscription_currency(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    currency = (message.text or "").strip().upper()
    if len(currency) != 3:
        await message.answer("Currency must contain exactly 3 letters, e.g. USD.")
        return

    await db.update_subscription_fields(sub_id, currency=currency)
    await state.clear()
    await message.answer("Currency updated.", reply_markup=admin_reply_keyboard())
    await send_subscription_detail(message, db, sub_id)


@admin_router.message(SubscriptionEditForm.due_date)
async def edit_subscription_due_date(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    raw = (message.text or "").strip()
    normalized = raw.replace("-", ".")
    try:
        due_date = datetime.strptime(normalized, DATE_INPUT_FORMAT).date()
    except ValueError:
        await message.answer("Date must be in DD.MM.YYYY format. Try again.")
        return

    await db.update_subscription_fields(sub_id, next_charge_at=due_date)
    await state.clear()
    await message.answer("Next charge date updated.", reply_markup=admin_reply_keyboard())
    await send_subscription_detail(message, db, sub_id)


@admin_router.message(SubscriptionEditForm.period)
async def edit_subscription_period(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    try:
        text = (message.text or "").strip()
        lower = text.lower()
        if lower in {"month", "monthly"}:
            period = MONTHLY_PERIOD_SENTINEL
        else:
            period = int(text)
            if period <= 0:
                raise ValueError
    except ValueError:
        await message.answer("Period must be a positive integer or 'monthly'.")
        return

    await db.update_subscription_fields(sub_id, period_days=period)
    await state.clear()
    await message.answer("Period updated.", reply_markup=admin_reply_keyboard())
    await send_subscription_detail(message, db, sub_id)


@admin_router.message(SubscriptionEditForm.share_limit)
async def edit_subscription_share(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    text = (message.text or "").strip()
    share_limit: Optional[int]
    if text:
        try:
            share_limit = int(text)
            if share_limit <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Share count must be a positive integer or empty.")
            return
    else:
        share_limit = None

    await db.update_subscription_fields(sub_id, share_limit=share_limit)
    await state.clear()
    await message.answer("Split limit updated.", reply_markup=admin_reply_keyboard())
    await send_subscription_detail(message, db, sub_id)


@admin_router.message(SubscriptionEditForm.reminder_time)
async def edit_subscription_reminder_time(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    candidate = (message.text or "").strip()
    try:
        datetime.strptime(candidate, "%H:%M")
    except ValueError:
        await message.answer("Time must be in HH:MM format (24-hour clock).")
        return

    await db.update_subscription_fields(sub_id, reminder_time=candidate)
    await state.clear()
    await message.answer("Reminder time updated.", reply_markup=admin_reply_keyboard())
    await send_subscription_detail(message, db, sub_id)


@admin_router.message(SubscriptionEditForm.reminder_offsets)
async def edit_subscription_reminder_offsets(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    text = (message.text or "").strip()
    if text:
        offsets = _parse_offset_input(text)
        if offsets is None:
            await message.answer(
                "Use integers separated by commas or spaces (example: -1 0). Negative = before, positive = after."
            )
            return
    else:
        offsets = DEFAULT_REMINDER_OFFSETS.copy()

    await db.update_subscription_fields(sub_id, reminder_offsets=serialize_offsets(offsets))
    await state.clear()
    await message.answer("Reminder schedule updated.", reply_markup=admin_reply_keyboard())
    await send_subscription_detail(message, db, sub_id)
