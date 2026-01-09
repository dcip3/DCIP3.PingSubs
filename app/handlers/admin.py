from __future__ import annotations

from aiogram import F
import contextlib
from datetime import datetime

from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.ui.keyboards import (
    admin_reply_keyboard,
    admin_settings_keyboard,
    dialog_keyboard,
    public_reply_keyboard,
    settings_currency_keyboard,
    settings_notifications_keyboard,
    settings_rounding_keyboard,
)
from app.ui.helpers import (
    _build_subscription_payment_report_text,
    send_public_subscription_detail,
    send_public_subscription_payment_report,
    send_public_user_payment_report,
    send_member_list,
    send_subscription_list,
    send_user_subscription_list,
)
from app.ui.states import FriendForm, PublicReminderForm, SettingsForm, SubscriptionAction
from app.core.constants import DEFAULT_CURRENCIES
from app.core.config import Settings
from app.storage.db import Database
from app.services import CurrencyConverter

from . import admin_router, public_router


def _is_cancel_text(text: str | None) -> bool:
    return bool(text and text.lower() == "cancel")


@public_router.message(Command("start"))
async def handle_start(message: Message, db: Database) -> None:
    greeting = (
        "PingSubs helps you manage shared subscriptions. "
        "Admins keep the schedule up to date and I send reminders on time."
    )

    if not message.from_user:
        await message.answer(greeting)
        return

    user_id = message.from_user.id
    is_admin = await db.is_admin(user_id)
    has_admin = await db.has_admins()

    if is_admin:
        await message.answer(greeting, reply_markup=admin_reply_keyboard())
        return

    if not has_admin:
        await message.answer(greeting, reply_markup=dialog_keyboard())
        return

    await message.answer(
        greeting,
        reply_markup=public_reply_keyboard(),
    )


@public_router.message(Command("help"))
async def handle_public_help(message: Message, db: Database) -> None:
    if message.from_user and await db.is_admin(message.from_user.id):
        await send_admin_help(message)
        return

    await message.answer(
        "I track shared subscriptions and remind everyone about payments. "
        "Ask an admin to invite you if you need access."
    )



@public_router.message(F.text.func(_is_cancel_text))
async def handle_public_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("There is no active dialog to cancel.", reply_markup=public_reply_keyboard())
        return
    await state.clear()
    await message.answer("Dialog canceled.", reply_markup=public_reply_keyboard())


@public_router.message(F.text == "📋 Subscriptions")
async def handle_public_subscriptions(message: Message, db: Database) -> None:
    if not message.from_user:
        await message.answer("Unable to identify your account.")
        return
    if await db.is_admin(message.from_user.id):
        await send_subscription_list(message, db)
        return
    await send_user_subscription_list(message, db, message.from_user.id)


@public_router.message(F.text == "📊 Payments report")
async def handle_public_payments_report(message: Message, db: Database) -> None:
    if not message.from_user:
        await message.answer("Unable to identify your account.")
        return
    if await db.is_admin(message.from_user.id):
        await handle_payments_report(message, db)
        return
    await send_public_user_payment_report(message, db, message.from_user.id)


