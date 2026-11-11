"""Participants subtree of the admin subscription card (WP5).

Participants (sub:participants) → Choose people (sub:participants_manage,
spart:*, spart_page:*), the payment-mode radio (sub_payment_mode:*), Share
count (sub:share + sharelimit:all) and Amount per user (sub:useramounts,
sub_user_amount*:*). Every callback string that ever shipped keeps a handler;
the Set-all confirmation is the new ``sub_user_amount_all_yes:<id>:<cents>``.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.core.constants import PAYMENT_MODE_FIXED, PAYMENT_MODE_SPLIT
from app.storage.db import Database
from app.ui.guards import guarded
from app.ui.helpers import (
    PROMPT_OUT_OF_DATE_TEXT,
    USER_GONE,
    answer_callback_once,
    edit_in_place,
    require_edit_subscription_id,
    respond_with_markup,
    show_subscription_gone,
    start_subscription_edit_flow,
)
from app.ui.keyboards import (
    PARTICIPANTS_PAGE_PREFIX,
    USER_AMOUNT_ALL_YES_PREFIX,
    build_share_limit_keyboard,
    dialog_cancel_inline_keyboard,
    user_amount_clear_keyboard,
)
from app.ui.participant_screens import (
    AMOUNT_HINT,
    AMOUNT_NEGATIVE_HINT,
    MAX_SET_ALL_AMOUNT,
    PERSON_LEFT_BANNER,
    SET_ALL_AMOUNT_HINT,
    SHARE_COUNT_HINT,
    USER_AMOUNT_OUT_OF_DATE_TOAST,
    build_participants_screen,
    build_set_all_confirm,
    build_user_amounts_screen,
    fixed_amount_of,
    send_participants_screen,
    send_picker_screen,
    send_user_amounts_screen,
    set_all_banner,
    set_all_prompt_text,
    share_count_banner,
    share_count_prompt_text,
    show_after_prompt,
    user_amount_banner,
    user_amount_default_banner,
    user_amount_prompt_text,
)
from app.ui.prompts import (
    CLEANUP_IDS,
    ORIGIN_CHAT_ID,
    ORIGIN_MESSAGE_ID,
    ORIGIN_SCREEN,
    PROMPT_MESSAGE_ID,
    finish_prompt,
    note_typo,
    origin_of,
    prompt_text,
    reset_flow_state,
    wind_down,
)
from app.ui.states import ParticipantAction, SubscriptionAction, SubscriptionEditForm
from app.ui.style import toast_from_html
from app.ui.text import format_shares, is_default_word, parse_amount

from . import admin_router
from .subscriptions import _drop_flow, parse_share_limit_input

MAX_SHARE_WEIGHT = 5
USER_AMOUNT_SUB_KEY = "user_amount_subscription_id"
USER_AMOUNT_FRIEND_KEY = "user_amount_friend_id"
USER_AMOUNT_ALL_SUB_KEY = "user_amount_all_subscription_id"


def _participants_origin(subscription_id: int) -> str:
    return f"participants:{int(subscription_id)}"


def _user_amounts_origin(subscription_id: int) -> str:
    return f"useramounts:{int(subscription_id)}"


def _plain_name(person: Mapping[str, Any]) -> str:
    """The person's name for a toast (plain text, spaces collapsed)."""
    return " ".join(str(person.get("full_name") or "Unknown").split())


def _int_parts(data: Optional[str], count: int) -> Optional[list[int]]:
    """``'prefix:1:2'`` -> ``[1, 2]`` for ``count=2``; ``None`` when malformed."""
    parts = (data or "").split(":")
    if len(parts) != count + 1:
        return None
    try:
        return [int(part) for part in parts[1:]]
    except ValueError:
        return None


async def _participant(db: Database, subscription_id: int, friend_id: int) -> Optional[Dict[str, Any]]:
    participants = await db.list_subscription_participants(subscription_id)
    return next((person for person in participants if int(person["id"]) == int(friend_id)), None)


# -------------------------------------------------------------- Participants


