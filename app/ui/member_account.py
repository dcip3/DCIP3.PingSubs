"""Member 👤 Account (WP10): the account screen, the rename prompt and own-report bits.

👤 Account is a top-level screen (reply menu): no nav row. Opened as a NEW
message from a push (``public_account:show_new``) it ends with [✖️ Close].
The texts are pure builders over an ``AccountSnapshot`` so tests can pin them
without a database; ``account_snapshot`` reads the data.

This module imports only the keyboards, text and style layers (never
``app.ui.helpers``), so ``helpers`` can build its screens from it.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from aiogram import Bot
from aiogram.utils.chat_action import ChatActionSender


# ------------------------------------------------------------------- progress


@asynccontextmanager
async def typing_action(bot: Optional[Bot], chat_id: int) -> AsyncIterator[None]:
    """Show 'typing…' while a heavy reply-menu screen (a report) is built; a no-op without a bot."""
    if bot is None:
        yield
        return
    async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
        yield