@public_router.callback_query(SubscriptionAction.filter(F.action == "open_public"))
async def handle_public_subscription_open(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    await send_public_subscription_detail(callback, db, callback_data.subscription_id)


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_back"))
async def handle_public_subscription_back(
    callback: CallbackQuery,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.")
        return
    await send_user_subscription_list(callback.message, db, callback.from_user.id)
    await callback.answer()


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_report"))
async def handle_public_subscription_report(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.")
        return
    await send_public_subscription_payment_report(
        callback,
        db,
        callback_data.subscription_id,
        callback.from_user.id,
    )


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_remindertime"))
async def handle_public_subscription_reminder_time(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.")
        return
    subs = await db.list_subscriptions_for_user(callback.from_user.id)
    if not any(sub["id"] == callback_data.subscription_id for sub in subs):
        await callback.answer("You don't have access to this subscription.", show_alert=True)
        return
    await state.set_state(PublicReminderForm.reminder_time)
    await state.update_data(subscription_id=callback_data.subscription_id)
    if callback.message:
        await callback.message.answer(
            "Send the reminder time in HH:MM (Moscow time).",
            reply_markup=dialog_keyboard(),
        )
    await callback.answer()


@public_router.message(PublicReminderForm.reminder_time)
async def handle_public_reminder_time_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    data = await state.get_data()
    subscription_id = data.get("subscription_id")
    if not subscription_id or not message.from_user:
        await state.clear()
        await message.answer("Session expired. Reopen the subscription.")
        return

    raw_time = (message.text or "").strip()
    try:
        datetime.strptime(raw_time, "%H:%M")
    except ValueError:
        await message.answer("Time must be in HH:MM format (24-hour clock).")
        return

    subs = await db.list_subscriptions_for_user(message.from_user.id)
    if not any(sub["id"] == subscription_id for sub in subs):
        await state.clear()
        await message.answer("You don't have access to this subscription.")
        return

    await db.update_subscription_fields(int(subscription_id), reminder_time=raw_time)
    await state.clear()
    await message.answer("Reminder time updated.", reply_markup=public_reply_keyboard())


async def send_admin_help(message: Message) -> None:
    text = (
        "ℹ️ PingSubs helps you manage shared subscriptions, users, and payment reminders.\n\n"
        "Buttons:\n"
        "👥 Users — add, edit, and remove users.\n"
        "📋 Subscriptions — manage plans and reminders.\n"
        "📊 Payments report — payment summaries.\n"
        "⚙️ Settings — bot configuration.\n\n"
        "Commands:\n"
        "/start — start the bot.\n"
        "/help — show this help."
    )
    await message.answer(text, reply_markup=admin_reply_keyboard())


@public_router.callback_query(F.data == "menu:close")
@admin_router.callback_query(F.data == "menu:close")
async def handle_menu_close(callback: CallbackQuery) -> None:
    if callback.message:
        with contextlib.suppress(Exception):
            await callback.message.delete()
        if callback.message.text:
            with contextlib.suppress(Exception):
                await callback.message.edit_text("Menu closed.")
    await callback.answer()



@admin_router.message(F.text.func(_is_cancel_text))
async def handle_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("There is no active dialog to cancel.", reply_markup=admin_reply_keyboard())
        return

    await state.clear()
    await message.answer("Dialog canceled.", reply_markup=admin_reply_keyboard())



@admin_router.message(FriendForm.telegram_id)
async def friend_form_id(message: Message, state: FSMContext) -> None:
    if message.forward_from:
        telegram_id = message.forward_from.id
    else:
        try:
            telegram_id = int((message.text or "").strip())
        except ValueError:
            await message.answer("The ID must be numeric. Try again.")
            return

    await state.update_data(telegram_id=telegram_id)
    await state.set_state(FriendForm.full_name)
    await message.answer("Great! Now enter their full name:")


@admin_router.message(FriendForm.full_name)
async def friend_form_name(message: Message, state: FSMContext, db: Database) -> None:
    full_name = (message.text or "").strip()
    if not full_name:
        await message.answer("Name cannot be empty. Please try again.")
        return

    data = await state.get_data()
    telegram_id = int(data["telegram_id"])
    friend_id = await db.upsert_friend(telegram_id, full_name)
    await state.clear()
    await message.answer("User saved.", reply_markup=admin_reply_keyboard())
    await send_member_list(message, db)


@admin_router.message(F.text == "📊 Payments report")
async def handle_payments_report(message: Message, db: Database) -> None:
    subscriptions = await db.list_subscriptions()
    if not subscriptions:
        await message.answer("No subscriptions yet.", reply_markup=admin_reply_keyboard())
        return

    blocks: list[str] = []
    for sub in subscriptions:
        participants = await db.list_subscription_participants(sub["id"])
        if not participants:
            continue
        block = await _build_subscription_payment_report_text(
            db,
            sub,
            participants,
            scope="all",
        )
        blocks.append(block)

    if not blocks:
        await message.answer("No users to report yet.", reply_markup=admin_reply_keyboard())
        return

    await message.answer("\n\n".join(blocks), reply_markup=admin_reply_keyboard())


async def _settings_menu_text(settings: Settings) -> str:
    return (
        "Settings:\n"
        f"Base currency: {settings.target_currency}\n"
        f"Rounding: {_rounding_label(settings.currency_rounding)}"
    )


@admin_router.message(F.text == "⚙️ Settings")
async def handle_settings_menu(message: Message, settings: Settings) -> None:
    await message.answer(await _settings_menu_text(settings), reply_markup=admin_settings_keyboard())


@admin_router.callback_query(F.data == "settings:menu")
async def handle_settings_menu_callback(callback: CallbackQuery, settings: Settings) -> None:
    if callback.message:
        await callback.message.edit_text(
            await _settings_menu_text(settings),
            reply_markup=admin_settings_keyboard(),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "settings:close")
async def handle_settings_close(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.message.edit_text("Settings closed.")
    await callback.answer()


@admin_router.callback_query(F.data == "settings:currency")
async def handle_settings_currency(callback: CallbackQuery, settings: Settings) -> None:
    text = (
        "Choose a base currency:\n"
        f"Current value: {settings.target_currency}."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=settings_currency_keyboard(DEFAULT_CURRENCIES))
    await callback.answer()


@admin_router.callback_query(F.data == "settings:currency_other")
async def handle_settings_currency_other(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsForm.base_currency)
    if callback.message:
        await callback.message.answer(
            "Send the base currency code (3 letters), for example USD.",
            reply_markup=dialog_keyboard(),
        )
    await callback.answer()


def _rounding_label(mode: str) -> str:
    labels = {
        "precise": "With cents",
        "floor": "Round down",
        "round": "Round correctly",
        "ceil": "Round up",
    }
    return labels.get(mode, "With cents")


@admin_router.callback_query(F.data == "settings:rounding")
async def handle_settings_rounding(callback: CallbackQuery, settings: Settings) -> None:
    text = (
        "Choose a rounding mode:\n"
        f"Current value: {_rounding_label(settings.currency_rounding)}."
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=settings_rounding_keyboard(settings.currency_rounding),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("settings_rounding:"))
async def handle_settings_rounding_select(
    callback: CallbackQuery,
    settings: Settings,
    db: Database,
) -> None:
    raw_value = callback.data.split(":", 1)[1].strip().lower()
    if raw_value not in {"precise", "floor", "round", "ceil"}:
        await callback.answer("Unsupported rounding mode.", show_alert=True)
        return
    settings.currency_rounding = raw_value
    await db.set_setting("currency_rounding", raw_value)
    await callback.answer(f"Rounding set to {_rounding_label(raw_value)}")
    if callback.message:
        await callback.message.edit_text(
            await _settings_menu_text(settings),
            reply_markup=admin_settings_keyboard(),
        )


@admin_router.callback_query(F.data == "settings:notifications")
async def handle_settings_notifications(callback: CallbackQuery, db: Database) -> None:
    reminders_enabled = await db.get_setting_bool("notify_admin_reminders", True)
    paid_enabled = await db.get_setting_bool("notify_admin_paid", True)
    closed_enabled = await db.get_setting_bool("notify_admin_closed", True)
    text = "Choose admin notifications:"
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=settings_notifications_keyboard(
                reminders_enabled,
                paid_enabled,
                closed_enabled,
            ),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("settings_notify:"))
async def handle_settings_notifications_toggle(
    callback: CallbackQuery,
    db: Database,
) -> None:
    raw_value = callback.data.split(":", 1)[1].strip().lower()
    key_map = {
        "reminders": "notify_admin_reminders",
        "paid": "notify_admin_paid",
        "closed": "notify_admin_closed",
    }
    setting_key = key_map.get(raw_value)
    if setting_key is None:
        await callback.answer("Unsupported option.", show_alert=True)
        return
    current = await db.get_setting_bool(setting_key, True)
    await db.set_setting(setting_key, "false" if current else "true")

    reminders_enabled = await db.get_setting_bool("notify_admin_reminders", True)
    paid_enabled = await db.get_setting_bool("notify_admin_paid", True)
    closed_enabled = await db.get_setting_bool("notify_admin_closed", True)
    if callback.message:
        await callback.message.edit_text(
            "Choose admin notifications:",
            reply_markup=settings_notifications_keyboard(
                reminders_enabled,
                paid_enabled,
                closed_enabled,
            ),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("settings_currency:"))
async def handle_settings_currency_select(
    callback: CallbackQuery,
    settings: Settings,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    raw_value = callback.data.split(":", 1)[1].strip().upper()
    if len(raw_value) != 3:
        await callback.answer("Currency must contain 3 letters.", show_alert=True)
        return
    settings.target_currency = raw_value
    converter.set_target_currency(raw_value)
    await db.set_setting("target_currency", raw_value)
    await callback.answer(f"Base currency set to {raw_value}")
    if callback.message:
        await callback.message.edit_text(
            await _settings_menu_text(settings),
            reply_markup=admin_settings_keyboard(),
        )


@admin_router.message(SettingsForm.base_currency)
async def handle_settings_currency_input(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    raw_value = (message.text or "").strip().upper()
    if len(raw_value) != 3 or not raw_value.isalpha():
        await message.answer("Currency must contain 3 letters, for example USD.")
        return
    settings.target_currency = raw_value
    converter.set_target_currency(raw_value)
    await db.set_setting("target_currency", raw_value)
    await state.clear()
    await message.answer(
        f"Base currency set to {raw_value}.",
        reply_markup=admin_reply_keyboard(),
    )
