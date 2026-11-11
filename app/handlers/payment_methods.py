"""Admin 💳 Payment section: the methods root, a method's card, the one-message
"New payment method" wizard, field edits and delete.

Screens live in app/ui/payment_screens.py. Every inline button edits the
message it sits on and answers the tap once; a method that was deleted in the
meantime redraws the root with the toast 'Payment method was deleted'. Typed
input goes through app/ui/prompts.py and, on success, redraws the method's
card with a result banner. Delete asks first; delete and the wizard's finish
run under a double-tap guard. The reply-menu handler ('💳 Payment') stays in
topups.py and calls ``show_payment_root``.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Mapping, Optional

from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.storage.db import Database
from app.ui.guards import guarded
from app.ui.helpers import (
    METHOD_GONE,
    prompt_out_of_date_text,
    answer_callback_once,
    delete_quietly,
    edit_in_place,
    is_not_modified_error,
    is_uneditable_error,
    respond_with_markup,
    stale_redirect,
)
from app.ui.keyboards import (
    PAYMENT_WIZARD_CURRENCY_PREFIX,
    PAYMENT_WIZARD_SKIP_CALLBACK,
    is_openable_payment_link,
    payment_link_clear_rows,
    payment_wizard_keyboard,
)
from app.ui.payment_screens import (
    DETAILS_HINT,
    DETAILS_MAX_LEN,
    DETAILS_TOO_LONG_HINT,
    CURRENCY_HINT,
    LINK_HINT,
    LINK_MAX_LEN,
    LINK_TEXT_HINT,
    LINK_TOO_LONG_HINT,
    METHOD_GONE_BANNER,
    TEXT_HINT,
    TITLE_MAX_LEN,
    TITLE_TOO_LONG_HINT,
    WIZARD_CURRENCY_KEY,
    WIZARD_DETAILS_KEY,
    WIZARD_TITLE_KEY,
    added_banner,
    currency_prompt_text,
    default_banner,
    deleted_banner,
    details_prompt_text,
    field_banner,
    link_prompt_text,
    method_delete_screen,
    method_detail_screen,
    payment_root_screen,
    payment_wizard_text,
    title_prompt_text,
)
from app.ui.prompts import (
    advance_wizard,
    ask,
    finish_prompt,
    finish_wizard,
    note_typo,
    origin_of,
    start_wizard,
    wizard_of,
)
from app.ui.states import PaymentDestinationAction, PaymentDestinationEditForm, PaymentDestinationForm, Responder
from app.ui.style import toast_from_html
from app.ui.text import parse_currency

from . import admin_router

logger = logging.getLogger(__name__)

EDIT_DESTINATION_ID = "edit_destination_id"
PENDING_LINK = "pending_link"  # a link that is not openable, sent once: the same text again keeps it
PROMPT_OUT_OF_DATE = prompt_out_of_date_text("💳 Payment")
STALE_STEP_TOAST = "This step is out of date"
ALREADY_DEFAULT_TOAST = "Already the default"
SKIP_WORDS = frozenset({"", "-", "skip", "none", "no"})

_WIZARD_STATES: dict[int, State] = {
    1: PaymentDestinationForm.title,
    2: PaymentDestinationForm.currency,
    3: PaymentDestinationForm.details,
    4: PaymentDestinationForm.payment_link,
}
_EDIT_STATES: dict[str, State] = {
    "title": PaymentDestinationEditForm.title,
    "currency": PaymentDestinationEditForm.currency,
    "details": PaymentDestinationEditForm.details,
    "payment_link": PaymentDestinationEditForm.payment_link,
}

# Wizards that became a method card: a late duplicate tap on their old step
# buttons must not delete the new card as a "stale step".
_FINISHED_WIZARDS: "OrderedDict[tuple[int, int], None]" = OrderedDict()
_FINISHED_WIZARDS_LIMIT = 256


def normalize_link(raw_value: str) -> str:
    """'' for the skip/clear words, otherwise the link as typed (trimmed)."""
    normalized = (raw_value or "").strip()
    return "" if normalized.lower() in SKIP_WORDS else normalized


def _wizard_data_complete(data: Mapping[str, Any]) -> bool:
    return all(data.get(key) for key in (WIZARD_TITLE_KEY, WIZARD_CURRENCY_KEY, WIZARD_DETAILS_KEY))


def _destination_id_of(data: Mapping[str, Any]) -> Optional[int]:
    try:
        return int(data.get(EDIT_DESTINATION_ID) or 0) or None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ screens


async def show_payment_root(
    target: Responder,
    db: Database,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> None:
    """The 💳 Payment root: a new message for the reply menu, an edit for a tap."""
    text, markup = await payment_root_screen(db, banner=banner)
    await respond_with_markup(target, text, reply_markup=markup, toast=toast)


async def _method_gone(target: Responder, db: Database) -> None:
    """A button or prompt for a deleted method: show the root as it is now."""
    if isinstance(target, CallbackQuery):
        await stale_redirect(target, METHOD_GONE, lambda: show_payment_root(target, db))
        return
    await show_payment_root(target, db, banner=METHOD_GONE_BANNER)


async def show_method_detail(
    target: Responder,
    db: Database,
    destination_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> None:
    screen = await method_detail_screen(db, destination_id, banner=banner)
    if screen is None:
        await _method_gone(target, db)
        return
    text, markup = screen
    await respond_with_markup(target, text, reply_markup=markup, toast=toast)


async def _show_on_origin(
    message: Message,
    data: Mapping[str, Any],
    text: str,
    markup: Optional[InlineKeyboardMarkup],
) -> None:
    """Redraw the screen a prompt came from; send the screen anew when it is gone."""
    origin = origin_of(data)
    bot = message.bot
    if origin is not None and bot is not None:
        chat_id, message_id = origin
        try:
            await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
            return
        except TelegramBadRequest as error:
            if is_not_modified_error(error):
                return
            if not is_uneditable_error(error):
                raise
            logger.debug("Origin screen %s is gone, sending a new one: %s", message_id, error)
    await message.answer(text, reply_markup=markup)


async def _show_root_on_origin(message: Message, data: Mapping[str, Any], db: Database) -> None:
    text, markup = await payment_root_screen(db, banner=METHOD_GONE_BANNER)
    await _show_on_origin(message, data, text, markup)


# ---------------------------------------------------------------- root/card


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "menu"))
async def handle_payment_methods_menu_callback(callback: CallbackQuery, db: Database) -> None:
    await show_payment_root(callback, db)


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "open"))
async def handle_payment_destination_open(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
) -> None:
    await show_method_detail(callback, db, callback_data.destination_id)


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "set_default"))
async def handle_payment_destination_set_default(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
) -> None:
    destination_id = callback_data.destination_id
    destination = await db.get_payment_destination(destination_id)
    if not destination:
        await _method_gone(callback, db)
        return
    if await db.get_default_payment_destination_id() == destination_id:
        # An old card still offering '⭐ Default' / '☆ Set default': just show it as it is.
        await show_method_detail(callback, db, destination_id, toast=ALREADY_DEFAULT_TOAST)
        return
    await db.set_default_payment_destination_id(destination_id)
    banner = default_banner(destination)
    await show_method_detail(callback, db, destination_id, banner=banner, toast=toast_from_html(banner))


# ------------------------------------------------------------------- delete


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "delete"))
async def handle_payment_destination_delete_prompt(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
) -> None:
    """Edit the card into '⚠️ <b>Delete Sber?</b>' + one consequence line."""
    screen = await method_delete_screen(db, callback_data.destination_id)
    if screen is None:
        await _method_gone(callback, db)
        return
    text, markup = screen
    await respond_with_markup(callback, text, reply_markup=markup)


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "confirm_delete"))
async def handle_payment_destination_delete(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
) -> None:
    destination_id = callback_data.destination_id
    async with guarded(callback, ("paydest_delete", destination_id)) as acquired:
        if not acquired:
            return
        destination = await db.get_payment_destination(destination_id)
        if not destination:
            await _method_gone(callback, db)
            return
        await db.delete_payment_destination(destination_id)
        banner = deleted_banner(destination)
        await show_payment_root(callback, db, banner=banner, toast=toast_from_html(banner))


# ------------------------------------------------------------------- wizard


async def _show_wizard_step(
    bot: Bot,
    state: FSMContext,
    chat_id: int,
    step: int,
    *,
    answer: Optional[Message] = None,
    hint: Optional[str] = None,
    data: Optional[dict[str, Any]] = None,
) -> None:
    """Edit the wizard into ``step``; the user's answer (or typo) is deleted."""
    update = dict(data or {})
    merged = {**await state.get_data(), **update}
    text = payment_wizard_text(merged, step, hint)
    markup = payment_wizard_keyboard(step)
    wizard_id = await advance_wizard(
        bot,
        state,
        text,
        markup,
        answer=answer,
        new_state=_WIZARD_STATES[step],
        data=update,
    )
    if wizard_id is None:
        # The state outlived the wizard message ids: show the step as a new wizard.
        sent = await bot.send_message(chat_id, text, reply_markup=markup)
        await state.set_state(_WIZARD_STATES[step])
        await state.update_data(**update, wizard_message_id=sent.message_id, wizard_chat_id=chat_id, cleanup_ids=[])
        if answer is not None:
            await delete_quietly(bot, chat_id, answer.message_id)


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "create"))
async def handle_payment_destination_create_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Step 1 as ONE new message; the previous flow is wound down."""
    text = payment_wizard_text({}, 1)
    await start_wizard(callback, state, PaymentDestinationForm.title, text, payment_wizard_keyboard(1))


@admin_router.message(PaymentDestinationForm.title)
async def handle_payment_destination_title(message: Message, state: FSMContext, bot: Bot) -> None:
    title_value = " ".join((message.text or "").split())
    chat_id = message.chat.id
    if not title_value:
        await _show_wizard_step(bot, state, chat_id, 1, answer=message, hint=TEXT_HINT)
        return
    if len(title_value) > TITLE_MAX_LEN:
        await _show_wizard_step(bot, state, chat_id, 1, answer=message, hint=TITLE_TOO_LONG_HINT)
        return
    await _show_wizard_step(bot, state, chat_id, 2, answer=message, data={WIZARD_TITLE_KEY: title_value})


@admin_router.message(PaymentDestinationForm.currency)
async def handle_payment_destination_currency(message: Message, state: FSMContext, bot: Bot) -> None:
    currency = parse_currency(message.text)
    if currency is None:
        await _show_wizard_step(bot, state, message.chat.id, 2, answer=message, hint=CURRENCY_HINT)
        return
    await _show_wizard_step(bot, state, message.chat.id, 3, answer=message, data={WIZARD_CURRENCY_KEY: currency})


async def _stale_wizard_button(callback: CallbackQuery, state: FSMContext) -> None:
    """A wizard button from a finished or abandoned wizard: say so and remove that message.

    A late double tap on the running wizard, or on the card it just became,
    is only answered.
    """
    message = callback.message
    wizard = wizard_of(await state.get_data())
    here = (message.chat.id, message.message_id) if message is not None else None
    if here is not None and (here == wizard or here in _FINISHED_WIZARDS):
        await answer_callback_once(callback)
        return
    if message is not None and callback.bot is not None:
        await delete_quietly(callback.bot, message.chat.id, message.message_id)
    await answer_callback_once(callback, STALE_STEP_TOAST)


@admin_router.callback_query(F.data.startswith(PAYMENT_WIZARD_CURRENCY_PREFIX))
async def handle_payment_wizard_currency_button(callback: CallbackQuery, state: FSMContext) -> None:
    currency = parse_currency((callback.data or "")[len(PAYMENT_WIZARD_CURRENCY_PREFIX):])
    message = callback.message
    bot = callback.bot
    if (
        currency is None
        or message is None
        or bot is None
        or await state.get_state() != PaymentDestinationForm.currency.state
        or wizard_of(await state.get_data()) != (message.chat.id, message.message_id)
    ):
        await _stale_wizard_button(callback, state)
        return
    await _show_wizard_step(bot, state, message.chat.id, 3, data={WIZARD_CURRENCY_KEY: currency})
    await answer_callback_once(callback)


@admin_router.message(PaymentDestinationForm.details)
async def handle_payment_destination_details(message: Message, state: FSMContext, bot: Bot) -> None:
    details = (message.text or "").strip()
    chat_id = message.chat.id
    if not details:
        await _show_wizard_step(bot, state, chat_id, 3, answer=message, hint=DETAILS_HINT)
        return
    if len(details) > DETAILS_MAX_LEN:
        await _show_wizard_step(bot, state, chat_id, 3, answer=message, hint=DETAILS_TOO_LONG_HINT)
        return
    await _show_wizard_step(bot, state, chat_id, 4, answer=message, data={WIZARD_DETAILS_KEY: details})


@admin_router.message(PaymentDestinationForm.payment_link)
async def handle_payment_destination_link(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    if message.text is None:
        await _show_wizard_step(bot, state, message.chat.id, 4, answer=message, hint=LINK_TEXT_HINT)
        return
    link = normalize_link(message.text)
    if len(link) > LINK_MAX_LEN:
        await _show_wizard_step(bot, state, message.chat.id, 4, answer=message, hint=LINK_TOO_LONG_HINT)
        return
    if link and not is_openable_payment_link(link) and (await state.get_data()).get(PENDING_LINK) != link:
        await _show_wizard_step(bot, state, message.chat.id, 4, answer=message, hint=LINK_HINT, data={PENDING_LINK: link})
        return
    await _finish_wizard(message, state, db, link)


@admin_router.callback_query(F.data == PAYMENT_WIZARD_SKIP_CALLBACK)
async def handle_payment_wizard_skip(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    message = callback.message
    if (
        message is None
        or await state.get_state() != PaymentDestinationForm.payment_link.state
        or wizard_of(await state.get_data()) != (message.chat.id, message.message_id)
    ):
        await _stale_wizard_button(callback, state)
        return
    await _finish_wizard(callback, state, db, "")


async def _finish_wizard(responder: Responder, state: FSMContext, db: Database, link: str) -> None:
    """Create the method once (guarded) and edit the wizard into its card with a banner."""
    callback = responder if isinstance(responder, CallbackQuery) else None
    message = callback.message if callback is not None else responder
    if not isinstance(message, Message):
        if callback is not None:
            await answer_callback_once(callback, STALE_STEP_TOAST)
        return
    bot = responder.bot or message.bot
    async with guarded(callback, ("paydest_create", message.chat.id)) as acquired:
        if not acquired:
            return
        data = await state.get_data()
        if await state.get_state() != PaymentDestinationForm.payment_link.state or not _wizard_data_complete(data):
            if callback is not None:
                await answer_callback_once(callback, STALE_STEP_TOAST)
            return
        title_value = str(data[WIZARD_TITLE_KEY])
        destination_id = await db.create_payment_destination(
            title=title_value,
            currency=str(data[WIZARD_CURRENCY_KEY]),
            details=str(data[WIZARD_DETAILS_KEY]),
            payment_link=link,
        )
        became_default = await db.get_default_payment_destination_id() is None
        if became_default:
            await db.set_default_payment_destination_id(destination_id)
        flow = await finish_wizard(state, answer=None if callback is not None else message, bot=bot)
        finished = wizard_of(flow)
        if finished is not None:
            _FINISHED_WIZARDS[finished] = None
            while len(_FINISHED_WIZARDS) > _FINISHED_WIZARDS_LIMIT:
                _FINISHED_WIZARDS.popitem(last=False)
        banner = added_banner(title_value, is_default=became_default)
        screen = await method_detail_screen(db, destination_id, banner=banner)
        if screen is None:  # deleted by another admin in the meantime
            await _method_gone(responder, db)
            return
        text, markup = screen
        if callback is not None:
            await respond_with_markup(callback, text, reply_markup=markup, toast=toast_from_html(banner))
            return
        if finished is not None and bot is not None and await edit_in_place(bot, finished[0], finished[1], text, markup):
            return
        await message.answer(text, reply_markup=markup)


# -------------------------------------------------------------- field edits


async def _start_field_edit(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    destination_id: int,
    field: str,
) -> None:
    destination = await db.get_payment_destination(destination_id)
    if not destination:
        await _method_gone(callback, db)
        return
    extra_rows: list = []
    if field == "title":
        text = title_prompt_text(destination)
    elif field == "currency":
        assigned = await db.list_payment_destination_assignees(destination_id)
        text = currency_prompt_text(destination, len(assigned))
    elif field == "details":
        text = details_prompt_text(destination)
    else:
        text = link_prompt_text(destination)
        if str(destination.get("payment_link") or "").strip():
            extra_rows = payment_link_clear_rows(destination_id)
    await ask(
        callback,
        state,
        _EDIT_STATES[field],
        text,
        origin_screen=f"paydest:{destination_id}",
        extra_rows=extra_rows,
        data={EDIT_DESTINATION_ID: destination_id},
    )


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "edit_title"))
async def handle_payment_destination_edit_title(
    callback: CallbackQuery, callback_data: PaymentDestinationAction, db: Database, state: FSMContext
) -> None:
    await _start_field_edit(callback, state, db, callback_data.destination_id, "title")


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "edit_currency"))
async def handle_payment_destination_edit_currency(
    callback: CallbackQuery, callback_data: PaymentDestinationAction, db: Database, state: FSMContext
) -> None:
    await _start_field_edit(callback, state, db, callback_data.destination_id, "currency")


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "edit_details"))
async def handle_payment_destination_edit_details(
    callback: CallbackQuery, callback_data: PaymentDestinationAction, db: Database, state: FSMContext
) -> None:
    await _start_field_edit(callback, state, db, callback_data.destination_id, "details")


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "edit_link"))
async def handle_payment_destination_edit_link(
    callback: CallbackQuery, callback_data: PaymentDestinationAction, db: Database, state: FSMContext
) -> None:
    await _start_field_edit(callback, state, db, callback_data.destination_id, "payment_link")


async def _apply_field(message: Message, state: FSMContext, db: Database, field: str, value: str) -> None:
    """Save the value, drop the prompt, redraw the method's card with a banner."""
    destination_id = _destination_id_of(await state.get_data())
    if destination_id is None:
        await finish_prompt(message, state)
        await message.answer(PROMPT_OUT_OF_DATE)
        return
    if not await db.get_payment_destination(destination_id):
        flow = await finish_prompt(message, state)
        await _show_root_on_origin(message, flow, db)
        return
    await db.update_payment_destination_fields(destination_id, **{field: value})
    flow = await finish_prompt(message, state)
    screen = await method_detail_screen(db, destination_id, banner=field_banner(field, value))
    if screen is None:
        await _show_root_on_origin(message, flow, db)
        return
    await _show_on_origin(message, flow, *screen)


