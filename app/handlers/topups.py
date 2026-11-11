"""Top-ups (WP14) plus the admin's 💳 Payment menu entry and the member payment method picker.

Member flow: “👤 Account” → “➕ Top up” edits the account into the method
picker (skipped when only one method fits the balance currency), the amount
prompt asks how much, and the screen the prompt came from becomes the request
with the transfer details and [✅ I paid] / [✖️ Cancel]. ✅ I paid turns it into
'⏳ sent for approval' (no buttons) and alerts every admin.

Admin flow: the review push (or a review opened from 📥 Pending top-ups)
offers [✅ Approve][❌ Reject]; Reject asks first. A decision on the push turns
it into its final state; from the list it redraws the list with a banner.
Decisions run under a double-tap guard and re-check the status, because every
admin gets the same push. The member is told through the retrying sender.

Screens live in app/ui/topup_screens.py; the 💳 Payment root and its methods
in app/handlers/payment_methods.py.
"""
from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping, Optional

from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.services import send_message_with_retry
from app.storage.db import Database
from app.ui.guards import guarded
from app.ui.helpers import (
    DEAD_BUTTON,
    METHOD_GONE,
    REQUEST_HANDLED,
    answer_callback_once,
    delete_quietly,
    is_not_modified_error,
    is_uneditable_error,
    resolve_report_timezone,
    respond_with_markup,
    stale_redirect,
)
from app.ui.keyboards import (
    CLOSE_CALLBACK,
    TOPUP_ADMIN_LIST_CALLBACK,
    admin_reply_keyboard,
    guest_reply_keyboard,
    topup_approved_push_keyboard,
    topup_reject_confirm_keyboard,
)
from app.ui.member_account import ACCOUNT_REOPEN_ALERT, NOT_SET_UP_ALERT, NOT_SET_UP_TEXT
from app.ui.member_screens import (
    balance_of,
    default_balance_currency,
    first_name_of,
    member_payment_method_screen,
    payment_method_banner,
    send_member_detail,
    show_user_gone,
)
from app.ui.prompts import ask, finish_prompt, note_typo, origin_of, reset_flow_state, wind_down
from app.ui.shell import ADMIN_TOPUP_TEXT, NO_IDENTITY_TEXT
from app.ui.states import MemberAction, Responder, TopUpAction, TopUpForm
from app.ui.style import toast_from_html
from app.ui.text import parse_amount
from app.ui.topup_screens import (
    AMOUNT_HINT,
    AMOUNT_TEXT_HINT,
    APPROVED_TOAST,
    CREATE_FAILED_TEXT,
    METHOD_GONE_TOAST,
    MISMATCH_TOAST,
    NOT_YOURS_ALERT,
    REJECTED_TOAST,
    SENT_TOAST,
    SUBMIT_FAILED_ALERT,
    TOPUP_CANCELLED_TOAST,
    TopUpPicker,
    admin_already_toast,
    amount_prompt_text,
    approved_push_text,
    decided_banner,
    member_already_toast,
    member_request_final_text,
    method_mismatch_toast,
    pending_list_screen,
    reject_confirm_text,
    rejected_push_text,
    review_final_text,
    review_screen,
    rounded_banner,
    submitted_text,
    topup_picker,
    topup_picker_screen,
    topup_request_screen,
)

from . import admin_router, public_router
from .payment_methods import show_payment_root as _send_payment_destinations_menu

logger = logging.getLogger(__name__)

DEAD_MEMBER_BUTTON_TOAST = DEAD_BUTTON
# FSM data of the amount prompt.
TOPUP_DESTINATION_ID = "topup_destination_id"
TOPUP_FRIEND_ID = "topup_friend_id"
TOPUP_ORIGIN_SCREEN = "topup"
CENT = Decimal("0.01")


# ------------------------------------------------------------------ helpers


