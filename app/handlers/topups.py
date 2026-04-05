from __future__ import annotations

import contextlib
from typing import Optional

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.storage.db import Database
from app.ui.helpers import respond_with_markup, send_member_detail
from app.ui.keyboards import (
    admin_reply_keyboard,
    dialog_keyboard,
    member_payment_destination_keyboard,
    payment_destination_delete_keyboard,
    payment_destination_detail_keyboard,
    payment_destinations_keyboard,
    public_reply_keyboard,
    topup_destination_keyboard,
    topup_pending_requests_keyboard,
    topup_request_review_keyboard,
    topup_request_submit_keyboard,
)
from app.ui.states import (
    MemberAction,
    PaymentDestinationAction,
    PaymentDestinationEditForm,
    PaymentDestinationForm,
    TopUpAction,
    TopUpForm,
)
from app.ui.text import escape_html

from . import admin_router, public_router


def _normalize_link(raw_value: str) -> str:
    normalized = raw_value.strip()
    if normalized.lower() in {"", "-", "skip", "none", "no"}:
        return ""
    return normalized


def _request_code(request_id: int) -> str:
    return f"TOPUP-{request_id}"


def _format_destination_text(
    destination: dict[str, object],
    *,
    is_default: bool,
    assigned_count: int,
) -> str:
    lines = [
        "💳 Payment method:",
        f"Title: <code>{escape_html(str(destination['title']))}</code>",
        f"Currency: <code>{escape_html(str(destination['currency']))}</code>",
        f"Default: <code>{'yes' if is_default else 'no'}</code>",
        f"Assigned users: <code>{assigned_count}</code>",
    ]
    details = str(destination.get("details") or "").strip()
    if details:
        lines.extend(["", "Details:", f"<pre>{escape_html(details)}</pre>"])
    payment_link = str(destination.get("payment_link") or "").strip()
    if payment_link:
        lines.append(f"Link: {escape_html(payment_link)}")
    return "\n".join(lines)


def _format_topup_user_text(
    request: dict[str, object],
    *,
    submitted: bool,
) -> str:
    lines = [
        f"💳 Top-up request <code>{escape_html(_request_code(int(request['id'])))}</code>",
        f"Amount: <code>{float(request['amount']):.2f} {escape_html(str(request['currency']))}</code>",
        f"Method: <code>{escape_html(str(request['destination_title']))}</code>",
        "",
        "Transfer to these details:",
        f"<pre>{escape_html(str(request.get('destination_details') or ''))}</pre>",
    ]
    destination_link = str(request.get("destination_link") or "").strip()
    if destination_link:
        lines.append(f"Link: {escape_html(destination_link)}")
    lines.extend(
        [
            "",
            "Status: "
            f"<code>{'waiting for admin confirmation' if submitted else 'waiting for your confirmation'}</code>",
        ]
    )
    if submitted:
        lines.append("Admin has been notified. The balance will be credited after manual confirmation.")
    else:
        lines.append("After the transfer, tap “I paid”.")
    return "\n".join(lines)


def _format_topup_admin_text(request: dict[str, object]) -> str:
    balance_value = float(request.get("balance") or 0.0)
    balance_currency = str(request.get("balance_currency") or "").strip().upper() or str(request["currency"])
    lines = [
        f"💳 Top-up request <code>{escape_html(_request_code(int(request['id'])))}</code>",
        f"User: <code>{escape_html(str(request['full_name']))}</code>",
        f"Telegram ID: <code>{int(request['telegram_id'])}</code>",
        f"Amount: <code>{float(request['amount']):.2f} {escape_html(str(request['currency']))}</code>",
        f"Current balance: <code>{balance_value:.2f} {escape_html(balance_currency)}</code>",
        f"Method: <code>{escape_html(str(request['destination_title']))}</code>",
        "",
        "Details shown to user:",
        f"<pre>{escape_html(str(request.get('destination_details') or ''))}</pre>",
    ]
    destination_link = str(request.get("destination_link") or "").strip()
    if destination_link:
        lines.append(f"Link: {escape_html(destination_link)}")
    return "\n".join(lines)


