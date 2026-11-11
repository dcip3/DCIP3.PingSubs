"""One place for unhandled exceptions: tell the user, alert the admins, keep quiet on noise.

``setup_error_handler(dp, db)`` registers a ``dp.errors`` handler:

* ``TelegramForbiddenError`` (the user blocked the bot) and ``TelegramRetryAfter``
  (flood control) are routine: logged, nothing else.
* "message is not modified" and "query is too old" come from double taps and
  slow handlers: the tap is answered silently, nothing else.
* Anything else is a bug. The user gets a short message (or an alert on a
  button), and every admin gets an alert with the user link, the error type
  and message. Alerts are throttled to one per error kind per 10 minutes; the
  next alert of that kind says how many were held back. When the user is a
  known member, the alert carries [👤 Card], which opens the member card as a
  new message.
"""
from __future__ import annotations

import html
import logging
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Iterable, Optional

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from app.storage.db import Database
from app.ui.helpers import answer_callback_once, is_not_modified_error, is_stale_query_error
from app.ui.states import MemberAction

logger = logging.getLogger(__name__)

USER_ERROR_TEXT = "⚠️ <b>Something went wrong</b>\n\nTry again. If it keeps happening, tell the admin."
USER_ERROR_ALERT = "Something went wrong. Try again."
ADMIN_ALERT_TITLE = "🚨 <b>Bot error</b>"
CARD_BUTTON_TEXT = "👤 Card"
ERROR_MESSAGE_LIMIT = 300
ALERT_THROTTLE_SECONDS = 10 * 60

# Routine Telegram-side conditions, not bugs: never alert anybody.
_BENIGN_EXCEPTIONS = (TelegramForbiddenError, TelegramRetryAfter)
_APP_ROOT = Path(__file__).resolve().parent


def _handler_location(exception: BaseException) -> str:
    """``module:function`` of the innermost frame inside the app, e.g. ``handlers/members.py:handle_x``.

    It tells apart two handlers failing with the same exception type, while
    repeats of one failure (with different ids in the message) share a key.
    """
    location = ""
    traceback: Optional[TracebackType] = exception.__traceback__
    while traceback is not None:
        code = traceback.tb_frame.f_code
        try:
            relative = Path(code.co_filename).resolve().relative_to(_APP_ROOT)
        except ValueError:
            relative = None
        if relative is not None:
            location = f"{relative.as_posix()}:{code.co_name}"
        traceback = traceback.tb_next
    return location