@admin_router.callback_query(SubscriptionAction.filter(F.action == "participants"))
async def handle_subscription_participants(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await _drop_flow(callback, state)
    await send_participants_screen(callback, db, callback_data.subscription_id)


@admin_router.callback_query(F.data.startswith("sub_payment_mode:"))
async def handle_subscription_payment_mode_select(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    """The mode radio: save, redraw Participants, toast 'Payment mode: fixed'. Stored amounts are kept."""
    parts = (callback.data or "").split(":")
    mode = parts[2].strip().lower() if len(parts) == 3 else ""
    try:
        subscription_id = int(parts[1]) if len(parts) == 3 else 0
    except ValueError:
        subscription_id = 0
    if not subscription_id or mode not in {PAYMENT_MODE_SPLIT, PAYMENT_MODE_FIXED}:
        await answer_callback_once(callback)
        return
    await _drop_flow(callback, state)
    if not await db.get_subscription(subscription_id):
        await show_subscription_gone(callback, db)
        return
    await db.update_subscription_fields(subscription_id, payment_mode=mode)
    toast = f"Payment mode: {'fixed' if mode == PAYMENT_MODE_FIXED else 'split'}"
    await send_participants_screen(callback, db, subscription_id, toast=toast)


# ----------------------------------------------------------- Choose people


@admin_router.callback_query(SubscriptionAction.filter(F.action == "participants_manage"))
async def handle_subscription_participants_manage(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await _drop_flow(callback, state)
    await send_picker_screen(callback, db, callback_data.subscription_id)


@admin_router.callback_query(F.data.startswith(PARTICIPANTS_PAGE_PREFIX))
async def handle_participants_page(callback: CallbackQuery, db: Database) -> None:
    """``spart_page:<sub>:<n>``: another page of the picker."""
    parsed = _int_parts(callback.data, 2)
    if parsed is None:
        await answer_callback_once(callback)
        return
    subscription_id, page = parsed
    await send_picker_screen(callback, db, subscription_id, page=page)


async def _picker_target(
    callback: CallbackQuery,
    callback_data: ParticipantAction,
    db: Database,
) -> Optional[Dict[str, Any]]:
    """The tapped user, or ``None`` after redrawing what is current (list or picker) with a toast."""
    if not await db.get_subscription(callback_data.subscription_id):
        await show_subscription_gone(callback, db)
        return None
    friends = await db.list_friends_with_membership(callback_data.subscription_id)
    target = next((friend for friend in friends if int(friend["id"]) == callback_data.friend_id), None)
    if target is None:
        await send_picker_screen(callback, db, callback_data.subscription_id, toast=USER_GONE)
    return target


@admin_router.callback_query(ParticipantAction.filter(F.action == "toggle"))
async def handle_participant_toggle(
    callback: CallbackQuery,
    callback_data: ParticipantAction,
    db: Database,
) -> None:
    target = await _picker_target(callback, callback_data, db)
    if target is None:
        return
    adding = not bool(target["is_member"])
    await db.set_participant(
        subscription_id=callback_data.subscription_id,
        friend_id=callback_data.friend_id,
        enabled=adding,
    )
    toast = f"{_plain_name(target)} {'added' if adding else 'removed'}"
    await send_picker_screen(
        callback, db, callback_data.subscription_id, friend_id=callback_data.friend_id, toast=toast
    )


@admin_router.callback_query(ParticipantAction.filter(F.action == "weight"))
async def handle_participant_weight(
    callback: CallbackQuery,
    callback_data: ParticipantAction,
    db: Database,
) -> None:
    """×N cycles the share 1 → 5 → 1 and toasts 'Alice Smith: 3 shares'."""
    target = await _picker_target(callback, callback_data, db)
    if target is None:
        return
    if not target["is_member"]:
        # An outdated picker: the person was removed meanwhile.
        await send_picker_screen(
            callback,
            db,
            callback_data.subscription_id,
            friend_id=callback_data.friend_id,
            toast=USER_AMOUNT_OUT_OF_DATE_TOAST,
        )
        return
    current_weight = int(target.get("share_weight") or 1)
    new_weight = 1 if current_weight >= MAX_SHARE_WEIGHT else current_weight + 1
    await db.update_participant_weight(
        subscription_id=callback_data.subscription_id,
        friend_id=callback_data.friend_id,
        share_weight=new_weight,
    )
    await send_picker_screen(
        callback,
        db,
        callback_data.subscription_id,
        friend_id=callback_data.friend_id,
        toast=f"{_plain_name(target)}: {format_shares(new_weight)}",
    )


# ------------------------------------------------------------- Share count


@admin_router.callback_query(SubscriptionAction.filter(F.action == "share"))
async def handle_subscription_share_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription_id = callback_data.subscription_id
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await _drop_flow(callback, state)
        await show_subscription_gone(callback, db)
        return
    participants = await db.list_subscription_participants(subscription_id)
    everyone = not int(subscription.get("share_limit") or 0)
    await start_subscription_edit_flow(
        callback,
        state,
        subscription_id,
        SubscriptionEditForm.share_limit,
        share_count_prompt_text(subscription, participants),
        reply_markup=build_share_limit_keyboard(everyone_selected=everyone),
        origin_screen=_participants_origin(subscription_id),
    )


@admin_router.message(SubscriptionEditForm.share_limit)
async def edit_subscription_share(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return
    valid, share_limit = parse_share_limit_input(message.text)
    if not valid:
        await note_typo(message, state, SHARE_COUNT_HINT)
        return
    if not await db.get_subscription(sub_id):
        await finish_prompt(message, state)
        await show_after_prompt(message, db, None, None)
        return
    await db.update_subscription_fields(sub_id, share_limit=share_limit)
    flow_data = await finish_prompt(message, state)
    screen = await build_participants_screen(db, sub_id, banner=share_count_banner(share_limit))
    await show_after_prompt(message, db, origin_of(flow_data), screen)


async def finish_share_count_button(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    sub_id: int,
    share_limit: Optional[int],
) -> None:
    """[Everyone] tapped on the Share count prompt (``sharelimit:all``): save, remove the prompt, redraw Participants."""
    from .subscriptions import _stale_wizard_button  # shared stale-step handling

    message = callback.message
    data = await state.get_data()
    if not isinstance(message, Message) or data.get(PROMPT_MESSAGE_ID) not in (None, message.message_id):
        await _stale_wizard_button(callback, state)
        return
    if not await db.get_subscription(sub_id):
        await _drop_flow(callback, state)
        await show_subscription_gone(callback, db)
        return
    await db.update_subscription_fields(sub_id, share_limit=share_limit)
    flow_data = await finish_prompt(message, state, bot=callback.bot)
    banner = share_count_banner(share_limit)
    screen = await build_participants_screen(db, sub_id, banner=banner)
    if screen is not None:
        text, markup = screen
        origin = origin_of(flow_data)
        bot = callback.bot
        if origin is None or bot is None or not await edit_in_place(bot, *origin, text, markup):
            await message.answer(text, reply_markup=markup)
    await answer_callback_once(callback, toast_from_html(banner))


# --------------------------------------------------------- Amount per user


@admin_router.callback_query(SubscriptionAction.filter(F.action == "useramounts"))
async def handle_subscription_user_amounts_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    """Amount per user; it is also the Cancel of the Set-all question."""
    await _drop_flow(callback, state)
    subscription = await db.get_subscription(callback_data.subscription_id)
    if not subscription:
        await show_subscription_gone(callback, db)
        return
    if str(subscription.get("payment_mode") or PAYMENT_MODE_SPLIT).strip().lower() != PAYMENT_MODE_FIXED:
        # An outdated screen: the mode is split now, so there is nothing to set per person.
        await send_participants_screen(callback, db, callback_data.subscription_id, toast="Payment mode: split")
        return
    await send_user_amounts_screen(callback, db, callback_data.subscription_id)


async def _ask_with_keyboard(
    callback: CallbackQuery,
    state: FSMContext,
    new_state: State,
    text: str,
    markup: InlineKeyboardMarkup,
    *,
    origin_screen: str,
    data: Mapping[str, Any],
) -> None:
    """``prompts.ask`` with a ready keyboard: the per-user prompt has [✖️ Cancel][↺ Use default] in one row."""
    message = callback.message
    bot = callback.bot
    if not isinstance(message, Message) or bot is None:
        await answer_callback_once(callback)
        return
    old_data = await reset_flow_state(state)
    await wind_down(bot, message.chat.id, old_data)
    prompt = await message.answer(prompt_text(text), reply_markup=markup)
    await state.set_state(new_state)
    await state.update_data(
        {
            **dict(data),
            PROMPT_MESSAGE_ID: prompt.message_id,
            ORIGIN_CHAT_ID: message.chat.id,
            ORIGIN_MESSAGE_ID: message.message_id,
            ORIGIN_SCREEN: origin_screen,
            CLEANUP_IDS: [],
        }
    )
    await answer_callback_once(callback)


@admin_router.callback_query(F.data.startswith("sub_user_amount:"))
async def handle_subscription_user_amount_edit_callback(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    """A person on Amount per user: ask for their amount."""
    parsed = _int_parts(callback.data, 2)
    if parsed is None:
        await answer_callback_once(callback)
        return
    subscription_id, friend_id = parsed
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await _drop_flow(callback, state)
        await show_subscription_gone(callback, db)
        return
    person = await _participant(db, subscription_id, friend_id)
    if person is None:
        await send_user_amounts_screen(callback, db, subscription_id, toast=USER_AMOUNT_OUT_OF_DATE_TOAST)
        return
    markup = (
        user_amount_clear_keyboard(subscription_id, friend_id)
        if fixed_amount_of(person) is not None
        else dialog_cancel_inline_keyboard()
    )
    await _ask_with_keyboard(
        callback,
        state,
        SubscriptionEditForm.user_amount,
        user_amount_prompt_text(subscription, person),
        markup,
        origin_screen=_user_amounts_origin(subscription_id),
        data={
            USER_AMOUNT_SUB_KEY: subscription_id,
            USER_AMOUNT_FRIEND_KEY: friend_id,
            "edit_subscription_id": subscription_id,
        },
    )


@admin_router.callback_query(F.data.startswith("sub_user_amount_clear:"))
async def handle_subscription_user_amount_clear_callback(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    """[↺ Use default] on the per-user prompt: the person pays the subscription amount again."""
    parsed = _int_parts(callback.data, 2)
    message = callback.message
    if parsed is None or not isinstance(message, Message):
        await answer_callback_once(callback)
        return
    subscription_id, friend_id = parsed
    data = await state.get_data()
    running = (
        await state.get_state() == SubscriptionEditForm.user_amount.state
        and data.get(PROMPT_MESSAGE_ID) == message.message_id
    )
    if not await db.get_subscription(subscription_id):
        await _drop_flow(callback, state)
        await show_subscription_gone(callback, db)
        return
    person = await _participant(db, subscription_id, friend_id)
    if person is not None:
        await db.update_participant_fixed_amount(subscription_id, friend_id, None)
        banner = user_amount_default_banner(person)
        toast = toast_from_html(banner)
    else:
        banner, toast = PERSON_LEFT_BANNER, USER_AMOUNT_OUT_OF_DATE_TOAST
    screen = await build_user_amounts_screen(db, subscription_id, banner=banner)
    if screen is None:
        await show_subscription_gone(callback, db)
        return
    text, markup = screen
    if not running:
        # An older prompt (or one from before a restart): it becomes the screen itself.
        await respond_with_markup(callback, text, reply_markup=markup, toast=toast)
        return
    flow_data = await finish_prompt(message, state, bot=callback.bot)
    origin = origin_of(flow_data)
    bot = callback.bot
    if origin is None or bot is None or not await edit_in_place(bot, *origin, text, markup):
        await message.answer(text, reply_markup=markup)
    await answer_callback_once(callback, toast)


@admin_router.message(SubscriptionEditForm.user_amount)
async def edit_subscription_user_amount(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    try:
        subscription_id = int(data[USER_AMOUNT_SUB_KEY])
        friend_id = int(data[USER_AMOUNT_FRIEND_KEY])
    except (KeyError, TypeError, ValueError):
        await _prompt_out_of_date(message, state)
        return
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await finish_prompt(message, state)
        await show_after_prompt(message, db, None, None)
        return
    person = await _participant(db, subscription_id, friend_id)
    if person is None:
        flow_data = await finish_prompt(message, state)
        screen = await build_user_amounts_screen(db, subscription_id, banner=PERSON_LEFT_BANNER)
        await show_after_prompt(message, db, origin_of(flow_data), screen)
        return

    currency = str(subscription.get("currency") or "").strip().upper()
    if is_default_word(message.text):
        amount: Optional[float] = None
        banner = user_amount_default_banner(person)
    else:
        parsed = parse_amount(message.text, allow_signed=True)
        if parsed is None:
            await note_typo(message, state, AMOUNT_HINT)
            return
        if parsed[0] < 0:
            await note_typo(message, state, AMOUNT_NEGATIVE_HINT)
            return
        amount = parsed[0]
        banner = user_amount_banner(person, amount, currency)
    await db.update_participant_fixed_amount(subscription_id, friend_id, amount)
    flow_data = await finish_prompt(message, state)
    screen = await build_user_amounts_screen(db, subscription_id, banner=banner)
    await show_after_prompt(message, db, origin_of(flow_data), screen)


async def _prompt_out_of_date(message: Message, state: FSMContext) -> None:
    old_data = await reset_flow_state(state)
    if message.bot is not None:
        await wind_down(message.bot, message.chat.id, old_data)
    await message.answer(PROMPT_OUT_OF_DATE_TEXT)


# ----------------------------------------------------------------- Set all


@admin_router.callback_query(F.data.startswith("sub_user_amount_all:"))
async def handle_subscription_user_amount_set_all_callback(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parsed = _int_parts(callback.data, 1)
    if parsed is None:
        await answer_callback_once(callback)
        return
    subscription_id = parsed[0]
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await _drop_flow(callback, state)
        await show_subscription_gone(callback, db)
        return
    participants = await db.list_subscription_participants(subscription_id)
    if not participants:
        await send_user_amounts_screen(callback, db, subscription_id, toast=USER_AMOUNT_OUT_OF_DATE_TOAST)
        return
    await start_subscription_edit_flow(
        callback,
        state,
        subscription_id,
        SubscriptionEditForm.user_amount_all,
        set_all_prompt_text(subscription, len(participants)),
        origin_screen=_user_amounts_origin(subscription_id),
        data={USER_AMOUNT_ALL_SUB_KEY: subscription_id},
    )


@admin_router.message(SubscriptionEditForm.user_amount_all)
async def edit_subscription_user_amount_all(message: Message, state: FSMContext, db: Database) -> None:
    """A valid amount turns Amount per user into the question '⚠️ <b>Set 300.00 USD for all 3 people?</b>'."""
    data = await state.get_data()
    try:
        subscription_id = int(data[USER_AMOUNT_ALL_SUB_KEY])
    except (KeyError, TypeError, ValueError):
        await _prompt_out_of_date(message, state)
        return
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await finish_prompt(message, state)
        await show_after_prompt(message, db, None, None)
        return
    parsed = parse_amount(message.text, allow_signed=True)
    if parsed is None or parsed[0] > MAX_SET_ALL_AMOUNT:
        await note_typo(message, state, SET_ALL_AMOUNT_HINT)
        return
    if parsed[0] < 0:
        await note_typo(message, state, AMOUNT_NEGATIVE_HINT)
        return
    amount = parsed[0]
    participants = await db.list_subscription_participants(subscription_id)
    flow_data = await finish_prompt(message, state)
    if not participants:
        screen = await build_user_amounts_screen(db, subscription_id)
    else:
        screen = build_set_all_confirm(subscription, amount, len(participants))
    await show_after_prompt(message, db, origin_of(flow_data), screen)


@admin_router.callback_query(F.data.startswith(USER_AMOUNT_ALL_YES_PREFIX))
async def handle_subscription_user_amount_all_yes(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    """[✏️ Set all] on the question: everyone in the subscription gets the amount from the question."""
    parsed = _int_parts(callback.data, 2)
    if parsed is None or parsed[1] < 0:
        await answer_callback_once(callback)
        return
    subscription_id, cents = parsed
    async with guarded(callback, ("user_amount_all", subscription_id)) as acquired:
        if not acquired:
            return
        subscription = await db.get_subscription(subscription_id)
        if not subscription:
            await show_subscription_gone(callback, db)
            return
        if not await db.list_subscription_participants(subscription_id):
            await send_user_amounts_screen(callback, db, subscription_id, toast=USER_AMOUNT_OUT_OF_DATE_TOAST)
            return
        amount = cents / 100
        await db.update_all_participants_fixed_amount(subscription_id, amount)
        banner = set_all_banner(amount, str(subscription.get("currency") or "").strip().upper())
        await send_user_amounts_screen(callback, db, subscription_id, banner=banner, toast=toast_from_html(banner))
