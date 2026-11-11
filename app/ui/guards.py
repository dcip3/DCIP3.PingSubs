"""Double-tap guard for non-idempotent or slow callback actions.

aiogram handles updates concurrently, so two quick taps (or two admins tapping
the same push) can run one action twice. Wrap the action in ``guarded``:

    async with guarded(callback, ("topup_approve", request_id)) as acquired:
        if not acquired:
            return  # the tap was already answered with "⏳ Already running"
        ...re-check the status in the DB, act, redraw the screen...

Redraw INSIDE the block so the key is held until the screen no longer offers
the action. The set is per process; that is enough for one bot instance.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Hashable

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

ALREADY_RUNNING_TEXT = "⏳ Already running"

_in_progress: set[tuple[str, Hashable]] = set()

logger = logging.getLogger(__name__)


def is_running(key: tuple[str, Hashable]) -> bool:
    return key in _in_progress


@asynccontextmanager
async def guarded(callback: CallbackQuery | None, key: tuple[str, Hashable]) -> AsyncIterator[bool]:
    """Yield ``True`` when ``key`` (action, object id) was free and is now held.

    When the same key is already held, answer the tap with ``⏳ Already running``
    and yield ``False``; the caller must then do nothing. The key is released
    when the block exits, also on errors.
    """
    if key in _in_progress:
        if callback is not None:
            from app.ui.helpers import answer_callback_once  # helpers is the heavier module

            try:
                await answer_callback_once(callback, ALREADY_RUNNING_TEXT)
            except TelegramBadRequest as error:  # query too old: nothing to tell
                logger.debug("Could not answer a repeated tap for %s: %s", key, error)
        yield False
        return
    _in_progress.add(key)
    try:
        yield True
    finally:
        _in_progress.discard(key)


def pay_cycle_key(subscription_id: int, due_value: str, telegram_id: int) -> tuple[str, Hashable]:
    """The one guard key for "this person pays this cycle".

    The reminder's ✅ Paid and the member card's ✅ Paid record the same
    payment, so they share this key: two near-simultaneous taps on both run
    the payment (and the admin push) once.
    """
    return ("pay", (int(subscription_id), str(due_value), int(telegram_id)))
