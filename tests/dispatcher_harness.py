"""The real dispatcher from ``main.build_dispatcher`` with a fresh database per test.

The routers are module-level objects and can be attached to one dispatcher
only, so the dispatcher is built once per process. Its database is a proxy
that each test points at its own temporary SQLite file; the FSM storage is
emptied before every test. Updates go through ``dp.feed_update`` with a
``FakeBot``, so routing, filters, middlewares and the error handler all run
exactly as in production, without the network.
"""
from __future__ import annotations

import itertools
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Chat, Message, Update

from app.core.config import Settings
from app.error_handler import USER_ERROR_ALERT, USER_ERROR_TEXT
from app.services import CurrencyConverter
from app.storage.db import Database
from app.ui.helpers import _SPAWNED_SCREENS
from telegram_fakes import CHAT_ID, USER_ID, FakeBot, make_callback, make_message

ADMIN_ID = USER_ID


class _DatabaseProxy:
    """Stands in for the Database the dispatcher was built with; forwards to the test's own."""

    def __init__(self) -> None:
        self.target: Optional[Database] = None

    def __getattr__(self, name: str) -> Any:
        target = self.__dict__.get("target")
        if target is None:
            raise RuntimeError("No test database is connected")
        return getattr(target, name)


_DB_PROXY = _DatabaseProxy()
_DISPATCHER: Optional[Dispatcher] = None
_update_ids = itertools.count(1)
_message_ids = itertools.count(5000)


def _settings() -> Settings:
    return Settings(
        bot_token="777:TEST",
        database_path=Path("unused.db"),
        reminder_check_interval=60,
        target_currency="RUB",
        currency_rounding="precise",
        base_reminder_time="10:00",
        base_timezone="Europe/Moscow",
        admin_ids=(),
    )


def real_dispatcher() -> Dispatcher:
    global _DISPATCHER
    if _DISPATCHER is None:
        import main

        _DISPATCHER = main.build_dispatcher(_settings(), _DB_PROXY, CurrencyConverter("RUB"))  # type: ignore[arg-type]
    return _DISPATCHER


class RealDispatcherTestCase(unittest.IsolatedAsyncioTestCase):
    """Each test gets the real dispatcher, an empty database and FSM storage, and a FakeBot."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.connect()
        _DB_PROXY.target = self.db
        self.dp = real_dispatcher()
        self.dp.fsm.storage.storage.clear()  # type: ignore[attr-defined]
        # Card copies opened from pushes are remembered per (chat, message id);
        # every FakeBot numbers its messages from 1000, so start each test afresh.
        _SPAWNED_SCREENS.clear()
        self.bot = FakeBot()

    async def asyncTearDown(self) -> None:
        _DB_PROXY.target = None
        await self.db.close()
        self._tmp.cleanup()

    # ----------------------------------------------------------------- helpers

    def state(self, chat_id: int = CHAT_ID, user_id: int = USER_ID) -> FSMContext:
        return self.dp.fsm.get_context(self.bot, chat_id=chat_id, user_id=user_id)

    async def set_state(self, state: State, **data: Any) -> FSMContext:
        context = self.state()
        await context.set_state(state)
        if data:
            await context.update_data(**data)
        return context

    async def make_admin(self, telegram_id: int = ADMIN_ID) -> None:
        await self.db.add_admin(telegram_id, "Test admin")

    def message(self, text: str, *, chat_type: str = "private") -> Message:
        message = make_message(self.bot, next(_message_ids), text)
        if chat_type != "private":
            message = message.model_copy(update={"chat": Chat(id=-100500, type=chat_type)}).as_(self.bot)
        return message

    def callback(self, data: str, message: Optional[Message] = None) -> CallbackQuery:
        if message is None:
            message = make_message(self.bot, next(_message_ids), "screen", from_bot=True)
        return make_callback(self.bot, message, data)

    async def feed(self, event: Message | CallbackQuery, *, allow_errors: bool = False) -> Any:
        if isinstance(event, Message):
            update = Update(update_id=next(_update_ids), message=event)
        else:
            update = Update(update_id=next(_update_ids), callback_query=event)
        result = await self.dp.feed_update(self.bot, update.as_(self.bot))
        if not allow_errors:
            self.assertNotIn(USER_ERROR_TEXT, self.bot.sent_texts(), "a handler raised; see the log")
            self.assertNotIn(USER_ERROR_ALERT, [a.text for a in self.bot.answers()], "a handler raised; see the log")
        return result
