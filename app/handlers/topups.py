from __future__ import annotations

import math
import contextlib
from datetime import datetime, timezone
from typing import Optional

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.core.constants import DATE_INPUT_FORMAT
from app.core.reminders import tg_time
from app.storage.db import Database
from app.ui.helpers import DEAD_BUTTON, METHOD_GONE, answer_callback_once, respond_with_markup
from app.ui.keyboards import (
    dialog_cancel_inline_keyboard,
    public_reply_keyboard,
    topup_destination_keyboard,
    topup_pending_requests_keyboard,
    topup_request_review_keyboard,
    topup_request_submit_keyboard,
)
from app.ui.member_screens import (
    member_payment_method_screen,
    payment_method_banner,
    send_member_detail,
    show_user_gone,
)
from app.ui.states import MemberAction, TopUpAction, TopUpForm
from app.ui.style import toast_from_html
from app.ui.text import escape_html

from . import admin_router, public_router
from .payment_methods import show_payment_root as _send_payment_destinations_menu

DEAD_MEMBER_BUTTON_TOAST = DEAD_BUTTON


def _normalize_link(raw_value: str) -> str:
    normalized = raw_value.strip()
    if normalized.lower() in {"", "-", "skip", "none", "no"}:
        return ""
    return normalized


def _request_code(request_id: int) -> str:
    return f"TOPUP-{request_id}"


def _parse_db_utc(value: object) -> datetime | None:
    """Parse a SQLite timestamp (``CURRENT_TIMESTAMP`` or ISO) as aware UTC."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _topup_moment(request: dict[str, object]) -> datetime | None:
    return _parse_db_utc(request.get("submitted_at")) or _parse_db_utc(request.get("created_at"))


def _age_fallback(moment: datetime, now: datetime | None = None) -> str:
    """Static ``2h ago`` / ``on 17.09.2026`` text for clients without date entities."""
    current = now if now is not None else datetime.now(timezone.utc)
    seconds = int((current - moment).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return "on " + moment.strftime(DATE_INPUT_FORMAT)


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
        f"User: <b>{escape_html(str(request['full_name']))}</b>",
        f"Telegram ID: <code>{int(request['telegram_id'])}</code>",
        f"Amount: <b>{float(request['amount']):.2f} {escape_html(str(request['currency']))}</b>",
        f"Current balance: <b>{balance_value:.2f} {escape_html(balance_currency)}</b>",
        f"Method: {escape_html(str(request['destination_title']))}",
        "",
        "Details shown to user:",
    ]
    for detail_line in str(request.get("destination_details") or "").splitlines():
        detail_line = detail_line.strip()
        if detail_line:
            lines.append(f"<code>{escape_html(detail_line)}</code>")
    destination_link = str(request.get("destination_link") or "").strip()
    if destination_link:
        lines.append(f"Link: {escape_html(destination_link)}")
    moment = _topup_moment(request)
    if moment is not None:
        lines.append(
            "Submitted: <b>"
            + tg_time(moment, "wdt", moment.strftime(f"{DATE_INPUT_FORMAT} %H:%M UTC"))
            + "</b> ("
            + tg_time(moment, "r", _age_fallback(moment))
            + ")"
        )
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


# ------------------------------------------------------------ member picker


async def _send_member_payment_destination_menu(
    target: Message | CallbackQuery,
    db: Database,
    friend_id: int,
    *,
    toast: Optional[str] = None,
) -> None:
    """The member's payment method picker (Users → card → 🏦 Method)."""
    screen = await member_payment_method_screen(db, friend_id)
    if screen is None:
        await show_user_gone(target, db)
        return
    text, markup = screen
    await respond_with_markup(target, text, reply_markup=markup, toast=toast)


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
            line = (
                f"{index}. <b>{escape_html(str(item['full_name']))}</b> "
                f"— <b>{float(item['amount']):.2f} {escape_html(str(item['currency']))}</b>"
            )
            moment = _topup_moment(item)
            if moment is not None:
                line += " · " + tg_time(moment, "r", _age_fallback(moment))
            lines.append(line)
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


@admin_router.message(F.text.in_({"💳 Payment", "💳 Payment methods"}))
async def handle_payment_methods_menu(message: Message, db: Database) -> None:
    """'💳 Payment' (and the old '💳 Payment methods'): the root, as a new message.

    Every paydest:* button and the "New payment method" wizard live in
    app/handlers/payment_methods.py.
    """
    await _send_payment_destinations_menu(message, db)


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
        friend_id = int((callback.data or "").split(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        await answer_callback_once(callback, DEAD_MEMBER_BUTTON_TOAST)
        return
    friend = await db.get_friend(friend_id)
    if not friend:
        await show_user_gone(callback, db)
        return
    if friend.get("telegram_id") is None:
        await _send_member_payment_destination_menu(callback, db, friend_id)
        return
    await db.clear_user_payment_destination_id(int(friend["telegram_id"]))
    default_id = await db.get_default_payment_destination_id()
    default = await db.get_payment_destination(default_id) if default_id is not None else None
    banner = payment_method_banner(default, default=True)
    await send_member_detail(callback, db, friend_id, banner=banner, toast=toast_from_html(banner))


@admin_router.callback_query(F.data.startswith("member_payment_destination:"))
async def handle_member_payment_destination_assign(
    callback: CallbackQuery,
    db: Database,
) -> None:
    parts = (callback.data or "").split(":")
    try:
        friend_id = int(parts[1])
        destination_id = int(parts[2])
    except (IndexError, ValueError):
        await answer_callback_once(callback, DEAD_MEMBER_BUTTON_TOAST)
        return
    friend = await db.get_friend(friend_id)
    if not friend:
        await show_user_gone(callback, db)
        return
    destination = await db.get_payment_destination(destination_id)
    if friend.get("telegram_id") is None or not destination:
        # Signed out in the meantime, or the method was deleted: show the picker as it is now.
        toast = METHOD_GONE if not destination else None
        await _send_member_payment_destination_menu(callback, db, friend_id, toast=toast)
        return
    await db.set_user_payment_destination_id(int(friend["telegram_id"]), destination_id)
    default_id = await db.get_default_payment_destination_id()
    banner = payment_method_banner(
        destination, default=False, is_default_method=default_id is not None and int(default_id) == destination_id
    )
    await send_member_detail(callback, db, friend_id, banner=banner, toast=toast_from_html(banner))


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
            reply_markup=dialog_cancel_inline_keyboard(),
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
    if not math.isfinite(amount) or amount <= 0:
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
        reply_markup=topup_request_submit_keyboard(
            request_id,
            payment_details=str(request.get("destination_details") or ""),
            payment_link=str(request.get("destination_link") or ""),
        ),
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