class AlertThrottle:
    """One admin alert per error kind (exception type and handler) per window.

    Repeats inside the window are only counted; the next alert of that kind
    reports how many there were.
    """

    def __init__(
        self,
        window_seconds: float = ALERT_THROTTLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_seconds = window_seconds
        self._clock = clock
        self._last_sent: dict[tuple[str, str], float] = {}
        self._repeats: dict[tuple[str, str], int] = {}

    @staticmethod
    def key(exception: BaseException) -> tuple[str, str]:
        location = _handler_location(exception) or str(exception)[:100]
        return type(exception).__name__, location

    def admit(self, exception: BaseException) -> Optional[int]:
        """None: hold this alert back (counted). Otherwise send it; the value is the repeat count."""
        key = self.key(exception)
        now = self._clock()
        last = self._last_sent.get(key)
        if last is not None and now - last < self.window_seconds:
            self._repeats[key] = self._repeats.get(key, 0) + 1
            return None
        # Forget kinds that went quiet long ago, so the dicts stay small.
        for stale in [k for k, sent in self._last_sent.items() if k != key and now - sent >= self.window_seconds]:
            if not self._repeats.get(stale):
                self._last_sent.pop(stale, None)
                self._repeats.pop(stale, None)
        self._last_sent[key] = now
        return self._repeats.pop(key, 0)


def _event_user(event: ErrorEvent) -> Optional[User]:
    update = event.update
    if update.message is not None:
        return update.message.from_user
    if update.callback_query is not None:
        return update.callback_query.from_user
    return None


def _is_noise(exception: BaseException) -> bool:
    return is_not_modified_error(exception) or is_stale_query_error(exception)  # type: ignore[arg-type]


def user_link(user: User) -> str:
    """``<a href="tg://user?id=…">Name</a> · <code>id</code>`` for admin texts."""
    name = html.escape(user.full_name or str(user.id))
    return f'<a href="tg://user?id={user.id}">{name}</a> · <code>{user.id}</code>'


def admin_alert_text(user: Optional[User], exception: BaseException, repeats: int = 0) -> str:
    details = str(exception).strip()
    error = type(exception).__name__ + (f": {details}" if details else "")
    lines = [
        ADMIN_ALERT_TITLE,
        "",
        f"User: {user_link(user) if user is not None else 'unknown'}",
        f"Error: <code>{html.escape(error[:ERROR_MESSAGE_LIMIT])}</code>",
    ]
    if repeats:
        lines.append(f"Repeats in 10 min: {repeats}")
    return "\n".join(lines)


def card_button_markup(friend_id: int) -> InlineKeyboardMarkup:
    """[👤 Card]: opens the member card as a NEW message, the alert stays."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=CARD_BUTTON_TEXT,
                    callback_data=MemberAction(action="open_new", friend_id=friend_id).pack(),
                )
            ]
        ]
    )


async def _answer_callback_silently(event: ErrorEvent) -> None:
    callback = event.update.callback_query
    if callback is None:
        return
    try:
        await answer_callback_once(callback)
    except Exception:
        logger.debug("Could not answer callback %s silently", callback.id, exc_info=True)


async def _notify_user(event: ErrorEvent, bot: Bot) -> None:
    message: Optional[Message] = event.update.message
    callback: Optional[CallbackQuery] = event.update.callback_query
    try:
        if message is not None:
            if message.chat.type == ChatType.PRIVATE:
                await bot.send_message(message.chat.id, USER_ERROR_TEXT)
            return
        if callback is None:
            return
        if await answer_callback_once(callback, USER_ERROR_ALERT, alert=True):
            return
        # The handler answered the tap before it failed, or the tap is too old
        # to answer: say it in the chat instead, so the failure is not silent.
        chat = callback.message.chat if callback.message is not None else None
        if chat is not None and chat.type == ChatType.PRIVATE:
            await bot.send_message(chat.id, USER_ERROR_TEXT)
    except Exception:
        logger.warning("Could not tell the user about an unhandled error", exc_info=True)


async def _admin_ids(db: Database, fallback: Iterable[int]) -> list[int]:
    ids: list[int] = []
    try:
        ids.extend(int(admin_id) for admin_id in await db.list_admin_ids())
    except Exception:
        logger.warning("Could not load admin ids for an error alert", exc_info=True)
    ids.extend(int(admin_id) for admin_id in fallback)
    return list(dict.fromkeys(ids))


async def _card_markup(db: Database, user: Optional[User]) -> Optional[InlineKeyboardMarkup]:
    if user is None:
        return None
    try:
        friend = await db.get_friend_by_telegram(user.id)
    except Exception:
        logger.warning("Could not look the user up for an error alert", exc_info=True)
        return None
    if not friend or friend.get("id") is None:
        return None
    return card_button_markup(int(friend["id"]))


async def alert_admins(
    bot: Bot,
    db: Database,
    event: ErrorEvent,
    throttle: AlertThrottle,
    fallback_admin_ids: Iterable[int] = (),
) -> int:
    """Send the alert to every admin, best effort. Returns how many alerts went out."""
    repeats = throttle.admit(event.exception)
    if repeats is None:
        return 0
    admin_ids = await _admin_ids(db, fallback_admin_ids)
    if not admin_ids:
        return 0
    user = _event_user(event)
    text = admin_alert_text(user, event.exception, repeats)
    markup = await _card_markup(db, user)
    sent = 0
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
            sent += 1
        except Exception:
            logger.warning("Could not deliver the error alert to admin %s", admin_id, exc_info=True)
    return sent


def setup_error_handler(
    dp: Dispatcher,
    db: Database,
    *,
    admin_ids: Iterable[int] = (),
    throttle: Optional[AlertThrottle] = None,
) -> AlertThrottle:
    """Register the global ``dp.errors`` handler. ``admin_ids`` adds config admins to the DB list."""
    the_throttle = throttle or AlertThrottle()
    fallback_admin_ids = tuple(admin_ids)

    @dp.errors()
    async def handle_error(event: ErrorEvent, bot: Bot, **_: Any) -> bool:
        exception = event.exception
        update_id = getattr(event.update, "update_id", "?")
        if isinstance(exception, _BENIGN_EXCEPTIONS):
            logger.info("Ignored %s while processing update %s: %s", type(exception).__name__, update_id, exception)
            return True
        if isinstance(exception, TelegramBadRequest) and _is_noise(exception):
            logger.debug("Ignored %s while processing update %s", exception, update_id)
            await _answer_callback_silently(event)
            return True
        logger.error("Unhandled exception while processing update %s", update_id, exc_info=exception)
        await _notify_user(event, bot)
        try:
            await alert_admins(bot, db, event, the_throttle, fallback_admin_ids)
        except Exception:
            logger.exception("Could not alert admins about an unhandled error")
        # Handled: aiogram stops here instead of re-raising into the polling loop.
        return True

    return the_throttle