async def _send_topup_destination_menu(
    target: Message | CallbackQuery,
    db: Database,
    *,
    notice: Optional[str] = None,
) -> None:
    destinations = await db.list_payment_destinations()
    if not destinations:
        text = "Top-up is not configured yet. Ask an admin to add payment methods."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await target.answer(text, reply_markup=public_reply_keyboard())
        return
    default_destination_id = await db.get_default_payment_destination_id()
    lines = ["💳 Top up balance:"]
    if notice:
        lines = [escape_html(notice), "", *lines]
    lines.append("Choose a payment method.")
    await respond_with_markup(
        target,
        "\n".join(lines),
        reply_markup=topup_destination_keyboard(destinations, default_destination_id),
    )


async def _send_payment_destinations_menu(
    target: Message | CallbackQuery,
    db: Database,
    *,
    notice: Optional[str] = None,
) -> None:
    destinations = await db.list_payment_destinations()
    default_destination_id = await db.get_default_payment_destination_id()
    pending_requests = await db.list_pending_topup_requests()
    lines = ["💳 Payment methods:"]
    if notice:
        lines = [escape_html(notice), "", *lines]
    if destinations:
        lines.append("Choose a method to edit or add a new one.")
        lines.append("")
        for index, item in enumerate(destinations, 1):
            marker = "⭐ " if int(item["id"]) == (default_destination_id or 0) else ""
            lines.append(
                f"{index}. <code>{escape_html(marker + str(item['title']))}</code> "
                f"({escape_html(str(item['currency']))})"
            )
    else:
        lines.append("No payment methods yet.")
    await respond_with_markup(
        target,
        "\n".join(lines),
        reply_markup=payment_destinations_keyboard(
            destinations,
            default_destination_id,
            len(pending_requests),
        ),
    )


async def _send_payment_destination_detail(
    target: Message | CallbackQuery,
    db: Database,
    destination_id: int,
    *,
    notice: Optional[str] = None,
) -> None:
    destination = await db.get_payment_destination(destination_id)
    if not destination:
        await respond_with_markup(target, "Payment method not found.")
        return
    default_destination_id = await db.get_default_payment_destination_id()
    assignees = await db.list_payment_destination_assignees(destination_id)
    text = _format_destination_text(
        destination,
        is_default=default_destination_id == destination_id,
        assigned_count=len(assignees),
    )
    if notice:
        text = f"{escape_html(notice)}\n\n{text}"
    await respond_with_markup(
        target,
        text,
        reply_markup=payment_destination_detail_keyboard(
            destination_id,
            is_default=default_destination_id == destination_id,
        ),
    )


async def _send_member_payment_destination_menu(
    target: Message | CallbackQuery,
    db: Database,
    friend_id: int,
) -> None:
    friend = await db.get_friend(friend_id)
    if not friend:
        await respond_with_markup(target, "User not found.")
        return
    destinations = await db.list_payment_destinations()
    if not destinations:
        await respond_with_markup(target, "No payment methods configured yet.")
        return
    assigned_destination_id = await db.get_user_payment_destination_id(int(friend["telegram_id"]))
    default_destination_id = await db.get_default_payment_destination_id()
    current_destination = await db.get_effective_payment_destination_for_user(int(friend["telegram_id"]))
    current_label = "Not configured"
    if current_destination:
        current_label = f"{current_destination['title']} ({current_destination['currency']})"
        if assigned_destination_id is None:
            current_label = f"Default: {current_label}"
    await respond_with_markup(
        target,
        "\n".join(
            [
                "💳 User payment method:",
                f"User: <code>{escape_html(str(friend['full_name']))}</code>",
                f"Current: <code>{escape_html(current_label)}</code>",
                "",
                "Choose a payment method for this user.",
            ]
        ),
        reply_markup=member_payment_destination_keyboard(
            friend_id,
            destinations,
            assigned_destination_id,
            default_destination_id,
        ),
    )


