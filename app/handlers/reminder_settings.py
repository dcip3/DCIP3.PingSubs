"""Reminders subtree of the admin subscription card (WP6).

Reminders (sub:reminders) with the After-due-date toggle (sub:reminderloop),
Reminder time (sub:remindertime, sub_remindertime:<id>:<HH:MM>,
sub:remindertime_default, sub_remindertime_other:<id>), the Days prompt
(sub:reminderdays + the quick value remdays_default:<id>) and Send now
(sub:reminders_send, remsend:*). Every recipient tap, including legacy remsend
buttons on old messages, opens a confirmation; its [📬 Send] is the new
ReminderSendConfirmAction ('remsendok').
"""
from __future__ import annotations

from typing import Optional

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.core.config import Settings
from app.core.reminders import DEFAULT_REMINDER_OFFSETS, normalize_time_string, serialize_offsets
from app.services import CurrencyConverter, send_reminders_now
from app.storage.db import Database
from app.ui.guards import guarded
from app.ui.helpers import (
    DEAD_BUTTON,
    answer_callback_once,
    edit_in_place,
    get_edit_subscription_id,
    require_edit_subscription_id,
    respond_with_markup,
    send_subscription_list,
    SUBSCRIPTION_GONE_BANNER,
    show_subscription_gone,
    start_subscription_edit_flow,
)
from app.ui.keyboards import REMINDER_DAYS_DEFAULT_PREFIX, SEND_STATUS_PAID, reminder_days_quick_rows
from app.ui.prompts import PROMPT_MESSAGE_ID, finish_prompt, note_typo, origin_of
from app.ui.reminder_screens import (
    DEFAULT_OFFSETS_TEXT,
    EVERYONE_PAID_TOAST,
    LIST_UPDATED_TOAST,
    REMINDER_DAYS_HINT,
    REMINDER_TIME_HINT,
    SENDING_TOAST,
    after_due_toast,
    already_paid_toast,
    base_reminder_time,
    build_reminders_screen,
    build_send_now_screen,
    parse_offsets_input,
    reminder_days_banner,
    reminder_days_prompt_text,
    reminder_days_toast,
    reminder_time_banner,
    reminder_time_prompt_text,
    reminder_time_toast,
    send_confirm_screen,
    send_reminder_time_screen,
    send_reminders_screen,
    send_result_banner,
    send_send_now_screen,
    show_reminders_after_prompt,
)
from app.ui.states import ReminderSendAction, ReminderSendConfirmAction, SubscriptionAction, SubscriptionEditForm
from app.ui.text import is_default_word, parse_time_loose

from . import admin_router
from .subscriptions import _drop_flow



def _reminders_origin(subscription_id: int) -> str:
    return f"reminders:{int(subscription_id)}"


def _id_after(prefix_parts: int, data: Optional[str]) -> Optional[list[str]]:
    parts = (data or "").split(":", prefix_parts)
    return parts if len(parts) == prefix_parts + 1 else None


async def _prompt_gone(message: Message, state: FSMContext, db: Database) -> None:
    """A Reminders prompt answered after the subscription was deleted: the list with a banner."""
    await finish_prompt(message, state)
    await send_subscription_list(message, db, banner=SUBSCRIPTION_GONE_BANNER)


# ------------------------------------------------------------------ Reminders


@admin_router.callback_query(SubscriptionAction.filter(F.action == "reminders"))
async def handle_subscription_reminders_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await _drop_flow(callback, state)
    await send_reminders_screen(callback, db, callback_data.subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "reminderloop"))
