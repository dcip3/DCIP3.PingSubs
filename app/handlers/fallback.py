"""Catch-alls for updates no other router handled. Included LAST in the dispatcher.

* A button nobody handles (a non-admin tapping an admin button, a callback
  from a retired screen, a stale wizard step) gets a short toast instead of an
  endless spinner.
* Text sent with no pending input gets a pointer to the reply menu, together
  with the menu that fits this user (admin, member or guest), which also
  replaces a stale menu left on the client.

The message catch-all only fires when no FSM state is set: text inside a flow
belongs to that flow, even when its handler declines it.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from app.storage.db import Database
from app.ui.helpers import DEAD_BUTTON, answer_callback_once

from .admin import _help_role, help_reply_keyboard

fallback_router = Router(name="fallback")

DEAD_BUTTON_TOAST = DEAD_BUTTON
USE_THE_MENU_TEXT = "Use the menu below 👇"


@fallback_router.callback_query()
async def handle_unknown_callback(callback: CallbackQuery) -> None:
    await answer_callback_once(callback, DEAD_BUTTON_TOAST)


@fallback_router.message(StateFilter(None), F.text)
async def handle_unknown_text(message: Message, db: Database) -> None:
    role = await _help_role(db, message.from_user.id if message.from_user else None)
    await message.answer(USE_THE_MENU_TEXT, reply_markup=help_reply_keyboard(role))