async def _send_pending_topups(
    target: Message | CallbackQuery,
    db: Database,
    *,
    notice: Optional[str] = None,
) -> None:
    requests = await db.list_pending_topup_requests()
    lines = ["📥 Pending top-ups:"]
    if notice:
        lines = [escape_html(notice), "", *lines]
    if requests:
        for index, item in enumerate(requests, 1):
            lines.append(
                f"{index}. <code>{escape_html(str(item['full_name']))}</code> "
                f"— <code>{float(item['amount']):.2f} {escape_html(str(item['currency']))}</code>"
            )
    else:
        lines.append("No pending top-up requests.")
    await respond_with_markup(
        target,
        "\n".join(lines),
        reply_markup=topup_pending_requests_keyboard(requests),
    )


async def _send_topup_review(
    target: Message | CallbackQuery,
    db: Database,
    request_id: int,
) -> None:
    request = await db.get_topup_request(request_id)
    if not request:
        await respond_with_markup(target, "Top-up request not found.")
        return
    if str(request.get("status") or "") != "pending":
        await _send_pending_topups(target, db, notice="This request is no longer pending.")
        return
    await respond_with_markup(
        target,
        _format_topup_admin_text(request),
        reply_markup=topup_request_review_keyboard(request_id),
    )


async def _start_public_topup_flow(
    target: Message | CallbackQuery,
    state: FSMContext,
    db: Database,
    telegram_id: int,
) -> None:
    friend = await db.get_friend_by_telegram(telegram_id)
    if not friend:
        text = "Your profile is not set up yet. Ask an admin to add you to the users list."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await target.answer(text, reply_markup=public_reply_keyboard())
        return
    await state.clear()
    await state.update_data(topup_friend_id=int(friend["id"]))
    await _send_topup_destination_menu(target, db)


@admin_router.message(F.text == "💳 Payment methods")
async def handle_payment_methods_menu(message: Message, db: Database) -> None:
    await _send_payment_destinations_menu(message, db)


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "menu"))
async def handle_payment_methods_menu_callback(
    callback: CallbackQuery,
    db: Database,
) -> None:
    await _send_payment_destinations_menu(callback, db)


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "open"))
async def handle_payment_destination_open(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
) -> None:
    await _send_payment_destination_detail(callback, db, callback_data.destination_id)


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "create"))
async def handle_payment_destination_create_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await state.clear()
    await state.set_state(PaymentDestinationForm.title)
    if callback.message:
        await callback.message.answer(
            "💳 New payment method:\n"
            "Send the title.\n"
            "Example: <code>Main RUB card</code>.",
            reply_markup=dialog_keyboard(),
        )
    await callback.answer()


@admin_router.message(PaymentDestinationForm.title)
async def handle_payment_destination_title(
    message: Message,
    state: FSMContext,
) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("Title cannot be empty.")
        return
    await state.update_data(payment_destination_title=title)
    await state.set_state(PaymentDestinationForm.currency)
    await message.answer(
        "Send the 3-letter currency code. Example: <code>RUB</code>.",
        reply_markup=dialog_keyboard(),
    )


