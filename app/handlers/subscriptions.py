from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core.constants import (
    DATE_INPUT_FORMAT,
    MONTHLY_PERIOD_SENTINEL,
    PAYMENT_MODE_FIXED,
    PAYMENT_MODE_SPLIT,
)
from app.storage.db import Database
from app.ui.helpers import (
    currency_prompt,
    get_edit_subscription_id,
    payment_mode_prompt,
    period_prompt,
    require_edit_subscription_id,
    send_participants_settings,
    send_pricing_settings,
    send_subscription_cycle_actions,
    send_subscription_more,
    send_subscription_open_cycles,
    send_subscription_payment_report,
    send_subscription_user_amounts,
    send_reminder_send_menu,
    send_reminder_settings,
    send_subscription_detail,
    send_subscription_list,
    send_subscription_payment_info,
    share_limit_prompt,
    start_subscription_edit_flow,
)
from app.ui.keyboards import (
    admin_reply_keyboard,
    comment_edit_keyboard,
    dialog_cancel_inline_keyboard,
    subscription_payment_destination_keyboard,
    subscription_payment_mode_keyboard,
    subscription_currency_keyboard,
    subscription_base_currency_keyboard,
    subscription_reminder_time_keyboard,
    user_amount_clear_keyboard,
)
from app.ui.states import (
    CycleAction,
    ReminderSendAction,
    Responder,
    SubscriptionAction,
    SubscriptionEditForm,
    SubscriptionForm,
)
from app.core.reminders import (
    DEFAULT_REMINDER_OFFSETS,
    format_offsets_for_display,
    normalize_monthly_anchor_day,
    normalize_time_string,
    parse_offsets,
    parse_time_string,
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


async def _load_open_cycle(
    callback: CallbackQuery,
    db: Database,
    subscription_id: int,
    due_value: str,
) -> Optional[Dict[str, object]]:
    subscription = await _load_subscription(callback, db, subscription_id)
    if not subscription:
        return None
    open_cycles = await db.list_open_cycles(subscription_id)
    if due_value not in open_cycles:
        await callback.answer("This cycle is already closed.", show_alert=True)
        return None
    return subscription


def _resolve_cycle_participants(
    cycle_state: Dict[str, object],
    live_participants: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    cycle_participants = list(cycle_state.get("participants") or [])
    snapshot_ready = bool(cycle_state.get("snapshot_ready"))
    if not cycle_participants and not snapshot_ready:
        return live_participants
    return cycle_participants


async def _advance_subscription_after_cycle_close(
    db: Database,
    subscription: Dict[str, object],
    due_value: str,
) -> None:
    if str(subscription.get("next_charge_at") or "") != due_value:
        return
    try:
        cycle_due = datetime.strptime(due_value, "%Y-%m-%d").date()
    except ValueError:
        return
    period_days = int(subscription.get("period_days") or 30)
    monthly_anchor_day = normalize_monthly_anchor_day(subscription.get("monthly_anchor_day"))
    next_due = calculate_next_charge_date(cycle_due, period_days, monthly_anchor_day)
    await db.update_subscription_fields(int(subscription["id"]), next_charge_at=next_due)


async def _show_payment_mode_menu(
    callback: CallbackQuery,
    subscription_id: int,
    current_mode: str,
) -> None:
    if callback.message:
        await callback.message.edit_text(
            "💳 Payment mode:\n"
            "\n"
            "Choose how each user amount is calculated.\n"
            "Then configure the option below.",
            reply_markup=subscription_payment_mode_keyboard(subscription_id, current_mode),
        )


async def _show_reminder_time_menu(
    callback: CallbackQuery,
    db: Database,
    subscription_id: int,
) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription or not callback.message:
        return
    base_time = parse_time_string(await db.get_effective_base_reminder_time()).strftime("%H:%M")
    current_override = normalize_time_string(subscription.get("reminder_time"))
    current_time = current_override or base_time
    current_label = current_override or f"default ({base_time})"
    await callback.message.edit_text(
        "⏰ Reminder time:\n"
        f"Current: <code>{escape_html(current_label)}</code>\n"
        "\n"
        "🌍 Applied in each recipient timezone.\n"
        "Choose a value:",
        reply_markup=subscription_reminder_time_keyboard(
            subscription_id,
            current_time,
            base_time,
        ),
    )


async def _show_subscription_currency_menu(
    callback: CallbackQuery,
    db: Database,
    subscription_id: int,
) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription or not callback.message:
        return
    current_currency = str(subscription.get("currency") or "RUB").strip().upper()
    await callback.message.edit_text(
        "💱 Currency:\n"
        f"Current: <code>{escape_html(current_currency)}</code>\n"
        "\n"
        "Choose a value:",
        reply_markup=subscription_currency_keyboard(
            subscription_id,
            current_currency,
        ),
    )


async def _show_subscription_payment_destination_menu(
    callback: CallbackQuery,
    db: Database,
    subscription_id: int,
) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription or not callback.message:
        return
    destinations = await db.list_payment_destinations()
    if not destinations:
        await callback.message.edit_text(
            "💳 Payment method:\nNo payment methods configured yet.",
            reply_markup=subscription_payment_destination_keyboard(
                subscription_id,
                [],
                None,
                await db.get_default_payment_destination_id(),
            ),
        )
        return
    default_destination_id = await db.get_default_payment_destination_id()
    try:
        selected_destination_id = (
            int(subscription.get("payment_destination_id"))
            if subscription.get("payment_destination_id") is not None
            else None
        )
    except (TypeError, ValueError):
        selected_destination_id = None
    current_destination = await db.get_effective_subscription_payment_destination(subscription_id)
    current_label = "not set"
    if current_destination:
        if selected_destination_id is None:
            current_label = f"Default ({current_destination['title']} · {current_destination['currency']})"
        else:
            current_label = f"{current_destination['title']} ({current_destination['currency']})"
    await callback.message.edit_text(
        "💳 Payment method:\n"
        f"Current: <code>{escape_html(current_label)}</code>\n\n"
        "Choose the payment method for this subscription.",
        reply_markup=subscription_payment_destination_keyboard(
            subscription_id,
            destinations,
            selected_destination_id,
            default_destination_id,
        ),
    )


async def start_subscription_creation(responder: Responder, state: FSMContext) -> None:
    target = _response_target(responder)
    await state.clear()
    await state.set_state(SubscriptionForm.name)
    await target.answer(
        "New Subscription:\n"
        "Name: send subscription name.\n"
        "Example: <code>Netflix</code>.",
        reply_markup=dialog_cancel_inline_keyboard(),
    )


async def _finalize_new_subscription(
    responder: Responder,
    state: FSMContext,
    db: Database,
    share_limit: Optional[int],
) -> None:
    data = await state.get_data()
    payment_mode = str(data.get("payment_mode") or PAYMENT_MODE_SPLIT).strip().lower()
    if payment_mode not in {PAYMENT_MODE_SPLIT, PAYMENT_MODE_FIXED}:
        payment_mode = PAYMENT_MODE_SPLIT
    sub_id = await db.create_subscription(
        name=data["subscription_name"],
        amount=float(data["amount"]),
        currency=data["currency"],
        due_date=data["due_date"],
        period_days=int(data["period_days"]),
        share_limit=share_limit,
        payment_mode=payment_mode,
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
    await message.answer(
        "Amount:\n"
        "Send charge amount.\n"
        "Example: <code>149.99</code>."
    )


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
    await message.answer(
        "Next Charge Date:\n"
        "Send date in <code>DD.MM.YYYY</code>."
    )


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
        await callback.message.answer(
            "Next Charge Date:\n"
            "Send date in <code>DD.MM.YYYY</code>."
        )
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


@admin_router.callback_query(F.data.startswith("sub_base_currency:"))
async def handle_subscription_base_currency_quick_select(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Invalid action.", show_alert=True)
        return
    _, subscription_id_raw, raw_value = parts
    try:
        subscription_id = int(subscription_id_raw)
    except ValueError:
        await callback.answer("Invalid subscription.", show_alert=True)
        return

    currency = raw_value.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        await callback.answer("Currency must contain 3 letters.", show_alert=True)
        return

    await db.update_subscription_fields(subscription_id, base_currency=currency)
    await state.clear()
    await callback.answer(f"Convert currency set to {currency}")
    await send_pricing_settings(callback, db, subscription_id)


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
    await state.set_state(SubscriptionForm.payment_mode)
    text_prompt, markup_prompt = payment_mode_prompt()
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
        await state.set_state(SubscriptionForm.payment_mode)
        text_prompt, markup_prompt = payment_mode_prompt()
        await callback.message.answer(text_prompt, reply_markup=markup_prompt)
        label = "monthly" if days == MONTHLY_PERIOD_SENTINEL else f"{days} day(s)"
        await callback.answer(f"Period set to {label}")
        return

    if current_state == SubscriptionEditForm.period.state:
        sub_id = await get_edit_subscription_id(state)
        if sub_id is None:
            await callback.answer("Session expired. Reopen the subscription.", show_alert=True)
            return
        subscription = await db.get_subscription(sub_id)
        monthly_anchor_day: Optional[int] = None
        if days == MONTHLY_PERIOD_SENTINEL:
            monthly_anchor_day = normalize_monthly_anchor_day(
                subscription.get("monthly_anchor_day") if subscription else None
            )
            if monthly_anchor_day is None and subscription:
                try:
                    monthly_anchor_day = datetime.strptime(
                        str(subscription["next_charge_at"]),
                        "%Y-%m-%d",
                    ).date().day
                except (KeyError, TypeError, ValueError):
                    monthly_anchor_day = None
        await db.update_subscription_fields(
            sub_id,
            period_days=days,
            monthly_anchor_day=monthly_anchor_day,
        )
        await state.clear()
        label = "monthly" if days == MONTHLY_PERIOD_SENTINEL else f"{days} day(s)"
        await callback.answer(f"Period set to {label}")
        await send_subscription_detail(callback, db, sub_id)
        return

    await callback.answer()


@admin_router.callback_query(F.data.startswith("create_payment_mode:"))
async def handle_create_payment_mode_quick_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    current_state = await state.get_state()
    if current_state != SubscriptionForm.payment_mode.state:
        await callback.answer()
        return

    mode = callback.data.split(":", 1)[1].strip().lower()
    if mode not in {PAYMENT_MODE_SPLIT, PAYMENT_MODE_FIXED}:
        await callback.answer("Unsupported mode.", show_alert=True)
        return
    await state.update_data(payment_mode=mode)

    if mode == PAYMENT_MODE_FIXED:
        await _finalize_new_subscription(callback, state, db, share_limit=None)
        return

    await state.set_state(SubscriptionForm.share_limit)
    text_prompt, markup_prompt = share_limit_prompt()
    await callback.message.answer(text_prompt, reply_markup=markup_prompt)
    await callback.answer("Payment mode set to split")


@admin_router.message(SubscriptionForm.payment_mode)
async def subscription_form_payment_mode(message: Message, state: FSMContext, db: Database) -> None:
    raw_value = (message.text or "").strip().lower()
    if raw_value in {"split", "shares", "share"}:
        mode = PAYMENT_MODE_SPLIT
    elif raw_value in {"fixed", "amount", "fixed per user"}:
        mode = PAYMENT_MODE_FIXED
    else:
        text_prompt, markup_prompt = payment_mode_prompt()
        await message.answer(
            "Choose a valid payment mode: split or fixed.\n"
            "You can tap a button below.",
            reply_markup=markup_prompt,
        )
        return

    await state.update_data(payment_mode=mode)
    if mode == PAYMENT_MODE_FIXED:
        await _finalize_new_subscription(message, state, db, share_limit=None)
        return

    await state.set_state(SubscriptionForm.share_limit)
    text_prompt, markup_prompt = share_limit_prompt()
    await message.answer(text_prompt, reply_markup=markup_prompt)


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
    prompt = (
        "✏️ Rename:\n"
        f"Current: <code>{escape_html(subscription['name'])}</code>\n"
        "\n"
        "Send new subscription name."
    )
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
        "💰 Amount:\n"
        f"Current: <code>{subscription['amount']:.2f} {escape_html(subscription['currency'])}</code>\n"
        "\n"
        "Send new value (example: <code>149.99</code>)."
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
    await state.clear()
    await _show_subscription_currency_menu(callback, db, callback_data.subscription_id)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("sub_currency:"))
async def handle_subscription_currency_select(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
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
    await db.update_subscription_fields(subscription_id, currency=currency)
    await state.clear()
    await callback.answer(f"Currency set to {currency}")
    await _show_subscription_currency_menu(callback, db, subscription_id)


@admin_router.callback_query(F.data.startswith("sub_currency_other:"))
async def handle_subscription_currency_other(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parts = callback.data.split(":", 1)
    if len(parts) != 2:
        await callback.answer("Invalid action.", show_alert=True)
        return
    _, subscription_id_raw = parts
    try:
        subscription_id = int(subscription_id_raw)
    except ValueError:
        await callback.answer("Invalid subscription.", show_alert=True)
        return
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await callback.answer("Subscription not found.", show_alert=True)
        return

    await state.set_state(SubscriptionEditForm.currency)
    await state.update_data(edit_subscription_id=subscription_id)
    if callback.message:
        await callback.message.answer(
            "💱 Currency:\n"
            "Send a 3-letter currency code.\n"
            "\n"
            "Example: <code>CHF</code>",
            reply_markup=dialog_cancel_inline_keyboard(),
        )
    await callback.answer()


@admin_router.callback_query(SubscriptionAction.filter(F.action == "basecurrency"))
async def handle_subscription_base_currency_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    await state.clear()
    current_currency = str(subscription.get("base_currency") or subscription.get("currency") or "RUB").upper()
    text = (
        "🌐 Convert currency:\n"
        f"Current: <code>{escape_html(current_currency)}</code>\n"
        "\n"
        "This is the default target currency for this subscription."
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=subscription_base_currency_keyboard(
                callback_data.subscription_id,
                current_currency,
            ),
        )
    await callback.answer()


@admin_router.callback_query(SubscriptionAction.filter(F.action == "basecurrency_other"))
async def handle_subscription_base_currency_other(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    state: FSMContext,
) -> None:
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.base_currency,
        "🌐 Convert currency:\nSend a 3-letter currency code.\nExample: <code>EUR</code>.",
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "paymentmode"))
async def handle_subscription_payment_mode_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    await state.clear()
    await send_participants_settings(callback, db, callback_data.subscription_id)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("sub_payment_mode:"))
async def handle_subscription_payment_mode_select(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Invalid action.", show_alert=True)
        return
    _, subscription_id_raw, raw_mode = parts
    try:
        subscription_id = int(subscription_id_raw)
    except ValueError:
        await callback.answer("Invalid subscription.", show_alert=True)
        return
    mode = raw_mode.strip().lower()
    if mode not in {PAYMENT_MODE_SPLIT, PAYMENT_MODE_FIXED}:
        await callback.answer("Unsupported mode.", show_alert=True)
        return
    await db.update_subscription_fields(subscription_id, payment_mode=mode)
    await state.clear()
    await callback.answer(f"Payment mode set to {'fixed' if mode == PAYMENT_MODE_FIXED else 'split'}.")
    await send_participants_settings(callback, db, subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "useramounts"))
async def handle_subscription_user_amounts_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    current_mode = str(subscription.get("payment_mode") or PAYMENT_MODE_SPLIT).strip().lower()
    if current_mode != PAYMENT_MODE_FIXED:
        await callback.answer("Switch Payment mode to Fixed first.", show_alert=True)
        await send_participants_settings(callback, db, callback_data.subscription_id)
        return
    await send_subscription_user_amounts(callback, db, callback_data.subscription_id)


@admin_router.callback_query(F.data.startswith("sub_user_amount:"))
async def handle_subscription_user_amount_edit_callback(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Invalid action.", show_alert=True)
        return
    _, subscription_id_raw, friend_id_raw = parts
    try:
        subscription_id = int(subscription_id_raw)
        friend_id = int(friend_id_raw)
    except ValueError:
        await callback.answer("Invalid user.", show_alert=True)
        return
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await callback.answer("Subscription not found.", show_alert=True)
        return
    default_amount = float(subscription.get("amount") or 0.0)
    participants = await db.list_subscription_participants(subscription_id)
    target_person = next((person for person in participants if int(person["id"]) == friend_id), None)
    if target_person is None:
        await callback.answer("User is not in this subscription.", show_alert=True)
        return

    current_value = target_person.get("fixed_amount")
    has_current_value = False
    if current_value is None:
        current_text = f"{default_amount:.2f}"
    else:
        try:
            current_text = f"{float(current_value):.2f}"
            has_current_value = True
        except (TypeError, ValueError):
            current_text = f"{default_amount:.2f}"

    await state.set_state(SubscriptionEditForm.user_amount)
    await state.update_data(
        user_amount_subscription_id=subscription_id,
        user_amount_friend_id=friend_id,
    )
    if callback.message:
        prompt = (
            "👥 Amount per user:\n\n"
            f"User: <code>{escape_html(str(target_person['full_name']))}</code>\n"
            f"Current: <code>{escape_html(current_text)}</code>\n\n"
            "Send amount (example: <code>300</code>)."
        )
        if has_current_value:
            prompt += "\nOr tap <code>Clear</code> to use subscription amount."
        await callback.message.answer(
            prompt,
            reply_markup=(
                user_amount_clear_keyboard(subscription_id, friend_id)
                if has_current_value
                else dialog_cancel_inline_keyboard()
            ),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("sub_user_amount_clear:"))
async def handle_subscription_user_amount_clear_callback(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Invalid action.", show_alert=True)
        return
    _, subscription_id_raw, friend_id_raw = parts
    try:
        subscription_id = int(subscription_id_raw)
        friend_id = int(friend_id_raw)
    except ValueError:
        await callback.answer("Invalid user.", show_alert=True)
        return

    participants = await db.list_subscription_participants(subscription_id)
    target_person = next((person for person in participants if int(person["id"]) == friend_id), None)
    if target_person is None:
        await callback.answer("User is not in this subscription.", show_alert=True)
        return

    await db.update_participant_fixed_amount(subscription_id, friend_id, None)
    await state.clear()
    await callback.answer("Using subscription amount.")
    await send_subscription_user_amounts(callback, db, subscription_id)


@admin_router.callback_query(F.data.startswith("sub_user_amount_all:"))
async def handle_subscription_user_amount_set_all_callback(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parts = callback.data.split(":", 1)
    if len(parts) != 2:
        await callback.answer("Invalid action.", show_alert=True)
        return
    _, subscription_id_raw = parts
    try:
        subscription_id = int(subscription_id_raw)
    except ValueError:
        await callback.answer("Invalid subscription.", show_alert=True)
        return

    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await callback.answer("Subscription not found.", show_alert=True)
        return
    participants = await db.list_subscription_participants(subscription_id)
    if not participants:
        await callback.answer("No users in this subscription.", show_alert=True)
        return

    await state.set_state(SubscriptionEditForm.user_amount_all)
    await state.update_data(user_amount_all_subscription_id=subscription_id)
    if callback.message:
        await callback.message.answer(
            "👥 Amount per user:\n\n"
            "Send amount to apply it to all users.\n"
            "Example: <code>300</code>.",
            reply_markup=dialog_cancel_inline_keyboard(),
        )
    await callback.answer()


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
    prompt = (
        "📅 Next charge:\n"
        f"Current: <code>{escape_html(current)}</code>\n"
        "\n"
        "Send new date in <code>DD.MM.YYYY</code>"
    )
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
    _, period_markup = period_prompt()
    current_period = int(subscription.get("period_days") or 30)
    if current_period == MONTHLY_PERIOD_SENTINEL:
        current_label = "monthly"
    else:
        current_label = f"{current_period} day(s)"
    prompt = (
        "🔁 Period:\n"
        f"Current: <code>{escape_html(current_label)}</code>\n"
        "\n"
        "Repeat period in days. Choose a preset or send your own number."
    )
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


@admin_router.callback_query(SubscriptionAction.filter(F.action == "more"))
async def handle_subscription_more_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    await send_subscription_more(callback, db, callback_data.subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "paymentinfo"))
async def handle_subscription_payment_info_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    await send_subscription_payment_info(callback, db, callback_data.subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "paymentmethod"))
async def handle_subscription_payment_method_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    await _show_subscription_payment_destination_menu(callback, db, callback_data.subscription_id)


@admin_router.callback_query(F.data.startswith("sub_payment_destination:"))
async def handle_subscription_payment_method_select(
    callback: CallbackQuery,
    db: Database,
) -> None:
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Invalid action.", show_alert=True)
        return
    try:
        subscription_id = int(parts[1])
    except ValueError:
        await callback.answer("Invalid subscription.", show_alert=True)
        return
    selected_value = parts[2].strip().lower()
    if selected_value == "default":
        await db.update_subscription_fields(subscription_id, payment_destination_id=None)
        await callback.answer("Using default payment method.")
        await send_subscription_payment_info(callback, db, subscription_id)
        return
    try:
        destination_id = int(selected_value)
    except ValueError:
        await callback.answer("Invalid payment method.", show_alert=True)
        return
    if not await db.get_payment_destination(destination_id):
        await callback.answer("Payment method not found.", show_alert=True)
        return
    await db.update_subscription_fields(subscription_id, payment_destination_id=destination_id)
    await callback.answer("Payment method updated.")
    await send_subscription_payment_info(callback, db, subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "report"))
async def handle_subscription_report_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    await send_subscription_payment_report(callback, db, callback_data.subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "cycles"))
async def handle_subscription_cycles_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    await send_subscription_open_cycles(callback, db, callback_data.subscription_id)


@admin_router.callback_query(CycleAction.filter(F.action == "open"))
async def handle_cycle_open_callback(
    callback: CallbackQuery,
    callback_data: CycleAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    await send_subscription_cycle_actions(
        callback,
        db,
        callback_data.subscription_id,
        callback_data.due_date,
    )


@admin_router.callback_query(CycleAction.filter(F.action == "recreate"))
async def handle_cycle_recreate_callback(
    callback: CallbackQuery,
    callback_data: CycleAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    subscription = await _load_open_cycle(
        callback,
        db,
        callback_data.subscription_id,
        callback_data.due_date,
    )
    if not subscription:
        return

    await db.reset_cycle(callback_data.subscription_id, callback_data.due_date)
    await db.ensure_cycle(callback_data.subscription_id, callback_data.due_date)
    await send_subscription_cycle_actions(
        callback,
        db,
        callback_data.subscription_id,
        callback_data.due_date,
        notice="Cycle recreated. Snapshot and payment marks were cleared.",
    )


@admin_router.callback_query(CycleAction.filter(F.action == "force_close"))
async def handle_cycle_force_close_callback(
    callback: CallbackQuery,
    callback_data: CycleAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    subscription = await _load_open_cycle(
        callback,
        db,
        callback_data.subscription_id,
        callback_data.due_date,
    )
    if not subscription:
        return

    await db.freeze_cycle_snapshot(callback_data.subscription_id, callback_data.due_date)
    cycle_state_map = await db.list_cycle_participants_for_due_dates(
        callback_data.subscription_id,
        [callback_data.due_date],
    )
    cycle_state = cycle_state_map.get(callback_data.due_date) or {}
    live_participants = await db.list_subscription_participants(callback_data.subscription_id)
    cycle_participants = _resolve_cycle_participants(cycle_state, live_participants)
    participant_ids = sorted(
        {
            int(person.get("telegram_id") or 0)
            for person in cycle_participants
            if int(person.get("telegram_id") or 0) > 0
        }
    )

    payments = await db.list_payments_for_cycles(
        callback_data.subscription_id,
        [callback_data.due_date],
    )
    paid_ids = {
        int(row["paid_by_telegram_id"])
        for row in payments
        if row.get("paid_by_telegram_id") is not None
    }
    added_count = 0
    for telegram_id in participant_ids:
        if telegram_id in paid_ids:
            continue
        await db.log_payment(callback_data.subscription_id, callback_data.due_date, telegram_id)
        added_count += 1

    await db.close_cycle(callback_data.subscription_id, callback_data.due_date)
    await _advance_subscription_after_cycle_close(db, subscription, callback_data.due_date)
    await send_subscription_open_cycles(
        callback,
        db,
        callback_data.subscription_id,
        notice=f"Cycle closed. Marked {added_count} unpaid user(s) as paid.",
    )


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
        "➗ Split limit:\n"
        f"Current: <code>{escape_html(current_label)}</code>\n"
        "\n"
        "Send number of users or tap “Split across all”."
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
    await state.clear()
    await _show_reminder_time_menu(callback, db, callback_data.subscription_id)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("sub_remindertime:"))
async def handle_subscription_reminder_time_select(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Invalid action.", show_alert=True)
        return
    _, subscription_id_raw, time_raw = parts
    try:
        subscription_id = int(subscription_id_raw)
    except ValueError:
        await callback.answer("Invalid subscription.", show_alert=True)
        return
    normalized = normalize_time_string(time_raw)
    if normalized is None:
        await callback.answer("Time must be HH:MM.", show_alert=True)
        return
    await db.update_subscription_fields(subscription_id, reminder_time=normalized)
    await state.clear()
    await callback.answer(f"Reminder time set to {normalized}")
    await _show_reminder_time_menu(callback, db, subscription_id)


@admin_router.callback_query(F.data.startswith("sub_remindertime_other:"))
async def handle_subscription_reminder_time_other(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parts = callback.data.split(":", 1)
    if len(parts) != 2:
        await callback.answer("Invalid action.", show_alert=True)
        return
    _, subscription_id_raw = parts
    try:
        subscription_id = int(subscription_id_raw)
    except ValueError:
        await callback.answer("Invalid subscription.", show_alert=True)
        return
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await callback.answer("Subscription not found.", show_alert=True)
        return
    await state.set_state(SubscriptionEditForm.reminder_time)
    await state.update_data(edit_subscription_id=subscription_id)
    if callback.message:
        await callback.message.answer(
            "⏰ Reminder time:\n"
            "Send time in <code>HH:MM</code>\n"
            "\n"
            "Example: <code>16:00</code>",
            reply_markup=dialog_cancel_inline_keyboard(),
        )
    await callback.answer()


@admin_router.callback_query(SubscriptionAction.filter(F.action == "remindertime_default"))
async def handle_subscription_reminder_time_default(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await db.update_subscription_fields(callback_data.subscription_id, reminder_time="")
    await state.clear()
    await callback.answer("Using base time now.")
    await _show_reminder_time_menu(callback, db, callback_data.subscription_id)


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
        "🔔 Reminder days:\n"
        "Send offsets separated by commas or spaces (example: <code>-1 0 1</code>).\n"
        "Negative means before due date, positive means after.\n"
        "\n"
        f"🏷️ Current: <code>{escape_html(current_offsets)}</code>."
    )
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.reminder_offsets,
        prompt,
    )


def _comment_prompt_text(current_value: str) -> str:
    base = "📝 Comment:"
    if current_value:
        return (
            f"{base}\n"
            f"Current: <code>{escape_html(current_value)}</code>.\n"
            "\n"
            "Send text to attach to reminders."
        )
    return f"{base}\nSend text to attach to reminders."


async def _open_comment_editor(
    callback: CallbackQuery,
    state: FSMContext,
    subscription_id: int,
    current_value: str,
    *,
    edit_existing: bool,
) -> None:
    await state.set_state(SubscriptionEditForm.comment)
    await state.update_data(edit_subscription_id=int(subscription_id))
    prompt = _comment_prompt_text(current_value)
    markup = comment_edit_keyboard(subscription_id, bool(current_value))
    if callback.message:
        if edit_existing:
            await callback.message.edit_text(prompt, reply_markup=markup)
        else:
            await callback.message.answer(prompt, reply_markup=markup)
    await callback.answer()


@admin_router.callback_query(SubscriptionAction.filter(F.action == "comment"))
async def handle_subscription_comment_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    current_value = (subscription.get("comment") or "").strip()
    await _open_comment_editor(
        callback,
        state,
        callback_data.subscription_id,
        current_value,
        edit_existing=False,
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "comment_clear"))
async def handle_subscription_comment_clear(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await db.update_subscription_fields(callback_data.subscription_id, comment="")
    subscription = await db.get_subscription(callback_data.subscription_id)
    if not subscription:
        await callback.answer("Subscription not found.", show_alert=True)
        return
    await _open_comment_editor(
        callback,
        state,
        callback_data.subscription_id,
        "",
        edit_existing=True,
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
        callback_data=SubscriptionAction(action="more", subscription_id=callback_data.subscription_id).pack(),
        style="primary",
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


@admin_router.message(SubscriptionEditForm.base_currency)
async def edit_subscription_base_currency(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    currency = (message.text or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        await message.answer("Currency must contain exactly 3 letters, e.g. EUR.")
        return

    await db.update_subscription_fields(sub_id, base_currency=currency)
    await state.clear()
    await message.answer("Convert currency updated.", reply_markup=admin_reply_keyboard())
    await send_subscription_detail(message, db, sub_id)


@admin_router.message(SubscriptionEditForm.user_amount)
async def edit_subscription_user_amount(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    subscription_id = data.get("user_amount_subscription_id")
    friend_id = data.get("user_amount_friend_id")
    if not subscription_id or not friend_id:
        await state.clear()
        await message.answer("Session expired. Reopen amount-per-user menu.")
        return

    participants = await db.list_subscription_participants(int(subscription_id))
    target_person = next((person for person in participants if int(person["id"]) == int(friend_id)), None)
    if target_person is None:
        await state.clear()
        await message.answer("User is not in this subscription.")
        return

    raw_value = (message.text or "").strip()
    if raw_value.lower() in {"clear", "default", "none", "-"}:
        await db.update_participant_fixed_amount(
            int(subscription_id),
            int(friend_id),
            None,
        )
        await state.clear()
        await message.answer("Using subscription amount.", reply_markup=admin_reply_keyboard())
        await send_subscription_user_amounts(message, db, int(subscription_id))
        return

    try:
        amount = float(raw_value.replace(",", "."))
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.answer("Amount must be zero or a positive number. Example: 300 or 0")
        return

    await db.update_participant_fixed_amount(
        int(subscription_id),
        int(friend_id),
        amount,
    )
    await state.clear()
    await message.answer("Fixed amount updated.", reply_markup=admin_reply_keyboard())
    await send_subscription_user_amounts(message, db, int(subscription_id))


@admin_router.message(SubscriptionEditForm.user_amount_all)
async def edit_subscription_user_amount_all(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    subscription_id = data.get("user_amount_all_subscription_id")
    if not subscription_id:
        await state.clear()
        await message.answer("Session expired. Reopen amount-per-user menu.")
        return

    participants = await db.list_subscription_participants(int(subscription_id))
    if not participants:
        await state.clear()
        await message.answer("No users in this subscription.")
        return

    raw_value = (message.text or "").strip()
    try:
        amount = float(raw_value.replace(",", "."))
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.answer("Amount must be zero or a positive number. Example: 300 or 0")
        return

    await db.update_all_participants_fixed_amount(int(subscription_id), amount)
    await state.clear()
    await message.answer("Fixed amount updated for all users.", reply_markup=admin_reply_keyboard())
    await send_subscription_user_amounts(message, db, int(subscription_id))


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

    subscription = await db.get_subscription(sub_id)
    update_fields: Dict[str, object] = {"next_charge_at": due_date}
    if subscription and int(subscription.get("period_days") or 30) == MONTHLY_PERIOD_SENTINEL:
        update_fields["monthly_anchor_day"] = due_date.day
    await db.update_subscription_fields(sub_id, **update_fields)
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

    subscription = await db.get_subscription(sub_id)
    monthly_anchor_day: Optional[int] = None
    if period == MONTHLY_PERIOD_SENTINEL:
        monthly_anchor_day = normalize_monthly_anchor_day(
            subscription.get("monthly_anchor_day") if subscription else None
        )
        if monthly_anchor_day is None and subscription:
            try:
                monthly_anchor_day = datetime.strptime(
                    str(subscription["next_charge_at"]),
                    "%Y-%m-%d",
                ).date().day
            except (KeyError, TypeError, ValueError):
                monthly_anchor_day = None
    await db.update_subscription_fields(
        sub_id,
        period_days=period,
        monthly_anchor_day=monthly_anchor_day,
    )
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
    normalized = normalize_time_string(candidate)
    if normalized is None:
        await message.answer("Time must be in HH:MM format (24-hour clock).")
        return

    await db.update_subscription_fields(sub_id, reminder_time=normalized)
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


@admin_router.message(SubscriptionEditForm.comment)
async def edit_subscription_comment(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    raw = (message.text or "").strip()
    if raw.lower() in {"clear", "none", "-"}:
        comment = ""
    else:
        comment = raw

    await db.update_subscription_fields(sub_id, comment=comment)
    await state.clear()
    await message.answer("Comment updated.", reply_markup=admin_reply_keyboard())
    await send_subscription_detail(message, db, sub_id)