@admin_router.message(PaymentDestinationEditForm.title)
async def handle_payment_destination_edit_title_input(message: Message, state: FSMContext, db: Database) -> None:
    title_value = " ".join((message.text or "").split())
    if not title_value:
        await note_typo(message, state, TEXT_HINT)
        return
    if len(title_value) > TITLE_MAX_LEN:
        await note_typo(message, state, TITLE_TOO_LONG_HINT)
        return
    await _apply_field(message, state, db, "title", title_value)


@admin_router.message(PaymentDestinationEditForm.currency)
async def handle_payment_destination_edit_currency_input(message: Message, state: FSMContext, db: Database) -> None:
    currency = parse_currency(message.text)
    if currency is None:
        await note_typo(message, state, CURRENCY_HINT)
        return
    await _apply_field(message, state, db, "currency", currency)


@admin_router.message(PaymentDestinationEditForm.details)
async def handle_payment_destination_edit_details_input(message: Message, state: FSMContext, db: Database) -> None:
    details = (message.text or "").strip()
    if not details:
        await note_typo(message, state, DETAILS_HINT)
        return
    if len(details) > DETAILS_MAX_LEN:
        await note_typo(message, state, DETAILS_TOO_LONG_HINT)
        return
    await _apply_field(message, state, db, "details", details)


