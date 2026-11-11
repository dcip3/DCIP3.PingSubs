"""A recording fake bot and real aiogram objects bound to it, for UI-flow tests.

``FakeBot`` is a real ``aiogram.Bot`` whose ``__call__`` never touches the
network: it records every API method and answers like Telegram would.
``fail(MethodType, "error text", message_id=...)`` makes the next matching
call raise ``TelegramBadRequest`` (or ``TelegramForbiddenError`` with
``forbidden=True``). Messages and callbacks are real aiogram objects bound to
the bot, so ``message.answer`` / ``callback.answer`` go through it too.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
)
from aiogram.methods.base import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, User

CHAT_ID = 4242
USER_ID = 4242
BOT_ID = 777
# One session for every fake bot: building its SSL context takes ~0.2 s. It never connects.
_SESSION = AiohttpSession()


class FakeBot(Bot):
    def __init__(self) -> None:
        super().__init__(token=f"{BOT_ID}:TEST", session=_SESSION)
        self.calls: list[TelegramMethod[Any]] = []
        self._failures: list[tuple[type, str, int | None, bool]] = []
        self._ids = itertools.count(1000)

    # ------------------------------------------------------------ configuration

    def fail(self, method_type: type, text: str, *, message_id: int | None = None, forbidden: bool = False) -> None:
        """The next ``method_type`` call (for ``message_id``, if given) raises."""
        self._failures.append((method_type, text, message_id, forbidden))

    # ------------------------------------------------------------------ queries

    def of(self, method_type: type) -> list[Any]:
        return [call for call in self.calls if isinstance(call, method_type)]

    def deleted_ids(self) -> list[int]:
        return [call.message_id for call in self.of(DeleteMessage)]

    def sent_texts(self) -> list[str]:
        return [call.text for call in self.of(SendMessage)]

    def answers(self) -> list[AnswerCallbackQuery]:
        return self.of(AnswerCallbackQuery)

    # --------------------------------------------------------------------- API

    async def __call__(self, method: TelegramMethod[Any], request_timeout: int | None = None) -> Any:
        self.calls.append(method)
        for index, (method_type, text, message_id, forbidden) in enumerate(self._failures):
            if not isinstance(method, method_type):
                continue
            if message_id is not None and getattr(method, "message_id", None) != message_id:
                continue
            del self._failures[index]
            if forbidden:
                raise TelegramForbiddenError(method=method, message=text)
            raise TelegramBadRequest(method=method, message=text)
        if isinstance(method, SendMessage):
            return make_message(self, next(self._ids), method.text, chat_id=int(method.chat_id), from_bot=True)
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            return True
        return True


def make_message(
    bot: Bot,
    message_id: int,
    text: str = "",
    *,
    chat_id: int = CHAT_ID,
    from_bot: bool = False,
) -> Message:
    sender = User(id=BOT_ID, is_bot=True, first_name="PingSubs") if from_bot else User(
        id=USER_ID, is_bot=False, first_name="Anna"
    )
    message = Message(
        message_id=message_id,
        date=datetime(2026, 11, 10, tzinfo=timezone.utc),
        chat=Chat(id=chat_id, type="private"),
        from_user=sender,
        text=text,
    )
    return message.as_(bot)


def make_callback(bot: Bot, message: Message | None, data: str = "x", callback_id: str | None = None) -> CallbackQuery:
    callback = CallbackQuery(
        id=callback_id or f"cb-{next(_callback_ids)}",
        from_user=User(id=USER_ID, is_bot=False, first_name="Anna"),
        chat_instance="ci",
        data=data,
        message=message,
    )
    return callback.as_(bot)


_callback_ids = itertools.count(1)


def make_state(bot: Bot, chat_id: int = CHAT_ID, user_id: int = USER_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id))


__all__ = [
    "AnswerCallbackQuery",
    "CHAT_ID",
    "DeleteMessage",
    "EditMessageReplyMarkup",
    "EditMessageText",
    "FakeBot",
    "SendMessage",
    "make_callback",
    "make_message",
    "make_state",
]
