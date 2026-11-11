"""Prompt lifecycle: ask for typed input, tolerate typos, finish or wind down.

A prompt is a small NEW message ``'✏️ <question>'`` with quick-value rows and a
single [✖️ Cancel] (``dialog:cancel``). The FSM data of a flow holds:

* ``prompt_message_id``: the prompt;
* ``origin_chat_id`` / ``origin_message_id``: the screen to redraw on success;
* ``origin_screen``: a short string such as ``'pricing:12'`` that the owning
  package's redraw callback interprets;
* ``cleanup_ids``: the user's typos and the hints sent back, deleted later;
* ``wizard_message_id`` / ``wizard_chat_id``: the one message of a wizard.

Every prompt or wizard start goes through ``reset_flow_state`` (never a bare
``state.clear()``) and winds the previous flow down, so an abandoned prompt
does not stay in the chat with a live Cancel. A flow ends by ``finish_prompt``
(success), ``cancel_prompt`` (the Cancel button) or ``wind_down`` (a menu tap
or a command left it). Everything works without FSM data too: after a restart
MemoryStorage is empty and Cancel still removes its own message.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Optional, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.ui.helpers import (
    answer_callback_once,
    delete_quietly,
    edit_in_place,
    is_not_modified_error,
    is_uneditable_error,
)
from app.ui.keyboards import cancel_button
from app.ui.states import Responder

logger = logging.getLogger(__name__)

PROMPT_MESSAGE_ID = "prompt_message_id"
ORIGIN_CHAT_ID = "origin_chat_id"
ORIGIN_MESSAGE_ID = "origin_message_id"
ORIGIN_SCREEN = "origin_screen"
CLEANUP_IDS = "cleanup_ids"
WIZARD_MESSAGE_ID = "wizard_message_id"
WIZARD_CHAT_ID = "wizard_chat_id"

PROMPT_PREFIX = "✏️ "
CANCELLED_TOAST = "Cancelled"
WIZARD_CANCELLED_TEXT = "✖️ Cancelled"

# FSM data keys that must outlive any flow reset. PingSubs has none today (it
# has no invoices or throttles); this is the one place to add such keys.
KEPT_FLOW_DATA_KEYS: frozenset[str] = frozenset()


# ------------------------------------------------------------------ the reset


async def reset_flow_state(state: FSMContext) -> dict[str, Any]:
    """Clear the FSM state and data, keep ``KEPT_FLOW_DATA_KEYS``, return the old data.

    Use it wherever a new prompt, wizard or screen flow starts, and pass the
    returned data to ``wind_down`` so the old flow's messages are cleaned up.
    """
    old_data = dict(await state.get_data())
    await state.clear()
    kept = {key: old_data[key] for key in KEPT_FLOW_DATA_KEYS if old_data.get(key) is not None}
    if kept:
        await state.update_data(**kept)
    return old_data


def _ids(values: object) -> list[int]:
    if not values:
        return []
    if isinstance(values, (int, str)):
        values = [values]
    result = []
    for value in values:  # type: ignore[union-attr]
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return result


async def _retire_wizard(bot: Bot, chat_id: int, message_id: int) -> None:
    """Delete an abandoned wizard; when that fails, leave '✖️ Cancelled' with no buttons."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        logger.debug("Could not delete wizard %s in chat %s: %s", message_id, chat_id, error)
    try:
        await edit_in_place(bot, chat_id, message_id, WIZARD_CANCELLED_TEXT, None)
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        logger.debug("Could not mark wizard %s in chat %s cancelled: %s", message_id, chat_id, error)


async def wind_down(
    bot: Bot,
    chat_id: int,
    data: Optional[Mapping[str, Any]],
    *,
    skip_message_id: Optional[int] = None,
) -> None:
    """Remove what an abandoned flow left behind: the prompt, typos, hints and wizard.

    ``data`` is the flow's FSM data (from ``reset_flow_state``). Messages that
    are gone or too old are skipped quietly. ``skip_message_id`` is left alone
    (the caller deletes it itself, e.g. the message the Cancel was tapped on).
    """
    if not data:
        return
    message_ids = [*_ids(data.get(PROMPT_MESSAGE_ID)), *_ids(data.get(CLEANUP_IDS))]
    message_ids = [message_id for message_id in message_ids if message_id != skip_message_id]
    if message_ids:
        await delete_quietly(bot, chat_id, *message_ids)
    wizard_ids = _ids(data.get(WIZARD_MESSAGE_ID))
    if wizard_ids and wizard_ids[0] != skip_message_id:
        wizard_chat = _ids(data.get(WIZARD_CHAT_ID))
        await _retire_wizard(bot, wizard_chat[0] if wizard_chat else chat_id, wizard_ids[0])


# -------------------------------------------------------------------- prompts


def _responder_message(responder: Responder) -> Message:
    message = responder.message if isinstance(responder, CallbackQuery) else responder
    if not isinstance(message, Message):
        raise ValueError("The prompt needs an accessible message to answer to")
    return message