async def handle_subscription_reminder_loop_toggle(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    subscription = await db.get_subscription(callback_data.subscription_id)
    if not subscription:
        await show_subscription_gone(callback, db)
        return
    new_value = 0 if subscription.get("remind_after_due") else 1
    await db.update_subscription_fields(callback_data.subscription_id, remind_after_due=new_value)
    await send_reminders_screen(callback, db, callback_data.subscription_id, toast=after_due_toast(bool(new_value)))


# -------------------------------------------------------------- Reminder time


@admin_router.callback_query(SubscriptionAction.filter(F.action == "remindertime"))
async def handle_subscription_reminder_time_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await _drop_flow(callback, state)
    await send_reminder_time_screen(callback, db, callback_data.subscription_id)


async def _save_reminder_time(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    subscription_id: int,
    value: Optional[str],
) -> None:
    """A preset (``value``) or Default (``None``) picked: save, back to Reminders with a banner."""
    if not await db.get_subscription(subscription_id):
        await _drop_flow(callback, state)
        await show_subscription_gone(callback, db)
        return
    await db.update_subscription_fields(subscription_id, reminder_time=value or "")
    await _drop_flow(callback, state)
    shown = value or await base_reminder_time(db)
    default = value is None
    await send_reminders_screen(
        callback,
        db,
        subscription_id,
        banner=reminder_time_banner(shown, default=default),
        toast=reminder_time_toast(shown, default=default),
    )


@admin_router.callback_query(F.data.startswith("sub_remindertime:"))
async def handle_subscription_reminder_time_select(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parts = _id_after(2, callback.data)
    normalized = normalize_time_string(parts[2]) if parts else None
    if parts is None or not parts[1].isdigit():
        await answer_callback_once(callback, DEAD_BUTTON)
        return
    if normalized is None:
        await send_reminders_screen(callback, db, int(parts[1]), toast=DEAD_BUTTON)
        return
    await _save_reminder_time(callback, state, db, int(parts[1]), normalized)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "remindertime_default"))
