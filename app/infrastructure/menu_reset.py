"""A reply-menu tap or a /command abandons any pending text input.

Without this, a pending prompt swallows "👥 Users" or "💳 Payment" as free
text: the admin router registers the state handlers (``SubscriptionForm.name``
and friends) before the menu handlers, and a handler without a state filter
matches in any state, so whichever comes first wins.

The middleware is registered as ``dp.message.outer_middleware``. aiogram's
``FSMContextMiddleware`` is an outer middleware of ``dp.update``, so by the
time a message outer middleware runs, ``data["state"]`` and
``data["raw_state"]`` are resolved; and outer middlewares of ``dp.message``
run before any router's filters (state filters included) are checked. On a
router it would run too late: the dispatcher's own observers and earlier
routers would already have matched the state handler.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

from app.ui.keyboards import MENU_BUTTON_TEXTS
from app.ui.prompts import reset_flow_state, wind_down

logger = logging.getLogger(__name__)


def leaves_pending_input(text: Optional[str]) -> bool:
    """A reply-menu label (current or legacy) or any slash command means "do this instead"."""
    if not text:
        return False
    return text in MENU_BUTTON_TEXTS or text.startswith("/")


class MenuStateResetMiddleware(BaseMiddleware):
    """Drop a pending FSM flow before handlers run when the user leaves it by the menu.

    The flow is wound down the way its ✖️ Cancel would: the prompt, the typos
    and hints are deleted (or lose their buttons when too old), a wizard is
    retired, and the state and data are cleared (``KEPT_FLOW_DATA_KEYS``
    survive). ``data["raw_state"]`` is set to None so that state filters see
    no state and the tapped section opens as usual. A failure to clean up is
    logged and never blocks the tap.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: Optional[FSMContext] = data.get("state")
        if (
            state is not None
            and data.get("raw_state") is not None
            and isinstance(event, Message)
            and leaves_pending_input(event.text)
        ):
            await self._abandon(state, event, data)
            data["raw_state"] = None
        return await handler(event, data)

    @staticmethod
    async def _abandon(state: FSMContext, event: Message, data: dict[str, Any]) -> None:
        raw_state = data.get("raw_state")
        old_data = await reset_flow_state(state)
        bot: Optional[Bot] = data.get("bot") or event.bot
        if bot is None:
            return
        try:
            await wind_down(bot, event.chat.id, old_data)
        except Exception:
            logger.exception("Could not wind down the abandoned %s flow", raw_state)
