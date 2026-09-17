from __future__ import annotations

import contextlib
import html
import logging
from datetime import datetime, timezone

from aiogram import Bot, F

from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.ui.keyboards import (
    admin_reply_keyboard,
    admin_settings_keyboard,
    build_test_payment_confirmation_keyboard,
    dialog_cancel_inline_keyboard,
    public_settings_keyboard,
    public_subscription_currency_keyboard,
    public_subscription_reminder_time_keyboard,
    public_settings_time_keyboard,
    public_settings_timezone_keyboard,
    public_reply_keyboard,
    settings_notifications_keyboard,
    settings_rounding_keyboard,
    settings_time_keyboard,
    settings_timezone_keyboard,
)
from app.ui.helpers import (
    ADMIN_REPORT_COLLAPSE_MIN_GREEN,
    _build_subscription_payment_report_text,
    assemble_payments_report,
    resolve_report_timezone,
    send_chunked_responder_text,
    send_public_account_detail,
    send_public_subscription_detail,
    send_public_subscription_payment_report,
    send_public_user_payment_report,
    send_settings_tests_menu,
    send_subscription_list,
    send_test_user_list,
    send_user_subscription_list,
)
from app.ui.states import (
    PublicAccountForm,
    PublicReminderForm,
    PublicSubscriptionCurrencyForm,
    PublicSettingsForm,
    SettingsForm,
    SubscriptionAction,
    TestListAction,
    TestSendAction,
)
from app.core.config import Settings
from app.core.reminders import (
    DEFAULT_REMINDER_TIMEZONE,
    normalize_time_string,
    normalize_timezone_name,
    parse_time_string,
    parse_timezone,
    tg_clock,
)
from app.storage.db import Database
from app.services import (
    CurrencyConverter,
    _build_reminder_message,
    _format_status_and_date,
    _send_message_with_retry,
    format_converted_amount,
)
from app.ui.text import validate_person_name

from . import admin_router, public_router

logger = logging.getLogger(__name__)

TEST_REMINDER_CURRENCY_FALLBACKS = ("RUB", "USD", "EUR", "GBP")


def _extract_start_payload(message: Message) -> str | None:
    text = (message.text or "").strip()
    if not text.startswith("/start"):
        return None
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return None
    payload = parts[1].strip()
    return payload or None


async def _public_settings_snapshot(
    db: Database,
    settings: Settings,
    telegram_id: int,
) -> tuple[str, str, bool, str, str, bool]:
    admin_time_raw = await db.get_setting("base_reminder_time")
    admin_time = parse_time_string(admin_time_raw, settings.base_reminder_time).strftime("%H:%M")
    user_time_raw = await db.get_user_setting(telegram_id, "base_reminder_time")
    has_time_override = bool(normalize_time_string(user_time_raw))
    current_time = parse_time_string(user_time_raw, admin_time).strftime("%H:%M")
    admin_timezone_raw = await db.get_setting("base_timezone")
    admin_timezone = normalize_timezone_name(admin_timezone_raw, settings.base_timezone) or DEFAULT_REMINDER_TIMEZONE
    user_timezone_raw = await db.get_user_setting(telegram_id, "timezone")
    has_timezone_override = bool(normalize_timezone_name(user_timezone_raw))
    current_timezone = normalize_timezone_name(user_timezone_raw, admin_timezone) or admin_timezone
    return (
        current_time,
        admin_time,
        has_time_override,
        current_timezone,
        admin_timezone,
        has_timezone_override,
    )


def _public_settings_text(
    current_time: str,
    admin_time: str,
    has_time_override: bool,
    current_timezone: str,
    admin_timezone: str,
    has_timezone_override: bool,
) -> str:
    time_value = current_time if has_time_override else f"Default ({admin_time})"
    timezone_value = current_timezone if has_timezone_override else f"Default ({admin_timezone})"
    return (
        "⚙️ Settings:\n\n"
        "⏰ Time:\n"
        f"Base time: <code>{html.escape(time_value)}</code>\n"
        f"Your local time: {tg_clock(current_time, current_timezone)}\n\n"
        "🌍 Timezone:\n"
        f"Timezone: <code>{html.escape(timezone_value)}</code>"
    )