@admin_router.message(PaymentDestinationForm.currency)
async def handle_payment_destination_currency(
    message: Message,
    state: FSMContext,
) -> None:
    currency = (message.text or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        await message.answer("Currency must contain exactly 3 letters. Example: <code>USD</code>.")
        return
    await state.update_data(payment_destination_currency=currency)
    await state.set_state(PaymentDestinationForm.details)
    await message.answer(
        "Send the payment details text.\n"
        "You can use multiple lines.",
        reply_markup=dialog_keyboard(),
    )


@admin_router.message(PaymentDestinationForm.details)
async def handle_payment_destination_details(
    message: Message,
    state: FSMContext,
) -> None:
    details = (message.text or "").strip()
    if not details:
        await message.answer("Details cannot be empty.")
        return
    await state.update_data(payment_destination_details=details)
    await state.set_state(PaymentDestinationForm.payment_link)
    await message.answer(
        "Send the payment link, or <code>-</code> to skip.",
        reply_markup=dialog_keyboard(),
    )


@admin_router.message(PaymentDestinationForm.payment_link)
async def handle_payment_destination_link(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    data = await state.get_data()
    destination_id = await db.create_payment_destination(
        title=str(data["payment_destination_title"]),
        currency=str(data["payment_destination_currency"]),
        details=str(data["payment_destination_details"]),
        payment_link=_normalize_link(message.text or ""),
    )
    if await db.get_default_payment_destination_id() is None:
        await db.set_default_payment_destination_id(destination_id)
    await state.clear()
    await message.answer("Payment method saved.", reply_markup=admin_reply_keyboard())
    await _send_payment_destination_detail(message, db, destination_id)


async def _start_destination_edit(
    callback: CallbackQuery,
    state: FSMContext,
    destination_id: int,
    *,
    field: str,
    prompt: str,
) -> None:
    await state.update_data(edit_destination_id=destination_id)
    if field == "title":
        await state.set_state(PaymentDestinationEditForm.title)
    elif field == "currency":
        await state.set_state(PaymentDestinationEditForm.currency)
    elif field == "details":
        await state.set_state(PaymentDestinationEditForm.details)
    else:
        await state.set_state(PaymentDestinationEditForm.payment_link)
    if callback.message:
        await callback.message.answer(prompt, reply_markup=dialog_keyboard())
    await callback.answer()


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "edit_title"))
async def handle_payment_destination_edit_title(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
    state: FSMContext,
) -> None:
    if not await db.get_payment_destination(callback_data.destination_id):
        await callback.answer("Payment method not found.", show_alert=True)
        return
    await _start_destination_edit(
        callback,
        state,
        callback_data.destination_id,
        field="title",
        prompt="Send the new title.",
    )


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "edit_currency"))
async def handle_payment_destination_edit_currency(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
    state: FSMContext,
) -> None:
    if not await db.get_payment_destination(callback_data.destination_id):
        await callback.answer("Payment method not found.", show_alert=True)
        return
    await _start_destination_edit(
        callback,
        state,
        callback_data.destination_id,
        field="currency",
        prompt="Send the new 3-letter currency code.",
    )


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "edit_details"))
async def handle_payment_destination_edit_details(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
    state: FSMContext,
) -> None:
    if not await db.get_payment_destination(callback_data.destination_id):
        await callback.answer("Payment method not found.", show_alert=True)
        return
    await _start_destination_edit(
        callback,
        state,
        callback_data.destination_id,
        field="details",
        prompt="Send the new details text.",
    )


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "edit_link"))
async def handle_payment_destination_edit_link(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
    state: FSMContext,
) -> None:
    if not await db.get_payment_destination(callback_data.destination_id):
        await callback.answer("Payment method not found.", show_alert=True)
        return
    await _start_destination_edit(
        callback,
        state,
        callback_data.destination_id,
        field="payment_link",
        prompt="Send the new link, or <code>-</code> to clear it.",
    )


@admin_router.message(PaymentDestinationEditForm.title)
async def handle_payment_destination_edit_title_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("Title cannot be empty.")
        return
    data = await state.get_data()
    destination_id = int(data["edit_destination_id"])
    await db.update_payment_destination_fields(destination_id, title=title)
    await state.clear()
    await message.answer("Payment method updated.", reply_markup=admin_reply_keyboard())
    await _send_payment_destination_detail(message, db, destination_id)


