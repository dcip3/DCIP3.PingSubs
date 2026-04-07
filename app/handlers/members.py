from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.ui.helpers import send_member_detail, send_member_list, send_member_report
from app.ui.keyboards import (
    admin_reply_keyboard,
    dialog_keyboard,
    member_balance_currency_keyboard,
    member_balance_keyboard,
    member_delete_confirm_keyboard,
)
from app.ui.states import FriendForm, MemberAction, MemberEditForm
from app.storage.db import Database
from app.ui.text import validate_person_name

from . import admin_router

INVITE_TTL_DAYS = 7


def _generate_invite_payload() -> str:
    return f"join_{secrets.token_urlsafe(18)}"


def _invite_expires_at() -> str:
    return (datetime.utcnow() + timedelta(days=INVITE_TTL_DAYS)).replace(microsecond=0).isoformat()


async def _build_invite_link(bot: Bot, payload: str) -> str:
    bot_user = await bot.get_me()
    username = (bot_user.username or "").strip()
    if not username:
        raise RuntimeError("Bot username is not configured.")
    return f"https://t.me/{username}?start={payload}"


@admin_router.message(F.text == "👥 Users")
async def handle_members_menu(message: Message, db: Database) -> None:
    await send_member_list(message, db)


@admin_router.callback_query(MemberAction.filter(F.action == "menu"))
async def handle_members_menu_back(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.message.edit_text("Admin menu closed.")
        await callback.message.answer("Admin menu:", reply_markup=admin_reply_keyboard())
    await callback.answer()


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


@admin_router.callback_query(MemberAction.filter(F.action == "add"))
async def handle_member_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(FriendForm.full_name)
    if callback.message:
        await callback.message.answer(
            "👥 New User:\n"
            "Send the user's full name.\n"
            "I'll create the profile and generate an authorization link.\n",
            reply_markup=dialog_keyboard(),
        )
    await callback.answer()


@admin_router.message(FriendForm.full_name)
async def handle_member_create(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    full_name, error_message = validate_person_name(message.text)
    if error_message:
        await message.answer(error_message)
        return

    invite_payload = _generate_invite_payload()
    invite_expires_at = _invite_expires_at()
    friend_id = await db.create_friend_invite(full_name, invite_payload, invite_expires_at)
    try:
        invite_link = await _build_invite_link(bot, invite_payload)
    except RuntimeError:
        await state.clear()
        await message.answer(
            "User created, but I couldn't build a Telegram link because the bot username is not configured.",
            reply_markup=admin_reply_keyboard(),
        )
        await send_member_detail(message, db, friend_id)
        return

    await state.clear()
    await message.answer(
        "User created.\n\n"
        "Share this authorization link with the user:\n"
        f"<code>{invite_link}</code>\n\n"
        f"Expires at (UTC): <code>{invite_expires_at}</code>",
        reply_markup=admin_reply_keyboard(),
    )
    await send_member_detail(message, db, friend_id)


@admin_router.callback_query(MemberAction.filter(F.action == "open"))
async def handle_member_open(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    await send_member_detail(callback, db, callback_data.friend_id)


@admin_router.callback_query(MemberAction.filter(F.action == "report"))
async def handle_member_report(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    await send_member_report(callback, db, callback_data.friend_id)


@admin_router.callback_query(MemberAction.filter(F.action == "invite"))
async def handle_member_invite_refresh(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
    bot: Bot,
) -> None:
    friend = await db.get_friend(callback_data.friend_id)
    if not friend:
        await callback.answer("User not found.", show_alert=True)
        return
    if friend.get("telegram_id") is not None:
        await callback.answer("This user is already linked.", show_alert=True)
        return

    invite_payload = _generate_invite_payload()
    invite_expires_at = _invite_expires_at()
    await db.refresh_friend_invite(callback_data.friend_id, invite_payload, invite_expires_at)
    try:
        invite_link = await _build_invite_link(bot, invite_payload)
    except RuntimeError:
        await callback.answer("Bot username is not configured.", show_alert=True)
        await send_member_detail(callback, db, callback_data.friend_id)
        return

    if callback.message:
        await callback.message.answer(
            "Authorization link refreshed:\n"
            f"<code>{invite_link}</code>\n\n"
            f"Expires at (UTC): <code>{invite_expires_at}</code>",
            reply_markup=admin_reply_keyboard(),
        )
    await callback.answer("Authorization link refreshed.")
    await send_member_detail(callback, db, callback_data.friend_id)


@admin_router.callback_query(MemberAction.filter(F.action == "balance"))
async def handle_member_balance_menu(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
    state: FSMContext,
) -> None:
    await state.clear()
    friend = await db.get_friend(callback_data.friend_id)
    if not friend:
        await callback.answer("User not found.", show_alert=True)
        return
    try:
        balance_value = float(friend.get("balance") or 0.0)
    except (TypeError, ValueError):
        balance_value = 0.0
    default_balance_currency = str(await db.get_setting("target_currency") or "RUB").strip().upper() or "RUB"
    balance_currency = str(friend.get("balance_currency") or "").strip().upper()
    if len(balance_currency) != 3 or not balance_currency.isalpha():
        balance_currency = default_balance_currency
    if callback.message:
        await callback.message.edit_text(
            "💰 Balance:\n"
            "\n"
            f"User: <code>{friend['full_name']}</code>\n"
            f"💰 Amount: <code>{balance_value:.2f}</code>\n"
            f"💱 Currency: <code>{balance_currency}</code>\n\n"
            "Choose an action:",
            reply_markup=member_balance_keyboard(callback_data.friend_id),
        )
    await callback.answer()


async def _start_member_balance_edit(callback: CallbackQuery, state: FSMContext, friend_id: int) -> None:
    await state.set_state(MemberEditForm.balance)
    await state.update_data(edit_member_id=friend_id)
    if callback.message:
        await callback.message.answer(
            "💰 Set balance:\n"
            "Send the new balance value.\n"
            "Example: <code>1200</code>.",
            reply_markup=dialog_keyboard(),
        )
    await callback.answer()


@admin_router.callback_query(MemberAction.filter(F.action == "balance_set"))
async def handle_member_balance_set_prompt(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
    state: FSMContext,
) -> None:
    if not await db.get_friend(callback_data.friend_id):
        await callback.answer("User not found.", show_alert=True)
        return
    await _start_member_balance_edit(callback, state, callback_data.friend_id)


@admin_router.callback_query(MemberAction.filter(F.action == "balance_currency"))
async def handle_member_balance_currency_menu(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    friend = await db.get_friend(callback_data.friend_id)
    if not friend:
        await callback.answer("User not found.", show_alert=True)
        return
    default_currency = str(await db.get_setting("target_currency") or "RUB").strip().upper() or "RUB"
    current_currency = str(friend.get("balance_currency") or "").strip().upper()
    if len(current_currency) != 3 or not current_currency.isalpha():
        current_currency = default_currency
    if callback.message:
        await callback.message.edit_text(
            "💰 Balance:\n"
            f"User: <code>{friend['full_name']}</code>\n"
            f"💱 Currency: <code>{current_currency}</code>\n\n"
            "Choose a value:",
            reply_markup=member_balance_currency_keyboard(callback_data.friend_id, current_currency),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("member_balance_currency:"))
async def handle_member_balance_currency_select(
    callback: CallbackQuery,
    db: Database,
) -> None:
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Invalid action.", show_alert=True)
        return
    _, friend_id_raw, raw_currency = parts
    try:
        friend_id = int(friend_id_raw)
    except ValueError:
        await callback.answer("Invalid user.", show_alert=True)
        return
    currency = raw_currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        await callback.answer("Currency must contain 3 letters.", show_alert=True)
        return
    updated = await db.set_friend_balance_currency(friend_id, currency)
    if updated is None:
        await callback.answer("User not found.", show_alert=True)
        return
    await callback.answer(f"Currency set to {updated}")
    await handle_member_balance_currency_menu(
        callback,
        MemberAction(action="balance_currency", friend_id=friend_id),
        db,
    )


@admin_router.callback_query(MemberAction.filter(F.action == "balance_currency_other"))
async def handle_member_balance_currency_other(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
    state: FSMContext,
) -> None:
    if not await db.get_friend(callback_data.friend_id):
        await callback.answer("User not found.", show_alert=True)
        return
    await state.set_state(MemberEditForm.balance_currency)
    await state.update_data(edit_member_id=callback_data.friend_id)
    if callback.message:
        await callback.message.answer(
            "💱 Balance currency:\n"
            "Send a 3-letter currency code.\n"
            "Example: <code>USD</code>.",
            reply_markup=dialog_keyboard(),
        )
    await callback.answer()


@admin_router.callback_query(MemberAction.filter(F.action == "rename"))
async def handle_member_rename(
    callback: CallbackQuery,
    callback_data: MemberAction,
    state: FSMContext,
) -> None:
    await state.set_state(MemberEditForm.full_name)
    await state.update_data(edit_member_id=callback_data.friend_id)
    if callback.message:
        await callback.message.answer(
            "Rename User:\n🏷️ Send new name:",
            reply_markup=dialog_keyboard(),
        )
    await callback.answer()


@admin_router.message(MemberEditForm.full_name)
async def handle_member_rename_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    raw_name, error_message = validate_person_name(message.text)
    if error_message:
        await message.answer(error_message)
        return
    data = await state.get_data()
    friend_id = data.get("edit_member_id")
    if not friend_id:
        await state.clear()
        await message.answer("Session expired. Open users again.")
        return
    await db.update_friend_name(int(friend_id), raw_name)
    await state.clear()
    await message.answer("Name updated.", reply_markup=admin_reply_keyboard())
    await send_member_detail(message, db, int(friend_id))


@admin_router.message(MemberEditForm.balance)
async def handle_member_balance_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    data = await state.get_data()
    friend_id = data.get("edit_member_id")
    if not friend_id:
        await state.clear()
        await message.answer("Session expired. Open users again.")
        return

    try:
        amount = float((message.text or "").strip().replace(",", "."))
    except ValueError:
        await message.answer("Amount must be a number. Example: 300")
        return

    if amount < 0:
        await message.answer("Balance cannot be negative.")
        return
    updated_balance = await db.set_friend_balance(int(friend_id), amount)

    if updated_balance is None:
        await state.clear()
        await message.answer("User not found.")
        return

    await state.clear()
    await message.answer("Balance updated.", reply_markup=admin_reply_keyboard())
    await send_member_detail(message, db, int(friend_id))


@admin_router.message(MemberEditForm.balance_currency)
async def handle_member_balance_currency_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    data = await state.get_data()
    friend_id = data.get("edit_member_id")
    if not friend_id:
        await state.clear()
        await message.answer("Session expired. Open users again.")
        return

    currency = (message.text or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        await message.answer("Currency must contain 3 letters. Example: USD")
        return
    updated = await db.set_friend_balance_currency(int(friend_id), currency)
    if updated is None:
        await state.clear()
        await message.answer("User not found.")
        return
    await state.clear()
    await message.answer("Balance currency updated.", reply_markup=admin_reply_keyboard())
    await send_member_detail(message, db, int(friend_id))


@admin_router.callback_query(MemberAction.filter(F.action == "delete"))
async def handle_member_delete_prompt(
    callback: CallbackQuery,
    callback_data: MemberAction,
) -> None:
    if callback.message:
        await callback.message.edit_text(
            "Are you sure you want to delete this user?",
            reply_markup=member_delete_confirm_keyboard(callback_data.friend_id),
        )
    await callback.answer()


@admin_router.callback_query(MemberAction.filter(F.action == "confirm_delete"))
async def handle_member_delete_confirm(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
) -> None:
    await db.delete_friend(callback_data.friend_id)
    await callback.answer("User deleted.")
    await send_member_list(callback, db)