async def handle_subscription_reminder_time_default(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await _save_reminder_time(callback, state, db, callback_data.subscription_id, None)


@admin_router.callback_query(F.data.startswith("sub_remindertime_other:"))
async def handle_subscription_reminder_time_other(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parts = _id_after(1, callback.data)
    if parts is None or not parts[1].isdigit():
        await answer_callback_once(callback, DEAD_BUTTON)
        return
    subscription_id = int(parts[1])
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await _drop_flow(callback, state)
        await show_subscription_gone(callback, db)
        return
    await start_subscription_edit_flow(
        callback,
        state,
        subscription_id,
        SubscriptionEditForm.reminder_time,
        reminder_time_prompt_text(subscription),
        origin_screen=_reminders_origin(subscription_id),
    )


@admin_router.message(SubscriptionEditForm.reminder_time)
async def edit_subscription_reminder_time(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return
    normalized = parse_time_loose(message.text)
    if normalized is None:
        await note_typo(message, state, REMINDER_TIME_HINT)
        return
    if not await db.get_subscription(sub_id):
        await _prompt_gone(message, state, db)
        return
    await db.update_subscription_fields(sub_id, reminder_time=normalized)
    flow_data = await finish_prompt(message, state)
    await show_reminders_after_prompt(message, db, sub_id, flow_data, banner=reminder_time_banner(normalized))


# ----------------------------------------------------------------------- Days


@admin_router.callback_query(SubscriptionAction.filter(F.action == "reminderdays"))
async def handle_subscription_reminder_days_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await db.get_subscription(callback_data.subscription_id)
    if not subscription:
        await _drop_flow(callback, state)
        await show_subscription_gone(callback, db)
        return
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.reminder_offsets,
        reminder_days_prompt_text(subscription),
        origin_screen=_reminders_origin(callback_data.subscription_id),
        extra_rows=reminder_days_quick_rows(callback_data.subscription_id, DEFAULT_OFFSETS_TEXT),
    )


@admin_router.message(SubscriptionEditForm.reminder_offsets)
async def edit_subscription_reminder_offsets(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return
    if is_default_word(message.text):
        offsets: Optional[list[int]] = sorted(set(DEFAULT_REMINDER_OFFSETS))
    else:
        offsets = parse_offsets_input(message.text)
    if offsets is None:
        await note_typo(message, state, REMINDER_DAYS_HINT)
        return
    if not await db.get_subscription(sub_id):
        await _prompt_gone(message, state, db)
        return
    await db.update_subscription_fields(sub_id, reminder_offsets=serialize_offsets(offsets))
    flow_data = await finish_prompt(message, state)
    await show_reminders_after_prompt(message, db, sub_id, flow_data, banner=reminder_days_banner(offsets))


@admin_router.callback_query(F.data.startswith(REMINDER_DAYS_DEFAULT_PREFIX))
async def handle_reminder_days_default(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    """[↺ 1 day before + on the day] on the Days prompt: save the default, back to Reminders."""
    raw_id = (callback.data or "")[len(REMINDER_DAYS_DEFAULT_PREFIX):]
    if not raw_id.isdigit():
        await answer_callback_once(callback, "Could not change the days. Try again.", alert=True)
        return
    subscription_id = int(raw_id)
    message = callback.message
    data = await state.get_data()
    running = (
        await state.get_state() == SubscriptionEditForm.reminder_offsets.state
        and await get_edit_subscription_id(state) == subscription_id
        and isinstance(message, Message)
        and data.get(PROMPT_MESSAGE_ID) == message.message_id
    )
    if not await db.get_subscription(subscription_id):
        if running:
            await _drop_flow(callback, state)
        await show_subscription_gone(callback, db)
        return
    offsets = sorted(set(DEFAULT_REMINDER_OFFSETS))
    await db.update_subscription_fields(subscription_id, reminder_offsets=serialize_offsets(offsets))
    banner, toast = reminder_days_banner(offsets), reminder_days_toast(offsets)
    if not running or not isinstance(message, Message):
        # A quick value on an older prompt: the tapped message becomes Reminders.
        await send_reminders_screen(callback, db, subscription_id, banner=banner, toast=toast)
        return
    flow_data = await finish_prompt(message, state, bot=callback.bot)
    screen = await build_reminders_screen(db, subscription_id, banner=banner)
    if screen is not None:
        text, markup = screen
        origin = origin_of(flow_data)
        bot = callback.bot
        if origin is None or bot is None or not await edit_in_place(bot, *origin, text, markup):
            await message.answer(text, reply_markup=markup)
    await answer_callback_once(callback, toast)


# ------------------------------------------------------------------- Send now


@admin_router.callback_query(SubscriptionAction.filter(F.action == "reminders_send"))
async def handle_subscription_reminders_send(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await _drop_flow(callback, state)
    await send_send_now_screen(callback, db, callback_data.subscription_id)


@admin_router.callback_query(ReminderSendAction.filter())
async def handle_subscription_reminders_send_target(
    callback: CallbackQuery,
    callback_data: ReminderSendAction,
    db: Database,
    state: FSMContext,
) -> None:
    """A recipient (0 = every unpaid person) tapped: ask before anything is sent.

    Legacy remsend buttons on old messages land here too, so they never send
    silently any more.
    """
    await _drop_flow(callback, state)
    subscription_id = callback_data.subscription_id
    built = await build_send_now_screen(db, subscription_id)
    if built is None:
        await show_subscription_gone(callback, db)
        return
    (text, markup), send_state = built
    if not send_state.has_participants:
        await respond_with_markup(callback, text, reply_markup=markup, toast=LIST_UPDATED_TOAST)
        return
    if callback_data.telegram_id == 0:
        count = len(send_state.unpaid)
        if not count:
            await respond_with_markup(callback, text, reply_markup=markup, toast=EVERYONE_PAID_TOAST)
            return
        confirm_text, confirm_markup = send_confirm_screen(send_state.subscription, 0, count=count)
        await respond_with_markup(callback, confirm_text, reply_markup=confirm_markup)
        return
    person = send_state.person(callback_data.telegram_id)
    if person is None:
        await respond_with_markup(callback, text, reply_markup=markup, toast=LIST_UPDATED_TOAST)
        return
    name = " ".join(str(person.get("full_name") or "Unknown").split())
    if person.get("status") == SEND_STATUS_PAID:
        await respond_with_markup(callback, text, reply_markup=markup, toast=already_paid_toast(name))
        return
    confirm_text, confirm_markup = send_confirm_screen(
        send_state.subscription,
        callback_data.telegram_id,
        person_name=name,
    )
    await respond_with_markup(callback, confirm_text, reply_markup=confirm_markup)


@admin_router.callback_query(ReminderSendConfirmAction.filter())
async def handle_subscription_reminders_send_confirm(
    callback: CallbackQuery,
    callback_data: ReminderSendConfirmAction,
    bot: Bot,
    settings: Settings,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    """[📬 Send]: answer at once, send, then show Reminders with what members got."""
    subscription_id = callback_data.subscription_id
    async with guarded(callback, ("remsend", subscription_id)) as acquired:
        if not acquired:
            return
        if not await db.get_subscription(subscription_id):
            await show_subscription_gone(callback, db)
            return
        await answer_callback_once(callback, SENDING_TOAST)
        result = await send_reminders_now(
            bot,
            db,
            converter,
            subscription_id=subscription_id,
            rounding_mode=settings.currency_rounding,
            target_telegram_id=callback_data.telegram_id or None,
        )
        await send_reminders_screen(callback, db, subscription_id, banner=send_result_banner(result))
