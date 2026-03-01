from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.ui.helpers import send_member_detail, send_member_list, send_member_report
from app.ui.keyboards import (
    admin_reply_keyboard,
    dialog_keyboard,
    member_balance_keyboard,
    member_delete_confirm_keyboard,
)
from app.ui.states import FriendForm, MemberAction, MemberEditForm
from app.storage.db import Database

from . import admin_router


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
    await state.set_state(FriendForm.telegram_id)
    if callback.message:
        await callback.message.answer(
            "👥 New User:\n"
            "Send Telegram ID (numbers only) or forward user's message.\n",
            reply_markup=dialog_keyboard(),
        )
    await callback.answer()


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
    balance_currency = str(await db.get_setting("target_currency") or "RUB").strip().upper() or "RUB"
    if callback.message:
        await callback.message.edit_text(
            "💰 Balance:\n"
            f"User: <code>{friend['full_name']}</code>\n"
            f"Current: <code>{balance_value:.2f} {balance_currency}</code>\n\n"
            "Choose an action:",
            reply_markup=member_balance_keyboard(callback_data.friend_id),
        )
    await callback.answer()


async def _start_member_balance_edit(
    callback: CallbackQuery,
    state: FSMContext,
    friend_id: int,
    mode: str,
) -> None:
    await state.set_state(MemberEditForm.balance)
    await state.update_data(edit_member_id=friend_id, balance_mode=mode)
    if callback.message:
        if mode == "set":
            await callback.message.answer(
                "💰 Set balance:\n"
                "Send the new balance value.\n"
                "Example: <code>1200</code>.",
                reply_markup=dialog_keyboard(),
            )
        else:
            await callback.message.answer(
                "💰 Add balance:\n"
                "Send amount to add.\n"
                "Example: <code>300</code>.",
                reply_markup=dialog_keyboard(),
            )
    await callback.answer()


@admin_router.callback_query(MemberAction.filter(F.action == "balance_add"))
async def handle_member_balance_add_prompt(
    callback: CallbackQuery,
    callback_data: MemberAction,
    db: Database,
    state: FSMContext,
) -> None:
    if not await db.get_friend(callback_data.friend_id):
        await callback.answer("User not found.", show_alert=True)
        return
    await _start_member_balance_edit(callback, state, callback_data.friend_id, "add")


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
    await _start_member_balance_edit(callback, state, callback_data.friend_id, "set")


@admin_router.callback_query(MemberAction.filter(F.action == "rename"))
async def handle_member_rename(
    callback: CallbackQuery,
    callback_data: MemberAction,
    state: FSMContext,
) -> None:
    await state.set_state(MemberEditForm.full_name)
    await state.update_data(edit_member_id=callback_data.friend_id)
    if callback.message:
        await callback.message.answer("Rename User:\n🏷️ Send new name:")
    await callback.answer()


@admin_router.message(MemberEditForm.full_name)
async def handle_member_rename_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    raw_name = (message.text or "").strip()
    if not raw_name:
        await message.answer("Name cannot be empty. Please try again.")
        return
    data = await state.get_data()
    friend_id = data.get("edit_member_id")
    if not friend_id:
        await state.clear()
        await message.answer("Session expired. Open users again.")
        return
    await db.update_friend_name(int(friend_id), raw_name)
    await state.clear()
    await send_member_detail(message, db, int(friend_id))


@admin_router.message(MemberEditForm.balance)
async def handle_member_balance_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    data = await state.get_data()
    friend_id = data.get("edit_member_id")
    mode = str(data.get("balance_mode") or "add").strip().lower()
    if not friend_id:
        await state.clear()
        await message.answer("Session expired. Open users again.")
        return

    try:
        amount = float((message.text or "").strip().replace(",", "."))
    except ValueError:
        await message.answer("Amount must be a number. Example: 300")
        return

    if mode == "set":
        if amount < 0:
            await message.answer("Balance cannot be negative.")
            return
        updated_balance = await db.set_friend_balance(int(friend_id), amount)
    else:
        if amount <= 0:
            await message.answer("Amount to add must be greater than zero.")
            return
        updated_balance = await db.add_friend_balance(int(friend_id), amount)

    if updated_balance is None:
        await state.clear()
        await message.answer("User not found.")
        return

    await state.clear()
    await message.answer("Balance updated.", reply_markup=admin_reply_keyboard())
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
