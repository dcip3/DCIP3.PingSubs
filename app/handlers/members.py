"""Admin Users section: list, add user, member card, sign-in link, balance, rename, delete.

Screens live in app/ui/member_screens.py. Every inline button edits the
message it sits on and answers the tap once; typed input goes through
app/ui/prompts.py and, on success, redraws the screen the prompt came from
with a result banner. Destructive actions (delete, a new sign-in link) ask
first and run under a double-tap guard.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.storage.db import Database
from app.ui.guards import guarded
from app.ui.helpers import (
    USER_GONE,
    prompt_out_of_date_text,
    answer_callback_once,
    is_not_modified_error,
    is_uneditable_error,
    resolve_report_timezone,
    respond_with_markup,
    is_spawned_screen,
    remember_spawned,
    spawned_screen_of,
)
from app.ui.keyboards import MENU_USERS
from app.ui.member_screens import (
    added_heading,
    balance_currency_prompt_text,
    balance_prompt_text,
    first_name_of,
    friend_balance,
    full_name_of,
    invite_expires_at,
    is_pending,
    member_balance_currency_screen,
    member_balance_screen,
    member_card_screen,
    member_delete_confirm_screen,
    member_invite_confirm_screen,
    member_list_screen,
    new_link_heading,
    members_in_list_order,
    page_of_member,
    send_member_detail,
    send_member_list,
    send_member_report,
    show_user_gone,
    sign_in_link_screen,
    storage_timestamp,
)
from app.ui.prompts import ask, finish_prompt, note_typo, origin_of
from app.ui.states import FriendForm, MemberAction, MemberEditForm
from app.ui.style import SEP, toast_from_html
from app.ui.text import escape_html, format_money, parse_amount, parse_currency, validate_person_name

from . import admin_router

logger = logging.getLogger(__name__)


EDIT_MEMBER_ID = "edit_member_id"
PROMPT_OUT_OF_DATE = prompt_out_of_date_text("👥 Users")
ADD_USER_PROMPT = "Name of the new user?\nExample: <code>Anna Petrova</code>"
AMOUNT_HINT = "⚠️ Need a number, for example <code>1200</code> or <code>+500</code>."
NEGATIVE_BALANCE_HINT = "⚠️ The balance cannot go below zero."
CURRENCY_HINT = "⚠️ Need three letters, for example <code>USD</code>."
NEW_LINK_TOAST = "New link ready"
USERNAME_MISSING_ALERT = "Bot username is not set"


def _generate_invite_payload() -> str:
    return f"join_{secrets.token_urlsafe(18)}"


def _invite_expires_at() -> str:
    """The stored (naive UTC ISO) expiry of a link made now."""
    return storage_timestamp(invite_expires_at())


async def _build_invite_link(bot: Bot, payload: str) -> str:
    bot_user = await bot.get_me()
    username = (bot_user.username or "").strip()
    if not username:
        raise RuntimeError("Bot username is not configured.")
    return f"https://t.me/{username}?start={payload}"


async def _try_invite_link(bot: Bot, payload: str) -> Optional[str]:
    try:
        return await _build_invite_link(bot, payload)
    except RuntimeError:
        return None


def _expiry_moment(stored: str) -> datetime:
    return datetime.fromisoformat(stored).replace(tzinfo=timezone.utc)


# ----------------------------------------------------------- shared plumbing


async def _show_on_origin(
    message: Message,
    data: Mapping[str, Any],
    text: str,
    markup: Optional[InlineKeyboardMarkup],
) -> None:
    """Redraw the screen a prompt came from; send the screen anew when it is gone."""
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
            logger.debug("Origin screen %s is gone, sending a new one: %s", message_id, error)
    await message.answer(text, reply_markup=markup)


def _friend_id_of(data: Mapping[str, Any]) -> Optional[int]:
    try:
        return int(data.get(EDIT_MEMBER_ID) or 0) or None
    except (TypeError, ValueError):
        return None


async def _prompt_out_of_date(message: Message, state: FSMContext) -> None:
    await finish_prompt(message, state)
    await message.answer(PROMPT_OUT_OF_DATE)


async def _show_list_after_gone(message: Message, data: Mapping[str, Any], db: Database) -> None:
    text, markup = await member_list_screen(db, banner="⚠️ This user was deleted")
    await _show_on_origin(message, data, text, markup)


# ---------------------------------------------------------------------- list


@admin_router.message(F.text == MENU_USERS)
async def handle_members_menu(message: Message, db: Database) -> None:
    await send_member_list(message, db)


@admin_router.callback_query(MemberAction.filter(F.action == "back"))
async def handle_members_back(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    await send_member_list(callback, db, focus_friend_id=callback_data.friend_id)


@admin_router.callback_query(MemberAction.filter(F.action == "page"))
async def handle_members_page(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    await send_member_list(callback, db, page=max(1, callback_data.friend_id))


# ------------------------------------------------------------------ add user


@admin_router.callback_query(MemberAction.filter(F.action == "add"))
async def handle_member_add(callback: CallbackQuery, state: FSMContext) -> None:
    await ask(callback, state, FriendForm.full_name, ADD_USER_PROMPT, origin_screen="member_list")


@admin_router.message(FriendForm.full_name)
async def handle_member_create(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    full_name, error_message = validate_person_name(message.text)
    if error_message:
        await note_typo(message, state, error_message)
        return

    invite_payload = _generate_invite_payload()
    stored_expiry = _invite_expires_at()
    friend_id = await db.create_friend_invite(full_name, invite_payload, stored_expiry)
    invite_link = await _try_invite_link(bot, invite_payload)
    data = await finish_prompt(message, state)
    friend = await db.get_friend(friend_id) or {"id": friend_id, "full_name": full_name}
    text, markup = sign_in_link_screen(
        friend,
        added_heading(friend),
        invite_link,
        _expiry_moment(stored_expiry),
        await resolve_report_timezone(db),
    )
    await _show_on_origin(message, data, text, markup)


# ---------------------------------------------------------------------- card


@admin_router.callback_query(MemberAction.filter(F.action == "open"))
async def handle_member_open(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    await send_member_detail(callback, db, callback_data.friend_id)


@admin_router.callback_query(MemberAction.filter(F.action == "open_new"))
async def handle_member_open_new(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    """[👤 Card] under an admin alert: the card as a NEW message with [✖️ Close]; the alert stays."""
    screen = await member_card_screen(db, callback_data.friend_id, spawned=True)
    if screen is None:
        await answer_callback_once(callback, USER_GONE)
        return
    text, markup = screen
    sent = None
    if isinstance(callback.message, Message):
        sent = await callback.message.answer(text, reply_markup=markup)
    elif callback.message is not None and callback.bot is not None:
        sent = await callback.bot.send_message(callback.message.chat.id, text, reply_markup=markup)
    # Its sub-screens come back through plain member:open; remembered, it keeps [✖️ Close].
    remember_spawned(sent)
    await answer_callback_once(callback)


@admin_router.callback_query(MemberAction.filter(F.action == "report"))
async def handle_member_report(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    await send_member_report(callback, db, callback_data.friend_id)


# ------------------------------------------------------------ sign-in link


@admin_router.callback_query(MemberAction.filter(F.action == "invite"))
async def handle_member_invite_refresh(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    """The old action now asks first: a new link makes the old one stop working."""
    friend = await db.get_friend(callback_data.friend_id)
    if not friend:
        await show_user_gone(callback, db)
        return
    if not is_pending(friend):
        await send_member_detail(callback, db, callback_data.friend_id, toast=_already_signed_in(friend))
        return
    text, markup = await member_invite_confirm_screen(db, friend)
    await respond_with_markup(callback, text, reply_markup=markup)


def _already_signed_in(friend: Mapping[str, Any]) -> str:
    return f"{first_name_of(dict(friend))} already signed in"


@admin_router.callback_query(MemberAction.filter(F.action == "invite_yes"))
async def handle_member_invite_yes(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
    bot: Bot,
) -> None:
    friend_id = callback_data.friend_id
    async with guarded(callback, ("member_invite", friend_id)) as acquired:
        if not acquired:
            return
        friend = await db.get_friend(friend_id)
        if not friend:
            await show_user_gone(callback, db)
            return
        if not is_pending(friend):
            await send_member_detail(callback, db, friend_id, toast=_already_signed_in(friend))
            return
        invite_payload = _generate_invite_payload()
        invite_link = await _try_invite_link(bot, invite_payload)
        if invite_link is None:
            # The old link keeps working: nothing is changed when the new one cannot be shown.
            screen = await member_card_screen(db, friend_id, spawned=spawned_screen_of(callback))
            if screen is None:
                await show_user_gone(callback, db)
                return
            await respond_with_markup(callback, screen[0], reply_markup=screen[1], toast=USERNAME_MISSING_ALERT, alert=True)
            return
        stored_expiry = _invite_expires_at()
        if not await db.refresh_friend_invite(friend_id, invite_payload, stored_expiry):
            await send_member_detail(callback, db, friend_id, toast=_already_signed_in(friend))
            return
        text, markup = sign_in_link_screen(
            friend,
            new_link_heading(friend),
            invite_link,
            _expiry_moment(stored_expiry),
            await resolve_report_timezone(db),
        )
        await respond_with_markup(callback, text, reply_markup=markup, toast=NEW_LINK_TOAST)


# ------------------------------------------------------------------- balance


async def _show_balance(callback: CallbackQuery, db: Database, friend_id: int, **kwargs: Any) -> None:
    banner = kwargs.pop("banner", None)
    screen = await member_balance_screen(db, friend_id, banner=banner)
    if screen is None:
        await show_user_gone(callback, db)
        return
    text, markup = screen
    await respond_with_markup(callback, text, reply_markup=markup, **kwargs)


@admin_router.callback_query(MemberAction.filter(F.action == "balance"))
async def handle_member_balance_menu(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    await _show_balance(callback, db, callback_data.friend_id)


@admin_router.callback_query(MemberAction.filter(F.action == "balance_set"))
async def handle_member_balance_set_prompt(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
    state: FSMContext,
) -> None:
    friend = await db.get_friend(callback_data.friend_id)
    if not friend:
        await show_user_gone(callback, db)
        return
    amount, currency = await friend_balance(db, friend)
    await ask(
        callback,
        state,
        MemberEditForm.balance,
        balance_prompt_text(friend, amount, currency),
        origin_screen=f"member_balance:{callback_data.friend_id}",
        data={EDIT_MEMBER_ID: callback_data.friend_id},
    )


@admin_router.callback_query(MemberAction.filter(F.action == "balance_currency"))
async def handle_member_balance_currency_menu(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    screen = await member_balance_currency_screen(db, callback_data.friend_id)
    if screen is None:
        await show_user_gone(callback, db)
        return
    text, markup = screen
    await respond_with_markup(callback, text, reply_markup=markup)


def _currency_banner(currency: str) -> str:
    return f"✅ Currency{SEP}<b>{escape_html(currency)}</b>"


@admin_router.callback_query(F.data.startswith("member_balance_currency:"))
async def handle_member_balance_currency_select(
    callback: CallbackQuery,
    db: Database,
) -> None:
    parts = (callback.data or "").split(":", 2)
    try:
        friend_id = int(parts[1]) if len(parts) == 3 else 0
    except ValueError:
        friend_id = 0
    currency = parse_currency(parts[2]) if len(parts) == 3 else None
    if not friend_id or currency is None:
        await answer_callback_once(callback, "This button no longer works")
        return
    updated = await db.set_friend_balance_currency(friend_id, currency)
    if updated is None:
        await show_user_gone(callback, db)
        return
    await _show_balance(callback, db, friend_id, banner=_currency_banner(updated), toast=f"Currency: {updated}")


@admin_router.callback_query(MemberAction.filter(F.action == "balance_currency_other"))
async def handle_member_balance_currency_other(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
    state: FSMContext,
) -> None:
    friend = await db.get_friend(callback_data.friend_id)
    if not friend:
        await show_user_gone(callback, db)
        return
    await ask(
        callback,
        state,
        MemberEditForm.balance_currency,
        balance_currency_prompt_text(friend),
        origin_screen=f"member_balance:{callback_data.friend_id}",
        data={EDIT_MEMBER_ID: callback_data.friend_id},
    )


@admin_router.message(MemberEditForm.balance)
async def handle_member_balance_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    friend_id = _friend_id_of(await state.get_data())
    if friend_id is None:
        await _prompt_out_of_date(message, state)
        return
    friend = await db.get_friend(friend_id)
    if not friend:
        await _show_list_after_gone(message, await finish_prompt(message, state), db)
        return
    parsed = parse_amount(message.text, allow_signed=True)
    if parsed is None:
        await note_typo(message, state, AMOUNT_HINT)
        return
    value, relative = parsed
    current, currency = await friend_balance(db, friend)
    new_balance = round(current + value if relative else value, 2)
    if new_balance < 0:
        await note_typo(message, state, NEGATIVE_BALANCE_HINT)
        return
    updated = await db.set_friend_balance(friend_id, new_balance)
    data = await finish_prompt(message, state)
    if updated is None:
        await _show_list_after_gone(message, data, db)
        return
    banner = f"✅ Balance updated{SEP}{format_money(updated, currency, bold=True)}"
    screen = await member_balance_screen(db, friend_id, banner=banner)
    if screen is None:
        await _show_list_after_gone(message, data, db)
        return
    await _show_on_origin(message, data, *screen)


@admin_router.message(MemberEditForm.balance_currency)
async def handle_member_balance_currency_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    friend_id = _friend_id_of(await state.get_data())
    if friend_id is None:
        await _prompt_out_of_date(message, state)
        return
    currency = parse_currency(message.text)
    if currency is None:
        await note_typo(message, state, CURRENCY_HINT)
        return
    updated = await db.set_friend_balance_currency(friend_id, currency)
    data = await finish_prompt(message, state)
    screen = None
    if updated is not None:
        screen = await member_balance_screen(db, friend_id, banner=_currency_banner(updated))
    if screen is None:
        await _show_list_after_gone(message, data, db)
        return
    await _show_on_origin(message, data, *screen)


# -------------------------------------------------------------------- rename


@admin_router.callback_query(MemberAction.filter(F.action == "rename"))
async def handle_member_rename(
    callback: CallbackQuery,
    callback_data: MemberAction,
    state: FSMContext,
    db: Database,
) -> None:
    friend = await db.get_friend(callback_data.friend_id)
    if not friend:
        await show_user_gone(callback, db)
        return
    await ask(
        callback,
        state,
        MemberEditForm.full_name,
        f"New name for {escape_html(full_name_of(friend))}?",
        origin_screen=f"member_card:{callback_data.friend_id}",
        data={EDIT_MEMBER_ID: callback_data.friend_id},
    )


@admin_router.message(MemberEditForm.full_name)
async def handle_member_rename_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    friend_id = _friend_id_of(await state.get_data())
    if friend_id is None:
        await _prompt_out_of_date(message, state)
        return
    raw_name, error_message = validate_person_name(message.text)
    if error_message:
        await note_typo(message, state, error_message)
        return
    data = await finish_prompt(message, state)
    if not await db.get_friend(friend_id):
        await _show_list_after_gone(message, data, db)
        return
    await db.update_friend_name(friend_id, raw_name)
    origin = origin_of(data)
    screen = await member_card_screen(
        db,
        friend_id,
        banner=f"✅ Renamed{SEP}<b>{escape_html(raw_name)}</b>",
        spawned=origin is not None and is_spawned_screen(*origin),
    )
    if screen is None:
        await _show_list_after_gone(message, data, db)
        return
    await _show_on_origin(message, data, *screen)


# -------------------------------------------------------------------- delete


@admin_router.callback_query(MemberAction.filter(F.action == "delete"))
async def handle_member_delete_prompt(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    friend = await db.get_friend(callback_data.friend_id)
    if not friend:
        await show_user_gone(callback, db)
        return
    text, markup = await member_delete_confirm_screen(db, friend)
    await respond_with_markup(callback, text, reply_markup=markup)


@admin_router.callback_query(MemberAction.filter(F.action == "confirm_delete"))
async def handle_member_delete_confirm(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    friend_id = callback_data.friend_id
    async with guarded(callback, ("member_delete", friend_id)) as acquired:
        if not acquired:
            return
        friend = await db.get_friend(friend_id)
        if not friend:
            await show_user_gone(callback, db)
            return
        page = page_of_member(await members_in_list_order(db), friend_id) or 1
        await db.delete_friend(friend_id)
        banner = f"✅ {escape_html(full_name_of(friend))} deleted"
        await send_member_list(callback, db, page=page, banner=banner, toast=toast_from_html(banner))