# A tap on a message too old to answer to (InaccessibleMessage): a calm hint, not an error.
REOPEN_SCREEN_ALERT = "This message is too old. Open the screen again."


async def _inaccessible(responder: Responder) -> bool:
    """True (and the tap answered with a reopen alert) when a callback's message is too old.

    Telegram sends an InaccessibleMessage for very old messages: there is no
    message to answer the prompt to. That is not an error, so the starter
    shows a calm hint instead of crashing into the global error handler.
    """
    if isinstance(responder, CallbackQuery) and not isinstance(responder.message, Message):
        await answer_callback_once(responder, REOPEN_SCREEN_ALERT, alert=True)
        return True
    return False


def _bot_of(responder: Responder, bot: Optional[Bot]) -> Bot:
    found = bot or responder.bot
    if found is None and isinstance(responder, CallbackQuery) and responder.message is not None:
        found = responder.message.bot
    if found is None:
        raise ValueError("No bot bound to the update; pass bot=")
    return found


def prompt_text(text: str) -> str:
    """``'✏️ <question>'``; a text that already starts with the pencil is left as is."""
    return text if text.startswith("✏️") else f"{PROMPT_PREFIX}{text}"


def prompt_markup(
    extra_rows: Iterable[Sequence[InlineKeyboardButton]] = (),
) -> InlineKeyboardMarkup:
    """Quick-value rows above a single [✖️ Cancel].

    No ``force_reply``: a message carries one reply_markup, and the inline
    [✖️ Cancel] wins, so ForceReply would never reach the client.
    """
    rows = [list(row) for row in extra_rows if row]
    rows.append([cancel_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ask(
    responder: Responder,
    state: FSMContext,
    new_state: State,
    text: str,
    *,
    origin_message: Optional[Message] = None,
    origin_screen: Optional[str] = None,
    extra_rows: Iterable[Sequence[InlineKeyboardButton]] = (),
    data: Optional[Mapping[str, Any]] = None,
    bot: Optional[Bot] = None,
) -> Optional[Message]:
    """Start a prompt: wind the previous flow down, send ``'✏️ text'``, store the ids.

    For a callback the tapped message is the origin screen unless
    ``origin_message`` says otherwise, and the tap is answered (silently) when
    the call site has not answered it yet. ``data`` adds the flow's own keys
    (for example ``edit_subscription_id``). Returns the prompt message, or None
    when the tapped message is too old to answer to: the tap then gets a
    reopen alert and no state is set.
    """
    if await _inaccessible(responder):
        return None
    message = _responder_message(responder)
    the_bot = _bot_of(responder, bot)
    chat_id = message.chat.id
    if origin_message is None and isinstance(responder, CallbackQuery):
        origin_message = message
    old_data = await reset_flow_state(state)
    await wind_down(the_bot, chat_id, old_data)
    prompt = await message.answer(
        prompt_text(text),
        reply_markup=prompt_markup(extra_rows),
    )
    await state.set_state(new_state)
    await state.update_data(
        {
            **dict(data or {}),
            PROMPT_MESSAGE_ID: prompt.message_id,
            ORIGIN_CHAT_ID: origin_message.chat.id if origin_message is not None else None,
            ORIGIN_MESSAGE_ID: origin_message.message_id if origin_message is not None else None,
            ORIGIN_SCREEN: origin_screen,
            CLEANUP_IDS: [],
        }
    )
    if isinstance(responder, CallbackQuery):
        await answer_callback_once(responder)
    return prompt


async def _append_cleanup(state: FSMContext, *message_ids: Optional[int]) -> None:
    data = await state.get_data()
    cleanup = _ids(data.get(CLEANUP_IDS))
    cleanup.extend(message_id for message_id in message_ids if message_id)
    await state.update_data(**{CLEANUP_IDS: cleanup})


async def note_typo(message: Message, state: FSMContext, hint_html: str) -> Message:
    """Keep the prompt, answer a bad value with a one-line hint, remember both for cleanup."""
    hint = await message.answer(hint_html)
    await _append_cleanup(state, message.message_id, hint.message_id)
    return hint


async def finish_prompt(message: Message, state: FSMContext, *, bot: Optional[Bot] = None) -> dict[str, Any]:
    """Input accepted: delete the prompt, typos, hints and the user's message; clear the state.

    Returns the flow data so the handler can redraw the origin screen
    (``origin_chat_id`` / ``origin_message_id``) with ``edit_in_place``.
    """
    the_bot = _bot_of(message, bot)
    data = await reset_flow_state(state)
    await delete_quietly(
        the_bot,
        message.chat.id,
        *_ids(data.get(PROMPT_MESSAGE_ID)),
        *_ids(data.get(CLEANUP_IDS)),
        message.message_id,
    )
    return data


def origin_of(data: Mapping[str, Any]) -> Optional[tuple[int, int]]:
    """``(chat_id, message_id)`` of the screen the prompt came from, if stored."""
    chat_ids, message_ids = _ids(data.get(ORIGIN_CHAT_ID)), _ids(data.get(ORIGIN_MESSAGE_ID))
    if not chat_ids or not message_ids:
        return None
    return chat_ids[0], message_ids[0]


async def cancel_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """The ``dialog:cancel`` tap: wind the flow down, remove this message, toast 'Cancelled'.

    Works without FSM data (after a restart): the tapped message is still removed.
    """
    old_data = await reset_flow_state(state)
    message = callback.message
    the_bot = callback.bot or (message.bot if message is not None else None)
    if message is not None and the_bot is not None:
        await wind_down(the_bot, message.chat.id, old_data, skip_message_id=message.message_id)
        await delete_quietly(the_bot, message.chat.id, message.message_id)
    await answer_callback_once(callback, CANCELLED_TOAST)


# -------------------------------------------------------------------- wizards


async def start_wizard(
    responder: Responder,
    state: FSMContext,
    new_state: State,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    data: Optional[Mapping[str, Any]] = None,
    bot: Optional[Bot] = None,
) -> Optional[Message]:
    """Send step 1 of a one-message wizard as a new message and remember it.

    ``reply_markup`` is sent as given. Later steps go through ``advance_wizard``.
    Returns None (after a reopen alert) when the tapped message is too old.
    """
    if await _inaccessible(responder):
        return None
    message = _responder_message(responder)
    the_bot = _bot_of(responder, bot)
    old_data = await reset_flow_state(state)
    await wind_down(the_bot, message.chat.id, old_data)
    wizard = await message.answer(text, reply_markup=reply_markup)
    await state.set_state(new_state)
    await state.update_data(
        {
            **dict(data or {}),
            WIZARD_MESSAGE_ID: wizard.message_id,
            WIZARD_CHAT_ID: wizard.chat.id,
            CLEANUP_IDS: [],
        }
    )
    if isinstance(responder, CallbackQuery):
        await answer_callback_once(responder)
    return wizard


async def advance_wizard(
    bot: Bot,
    state: FSMContext,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    answer: Optional[Message] = None,
    new_state: Optional[State] = None,
    data: Optional[Mapping[str, Any]] = None,
) -> Optional[int]:
    """Edit the wizard message into the next step.

    ``answer`` is the user's typed answer: it is deleted together with the
    pending typos and hints, so the wizard stays the last message in the chat.
    When the wizard message is gone, the step is sent as a new wizard message.
    ``new_state``/``data`` move the FSM on. Returns the wizard message id, or
    None when no wizard is running (the caller should start over).
    """
    current = await state.get_data()
    wizard_ids = _ids(current.get(WIZARD_MESSAGE_ID))
    chat_ids = _ids(current.get(WIZARD_CHAT_ID))
    if not chat_ids and answer is not None:
        chat_ids = [answer.chat.id]
    if not wizard_ids or not chat_ids:
        return None
    chat_id, wizard_id = chat_ids[0], wizard_ids[0]
    cleanup = _ids(current.get(CLEANUP_IDS))
    if answer is not None:
        cleanup.append(answer.message_id)
    if cleanup:
        await delete_quietly(bot, chat_id, *cleanup)
    try:
        await bot.edit_message_text(text=text, chat_id=chat_id, message_id=wizard_id, reply_markup=reply_markup)
    except TelegramBadRequest as error:
        if is_uneditable_error(error):
            sent = await bot.send_message(chat_id, text, reply_markup=reply_markup)
            wizard_id = sent.message_id
        elif not is_not_modified_error(error):
            raise
    if new_state is not None:
        await state.set_state(new_state)
    await state.update_data({**dict(data or {}), WIZARD_MESSAGE_ID: wizard_id, CLEANUP_IDS: []})
    return wizard_id


async def finish_wizard(
    state: FSMContext,
    *,
    answer: Optional[Message] = None,
    bot: Optional[Bot] = None,
) -> dict[str, Any]:
    """Last step done: delete the user's answer, typos and hints; clear the state.

    The wizard message itself stays: the caller edits it into the result
    (for example the new subscription's card). Returns the flow data.
    """
    data = await reset_flow_state(state)
    the_bot = bot or (answer.bot if answer is not None else None)
    chat_ids = _ids(data.get(WIZARD_CHAT_ID)) or ([answer.chat.id] if answer is not None else [])
    ids = [*_ids(data.get(CLEANUP_IDS)), *([answer.message_id] if answer is not None else [])]
    if the_bot is not None and chat_ids and ids:
        await delete_quietly(the_bot, chat_ids[0], *ids)
    return data


def wizard_of(data: Mapping[str, Any]) -> Optional[tuple[int, int]]:
    """``(chat_id, message_id)`` of the running wizard message, if stored."""
    chat_ids, message_ids = _ids(data.get(WIZARD_CHAT_ID)), _ids(data.get(WIZARD_MESSAGE_ID))
    if not chat_ids or not message_ids:
        return None
    return chat_ids[0], message_ids[0]