@admin_router.message(PaymentDestinationEditForm.payment_link)
async def handle_payment_destination_edit_link_input(message: Message, state: FSMContext, db: Database) -> None:
    if message.text is None:
        await note_typo(message, state, LINK_TEXT_HINT)
        return
    link = normalize_link(message.text)
    if len(link) > LINK_MAX_LEN:
        await note_typo(message, state, LINK_TOO_LONG_HINT)
        return
    if link and not is_openable_payment_link(link) and (await state.get_data()).get(PENDING_LINK) != link:
        await note_typo(message, state, LINK_HINT)
        await state.update_data(**{PENDING_LINK: link})
        return
    await _apply_field(message, state, db, "payment_link", link)


@admin_router.callback_query(PaymentDestinationAction.filter(F.action == "clear_link"))
async def handle_payment_destination_clear_link(
    callback: CallbackQuery,
    callback_data: PaymentDestinationAction,
    db: Database,
    state: FSMContext,
) -> None:
    """[🗑 Clear link] on the link prompt: clear it, drop the prompt, redraw the card."""
    destination_id = callback_data.destination_id
    message = callback.message
    data = await state.get_data()
    running = (
        await state.get_state() == PaymentDestinationEditForm.payment_link.state
        and _destination_id_of(data) == destination_id
        and isinstance(message, Message)
    )
    destination = await db.get_payment_destination(destination_id)
    if not destination:
        if running:
            flow = await finish_prompt(message, state, bot=callback.bot)
            await _show_root_on_origin(message, flow, db)
            await answer_callback_once(callback, METHOD_GONE)
            return
        await _method_gone(callback, db)
        return
    await db.update_payment_destination_fields(destination_id, payment_link="")
    banner = field_banner("payment_link", "")
    screen = await method_detail_screen(db, destination_id, banner=banner)
    if screen is None:
        await _method_gone(callback, db)
        return
    if running:
        flow = await finish_prompt(message, state, bot=callback.bot)
        await _show_on_origin(message, flow, *screen)
        await answer_callback_once(callback, toast_from_html(banner))
        return
    # A prompt from an earlier flow (or from before a restart): show the card on it.
    text, markup = screen
    await respond_with_markup(callback, text, reply_markup=markup, toast=toast_from_html(banner))


__all__ = [
    "normalize_link",
    "show_method_detail",
    "show_payment_root",
]