async def _show_public_settings_menu(
    callback: CallbackQuery | Message,
    db: Database,
    settings: Settings,
    telegram_id: int,
) -> None:
    (
        current_time,
        admin_time,
        has_time_override,
        current_timezone,
        admin_timezone,
        has_timezone_override,
    ) = await _public_settings_snapshot(db, settings, telegram_id)
    text = _public_settings_text(
        current_time,
        admin_time,
        has_time_override,
        current_timezone,
        admin_timezone,
        has_timezone_override,
    )
    markup = public_settings_keyboard(
        current_time,
        current_timezone,
    )
    if isinstance(callback, CallbackQuery):
        if callback.message:
            await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()
        return
    await callback.answer(text, reply_markup=markup)


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
    start_payload = _extract_start_payload(message)

    if is_admin:
        await message.answer(greeting, reply_markup=admin_reply_keyboard())
        return

    if start_payload and start_payload.startswith("join_"):
        invited_friend = await db.get_friend_by_invite_token(start_payload)
        if not invited_friend:
            await message.answer(
                "This authorization link is invalid or has already been used.",
                reply_markup=public_reply_keyboard(),
            )
            return

        invite_expires_at = str(invited_friend.get("invite_expires_at") or "").strip()
        if invite_expires_at:
            try:
                expires_at = datetime.fromisoformat(invite_expires_at)
            except ValueError:
                expires_at = None
            if expires_at is not None and expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
                await message.answer(
                    "This authorization link has expired. Ask an admin for a new one.",
                    reply_markup=public_reply_keyboard(),
                )
                return

        existing_friend = await db.get_friend_by_telegram(user_id)
        if existing_friend and int(existing_friend["id"]) != int(invited_friend["id"]):
            await message.answer(
                "This Telegram account is already linked to another user.",
                reply_markup=public_reply_keyboard(),
            )
            return

        if invited_friend.get("telegram_id") is not None:
            await message.answer(
                "This authorization link has already been used.",
                reply_markup=public_reply_keyboard(),
            )
            return

        claimed = await db.claim_friend_invite(
            int(invited_friend["id"]),
            user_id,
            datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat(),
        )
        if not claimed:
            await message.answer(
                "Unable to complete authorization. Please try again or ask an admin for a new link.",
                reply_markup=public_reply_keyboard(),
            )
            return

        await message.answer(
            f"{greeting}\n\nYour account is now linked. You can use the user menu below.",
            reply_markup=public_reply_keyboard(),
        )
        return

    if not has_admin:
        admin_name, _ = validate_person_name(message.from_user.full_name)
        claimed = await db.ensure_first_admin(user_id, admin_name or "Admin")
        if claimed or await db.is_admin(user_id):
            await message.answer(
                f"{greeting}\n\nYou are the first user, so admin access is enabled for you.",
                reply_markup=admin_reply_keyboard(),
            )
            return
        await message.answer(greeting, reply_markup=public_reply_keyboard())
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
        "I track shared subscriptions and remind everyone about payments.\n\n"
        "Buttons:\n"
        "👤 Account — your balance and subscriptions.\n"
        "➕ Top up — create a balance top-up request.\n"
        "📋 Subscriptions — your plans.\n"
        "📊 Payments report — your payment history.\n"
        "⚙️ Settings — your timezone and reminder time.\n\n"
        "Ask an admin to invite you if you need access."
    )



@public_router.message(F.text == "📋 Subscriptions")
async def handle_public_subscriptions(message: Message, db: Database) -> None:
    if not message.from_user:
        await message.answer("Unable to identify your account.")
        return
    if await db.is_admin(message.from_user.id):
        await send_subscription_list(message, db)
        return
    await send_user_subscription_list(message, db, message.from_user.id)


@public_router.message(F.text == "👤 Account")
async def handle_public_account(message: Message, db: Database) -> None:
    if not message.from_user:
        await message.answer("Unable to identify your account.")
        return
    if await db.is_admin(message.from_user.id):
        await message.answer("This shortcut is available in the user menu only.", reply_markup=admin_reply_keyboard())
        return
    await send_public_account_detail(message, db, message.from_user.id)