@admin_router.message(PaymentDestinationEditForm.currency)
async def handle_payment_destination_edit_currency_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    currency = (message.text or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        await message.answer("Currency must contain exactly 3 letters.")
        return
    data = await state.get_data()
    destination_id = int(data["edit_destination_id"])
    await db.update_payment_destination_fields(destination_id, currency=currency)
    await state.clear()
    await message.answer("Payment method updated.", reply_markup=admin_reply_keyboard())
    await _send_payment_destination_detail(message, db, destination_id)


@admin_router.message(PaymentDestinationEditForm.details)
async def handle_payment_destination_edit_details_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    details = (message.text or "").strip()
    if not details:
        await message.answer("Details cannot be empty.")
        return
    data = await state.get_data()
    destination_id = int(data["edit_destination_id"])
    await db.update_payment_destination_fields(destination_id, details=details)
    await state.clear()
    await message.answer("Payment method updated.", reply_markup=admin_reply_keyboard())
    await _send_payment_destination_detail(message, db, destination_id)


@admin_router.message(PaymentDestinationEditForm.payment_link)
async def handle_payment_destination_edit_link_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    data = await state.get_data()
    destination_id = int(data["edit_destination_id"])
    await db.update_payment_destination_fields(
        destination_id,
        payment_link=_normalize_link(message.text or ""),
    )
    await state.clear()
    await message.answer("Payment method updated.", reply_markup=admin_reply_keyboard())
    await _send_payment_destination_detail(message, db, destination_id)


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "set_default"))
async def handle_payment_destination_set_default(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
) -> None:
    destination = await db.get_payment_destination(callback_data.destination_id)
    if not destination:
        await callback.answer("Payment method not found.", show_alert=True)
        return
    await db.set_default_payment_destination_id(callback_data.destination_id)
    await callback.answer("Default payment method updated.")
    await _send_payment_destination_detail(
        callback,
        db,
        callback_data.destination_id,
        notice="Default payment method updated.",
    )


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "assignees"))
async def handle_payment_destination_assignees(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
) -> None:
    assignees = await db.list_payment_destination_assignees(callback_data.destination_id)
    if not assignees:
        await callback.answer("No users with an explicit assignment.", show_alert=True)
        return
    preview = "\n".join(str(item["full_name"]) for item in assignees[:10])
    if len(assignees) > 10:
        preview += "\n..."
    await callback.answer(preview, show_alert=True)


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "delete"))
async def handle_payment_destination_delete_prompt(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
) -> None:
    destination = await db.get_payment_destination(callback_data.destination_id)
    if not destination:
        await callback.answer("Payment method not found.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(
            "Delete this payment method?\n"
            f"<code>{escape_html(str(destination['title']))}</code>",
            reply_markup=payment_destination_delete_keyboard(callback_data.destination_id),
        )
    await callback.answer()


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "confirm_delete"))
async def handle_payment_destination_delete(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
) -> None:
    if not await db.get_payment_destination(callback_data.destination_id):
        await callback.answer("Payment method not found.", show_alert=True)
        return
    await db.delete_payment_destination(callback_data.destination_id)
    await callback.answer("Payment method deleted.")
    await _send_payment_destinations_menu(callback, db, notice="Payment method deleted.")


