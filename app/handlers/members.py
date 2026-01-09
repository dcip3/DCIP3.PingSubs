from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.ui.helpers import send_member_detail, send_member_list, send_member_report
from app.ui.keyboards import admin_reply_keyboard, dialog_keyboard, member_delete_confirm_keyboard
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
async def handle_members_back(callback: CallbackQuery, db: Database) -> None:
    await send_member_list(callback, db)


@admin_router.callback_query(MemberAction.filter(F.action == "add"))
async def handle_member_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(FriendForm.telegram_id)
    if callback.message:
        await callback.message.answer(
            "Send the user's Telegram ID (numbers only) or forward their message. Use the Cancel button to stop.",
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


@admin_router.callback_query(MemberAction.filter(F.action == "rename"))
async def handle_member_rename(
    callback: CallbackQuery,
    callback_data: MemberAction,
    state: FSMContext,
) -> None:
    await state.set_state(MemberEditForm.full_name)
    await state.update_data(edit_member_id=callback_data.friend_id)
    if callback.message:
        await callback.message.answer("Send the new user name:")
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