@public_router.callback_query(F.data == "public_account:rename")
async def handle_public_account_rename_start(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    if await db.is_admin(callback.from_user.id):
        await callback.answer("This shortcut is available in the user menu only.", show_alert=True)
        return
    friend = await db.get_friend_by_telegram(callback.from_user.id)
    if not friend:
        await callback.answer("Your profile is not set up yet.", show_alert=True)
        return
    await state.clear()
    await state.set_state(PublicAccountForm.full_name)
    if callback.message:
        await callback.message.answer(
            "✏️ Rename account:\n"
            "Send your new display name.",
            reply_markup=dialog_cancel_inline_keyboard(),
        )
    await callback.answer()


@public_router.message(F.text == "📊 Payments report")
async def handle_public_payments_report(message: Message, db: Database) -> None:
    if not message.from_user:
        await message.answer("Unable to identify your account.")
        return
    if await db.is_admin(message.from_user.id):
        await handle_payments_report(message, db)
        return
    await send_public_user_payment_report(message, db, message.from_user.id)


@public_router.message(F.text == "⚙️ Settings")
async def handle_public_settings(
    message: Message,
    db: Database,
    settings: Settings,
) -> None:
    if not message.from_user:
        await message.answer("Unable to identify your account.")
        return
    if await db.is_admin(message.from_user.id):
        await message.answer(await _settings_menu_text(settings), reply_markup=admin_settings_keyboard())
        return
    await _show_public_settings_menu(message, db, settings, message.from_user.id)


@public_router.callback_query(F.data == "public_settings:menu")
async def handle_public_settings_menu_callback(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    if await db.is_admin(callback.from_user.id):
        if callback.message:
            await callback.message.edit_text(
                await _settings_menu_text(settings),
                reply_markup=admin_settings_keyboard(),
            )
        await callback.answer()
        return
    await _show_public_settings_menu(callback, db, settings, callback.from_user.id)


@public_router.callback_query(F.data == "public_settings:time")
async def handle_public_settings_time(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    (
        current_time,
        admin_time,
        _,
        current_timezone,
        admin_timezone,
        _,
    ) = await _public_settings_snapshot(
        db,
        settings,
        callback.from_user.id,
    )
    if callback.message:
        await callback.message.edit_text(
            "⏰ Base time:\n"
            f"Current: <code>{current_time} ({current_timezone})</code>\n"
            f"Default: <code>{admin_time} ({admin_timezone})</code>\n"
            f"Your local time: {tg_clock(current_time, current_timezone)}\n"
            "\n"
            "Choose a value:",
            reply_markup=public_settings_time_keyboard(current_time, admin_time),
        )
    await callback.answer()


@public_router.callback_query(F.data == "public_settings:time_other")
async def handle_public_settings_time_other(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PublicSettingsForm.base_time)
    if callback.message:
        await callback.message.answer(
            "⏰ Base time:\n"
            "\n"
            "Send time in <code>HH:MM</code>.",
            reply_markup=dialog_cancel_inline_keyboard(),
        )
    await callback.answer()


@public_router.callback_query(F.data.startswith("public_settings_time:"))
async def handle_public_settings_time_select(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    raw_value = callback.data.split(":", 1)[1].strip()
    normalized = normalize_time_string(raw_value)
    if normalized is None:
        await callback.answer("Time must be HH:MM.", show_alert=True)
        return
    await db.set_user_setting(callback.from_user.id, "base_reminder_time", normalized)
    await callback.answer(f"Base time set to {normalized}")
    await _show_public_settings_menu(callback, db, settings, callback.from_user.id)


@public_router.callback_query(F.data == "public_settings:time_reset")
async def handle_public_settings_time_reset(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    await db.delete_user_setting(callback.from_user.id, "base_reminder_time")
    await callback.answer("Using admin default time now.")
    await _show_public_settings_menu(callback, db, settings, callback.from_user.id)


@public_router.callback_query(F.data == "public_settings:timezone")
async def handle_public_settings_timezone(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    (
        _,
        _,
        _,
        current_timezone,
        admin_timezone,
        _,
    ) = await _public_settings_snapshot(
        db,
        settings,
        callback.from_user.id,
    )
    if callback.message:
        await callback.message.edit_text(
            "🌍 Timezone:\n"
            f"Current: <code>{current_timezone}</code>\n"
            f"Default: <code>{admin_timezone}</code>\n"
            "\n"
            "Choose a value:",
            reply_markup=public_settings_timezone_keyboard(
                current_timezone,
                admin_timezone,
            ),
        )
    await callback.answer()


@public_router.callback_query(F.data == "public_settings:timezone_other")
async def handle_public_settings_timezone_other(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PublicSettingsForm.base_timezone)
    if callback.message:
        await callback.message.answer(
            "🌍 Timezone:\n"
            "\n"
            "Send an IANA timezone.\n"
            "Examples: <code>Europe/Moscow</code>, <code>America/New_York</code>.",
            reply_markup=dialog_cancel_inline_keyboard(),
        )
    await callback.answer()


@public_router.callback_query(F.data.startswith("public_settings_timezone:"))
async def handle_public_settings_timezone_select(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    raw_value = callback.data.split(":", 1)[1].strip()
    normalized = normalize_timezone_name(raw_value)
    if normalized is None:
        await callback.answer("Unsupported timezone.", show_alert=True)
        return
    await db.set_user_setting(callback.from_user.id, "timezone", normalized)
    await callback.answer(f"Timezone set to {normalized}")
    await _show_public_settings_menu(callback, db, settings, callback.from_user.id)


@public_router.callback_query(F.data == "public_settings:timezone_reset")
async def handle_public_settings_timezone_reset(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    await db.delete_user_setting(callback.from_user.id, "timezone")
    await callback.answer("Using admin default timezone now.")
    await _show_public_settings_menu(callback, db, settings, callback.from_user.id)


@public_router.callback_query(SubscriptionAction.filter(F.action == "open_public"))
async def handle_public_subscription_open(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.")
        return
    subs = await db.list_subscriptions_for_user(callback.from_user.id)
    if not any(sub["id"] == callback_data.subscription_id for sub in subs) and not await db.is_admin(
        callback.from_user.id
    ):
        await callback.answer("You don't have access to this subscription.", show_alert=True)
        return
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


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_currency"))
async def handle_public_subscription_currency(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.")
        return
    subs = await db.list_subscriptions_for_user(callback.from_user.id)
    if not any(sub["id"] == callback_data.subscription_id for sub in subs):
        await callback.answer("You don't have access to this subscription.", show_alert=True)
        return
    subscription = await db.get_subscription(callback_data.subscription_id)
    if not subscription:
        await callback.answer("Subscription not found.", show_alert=True)
        return

    default_currency = str(
        subscription.get("base_currency") or subscription.get("currency") or "RUB"
    ).strip().upper()
    user_override_raw = str(
        await db.get_user_subscription_setting(
            callback.from_user.id,
            callback_data.subscription_id,
            "target_currency",
        ) or ""
    ).strip().upper()
    current_currency = user_override_raw or default_currency
    if callback.message:
        await callback.message.edit_text(
            "💱 Currency for this subscription:\n"
            f"Current: <code>{html.escape(current_currency)}</code>\n"
            f"Default: <code>{html.escape(default_currency)}</code>\n"
            "\n"
            "Choose a value:",
            reply_markup=public_subscription_currency_keyboard(
                callback_data.subscription_id,
                current_currency,
                default_currency,
            ),
        )
    await callback.answer()


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_currency_default"))
async def handle_public_subscription_currency_default(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    subs = await db.list_subscriptions_for_user(callback.from_user.id)
    if not any(sub["id"] == callback_data.subscription_id for sub in subs):
        await callback.answer("You don't have access to this subscription.", show_alert=True)
        return
    await db.delete_user_subscription_setting(
        callback.from_user.id,
        callback_data.subscription_id,
        "target_currency",
    )
    await callback.answer("Using subscription default currency.")
    await send_public_subscription_detail(callback, db, callback_data.subscription_id)


@public_router.callback_query(F.data.startswith("public_sub_currency_other:"))
async def handle_public_subscription_currency_other(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    try:
        subscription_id = int(callback.data.split(":", 1)[1].strip())
    except (TypeError, ValueError):
        await callback.answer("Invalid subscription.", show_alert=True)
        return

    subs = await db.list_subscriptions_for_user(callback.from_user.id)
    if not any(sub["id"] == subscription_id for sub in subs):
        await callback.answer("You don't have access to this subscription.", show_alert=True)
        return

    await state.set_state(PublicSubscriptionCurrencyForm.currency)
    await state.update_data(subscription_id=subscription_id)
    if callback.message:
        await callback.message.answer(
            "💱 Currency for this subscription:\n"
            "Send a 3-letter currency code.\n"
            "\n"
            "Example: <code>CHF</code>",
            reply_markup=dialog_cancel_inline_keyboard(),
        )
    await callback.answer()


@public_router.callback_query(F.data.startswith("public_sub_currency:"))
async def handle_public_subscription_currency_select(
    callback: CallbackQuery,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Invalid action.", show_alert=True)
        return
    _, subscription_id_raw, raw_currency = parts
    try:
        subscription_id = int(subscription_id_raw)
    except ValueError:
        await callback.answer("Invalid subscription.", show_alert=True)
        return

    currency = raw_currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        await callback.answer("Currency must contain 3 letters.", show_alert=True)
        return

    subs = await db.list_subscriptions_for_user(callback.from_user.id)
    if not any(sub["id"] == subscription_id for sub in subs):
        await callback.answer("You don't have access to this subscription.", show_alert=True)
        return

    await db.set_user_subscription_setting(
        callback.from_user.id,
        subscription_id,
        "target_currency",
        currency,
    )
    await callback.answer(f"Currency set to {currency}")
    await send_public_subscription_detail(callback, db, subscription_id)


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_remindertime"))
async def handle_public_subscription_reminder_time(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    settings: Settings,
    state: FSMContext,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.")
        return
    subs = await db.list_subscriptions_for_user(callback.from_user.id)
    if not any(sub["id"] == callback_data.subscription_id for sub in subs):
        await callback.answer("You don't have access to this subscription.", show_alert=True)
        return
    subscription = await db.get_subscription(callback_data.subscription_id)
    if not subscription:
        await callback.answer("Subscription not found.", show_alert=True)
        return
    subscription_time_raw = normalize_time_string(subscription.get("reminder_time"))
    user_override_raw = await db.get_user_subscription_setting(
        callback.from_user.id,
        callback_data.subscription_id,
        "reminder_time",
    )
    user_override_time = normalize_time_string(user_override_raw)
    user_base_time = parse_time_string(
        await db.get_effective_user_base_reminder_time(
            callback.from_user.id,
            settings.base_reminder_time,
        )
    ).strftime("%H:%M")
    user_timezone = normalize_timezone_name(
        await db.get_effective_user_timezone(callback.from_user.id, settings.base_timezone),
        settings.base_timezone,
    ) or settings.base_timezone
    default_time = subscription_time_raw or user_base_time
    effective_time = user_override_time or default_time
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "⏰ Reminder time:\n"
            "\n"
            f"Current: <code>{effective_time}</code>\n"
            f"Default: <code>{default_time}</code>\n"
            f"Timezone: <code>{user_timezone}</code>\n"
            f"Your local time: {tg_clock(effective_time, user_timezone)}\n"
            "\n"
            "Choose a value:",
            reply_markup=public_subscription_reminder_time_keyboard(
                callback_data.subscription_id,
                effective_time,
                default_time,
            ),
        )
    await callback.answer()


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_remindertime_default"))
async def handle_public_subscription_reminder_time_default(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    subs = await db.list_subscriptions_for_user(callback.from_user.id)
    if not any(sub["id"] == callback_data.subscription_id for sub in subs):
        await callback.answer("You don't have access to this subscription.", show_alert=True)
        return
    await db.delete_user_subscription_setting(
        callback.from_user.id,
        callback_data.subscription_id,
        "reminder_time",
    )
    await state.clear()
    await callback.answer("Using default reminder time.")
    await send_public_subscription_detail(callback, db, callback_data.subscription_id)


@public_router.callback_query(F.data.startswith("public_sub_remindertime_other:"))
async def handle_public_subscription_reminder_time_other(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    try:
        subscription_id = int(callback.data.split(":", 1)[1].strip())
    except (TypeError, ValueError):
        await callback.answer("Invalid subscription.", show_alert=True)
        return

    subs = await db.list_subscriptions_for_user(callback.from_user.id)
    if not any(sub["id"] == subscription_id for sub in subs):
        await callback.answer("You don't have access to this subscription.", show_alert=True)
        return

    await state.set_state(PublicReminderForm.reminder_time)
    await state.update_data(subscription_id=subscription_id)
    if callback.message:
        await callback.message.answer(
            "⏰ Reminder time for this subscription:\n"
            "Send time in <code>HH:MM</code>\n"
            "\n"
            "Example: <code>16:00</code>",
            reply_markup=dialog_cancel_inline_keyboard(),
        )
    await callback.answer()


@public_router.callback_query(F.data.startswith("public_sub_remindertime:"))
async def handle_public_subscription_reminder_time_select(
    callback: CallbackQuery,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Invalid action.", show_alert=True)
        return
    _, subscription_id_raw, raw_time = parts
    try:
        subscription_id = int(subscription_id_raw)
    except ValueError:
        await callback.answer("Invalid subscription.", show_alert=True)
        return

    normalized = normalize_time_string(raw_time)
    if normalized is None:
        await callback.answer("Time must be HH:MM.", show_alert=True)
        return

    subs = await db.list_subscriptions_for_user(callback.from_user.id)
    if not any(sub["id"] == subscription_id for sub in subs):
        await callback.answer("You don't have access to this subscription.", show_alert=True)
        return

    await db.set_user_subscription_setting(
        callback.from_user.id,
        subscription_id,
        "reminder_time",
        normalized,
    )
    await callback.answer(f"Reminder time set to {normalized}")
    await send_public_subscription_detail(callback, db, subscription_id)


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

    subs = await db.list_subscriptions_for_user(message.from_user.id)
    if not any(sub["id"] == subscription_id for sub in subs):
        await state.clear()
        await message.answer("You don't have access to this subscription.")
        return

    lowered = raw_time.lower()
    if lowered in {"default", "base", "admin", "-"}:
        await db.delete_user_subscription_setting(message.from_user.id, int(subscription_id), "reminder_time")
        await state.clear()
        await send_public_subscription_detail(message, db, int(subscription_id))
        return

    normalized = normalize_time_string(raw_time)
    if normalized is None:
        await message.answer("Time must be in HH:MM format (24-hour clock).")
        return

    await db.set_user_subscription_setting(
        message.from_user.id,
        int(subscription_id),
        "reminder_time",
        normalized,
    )
    await state.clear()
    await send_public_subscription_detail(message, db, int(subscription_id))


@public_router.message(PublicSubscriptionCurrencyForm.currency)
async def handle_public_subscription_currency_input(
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

    subs = await db.list_subscriptions_for_user(message.from_user.id)
    if not any(sub["id"] == subscription_id for sub in subs):
        await state.clear()
        await message.answer("You don't have access to this subscription.")
        return

    raw_value = (message.text or "").strip().upper()
    lowered = raw_value.lower()
    if lowered in {"default", "base", "admin", "-"}:
        await db.delete_user_subscription_setting(
            message.from_user.id,
            int(subscription_id),
            "target_currency",
        )
        await state.clear()
        await send_public_subscription_detail(message, db, int(subscription_id))
        return

    if len(raw_value) != 3 or not raw_value.isalpha():
        await message.answer("Currency must contain 3 letters, for example USD.")
        return

    await db.set_user_subscription_setting(
        message.from_user.id,
        int(subscription_id),
        "target_currency",
        raw_value,
    )
    await state.clear()
    await send_public_subscription_detail(message, db, int(subscription_id))


@public_router.message(PublicSettingsForm.base_time)
async def handle_public_settings_time_input(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    if not message.from_user:
        await state.clear()
        await message.answer("Unable to identify your account.")
        return
    raw_value = (message.text or "").strip()
    normalized = normalize_time_string(raw_value)
    if normalized is None:
        await message.answer("Time must be in HH:MM format, for example 16:00.")
        return
    await db.set_user_setting(message.from_user.id, "base_reminder_time", normalized)
    await state.clear()
    await message.answer("Base time updated.", reply_markup=public_reply_keyboard())
    await _show_public_settings_menu(message, db, settings, message.from_user.id)


@public_router.message(PublicSettingsForm.base_timezone)
async def handle_public_settings_timezone_input(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    if not message.from_user:
        await state.clear()
        await message.answer("Unable to identify your account.")
        return
    raw_value = (message.text or "").strip()
    normalized = normalize_timezone_name(raw_value)
    if normalized is None:
        await message.answer("Unsupported timezone. Example: Europe/Moscow.")
        return
    await db.set_user_setting(message.from_user.id, "timezone", normalized)
    await state.clear()
    await message.answer("Timezone updated.", reply_markup=public_reply_keyboard())
    await _show_public_settings_menu(message, db, settings, message.from_user.id)


@public_router.message(PublicAccountForm.full_name)
async def handle_public_account_rename_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    if not message.from_user:
        await state.clear()
        await message.answer("Unable to identify your account.")
        return
    normalized_name, error_message = validate_person_name(message.text)
    if error_message:
        await message.answer(error_message)
        return
    friend = await db.get_friend_by_telegram(message.from_user.id)
    if not friend:
        await state.clear()
        await message.answer(
            "Your profile is not set up yet. Ask an admin to add you to the users list.",
            reply_markup=public_reply_keyboard(),
        )
        return
    await db.update_friend_name(int(friend["id"]), normalized_name)
    await state.clear()
    await message.answer("Name updated.", reply_markup=public_reply_keyboard())
    await send_public_account_detail(message, db, message.from_user.id)


async def send_admin_help(message: Message) -> None:
    text = (
        "ℹ️ PingSubs helps you manage shared subscriptions, users, and payment reminders.\n\n"
        "Buttons:\n"
        "👥 Users — add, edit, and remove users.\n"
        "📋 Subscriptions — manage plans and reminders.\n"
        "📊 Payments report — payment summaries.\n"
        "💳 Payment methods — top-up routes and manual approvals.\n"
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


@public_router.callback_query(F.data == "dialog:cancel")
@admin_router.callback_query(F.data == "dialog:cancel")
async def handle_dialog_cancel_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await state.clear()
    if callback.message:
        with contextlib.suppress(Exception):
            await callback.message.delete()
    await callback.answer()



@admin_router.message(F.text == "📊 Payments report")
async def handle_payments_report(message: Message, db: Database) -> None:
    subscriptions = await db.list_subscriptions()
    if not subscriptions:
        await message.answer("No subscriptions yet.", reply_markup=admin_reply_keyboard())
        return

    tz_name = await resolve_report_timezone(db)
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
            tz_name=tz_name,
        )
        blocks.append(block)

    if not blocks:
        await message.answer("No users to report yet.", reply_markup=admin_reply_keyboard())
        return

    await send_chunked_responder_text(
        message,
        assemble_payments_report(blocks, collapse_min_green=ADMIN_REPORT_COLLAPSE_MIN_GREEN),
        reply_markup=admin_reply_keyboard(),
    )


async def _settings_menu_text(settings: Settings) -> str:
    return (
        "⚙️ Settings:\n\n"
        "⏰ Time:\n"
        f"Base time: <code>{html.escape(settings.base_reminder_time)}</code>\n\n"
        "🌍 Timezone:\n"
        f"Timezone: <code>{html.escape(settings.base_timezone)}</code>\n\n"
        "🔢 Rounding:\n"
        f"Rounding: <code>{html.escape(_rounding_label(settings.currency_rounding))}</code>"
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


@admin_router.callback_query(F.data == "settings:time")
async def handle_settings_time(callback: CallbackQuery, settings: Settings) -> None:
    text = (
        "⏰ Base time:\n"
        "\n"
        f"Current: <code>{html.escape(settings.base_reminder_time)} ({html.escape(settings.base_timezone)})</code>\n"
        f"Your local time: {tg_clock(settings.base_reminder_time, settings.base_timezone)}\n"
        "\n"
        "Choose a value:"
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=settings_time_keyboard(settings.base_reminder_time),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "settings:time_other")
async def handle_settings_time_other(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsForm.base_time)
    if callback.message:
        await callback.message.answer(
            "⏰ Base time:\n"
            "\n"
            "Send time in <code>HH:MM</code>.",
            reply_markup=dialog_cancel_inline_keyboard(),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "settings:timezone")
async def handle_settings_timezone(callback: CallbackQuery, settings: Settings) -> None:
    text = (
        "🌍 Timezone:\n"
        "\n"
        f"Current: <code>{html.escape(settings.base_timezone)}</code>\n"
        "\n"
        "Choose a value:"
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=settings_timezone_keyboard(settings.base_timezone),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "settings:timezone_other")
async def handle_settings_timezone_other(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsForm.base_timezone)
    if callback.message:
        await callback.message.answer(
            "🌍 Timezone:\n"
            "\n"
            "Send an IANA timezone.\n"
            "Examples: <code>Europe/Moscow</code>, <code>America/New_York</code>.",
            reply_markup=dialog_cancel_inline_keyboard(),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("settings_timezone:"))
async def handle_settings_timezone_select(
    callback: CallbackQuery,
    settings: Settings,
    db: Database,
) -> None:
    raw_value = callback.data.split(":", 1)[1].strip()
    normalized = normalize_timezone_name(raw_value)
    if normalized is None:
        await callback.answer("Unsupported timezone.", show_alert=True)
        return
    settings.base_timezone = normalized
    await db.set_setting("base_timezone", normalized)
    await callback.answer(f"Timezone set to {normalized}")
    if callback.message:
        await callback.message.edit_text(
            await _settings_menu_text(settings),
            reply_markup=admin_settings_keyboard(),
        )


@admin_router.callback_query(F.data.startswith("settings_time:"))
async def handle_settings_time_select(
    callback: CallbackQuery,
    settings: Settings,
    db: Database,
) -> None:
    raw_value = callback.data.split(":", 1)[1].strip()
    normalized = normalize_time_string(raw_value)
    if normalized is None:
        await callback.answer("Time must be HH:MM.", show_alert=True)
        return
    settings.base_reminder_time = normalized
    await db.set_setting("base_reminder_time", normalized)
    await callback.answer(f"Base time set to {normalized}")
    if callback.message:
        await callback.message.edit_text(
            await _settings_menu_text(settings),
            reply_markup=admin_settings_keyboard(),
        )


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
        "🔢 Rounding:\n"
        "\n"
        f"Current: <code>{html.escape(_rounding_label(settings.currency_rounding))}</code>\n"
        "\n"
        "Choose a mode:"
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
    text = "Notifications:\nChoose admin notification events:"
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


@admin_router.callback_query(F.data == "settings:tests")
async def handle_settings_tests(callback: CallbackQuery, db: Database) -> None:
    await send_settings_tests_menu(callback, db)


@admin_router.callback_query(F.data == "tests:send")
async def handle_test_send_menu(callback: CallbackQuery, db: Database) -> None:
    await send_test_user_list(callback, db)


@admin_router.callback_query(TestListAction.filter(F.action == "page"))
async def handle_test_list_page(
    callback: CallbackQuery,
    callback_data: TestListAction,
    db: Database,
) -> None:
    await send_test_user_list(callback, db, page=max(1, callback_data.page))


@admin_router.callback_query(TestSendAction.filter())
async def handle_test_reminder_send(
    callback: CallbackQuery,
    callback_data: TestSendAction,
    bot: Bot,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    friends = await db.list_friends()
    person = next(
        (
            friend
            for friend in friends
            if friend.get("telegram_id") is not None
            and int(friend["telegram_id"]) == callback_data.telegram_id
        ),
        None,
    )
    if person is None:
        await callback.answer("This user no longer exists.", show_alert=True)
        await send_test_user_list(callback, db)
        return

    full_name = str(person.get("full_name") or "Unknown")
    recipient_timezone = normalize_timezone_name(
        await db.get_effective_user_timezone(int(person["telegram_id"]), DEFAULT_REMINDER_TIMEZONE),
        DEFAULT_REMINDER_TIMEZONE,
    ) or DEFAULT_REMINDER_TIMEZONE
    today = datetime.now(parse_timezone(recipient_timezone)).date()
    default_destination = None
    default_destination_id = await db.get_default_payment_destination_id()
    if default_destination_id is not None:
        default_destination = await db.get_payment_destination(default_destination_id)

    payment_label = "not set"
    payment_details = ""
    payment_link = ""
    amount_currency = "RUB"
    if default_destination:
        amount_currency = str(default_destination.get("currency") or "RUB").upper()
        payment_label = (
            f"Default ({default_destination['title']} · {amount_currency})"
        )
        payment_details = str(default_destination.get("details") or "").strip()
        payment_link = str(default_destination.get("payment_link") or "").strip()

    target_currency = await db.get_effective_target_currency(
        int(person["telegram_id"]),
        0,
        subscription_default=amount_currency,
        default_currency=converter.target_currency,
    )
    test_amount_currency = amount_currency
    if test_amount_currency == target_currency:
        for candidate_currency in TEST_REMINDER_CURRENCY_FALLBACKS:
            if candidate_currency != target_currency:
                test_amount_currency = candidate_currency
                break

    test_amount_value = 100.0
    if test_amount_currency != "RUB":
        try:
            converted_from_rub = await converter.convert_to(100.0, "RUB", test_amount_currency)
        except Exception:  # noqa: BLE001
            converted_from_rub = None
        if converted_from_rub is not None and converted_from_rub > 0:
            test_amount_value = converted_from_rub

    converted_text = None
    if target_currency != test_amount_currency:
        try:
            converted_amount = await converter.convert_to(
                test_amount_value,
                test_amount_currency,
                target_currency,
            )
        except Exception:  # noqa: BLE001
            converted_amount = None
        if converted_amount is not None:
            converted_text = format_converted_amount(
                converted_amount,
                target_currency,
                "precise",
            )

    status_text, due_date_text = _format_status_and_date(today, today)
    text = _build_reminder_message(
        person_name=full_name,
        subscription_name="Test subscription",
        status_text=status_text,
        due_date_text=due_date_text,
        amount_text=f"{test_amount_value:.2f} {test_amount_currency}",
        converted_text=converted_text,
        payment_label=payment_label,
        payment_details=payment_details,
        payment_link=payment_link,
        comment="Test comment",
        footer="This is a test reminder. Tapping “Paid” will not record anything.",
        due_value=today.isoformat(),
        tz_name=recipient_timezone,
        today=today,
    )
    sent_message_id = await _send_message_with_retry(
        bot,
        int(person["telegram_id"]),
        text,
        reply_markup=build_test_payment_confirmation_keyboard(
            0,
            today,
            payment_details=payment_details,
            payment_link=payment_link,
        ),
        logger=logger,
    )
    if sent_message_id is None:
        await callback.answer("Could not deliver the test message. Try again.", show_alert=True)
        return
    await callback.answer("Test message sent.")
    await send_test_user_list(callback, db)


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
            "Notifications:\nChoose admin notification events:",
            reply_markup=settings_notifications_keyboard(
                reminders_enabled,
                paid_enabled,
                closed_enabled,
            ),
        )
    await callback.answer()


@admin_router.message(SettingsForm.base_time)
async def handle_settings_time_input(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db: Database,
) -> None:
    raw_value = (message.text or "").strip()
    normalized = normalize_time_string(raw_value)
    if normalized is None:
        await message.answer("Time must be in HH:MM format, for example 16:00.")
        return
    settings.base_reminder_time = normalized
    await db.set_setting("base_reminder_time", normalized)
    await state.clear()
    await message.answer(
        f"Base time set to {normalized}.",
        reply_markup=admin_reply_keyboard(),
    )


@admin_router.message(SettingsForm.base_timezone)
async def handle_settings_timezone_input(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db: Database,
) -> None:
    raw_value = (message.text or "").strip()
    normalized = normalize_timezone_name(raw_value)
    if normalized is None:
        await message.answer("Unsupported timezone. Example: Europe/Moscow.")
        return
    settings.base_timezone = normalized
    await db.set_setting("base_timezone", normalized)
    await state.clear()
    await message.answer(
        f"Timezone set to {normalized}.",
        reply_markup=admin_reply_keyboard(),
    )