def _as_int(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def parse_topup_amount(text: str | None) -> Optional[tuple[float, bool]]:
    """``(amount, rounded)``: the typed amount in cents, and whether it had more than 2 decimals.

    Accepts '1 000', '1,000.50', '149,99'; a sign, scientific notation or a
    value that rounds to zero is refused (None).
    """
    parsed = parse_amount(text)
    if parsed is None:
        return None
    try:
        exact = Decimal(str(parsed[0]))
        cents = exact.quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    if cents <= 0:
        return None
    return float(cents), cents != exact


def _callback_targets(callback: CallbackQuery) -> set[str]:
    """The callback_data of every button on the tapped message (as it was when tapped)."""
    markup = getattr(callback.message, "reply_markup", None)
    if not isinstance(markup, InlineKeyboardMarkup):
        return set()
    return {button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data}


def _opened_from_list(callback: CallbackQuery, request_id: int) -> bool:
    """A review (or its reject question) opened from 📥 Pending top-ups, not the push.

    The list's review has [◀️ To pending]; its question's Keep reopens it from
    the list. Old reviews (before WP14) had a Back to the list too, and their
    decisions used to redraw the list, so they keep doing that.
    """
    targets = _callback_targets(callback)
    return TOPUP_ADMIN_LIST_CALLBACK in targets or TopUpAction(action="admin_open", request_id=request_id).pack() in targets


async def _default_currency(db: Database) -> str:
    return await default_balance_currency(db)


async def _show_on_origin(
    message: Message,
    data: Mapping[str, Any],
    text: str,
    markup: Optional[InlineKeyboardMarkup],
) -> None:
    """Redraw the screen the amount prompt came from; send it anew when there is none."""
    origin = origin_of(data)
    bot = message.bot
    if origin is not None and bot is not None:
        chat_id, message_id = origin
        try:
            await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
            return
        except TelegramBadRequest as error:
            if is_not_modified_error(error):
                return
            if not is_uneditable_error(error):
                raise
            logger.debug("Top-up origin %s is gone, sending the request anew: %s", message_id, error)
    await message.answer(text, reply_markup=markup)


async def _strip_buttons(callback: CallbackQuery) -> None:
    message = callback.message
    if not isinstance(message, Message):
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as error:
        logger.debug("Could not strip the buttons of message %s: %s", message.message_id, error)


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



async def _ask_amount(
    responder: Responder,
    state: FSMContext,
    destination: Mapping[str, Any],
    picker: TopUpPicker,
) -> None:
    """The amount prompt; a callback's tapped message (picker or account) is the origin."""
    currency = str(destination.get("currency") or "").strip().upper()
    await ask(
        responder,
        state,
        TopUpForm.amount,
        amount_prompt_text(currency),
        origin_screen=TOPUP_ORIGIN_SCREEN,
        data={TOPUP_DESTINATION_ID: int(destination["id"]), TOPUP_FRIEND_ID: int(picker.friend["id"])},
    )


async def _start_topup(
    target: Responder,
    state: FSMContext,
    db: Database,
    telegram_id: int,
    *,
    spawned: bool = False,
) -> None:
    """The picker: edits the tapped 👤 Account, or a new message (typed alias, ``spawned`` from a push).

    With exactly one method that fits, the picker is skipped and the amount
    prompt opens straight away (the tapped account is then its origin).
    """
    picker = await topup_picker(db, telegram_id)
    if picker is None:
        if isinstance(target, CallbackQuery):
            await answer_callback_once(target, NOT_SET_UP_ALERT, alert=True)
        else:
            await target.answer(NOT_SET_UP_TEXT, reply_markup=guest_reply_keyboard())
        return
    message = target.message if isinstance(target, CallbackQuery) else target
    if not isinstance(message, Message):
        await answer_callback_once(target, ACCOUNT_REOPEN_ALERT, alert=True)  # type: ignore[arg-type]
        return
    if len(picker.methods) == 1:
        # From a push the prompt gets no origin: the request comes as a new message.
        await _ask_amount(message if spawned else target, state, picker.methods[0], picker)
        if isinstance(target, CallbackQuery):
            await answer_callback_once(target)
        return
    old_data = await reset_flow_state(state)
    if message.bot is not None:
        await wind_down(message.bot, message.chat.id, old_data)
    # The 👤 Account copy opened from a push ends in [✖️ Close]; its picker keeps Close.
    close = spawned or (isinstance(target, CallbackQuery) and CLOSE_CALLBACK in _callback_targets(target))
    text, markup = topup_picker_screen(picker, close=close)
    if spawned and isinstance(target, CallbackQuery):
        await message.answer(text, reply_markup=markup)
        await answer_callback_once(target)
        return
    await respond_with_markup(target, text, reply_markup=markup)


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
    """The legacy reply label: the picker as a new message.

    An admin always keeps the admin menu: the label answers with a pointer
    and the admin menu, never with the guest menu.
    """
    if not message.from_user:
        await message.answer(NO_IDENTITY_TEXT, reply_markup=guest_reply_keyboard())
        return
    if await db.is_admin(message.from_user.id):
        await message.answer(ADMIN_TOPUP_TEXT, reply_markup=admin_reply_keyboard())
        return
    await _start_topup(message, state, db, message.from_user.id)


@public_router.callback_query(TopUpAction.filter(F.action == "start"))
async def handle_public_topup_start_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    """“👤 Account” → “➕ Top up”: the picker edits the account."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    await _start_topup(callback, state, db, callback.from_user.id)


@public_router.callback_query(TopUpAction.filter(F.action == "start_new"))
async def handle_public_topup_start_new(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    """[➕ Top up] under a push: the picker as a NEW message ending in [✖️ Close]."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    await _start_topup(callback, state, db, callback.from_user.id, spawned=True)


@public_router.callback_query(F.data.startswith("topup_destination:"))
async def handle_public_topup_destination_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    """A method on the picker: the amount prompt (the picker is its origin)."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    try:
        destination_id = int((callback.data or "").split(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        await answer_callback_once(callback, DEAD_MEMBER_BUTTON_TOAST)
        return
    picker = await topup_picker(db, callback.from_user.id)
    if picker is None:
        await answer_callback_once(callback, NOT_SET_UP_ALERT, alert=True)
        return
    close = CLOSE_CALLBACK in _callback_targets(callback)
    destination = await db.get_payment_destination(destination_id)
    toast: Optional[str] = None
    if not destination:
        toast = METHOD_GONE_TOAST
    elif picker.filtered and str(destination.get("currency") or "").strip().upper() != picker.currency:
        toast = method_mismatch_toast(destination, picker.currency)
    if toast is not None:
        text, markup = topup_picker_screen(picker, close=close)
        await stale_redirect(callback, toast, lambda: respond_with_markup(callback, text, reply_markup=markup))
        return
    await _ask_amount(callback, state, destination, picker)


def _blocks_balance(friend: Mapping[str, Any], destination: Mapping[str, Any], default_currency: str) -> bool:
    balance, currency = balance_of(dict(friend), default_currency)
    return balance > 0 and str(destination.get("currency") or "").strip().upper() != currency


@public_router.message(TopUpForm.amount)
async def handle_public_topup_amount(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    """The amount: create the request and turn the origin screen into it."""
    if not message.from_user:
        await reset_flow_state(state)
        await message.answer(NO_IDENTITY_TEXT, reply_markup=guest_reply_keyboard())
        return
    if message.text is None:
        await note_typo(message, state, AMOUNT_TEXT_HINT)
        return
    parsed = parse_topup_amount(message.text)
    if parsed is None:
        await note_typo(message, state, AMOUNT_HINT)
        return
    amount, rounded = parsed
    data = await state.get_data()
    destination = await db.get_payment_destination(_as_int(data.get(TOPUP_DESTINATION_ID)))
    friend = await db.get_friend_by_telegram(message.from_user.id)
    expected_friend = _as_int(data.get(TOPUP_FRIEND_ID))
    if (
        not destination
        or not friend
        or (expected_friend and int(friend["id"]) != expected_friend)
        or _blocks_balance(friend, destination, await _default_currency(db))
    ):
        # The method or the account changed meanwhile: the screen stays as it is.
        await finish_prompt(message, state)
        await message.answer(CREATE_FAILED_TEXT)
        return
    request_id = await db.create_topup_request(
        friend_id=int(friend["id"]),
        destination_id=int(destination["id"]),
        amount=amount,
        currency=str(destination["currency"]),
        destination_title=str(destination["title"]),
        destination_details=str(destination.get("details") or ""),
        destination_link=str(destination.get("payment_link") or ""),
    )
    request = await db.get_topup_request(request_id)
    flow = await finish_prompt(message, state)
    if not request:
        await message.answer(CREATE_FAILED_TEXT)
        return
    currency = str(request.get("currency") or "").strip().upper()
    text, markup = topup_request_screen(request, banner=rounded_banner(amount, currency) if rounded else None)
    await _show_on_origin(message, flow, text, markup)


async def _own_request(callback: CallbackQuery, db: Database, request_id: int) -> Optional[dict[str, Any]]:
    """The tapped request when it is this member's; otherwise answer the tap and return None."""
    request = await db.get_topup_request(request_id)
    if not request:
        await _strip_buttons(callback)
        await answer_callback_once(callback, REQUEST_HANDLED)
        return None
    if callback.from_user is None or int(request.get("telegram_id") or 0) != callback.from_user.id:
        await answer_callback_once(callback, NOT_YOURS_ALERT, alert=True)
        return None
    return request


async def _show_member_final(callback: CallbackQuery, db: Database, request: Mapping[str, Any]) -> None:
    """An old request screen tapped after the request moved on: show where it stands."""
    text = member_request_final_text(request, await resolve_report_timezone(db), await _default_currency(db))
    await respond_with_markup(callback, text, reply_markup=None, toast=member_already_toast(request))


@public_router.callback_query(TopUpAction.filter(F.action == "cancel"))
async def handle_public_topup_cancel(
    callback: CallbackQuery,
    callback_data: TopUpAction,
    db: Database,
) -> None:
    """[✖️ Cancel] on the request screen: delete it; the request stays unsent."""
    request = await _own_request(callback, db, callback_data.request_id)
    if request is None:
        return
    if str(request.get("status") or "") != "new":
        await _show_member_final(callback, db, request)
        return
    message = callback.message
    if message is not None and callback.bot is not None:
        await delete_quietly(callback.bot, message.chat.id, message.message_id)
    await answer_callback_once(callback, TOPUP_CANCELLED_TOAST)


async def _notify_admins(bot: Bot, db: Database, request: Mapping[str, Any]) -> None:
    """The review push to every admin, best effort; failures are logged."""
    text, markup = await review_screen(db, request, tz_name=await resolve_report_timezone(db), from_list=False)
    for admin_id in await db.list_admin_ids():
        sent = await send_message_with_retry(bot, int(admin_id), text, reply_markup=markup, logger=logger)
        if sent is None:
            logger.warning("Top-up %s: could not alert admin %s", request.get("id"), admin_id)


@public_router.callback_query(TopUpAction.filter(F.action == "submit"))
async def handle_public_topup_submit(
    callback: CallbackQuery,
    callback_data: TopUpAction,
    db: Database,
) -> None:
    """✅ I paid: the request goes to the admins and its screen becomes final (no buttons)."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    submitted: Optional[dict[str, Any]] = None
    async with guarded(callback, ("topup_submit", callback_data.request_id)) as acquired:
        if not acquired:
            return
        request = await _own_request(callback, db, callback_data.request_id)
        if request is None:
            return
        if str(request.get("status") or "") != "new":
            await _show_member_final(callback, db, request)
            return
        request = await db.mark_topup_request_pending(callback_data.request_id)
        if not request or str(request.get("status") or "") != "pending":
            await answer_callback_once(callback, SUBMIT_FAILED_ALERT, alert=True)
            return
        text = submitted_text(request, await resolve_report_timezone(db))
        await respond_with_markup(callback, text, reply_markup=None, toast=SENT_TOAST)
        submitted = request
    if submitted is not None and callback.bot is not None:
        await _notify_admins(callback.bot, db, submitted)


# ------------------------------------------------------------- admin review


async def show_pending_topups(
    target: Responder,
    db: Database,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> None:
    """📥 Pending top-ups (from the 💳 Payment root); a decision made here comes back as a banner."""
    text, markup = await pending_list_screen(db, banner=banner)
    await respond_with_markup(target, text, reply_markup=markup, toast=toast)


async def _decided_by(db: Database, request: Mapping[str, Any], viewer_id: Optional[int]) -> Optional[str]:
    """The first name of the other admin who decided the request; None when it was the viewer."""
    status = str(request.get("status") or "")
    key = "approved_by_telegram_id" if status == "approved" else "rejected_by_telegram_id"
    decider = _as_int(request.get(key))
    if not decider or decider == viewer_id:
        return None
    name = await db.get_admin_name(decider)
    if not name:
        friend = await db.get_friend_by_telegram(decider)
        name = str(friend.get("full_name") or "") if friend else ""
    return first_name_of(name) if name.strip() else "another admin"


async def _request_gone(callback: CallbackQuery, db: Database, from_list: bool) -> None:
    """The request no longer exists (its member was deleted)."""
    if from_list:
        await stale_redirect(callback, REQUEST_HANDLED, lambda: show_pending_topups(callback, db))
        return
    await _strip_buttons(callback)
    await answer_callback_once(callback, REQUEST_HANDLED)


async def _show_decided(callback: CallbackQuery, db: Database, request: Mapping[str, Any], from_list: bool) -> None:
    """A stale review: the request was already decided (maybe by another admin)."""
    decided_by = await _decided_by(db, request, callback.from_user.id if callback.from_user else None)
    toast = admin_already_toast(request, decided_by)
    if from_list or str(request.get("status") or "") not in {"approved", "rejected"}:
        await stale_redirect(callback, toast, lambda: show_pending_topups(callback, db))
        return
    text = review_final_text(request, await _default_currency(db), decided_by=decided_by)
    await respond_with_markup(callback, text, reply_markup=None, toast=toast)


async def _show_review(
    callback: CallbackQuery,
    db: Database,
    request_id: int,
    *,
    from_list: bool,
    toast: Optional[str] = None,
) -> None:
    request = await db.get_topup_request(request_id)
    if not request:
        await _request_gone(callback, db, from_list)
        return
    if str(request.get("status") or "") != "pending":
        await _show_decided(callback, db, request, from_list)
        return
    text, markup = await review_screen(db, request, tz_name=await resolve_report_timezone(db), from_list=from_list)
    await respond_with_markup(callback, text, reply_markup=markup, toast=toast)


async def _show_decision(callback: CallbackQuery, db: Database, request: Mapping[str, Any], from_list: bool) -> None:
    """After approve/reject: the push becomes final; from the list, the list gets a banner."""
    toast = APPROVED_TOAST if str(request.get("status") or "") == "approved" else REJECTED_TOAST
    if from_list:
        await show_pending_topups(callback, db, banner=decided_banner(request), toast=toast)
        return
    text = review_final_text(request, await _default_currency(db))
    await respond_with_markup(callback, text, reply_markup=None, toast=toast)


async def _tell_member(bot: Optional[Bot], db: Database, request: Mapping[str, Any]) -> None:
    """The decision push to the member (no reply keyboard: it would replace an admin's menu)."""
    telegram_id = request.get("telegram_id")
    if bot is None or telegram_id is None:
        return
    if str(request.get("status") or "") == "approved":
        balance, currency = balance_of(dict(request), await _default_currency(db))
        text = approved_push_text(request, balance, currency)
        # 👤 Account is a member screen; an admin who topped up gets no button.
        markup = None if await db.is_admin(int(telegram_id)) else topup_approved_push_keyboard()
    else:
        text, markup = rejected_push_text(request), None
    sent = await send_message_with_retry(bot, int(telegram_id), text, reply_markup=markup, logger=logger)
    if sent is None:
        logger.warning("Top-up %s: could not tell user %s about the decision", request.get("id"), telegram_id)


@admin_router.callback_query(TopUpAction.filter(F.action == "admin_list"))
async def handle_admin_topup_list(callback: CallbackQuery, db: Database) -> None:
    await show_pending_topups(callback, db)


@admin_router.callback_query(TopUpAction.filter(F.action == "admin_open"))
async def handle_admin_topup_open(
    callback: CallbackQuery,
    callback_data: TopUpAction,
    db: Database,
) -> None:
    """A request from 📥 Pending top-ups: the review with [◀️ To pending]."""
    await _show_review(callback, db, callback_data.request_id, from_list=True)


@admin_router.callback_query(TopUpAction.filter(F.action == "review"))
async def handle_admin_topup_review(
    callback: CallbackQuery,
    callback_data: TopUpAction,
    db: Database,
) -> None:
    """Keep on the push's reject question: the push's review again."""
    await _show_review(callback, db, callback_data.request_id, from_list=False)


@admin_router.callback_query(TopUpAction.filter(F.action == "approve"))
async def handle_admin_topup_approve(
    callback: CallbackQuery,
    callback_data: TopUpAction,
    db: Database,
) -> None:
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    request_id = callback_data.request_id
    from_list = _opened_from_list(callback, request_id)
    decided: Optional[dict[str, Any]] = None
    async with guarded(callback, ("topup_decide", request_id)) as acquired:
        if not acquired:
            return
        current = await db.get_topup_request(request_id)
        if not current:
            await _request_gone(callback, db, from_list)
            return
        if str(current.get("status") or "") != "pending":
            await _show_decided(callback, db, current, from_list)
            return
        request, error = await db.approve_topup_request(request_id, callback.from_user.id)
        if error == "not_pending":
            if request:
                await _show_decided(callback, db, request, from_list)
            else:
                await _request_gone(callback, db, from_list)
            return
        if error == "currency_mismatch":
            # The balance changed currency since the review was drawn: show why Approve is gone.
            await _show_review(callback, db, request_id, from_list=from_list, toast=MISMATCH_TOAST)
            return
        if not request:
            await _request_gone(callback, db, from_list)
            return
        await _show_decision(callback, db, request, from_list)
        decided = request
    if decided is not None:
        await _tell_member(callback.bot, db, decided)


@admin_router.callback_query(TopUpAction.filter(F.action == "reject"))
async def handle_admin_topup_reject(
    callback: CallbackQuery,
    callback_data: TopUpAction,
    db: Database,
) -> None:
    """❌ Reject (also on old messages) now asks first: [Keep][❌ Reject]."""
    request_id = callback_data.request_id
    from_list = _opened_from_list(callback, request_id)
    request = await db.get_topup_request(request_id)
    if not request:
        await _request_gone(callback, db, from_list)
        return
    if str(request.get("status") or "") != "pending":
        await _show_decided(callback, db, request, from_list)
        return
    await respond_with_markup(
        callback,
        reject_confirm_text(request),
        reply_markup=topup_reject_confirm_keyboard(request_id, from_list=from_list),
    )


@admin_router.callback_query(TopUpAction.filter(F.action == "reject_yes"))
async def handle_admin_topup_reject_yes(
    callback: CallbackQuery,
    callback_data: TopUpAction,
    db: Database,
) -> None:
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    request_id = callback_data.request_id
    from_list = _opened_from_list(callback, request_id)
    decided: Optional[dict[str, Any]] = None
    async with guarded(callback, ("topup_decide", request_id)) as acquired:
        if not acquired:
            return
        current = await db.get_topup_request(request_id)
        if not current:
            await _request_gone(callback, db, from_list)
            return
        if str(current.get("status") or "") != "pending":
            await _show_decided(callback, db, current, from_list)
            return
        request, error = await db.reject_topup_request(request_id, callback.from_user.id)
        if error == "not_pending" or not request:
            if request:
                await _show_decided(callback, db, request, from_list)
            else:
                await _request_gone(callback, db, from_list)
            return
        await _show_decision(callback, db, request, from_list)
        decided = request
    if decided is not None:
        await _tell_member(callback.bot, db, decided)
