"""Payment subtree of the admin subscription card (WP7).

Payment (sub:paymentinfo), the Payment method picker (sub:paymentmethod and
the old sub_payment_destination:<sub>:default|<id> choices) and the comment
prompt (sub:comment, its quick value sub:comment_clear and the typed answer).
Every result lands on Payment with a banner; nothing is sent as a throwaway
'… updated.' message any more.
"""
from __future__ import annotations

from typing import Optional

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.storage.db import Database
from app.ui.helpers import (
    DEAD_BUTTON,
    METHOD_GONE,
    SUBSCRIPTION_GONE_BANNER,
    answer_callback_once,
    edit_in_place,
    get_edit_subscription_id,
    require_edit_subscription_id,
    send_subscription_list,
    show_subscription_gone,
    start_subscription_edit_flow,
)
from app.ui.keyboards import SUB_PAYMENT_DESTINATION_PREFIX, comment_quick_rows
from app.ui.prompts import finish_prompt, note_typo, origin_of
from app.ui.states import SubscriptionAction, SubscriptionEditForm
from app.ui.style import toast_from_html
from app.ui.subscription_payment_screens import (
    COMMENT_CLEAR_WORDS,
    COMMENT_CLEARED_BANNER,
    COMMENT_MAX_LEN,
    COMMENT_SAVED_BANNER,
    COMMENT_TEXT_HINT,
    COMMENT_TOO_LONG_HINT,
    build_payment_info_screen,
    comment_prompt_text,
    method_banner,
    method_toast,
    payment_choice,
    send_payment_info_screen,
    send_payment_method_screen,
    show_payment_after_prompt,
)

from . import admin_router
from .subscriptions import _drop_flow


def _payment_origin(subscription_id: int) -> str:
    return f"payment:{int(subscription_id)}"


# -------------------------------------------------------------------- Payment


@admin_router.callback_query(SubscriptionAction.filter(F.action == "paymentinfo"))
async def handle_subscription_payment_info_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await _drop_flow(callback, state)
    await send_payment_info_screen(callback, db, callback_data.subscription_id)


# ------------------------------------------------------------- Payment method


@admin_router.callback_query(SubscriptionAction.filter(F.action == "paymentmethod"))
async def handle_subscription_payment_method_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await _drop_flow(callback, state)
    await send_payment_method_screen(callback, db, callback_data.subscription_id)


def _parse_destination_choice(data: Optional[str]) -> Optional[tuple[int, Optional[int]]]:
    """``(subscription_id, method_id)`` from 'sub_payment_destination:<sub>:<id>|default'; None when broken."""
    parts = (data or "")[len(SUB_PAYMENT_DESTINATION_PREFIX):].split(":")
    if len(parts) != 2 or not parts[0].strip().isdigit():
        return None
    value = parts[1].strip().lower()
    if value == "default":
        return int(parts[0]), None
    if not value.isdigit():
        return None
    return int(parts[0]), int(value)


@admin_router.callback_query(F.data.startswith(SUB_PAYMENT_DESTINATION_PREFIX))
async def handle_subscription_payment_method_select(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    """A method (or Default) picked: save it and return to Payment with a banner and a toast."""
    parsed = _parse_destination_choice(callback.data)
    if parsed is None:
        sub_part = (callback.data or "")[len(SUB_PAYMENT_DESTINATION_PREFIX):].split(":", 1)[0].strip()
        if sub_part.isdigit():
            await send_payment_info_screen(callback, db, int(sub_part), toast=DEAD_BUTTON)
        else:
            await answer_callback_once(callback, DEAD_BUTTON)
        return
    subscription_id, destination_id = parsed
    await _drop_flow(callback, state)
    if not await db.get_subscription(subscription_id):
        await show_subscription_gone(callback, db)
        return
    if destination_id is not None and not await db.get_payment_destination(destination_id):
        await send_payment_method_screen(callback, db, subscription_id, toast=METHOD_GONE)
        return
    await db.update_subscription_fields(subscription_id, payment_destination_id=destination_id)
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await show_subscription_gone(callback, db)
        return
    choice = await payment_choice(db, subscription)
    await send_payment_info_screen(
        callback,
        db,
        subscription_id,
        banner=method_banner(choice),
        toast=method_toast(choice),
    )


# -------------------------------------------------------------------- Comment


@admin_router.callback_query(SubscriptionAction.filter(F.action == "comment"))
async def handle_subscription_comment_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    """📝 Comment: ask for the text; [🗑 Clear comment] sits above Cancel while there is one."""
    subscription_id = callback_data.subscription_id
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await _drop_flow(callback, state)
        await show_subscription_gone(callback, db)
        return
    has_comment = bool(str(subscription.get("comment") or "").strip())
    await start_subscription_edit_flow(
        callback,
        state,
        subscription_id,
        SubscriptionEditForm.comment,
        comment_prompt_text(subscription),
        origin_screen=_payment_origin(subscription_id),
        extra_rows=comment_quick_rows(subscription_id, has_comment),
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "comment_clear"))
async def handle_subscription_comment_clear(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    """[🗑 Clear comment]: end the prompt and edit Payment with '✅ Comment cleared'.

    On an old prompt (no comment input running for this subscription, e.g.
    after a restart) the tapped message itself becomes Payment.
    """
    subscription_id = callback_data.subscription_id
    message = callback.message
    running = (
        isinstance(message, Message)
        and await state.get_state() == SubscriptionEditForm.comment.state
        and await get_edit_subscription_id(state) == subscription_id
    )
    if not await db.get_subscription(subscription_id):
        await _drop_flow(callback, state)
        await show_subscription_gone(callback, db)
        return
    await db.update_subscription_fields(subscription_id, comment="")
    toast = toast_from_html(COMMENT_CLEARED_BANNER)
    if not running or not isinstance(message, Message):
        await _drop_flow(callback, state)
        await send_payment_info_screen(callback, db, subscription_id, banner=COMMENT_CLEARED_BANNER, toast=toast)
        return
    flow_data = await finish_prompt(message, state, bot=callback.bot)
    screen = await build_payment_info_screen(db, subscription_id, banner=COMMENT_CLEARED_BANNER)
    if screen is not None:
        text, markup = screen
        origin = origin_of(flow_data)
        bot = callback.bot
        if origin is None or bot is None or not await edit_in_place(bot, *origin, text, markup):
            await message.answer(text, reply_markup=markup)
    await answer_callback_once(callback, toast)


@admin_router.message(SubscriptionEditForm.comment)
async def edit_subscription_comment(message: Message, state: FSMContext, db: Database) -> None:
    """The typed comment: saved ('✅ Comment saved'), or cleared by 'clear', 'none' or '-'."""
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return
    raw = (message.text or "").strip()
    if not raw:
        await note_typo(message, state, COMMENT_TEXT_HINT)
        return
    if len(raw) > COMMENT_MAX_LEN:
        await note_typo(message, state, COMMENT_TOO_LONG_HINT)
        return
    cleared = raw.lower() in COMMENT_CLEAR_WORDS
    if not await db.get_subscription(sub_id):
        await finish_prompt(message, state)
        await send_subscription_list(message, db, banner=SUBSCRIPTION_GONE_BANNER)
        return
    await db.update_subscription_fields(sub_id, comment="" if cleared else raw)
    flow_data = await finish_prompt(message, state)
    banner = COMMENT_CLEARED_BANNER if cleared else COMMENT_SAVED_BANNER
    await show_payment_after_prompt(message, db, sub_id, flow_data, banner=banner)