@admin_router.callback_query(MemberAction.filter(F.action == "payment_destination"))
async def handle_member_payment_destination_menu(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    await _send_member_payment_destination_menu(callback, db, callback_data.friend_id)


@admin_router.callback_query(F.data.startswith("member_payment_destination_default:"))
async def handle_member_payment_destination_default(
    callback: CallbackQuery,
    db: Database,
) -> None:
    try:
        friend_id = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Invalid user.", show_alert=True)
        return
    friend = await db.get_friend(friend_id)
    if not friend:
        await callback.answer("User not found.", show_alert=True)
        return
    await db.clear_user_payment_destination_id(int(friend["telegram_id"]))
    await callback.answer("User now uses the default payment method.")
    await send_member_detail(callback, db, friend_id)


@admin_router.callback_query(F.data.startswith("member_payment_destination:"))
async def handle_member_payment_destination_assign(
    callback: CallbackQuery,
    db: Database,
) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Invalid action.", show_alert=True)
        return
    try:
        friend_id = int(parts[1])
        destination_id = int(parts[2])
    except ValueError:
        await callback.answer("Invalid action.", show_alert=True)
        return
    friend = await db.get_friend(friend_id)
    destination = await db.get_payment_destination(destination_id)
    if not friend:
        await callback.answer("User not found.", show_alert=True)
        return
    if not destination:
        await callback.answer("Payment method not found.", show_alert=True)
        return
    await db.set_user_payment_destination_id(int(friend["telegram_id"]), destination_id)
    await callback.answer("Payment method assigned.")
    await send_member_detail(callback, db, friend_id)


@public_router.message(F.text == "➕ Top up")
async def handle_public_topup_start(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    if not message.from_user:
        await message.answer("Unable to identify your account.")
        return
    await _start_public_topup_flow(message, state, db, message.from_user.id)


@public_router.callback_query(TopUpAction.filter(F.action == "start"))
async def handle_public_topup_start_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    await _start_public_topup_flow(callback, state, db, callback.from_user.id)


@public_router.callback_query(F.data.startswith("topup_destination:"))
async def handle_public_topup_destination_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    try:
        destination_id = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Invalid payment method.", show_alert=True)
        return
    destination = await db.get_payment_destination(destination_id)
    if not destination:
        await callback.answer("Payment method not found.", show_alert=True)
        return
    friend = await db.get_friend_by_telegram(callback.from_user.id)
    if not friend:
        await callback.answer("Your profile is not set up yet.", show_alert=True)
        return
    current_balance = max(float(friend.get("balance") or 0.0), 0.0)
    current_currency = str(friend.get("balance_currency") or "").strip().upper()
    destination_currency = str(destination.get("currency") or "").strip().upper()
    if current_balance > 0 and current_currency and current_currency != destination_currency:
        await callback.answer(
            "This payment method uses another currency than the current balance.",
            show_alert=True,
        )
        return
    await state.set_state(TopUpForm.amount)
    await state.update_data(topup_destination_id=destination_id, topup_friend_id=int(friend["id"]))
    if callback.message:
        await callback.message.answer(
            "💳 Top up balance:\n"
            f"Method: <code>{escape_html(str(destination['title']))}</code>\n"
            f"Currency: <code>{escape_html(destination_currency)}</code>\n\n"
            "Send the amount you want to top up.",
            reply_markup=dialog_keyboard(),
        )
    await callback.answer()


@public_router.message(TopUpForm.amount)
async def handle_public_topup_amount(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    if not message.from_user:
        await state.clear()
        await message.answer("Unable to identify your account.", reply_markup=public_reply_keyboard())
        return
    try:
        amount = float((message.text or "").strip().replace(",", "."))
    except ValueError:
        await message.answer("Amount must be a positive number. Example: <code>1000</code>.")
        return
    if amount <= 0:
        await message.answer("Amount must be greater than zero.")
        return
    data = await state.get_data()
    destination_id = int(data.get("topup_destination_id") or 0)
    destination = await db.get_payment_destination(destination_id)
    friend = await db.get_friend_by_telegram(message.from_user.id)
    if not destination or not friend:
        await state.clear()
        await message.answer(
            "Unable to create the top-up request right now. Please try again.",
            reply_markup=public_reply_keyboard(),
        )
        return
    request_id = await db.create_topup_request(
        friend_id=int(friend["id"]),
        destination_id=destination_id,
        amount=amount,
        currency=str(destination["currency"]),
        destination_title=str(destination["title"]),
        destination_details=str(destination.get("details") or ""),
        destination_link=str(destination.get("payment_link") or ""),
    )
    request = await db.get_topup_request(request_id)
    await state.clear()
    if not request:
        await message.answer(
            "Unable to load the top-up request right now.",
            reply_markup=public_reply_keyboard(),
        )
        return
    await message.answer(
        _format_topup_user_text(request, submitted=False),
        reply_markup=topup_request_submit_keyboard(request_id),
    )


@public_router.callback_query(TopUpAction.filter(F.action == "submit"))
async def handle_public_topup_submit(
    callback: CallbackQuery,
    callback_data: TopUpAction,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify your account.", show_alert=True)
        return
    request = await db.get_topup_request(callback_data.request_id)
    if not request:
        await callback.answer("Top-up request not found.", show_alert=True)
        return
    if int(request["telegram_id"]) != callback.from_user.id:
        await callback.answer("This request belongs to another user.", show_alert=True)
        return
    status = str(request.get("status") or "")
    if status == "pending":
        await callback.answer("This request is already waiting for admin confirmation.", show_alert=True)
        return
    if status == "approved":
        await callback.answer("This request has already been approved.", show_alert=True)
        return
    if status == "rejected":
        await callback.answer("This request was rejected.", show_alert=True)
        return
    request = await db.mark_topup_request_pending(callback_data.request_id)
    if not request or str(request.get("status") or "") != "pending":
        await callback.answer("Unable to submit the request.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(_format_topup_user_text(request, submitted=True))
    await callback.answer("Admin has been notified.")
    admin_ids = await db.list_admin_ids()
    for admin_id in admin_ids:
        with contextlib.suppress(Exception):
            await callback.bot.send_message(
                admin_id,
                _format_topup_admin_text(request),
                reply_markup=topup_request_review_keyboard(callback_data.request_id),
            )


@admin_router.callback_query(TopUpAction.filter(F.action == "admin_list"))
async def handle_admin_topup_list(callback: CallbackQuery, db: Database) -> None:
    await _send_pending_topups(callback, db)


@admin_router.callback_query(TopUpAction.filter(F.action == "admin_open"))
async def handle_admin_topup_open(
    callback: CallbackQuery,
    callback_data: TopUpAction,
    db: Database,
) -> None:
    await _send_topup_review(callback, db, callback_data.request_id)


@admin_router.callback_query(TopUpAction.filter(F.action == "approve"))
async def handle_admin_topup_approve(
    callback: CallbackQuery,
    callback_data: TopUpAction,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify admin.", show_alert=True)
        return
    request, error = await db.approve_topup_request(callback_data.request_id, callback.from_user.id)
    if error == "not_pending":
        await callback.answer("This request is no longer pending.", show_alert=True)
        await _send_pending_topups(callback, db)
        return
    if error == "currency_mismatch":
        await callback.answer(
            "User balance currency differs from the top-up currency. Fix it first.",
            show_alert=True,
        )
        return
    if not request:
        await callback.answer("Top-up request not found.", show_alert=True)
        return
    await callback.answer("Top-up approved.")
    await _send_pending_topups(callback, db, notice="Top-up approved.")
    with contextlib.suppress(Exception):
        await callback.bot.send_message(
            int(request["telegram_id"]),
            "✅ Your top-up request was approved.\n"
            f"Amount credited: <code>{float(request['amount']):.2f} {escape_html(str(request['currency']))}</code>",
            reply_markup=public_reply_keyboard(),
        )


@admin_router.callback_query(TopUpAction.filter(F.action == "reject"))
async def handle_admin_topup_reject(
    callback: CallbackQuery,
    callback_data: TopUpAction,
    db: Database,
) -> None:
    if not callback.from_user:
        await callback.answer("Unable to identify admin.", show_alert=True)
        return
    request, error = await db.reject_topup_request(callback_data.request_id, callback.from_user.id)
    if error == "not_pending":
        await callback.answer("This request is no longer pending.", show_alert=True)
        await _send_pending_topups(callback, db)
        return
    if not request:
        await callback.answer("Top-up request not found.", show_alert=True)
        return
    await callback.answer("Top-up rejected.")
    await _send_pending_topups(callback, db, notice="Top-up rejected.")
    with contextlib.suppress(Exception):
        await callback.bot.send_message(
            int(request["telegram_id"]),
            "❌ Your top-up request was rejected.\n"
            "Please contact the admin if you need clarification.",
            reply_markup=public_reply_keyboard(),
        )
