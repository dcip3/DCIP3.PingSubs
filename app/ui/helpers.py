from __future__ import annotations

import logging
from collections import OrderedDict
from contextvars import ContextVar
from datetime import date, datetime
from typing import Awaitable, Callable, Dict, Optional, Sequence, Tuple, Union

from aiogram import Bot

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.core.constants import (
    MONTHLY_PERIOD_SENTINEL,
    PAYMENT_MODE_FIXED,
)
from app.storage.db import Database
from app.services import normalize_payment_mode, parse_fixed_amount
from app.ui.keyboards import (
    build_members_list_keyboard,
    build_currency_keyboard,
    build_creation_payment_mode_keyboard,
    build_period_keyboard,
    build_public_subscription_list_keyboard,
    public_account_keyboard,
    build_share_limit_keyboard,
    build_subscription_list_keyboard,
    member_detail_keyboard,
    member_report_keyboard,
    pricing_settings_keyboard,
    public_subscription_detail_keyboard,
    public_subscription_report_keyboard,
    public_reply_keyboard,
    settings_tests_keyboard,
    subscription_detail_keyboard,
    tests_menu_keyboard,
)
from app.ui.states import Responder
from app.core.reminders import (
    DEFAULT_REMINDER_TIMEZONE,
    calculate_next_charge_date,
    format_offsets_for_display,
    normalize_monthly_anchor_day,
    normalize_time_string,
    normalize_timezone_name,
    parse_offsets,
    parse_time_string,
    parse_timezone,
    tg_due,
)
from app.ui.style import TOAST_MAX_LEN
from app.ui.text import escape_html, format_display_name
# Admin subscription list and card (WP3).
from app.ui.keyboards import (  # noqa: E402
    CANCEL_CALLBACK as _CANCEL_CALLBACK,
    CURRENCY_OPTIONS,
    SUBSCRIPTION_LIST_PAGE_SIZE,
    payment_method_label,
)
from app.ui.style import (  # noqa: E402
    BULLET,
    SEP,
    bullet,
    cut_visible,
    expandable,
    join_blocks,
    progress_bar,
    sub_line,
    title as screen_title,
    with_banner,
)
from app.ui.text import days_left_phrase, format_days, format_money, format_shares  # noqa: E402

logger = logging.getLogger(__name__)

MAX_TELEGRAM_MESSAGE_LEN = 3900
BLOCKQUOTE_OPEN = "<blockquote expandable>"
BLOCKQUOTE_CLOSE = "</blockquote>"


def _build_user_info_text(
    friend: Dict[str, object],
    subscriptions: Sequence[Dict[str, object]],
    *,
    include_telegram_id: bool,
    title: str,
) -> str:
    try:
        balance_value = float(friend.get("balance") or 0.0)
    except (TypeError, ValueError):
        balance_value = 0.0
    balance_currency = str(friend.get("balance_currency") or "").strip().upper() or "RUB"
    if subscriptions:
        sub_lines = "\n".join(f"• {escape_html(sub['name'])}" for sub in subscriptions)
    else:
        sub_lines = "No subscriptions yet."

    lines = [
        title,
        f"<b>{escape_html(str(friend.get('full_name') or 'Unknown'))}</b>",
    ]
    if include_telegram_id:
        telegram_id = friend.get("telegram_id")
        if telegram_id is None:
            lines.append("Telegram ID: not linked")
        else:
            lines.append(f"Telegram ID: <code>{telegram_id}</code>")
    lines.extend(
        [
            "",
            f"Balance: <b>{balance_value:.2f} {escape_html(balance_currency)}</b>",
            "",
            "Subscriptions:",
            sub_lines,
        ]
    )
    return "\n".join(lines)


def _effective_fixed_amount(subscription_amount: float, raw_value: object) -> float:
    fixed_amount = parse_fixed_amount(raw_value)
    if fixed_amount is None:
        return subscription_amount
    return fixed_amount


def _split_share_base(
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
) -> int:
    share_limit = int(subscription.get("share_limit") or 0)
    if share_limit > 0:
        return share_limit
    if not participants:
        return 1
    total_shares = sum(int(person.get("share_weight") or 1) for person in participants)
    return total_shares or 1


def _sum_users_total(
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
) -> float:
    if not participants:
        return 0.0
    payment_mode = normalize_payment_mode(subscription.get("payment_mode"))
    if payment_mode == PAYMENT_MODE_FIXED:
        subscription_amount = float(subscription.get("amount") or 0.0)
        total = 0.0
        for person in participants:
            weight = int(person.get("share_weight") or 1)
            fixed_amount = _effective_fixed_amount(subscription_amount, person.get("fixed_amount"))
            total += fixed_amount * weight
        return total

    split_share_base = _split_share_base(subscription, participants)
    per_share = float(subscription.get("amount") or 0.0) / split_share_base
    total = 0.0
    for person in participants:
        weight = int(person.get("share_weight") or 1)
        total += per_share * weight
    return total


def _on_off(value: object) -> str:
    return "<b>on</b>" if value else "<b>off</b>"


def _money(amount: float, currency: object) -> str:
    return f"<b>{amount:.2f} {escape_html(str(currency))}</b>"


def _next_charge_html(next_value: str, tz_name: str, today: date) -> str:
    """Live "next charge" date plus a relative part for charges still ahead.

    A charge due today is labelled with a static "(today)" instead of a
    relative entity, so clients never render it as "in 3 hours" / "2 hours ago".
    """
    text = tg_due(next_value, tz_name, "wD")
    try:
        due_date = datetime.strptime(next_value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return text
    if due_date < today:
        return text
    if due_date == today:
        return f"{text} (today)"
    days = (due_date - today).days
    return f"{text} ({tg_due(next_value, tz_name, 'r', f'in {days} d')})"


def _approx_money(amount: float, currency: object) -> str:
    return f"<b>≈ {amount:.2f} {escape_html(str(currency))}</b>"


def _quote(lines: Sequence[str]) -> str:
    """Wrap card lines into one plain (non-expandable) blockquote."""
    body = "\n".join(lines)
    return f"<blockquote>{body}</blockquote>"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _cadence_text(subscription: Dict[str, object]) -> str:
    if subscription["period_days"] == MONTHLY_PERIOD_SENTINEL:
        return "Every month on the same day"
    return f"Every {escape_html(str(subscription['period_days']))} days"


def _next_charge_line(next_value: str, tz_name: str, today: date) -> str:
    """Bold localized next-charge date with its distance from today.

    Future dates get a live relative entity ("in 14 d" fallback); a charge due
    today or already overdue is labelled with static text, so no client
    renders it as "in 3 hours" / "2 hours ago".
    """
    text = f"<b>{tg_due(next_value, tz_name, 'wD')}</b>"
    try:
        due_date = datetime.strptime(next_value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return text
    if due_date == today:
        return f"{text} (today)"
    days = abs((due_date - today).days)
    if due_date < today:
        return f"{text} ({days} d overdue)"
    return f"{text} ({tg_due(next_value, tz_name, 'r', f'in {days} d')})"


def _amount_line(subscription: Dict[str, object]) -> str:
    """Bold amount line, plus "· shown in X" when the conversion currency differs."""
    currency = str(subscription["currency"]).upper()
    base_currency = str(subscription.get("base_currency") or currency).strip().upper() or currency
    line = f"💰 {_money(float(subscription['amount']), currency)}"
    if base_currency != currency:
        line += f" · shown in {escape_html(base_currency)}"
    return line


def _users_summary_line(
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
    payment_mode: str,
) -> str:
    """Users count and payment mode, with the users total (fixed) or share count and value (split)."""
    users_text = f"👥 <b>{_plural(len(participants), 'user')}</b>"
    if payment_mode == PAYMENT_MODE_FIXED:
        users_total = _sum_users_total(subscription, participants)
        return f"{users_text} · fixed per user, total {_money(users_total, subscription['currency'])}"
    if not participants and not int(subscription.get("share_limit") or 0):
        return f"{users_text} · split by shares"
    split_share_base = _split_share_base(subscription, participants)
    per_person = float(subscription["amount"]) / split_share_base
    return (
        f"{users_text} · split by shares, {_plural(split_share_base, 'share')} × "
        f"{_approx_money(per_person, subscription['currency'])}"
    )


def _participants_block(
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
    payment_mode: str,
) -> str:
    if not participants:
        return "No users yet."
    subscription_amount = float(subscription.get("amount") or 0.0)
    per_person = subscription_amount / _split_share_base(subscription, participants)
    lines = []
    for person in participants:
        weight = int(person.get("share_weight") or 1)
        weight_text = f" ×{weight}" if weight > 1 else ""
        if payment_mode == PAYMENT_MODE_FIXED:
            fixed_amount = _effective_fixed_amount(subscription_amount, person.get("fixed_amount"))
            amount_text = _money(fixed_amount * weight, subscription["currency"])
        else:
            amount_text = _approx_money(per_person * weight, subscription["currency"])
        lines.append(f"• {escape_html(person['full_name'])}{weight_text} — {amount_text}")
    return "\n".join(lines)


def _users_quote(subscription: Dict[str, object], participants: Sequence[Dict[str, object]]) -> str:
    payment_mode = normalize_payment_mode(subscription.get("payment_mode"))
    return _quote(
        [
            _users_summary_line(subscription, participants, payment_mode),
            _participants_block(subscription, participants, payment_mode),
        ]
    )


def _subscription_payment_label(
    subscription: Dict[str, object],
    destination: Optional[Dict[str, object]],
) -> str:
    raw_destination_id = subscription.get("payment_destination_id")
    try:
        selected_destination_id = int(raw_destination_id) if raw_destination_id is not None else None
    except (TypeError, ValueError):
        selected_destination_id = None
    if not destination:
        return "not set"
    base = f"{destination['title']} ({destination['currency']})"
    if selected_destination_id is None:
        return f"Default ({base})"
    return base


def _payment_info_lines(
    payment_label: str,
    payment_details: str,
    payment_link: str,
    comment_value: str,
    *,
    compact: bool = False,
) -> list[str]:
    """Payment block: label, one <code> per requisite line, link and comment.

    ``compact`` is the card variant: the "Payment:"/"Comment:" words are
    dropped and a comment that merely repeats the requisites is skipped.
    """
    label_prefix = "💳 " if compact else "💳 Payment: "
    lines = [f"{label_prefix}{escape_html(payment_label)}"]
    for detail_line in payment_details.splitlines():
        detail_line = detail_line.strip()
        if detail_line:
            lines.append(f"<code>{escape_html(detail_line)}</code>")
    if payment_link:
        lines.append(f"🔗 Link: {escape_html(payment_link)}")
    if compact and " ".join(comment_value.split()) == " ".join(payment_details.split()):
        comment_value = ""
    if comment_value:
        comment_prefix = "📝 " if compact else "📝 Comment: "
        lines.append(f"{comment_prefix}{escape_html(comment_value)}")
    return lines


def _build_open_cycle_summary(
    subscription: Dict[str, object],
    due_value: str,
    today: date,
    cycle_state_map: Dict[str, Dict[str, object]],
    live_participants: Sequence[Dict[str, object]],
    paid_by_due: Dict[str, set[int]],
) -> Dict[str, object]:
    cycle_state = cycle_state_map.get(due_value) or {}
    cycle_participants = list(cycle_state.get("participants") or [])
    snapshot_ready = bool(cycle_state.get("snapshot_ready"))
    settings_snapshot_ready = bool(cycle_state.get("settings_snapshot_ready"))
    if not cycle_participants and not snapshot_ready:
        cycle_participants = list(live_participants)

    participant_ids = {
        int(person.get("telegram_id") or 0)
        for person in cycle_participants
        if int(person.get("telegram_id") or 0) > 0
    }
    users_count = len(participant_ids)
    paid_count = len(participant_ids.intersection(paid_by_due.get(due_value, set())))

    amount_source = cycle_state.get("amount") if settings_snapshot_ready else subscription.get("amount")
    try:
        amount_value = float(amount_source)
    except (TypeError, ValueError):
        amount_value = float(subscription.get("amount") or 0.0)
    currency_source = cycle_state.get("currency") if settings_snapshot_ready else subscription.get("currency")
    currency_value = str(currency_source or subscription.get("currency") or "").upper()

    return {
        "due_value": due_value,
        "amount": amount_value,
        "currency": currency_value,
        "users_count": users_count,
        "paid_count": paid_count,
        "snapshot_ready": snapshot_ready,
        "settings_snapshot_ready": settings_snapshot_ready,
    }


# ------------------------------------------------------- screens and edits
#
# One message per flow: inline buttons edit the message they sit on, and every
# callback is answered exactly once (with the toast, when there is one).

STALE_QUERY_MARKERS = ("query is too old", "query_id_invalid", "query id is invalid")
NOT_MODIFIED_MARKER = "message is not modified"
UNEDITABLE_MARKERS = (
    "message can't be edited",
    "message to edit not found",
    "there is no text in the message to edit",
    "message_id_invalid",
)
_ANSWERED_LIMIT = 4096
_answered_callbacks: "OrderedDict[str, None]" = OrderedDict()
# Set by stale_redirect so that the redraw's own answer carries the toast.
_pending_toast: ContextVar[Optional[Tuple[str, str, bool]]] = ContextVar("pending_toast", default=None)

SUBSCRIPTION_GONE = "Subscription was deleted"
USER_GONE = "User was deleted"
METHOD_GONE = "Payment method was deleted"
REQUEST_HANDLED = "Request already handled"
CYCLE_CLOSED = "This cycle is already closed"
NO_ACCESS = "You are no longer in this subscription"
# A malformed or retired button: one toast, plus a redraw of the parent when one is known.
DEAD_BUTTON = "This button no longer works"
def prompt_out_of_date_text(section: str = "📋 Subscriptions") -> str:
    """The one wording for an answer to a prompt whose flow data is gone (e.g. after a restart)."""
    return f"⌛️ This prompt is out of date. Open “{section}” again."


PROMPT_OUT_OF_DATE_TEXT = prompt_out_of_date_text()


class StaleTexts:
    """Toasts for a button pressed on an outdated screen (not an error)."""

    SUBSCRIPTION_GONE = SUBSCRIPTION_GONE
    USER_GONE = USER_GONE
    METHOD_GONE = METHOD_GONE
    REQUEST_HANDLED = REQUEST_HANDLED
    CYCLE_CLOSED = CYCLE_CLOSED
    NO_ACCESS = NO_ACCESS
    DEAD_BUTTON = DEAD_BUTTON


STALE_TEXTS = StaleTexts


def _error_text(error: Exception) -> str:
    return str(error).lower()


def is_stale_query_error(error: Exception) -> bool:
    """A second answer, or an answer after ~15 s: nothing left to tell the user."""
    return isinstance(error, TelegramBadRequest) and any(m in _error_text(error) for m in STALE_QUERY_MARKERS)


def is_not_modified_error(error: Exception) -> bool:
    return isinstance(error, TelegramBadRequest) and NOT_MODIFIED_MARKER in _error_text(error)


def is_uneditable_error(error: Exception) -> bool:
    """The message is gone, too old, or not a text message: send a new one instead."""
    return isinstance(error, TelegramBadRequest) and any(m in _error_text(error) for m in UNEDITABLE_MARKERS)


def _remember_answered(callback_id: str) -> None:
    _answered_callbacks[callback_id] = None
    while len(_answered_callbacks) > _ANSWERED_LIMIT:
        _answered_callbacks.popitem(last=False)


def callback_was_answered(callback: CallbackQuery) -> bool:
    return callback.id in _answered_callbacks


async def answer_callback_once(
    callback: CallbackQuery,
    text: Optional[str] = None,
    *,
    alert: bool = False,
) -> bool:
    """Answer a tap at most once; later calls for the same callback do nothing.

    ``text`` is the toast (cut to 200 characters); ``alert=True`` shows it as a
    blocking alert. A pending toast set by ``stale_redirect`` is used when no
    text is given. An answer that comes too late, or after the call site
    answered by itself, is ignored. Returns True when this call answered.
    """
    if callback_was_answered(callback):
        return False
    if text is None:
        pending = _pending_toast.get()
        if pending is not None and pending[0] == callback.id:
            _, text, alert = pending
    _remember_answered(callback.id)
    toast = text[:TOAST_MAX_LEN] if text else None
    try:
        await callback.answer(toast, show_alert=bool(alert and toast))
    except TelegramBadRequest as error:
        if not is_stale_query_error(error):
            raise
        logger.debug("Callback %s was already answered or is too old: %s", callback.id, error)
        return False
    return True


async def respond_with_markup(
    target: Responder,
    text: str,
    *,
    reply_markup: Optional[Union[InlineKeyboardMarkup, ReplyKeyboardMarkup]] = None,
    toast: Optional[str] = None,
    alert: bool = False,
) -> Optional[Message]:
    """Show a screen: edit the tapped message, or answer a typed one.

    For a callback the message is edited in place ("not modified" is fine); a
    message that can no longer be edited gets the screen as a new message. The
    callback is then answered exactly once, with ``toast`` (``alert=True`` for
    a blocking problem). Returns the message that shows the screen, if known.
    """
    if not isinstance(target, CallbackQuery):
        return await target.answer(text, reply_markup=reply_markup)
    shown: Optional[Message] = None
    message = target.message
    if isinstance(message, Message):
        try:
            result = await message.edit_text(text, reply_markup=reply_markup)
            shown = result if isinstance(result, Message) else message
        except TelegramBadRequest as error:
            if is_not_modified_error(error):
                shown = message
            elif is_uneditable_error(error):
                shown = await message.answer(text, reply_markup=reply_markup)
            else:
                raise
    elif message is not None and target.bot is not None:
        # Too old to touch (InaccessibleMessage), but the chat is still known.
        shown = await target.bot.send_message(message.chat.id, text, reply_markup=reply_markup)
    await answer_callback_once(target, toast, alert=alert)
    return shown


# Hub cards opened as a NEW message from a push, alert or report end in
# [✖️ Close] (push-buttons-open-new). Their sub-screens edit that same message
# and come back through the plain "To card" callbacks, so the copy is
# remembered by (chat id, message id): a redraw of a remembered message keeps
# [✖️ Close] instead of offering [◀️ To list] (which would turn the spawned
# copy into the list). The memory is per process and bounded; after a restart
# the copy falls back to the list nav.
_SPAWNED_SCREENS: "OrderedDict[tuple[int, int], None]" = OrderedDict()
SPAWNED_SCREENS_MAX = 5000


def remember_spawned(message: object) -> None:
    """Remember a card copy just sent as a new message (from a push, alert or report)."""
    if not isinstance(message, Message):
        return
    key = (int(message.chat.id), int(message.message_id))
    _SPAWNED_SCREENS[key] = None
    _SPAWNED_SCREENS.move_to_end(key)
    while len(_SPAWNED_SCREENS) > SPAWNED_SCREENS_MAX:
        _SPAWNED_SCREENS.popitem(last=False)


def is_spawned_screen(chat_id: Optional[int], message_id: Optional[int]) -> bool:
    if chat_id is None or message_id is None:
        return False
    return (int(chat_id), int(message_id)) in _SPAWNED_SCREENS


def spawned_screen_of(target: object) -> bool:
    """True when a tap came from a remembered card copy (or one of its sub-screens)."""
    message = target.message if isinstance(target, CallbackQuery) else None
    if message is None:
        return False
    return is_spawned_screen(message.chat.id, message.message_id)


async def edit_in_place(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    """Edit a known message (the screen a prompt came from) without any fallback.

    Returns True when the message now shows ``text``: it was edited, or it
    already showed exactly this ("message is not modified", for example after
    saving the same value twice). Returns False only when the message cannot
    be edited any more (deleted, too old); the caller may then send a new
    screen. Any other bad request (broken HTML, text over 4096) is a bug and
    is raised.
    """
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as error:
        if is_not_modified_error(error):
            logger.debug("Message %s in chat %s already up to date", message_id, chat_id)
            return True
        if is_uneditable_error(error):
            logger.debug("Message %s in chat %s not edited: %s", message_id, chat_id, error)
            return False
        logger.warning("Could not edit message %s in chat %s: %s", message_id, chat_id, error)
        raise
    return True


async def safe_edit_text(
    message: Message,
    text: str,
    reply_markup: Optional[Union[InlineKeyboardMarkup, ReplyKeyboardMarkup]] = None,
) -> Message:
    """Edit ``message``; when it cannot be edited, send the text as a new message.

    Returns the message that now shows the text. A ``ReplyKeyboardMarkup`` can
    only travel with a new message, so it always takes the new-message path.
    """
    if isinstance(reply_markup, ReplyKeyboardMarkup):
        return await message.answer(text, reply_markup=reply_markup)
    try:
        result = await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as error:
        if is_not_modified_error(error):
            return message
        if is_uneditable_error(error):
            return await message.answer(text, reply_markup=reply_markup)
        raise
    return result if isinstance(result, Message) else message


async def delete_quietly(bot: Bot, chat_id: int, *message_ids: Optional[int]) -> None:
    """Delete messages, ignoring the ones that are gone or not ours to delete.

    A message older than 48 hours cannot be deleted; it loses its buttons
    instead, so nothing on it invites another tap. Empty ids are skipped and
    duplicates are handled once.
    """
    seen: set[int] = set()
    for raw_id in message_ids:
        if not raw_id:
            continue
        message_id = int(raw_id)
        if message_id in seen:
            continue
        seen.add(message_id)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            continue
        except TelegramForbiddenError:
            continue
        except TelegramBadRequest as error:
            if "not found" in _error_text(error):
                continue
            logger.debug("Could not delete message %s in chat %s: %s", message_id, chat_id, error)
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            logger.debug("Could not strip buttons from message %s in chat %s: %s", message_id, chat_id, error)


async def stale_redirect(
    callback: CallbackQuery,
    toast: str,
    redraw: Union[Awaitable[object], Callable[[], Awaitable[object]]],
    *,
    alert: bool = False,
) -> None:
    """Redraw the current state for a button pressed on an outdated screen.

    ``redraw`` (a coroutine, or a no-argument callable returning one) redraws
    the parent screen, for example the list after the object was deleted. The
    tap is answered once with ``toast`` ("Subscription was deleted"): by the
    redraw itself when it goes through ``respond_with_markup``, otherwise here.
    """
    token = _pending_toast.set((callback.id, toast, alert))
    try:
        await (redraw() if callable(redraw) else redraw)
    finally:
        _pending_toast.reset(token)
    await answer_callback_once(callback, toast, alert=alert)


def _slice_line(line: str, limit: int) -> list[str]:
    """Last resort for ONE line longer than the limit: slice it, never inside a tag."""
    parts: list[str] = []
    remaining = line
    while len(remaining) > limit:
        cut = limit
        opening = remaining.rfind("<", 0, cut)
        if opening > remaining.rfind(">", 0, cut) and opening > 0:
            cut = opening  # do not end a piece inside '<tag …'
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        parts.append(remaining)
    return parts


def _split_plain_block(block: str, limit: int) -> list[str]:
    """Split an oversized block on line boundaries, so no line (and no tag in it) is cut.

    Report lines carry inline tags such as ``<b><tg-time …>…</tg-time></b>``;
    cutting inside a line would leave both halves with broken HTML. Only a
    single line longer than the limit is sliced, and never inside a tag.
    """
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in block.split("\n"):
        extra = len(line) + (1 if current else 0)
        if current and size + extra > limit:
            parts.append("\n".join(current))
            current, size = [], 0
            extra = len(line)
        if len(line) > limit:
            parts.extend(_slice_line(line, limit))
            continue
        current.append(line)
        size += extra
    if current:
        parts.append("\n".join(current))
    return parts


def _split_pre_block(block: str, limit: int) -> list[str]:
    if "<pre>" not in block or "</pre>" not in block:
        return _split_plain_block(block, limit)

    prefix, suffix_part = block.split("<pre>", 1)
    pre_content, suffix = suffix_part.split("</pre>", 1)
    head = f"{prefix}<pre>"
    tail = f"</pre>{suffix}"
    available = limit - len(head) - len(tail)
    if available <= 0:
        return _split_plain_block(block, limit)

    chunks: list[str] = []
    current_lines: list[str] = []
    for line in pre_content.splitlines():
        candidate_lines = current_lines + [line]
        candidate = head + "\n".join(candidate_lines) + tail
        if len(candidate) <= limit:
            current_lines = candidate_lines
            continue

        if current_lines:
            chunks.append(head + "\n".join(current_lines) + tail)
            current_lines = []

        if len(line) <= available:
            current_lines = [line]
            continue

        pieces = _split_plain_block(line, available)
        for piece in pieces:
            chunks.append(head + piece + tail)

    if current_lines:
        chunks.append(head + "\n".join(current_lines) + tail)
    return chunks


def _merge_blockquote_blocks(blocks: Sequence[str]) -> list[str]:
    """Re-join blank-line separated pieces that belong to one <blockquote>.

    A quote must never be cut in the middle, or the HTML of both halves breaks;
    report builders keep each quote under the message limit for this reason.
    """
    merged: list[str] = []
    depth = 0
    for block in blocks:
        if depth > 0 and merged:
            merged[-1] = f"{merged[-1]}\n\n{block}"
        else:
            merged.append(block)
        depth = max(0, depth + block.count("<blockquote") - block.count(BLOCKQUOTE_CLOSE))
    return merged


def split_text_chunks(text: str, limit: int = MAX_TELEGRAM_MESSAGE_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]

    blocks = _merge_blockquote_blocks(text.split("\n\n"))
    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(block) <= limit:
            current = block
            continue

        chunks.extend(_split_pre_block(block, limit))

    if current:
        chunks.append(current)
    return chunks or [text]


async def send_chunked_responder_text(
    target: Responder,
    text: str,
    *,
    reply_markup: Optional[Union[InlineKeyboardMarkup, ReplyKeyboardMarkup]] = None,
) -> None:
    chunks = split_text_chunks(text)
    if isinstance(target, CallbackQuery):
        if not target.message:
            await answer_callback_once(target, "Could not show the report. Try again.", alert=True)
            return
        if len(chunks) == 1:
            inline_markup = reply_markup if isinstance(reply_markup, InlineKeyboardMarkup) else None
            await respond_with_markup(target, chunks[0], reply_markup=inline_markup)
            return

        await respond_with_markup(target, chunks[0], reply_markup=None)
        for chunk in chunks[1:-1]:
            await target.message.answer(chunk)
        await target.message.answer(chunks[-1], reply_markup=reply_markup)
        return

    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        await target.answer(chunk, reply_markup=markup)


SUBSCRIPTION_GONE_BANNER = "⚠️ That subscription was deleted"
SUBSCRIPTION_LIST_HINT = "Tap a subscription to manage it."
SUBSCRIPTION_LIST_EMPTY = "No subscriptions yet."


async def _open_cycle_states(
    db: Database,
    subscription: Dict[str, object],
    live_participants: Sequence[Dict[str, object]],
) -> list[Dict[str, object]]:
    """Every open cycle of a subscription, oldest first, with who has paid.

    Each item: ``due_value``, ``due`` (a date or None), ``paid``, ``total`` and
    ``unpaid_names`` (display names, sorted). Cycle participants come from the
    cycle snapshot, or from the live list while no snapshot exists.
    """
    subscription_id = int(subscription["id"])
    open_cycles = await db.list_open_cycles(subscription_id)
    if not open_cycles:
        return []
    state_map = await db.list_cycle_participants_for_due_dates(subscription_id, open_cycles)
    paid_by_due: Dict[str, set[int]] = {}
    for row in await db.list_payments_for_cycles(subscription_id, open_cycles):
        payer_id = row.get("paid_by_telegram_id")
        if payer_id is not None:
            paid_by_due.setdefault(str(row.get("due_date") or ""), set()).add(int(payer_id))
    states: list[Dict[str, object]] = []
    for due_value in open_cycles:
        names_by_id: Dict[int, str] = {}
        for person in _resolve_report_cycle_participants(state_map.get(due_value) or {}, live_participants):
            telegram_id = int(person.get("telegram_id") or 0)
            if telegram_id > 0:
                names_by_id[telegram_id] = str(person.get("full_name") or f"User {telegram_id}")
        paid_ids = set(names_by_id) & paid_by_due.get(due_value, set())
        unpaid_ids = sorted(set(names_by_id) - paid_ids, key=lambda tid: names_by_id[tid].lower())
        try:
            due: Optional[date] = datetime.strptime(due_value, "%Y-%m-%d").date()
        except ValueError:
            due = None
        states.append(
            {
                "due_value": due_value,
                "due": due,
                "paid": len(paid_ids),
                "total": len(names_by_id),
                "unpaid_names": [format_display_name(names_by_id[tid]) for tid in unpaid_ids],
            }
        )
    return states


def _has_unpaid_due(cycles: Sequence[Dict[str, object]], today: date) -> bool:
    """A cycle due today or earlier that someone has not paid: the subscription needs attention."""
    return any(
        cycle["due"] is not None and cycle["due"] <= today and cycle["unpaid_names"]  # type: ignore[operator]
        for cycle in cycles
    )


def _build_subscription_list_text(total: int, unpaid: int, no_participants: int) -> str:
    """``'📋 <b>Subscriptions</b> · 4'``, the attention counts (only when non-zero), the hint."""
    if total <= 0:
        return join_blocks(screen_title("📋", "Subscriptions"), SUBSCRIPTION_LIST_EMPTY)
    attention = [
        f"⚠️ Unpaid: <b>{unpaid}</b>" if unpaid else None,
        f"👥 No participants: <b>{no_participants}</b>" if no_participants else None,
    ]
    return join_blocks(
        screen_title("📋", "Subscriptions", total),
        [line for line in attention if line],
        SUBSCRIPTION_LIST_HINT,
    )


def _sort_subscription_rows(rows: Sequence[Dict[str, object]]) -> list[Dict[str, object]]:
    """Unpaid first, then by name."""
    return sorted(rows, key=lambda row: (0 if row.get("unpaid") else 1, str(row.get("name") or "").lower()))


async def send_subscription_list(
    target: Responder,
    db: Database,
    *,
    page: Optional[int] = None,
    focus_subscription_id: Optional[int] = None,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """The admin subscription list (a top-level screen: no Back/Close row).

    ``focus_subscription_id`` opens the page that holds that subscription (the
    way back from its card); ``banner`` puts a result line on top.
    """
    subs = await db.list_subscriptions()
    if not subs:
        return await respond_with_markup(
            target,
            with_banner(banner, _build_subscription_list_text(0, 0, 0)),
            reply_markup=build_subscription_list_keyboard([]),
            toast=toast,
        )

    today = datetime.now(parse_timezone(await resolve_report_timezone(db))).date()
    rows: list[Dict[str, object]] = []
    for sub in subs:
        row = dict(sub)
        if int(sub.get("participant_count") or 0):
            participants = await db.list_subscription_participants(int(sub["id"]))
            row["unpaid"] = _has_unpaid_due(await _open_cycle_states(db, sub, participants), today)
        else:
            row["unpaid"] = False
        rows.append(row)
    rows = _sort_subscription_rows(rows)

    total_pages = max(1, -(-len(rows) // SUBSCRIPTION_LIST_PAGE_SIZE))
    if focus_subscription_id is not None and page is None:
        index = next((i for i, row in enumerate(rows) if int(row["id"]) == int(focus_subscription_id)), 0)
        page = index // SUBSCRIPTION_LIST_PAGE_SIZE + 1
    page = min(max(int(page or 1), 1), total_pages)
    start = (page - 1) * SUBSCRIPTION_LIST_PAGE_SIZE

    text = _build_subscription_list_text(
        len(rows),
        sum(1 for row in rows if row["unpaid"]),
        sum(1 for row in rows if not int(row.get("participant_count") or 0)),
    )
    return await respond_with_markup(
        target,
        with_banner(banner, text),
        reply_markup=build_subscription_list_keyboard(
            rows[start:start + SUBSCRIPTION_LIST_PAGE_SIZE],
            page=page,
            total_pages=total_pages,
        ),
        toast=toast,
    )


async def show_subscription_gone(target: Responder, db: Database) -> None:
    """A subscription that no longer exists: back to the list, never a dead end.

    A tap redraws the list with the toast 'Subscription was deleted'; a typed
    message gets the list with the banner '⚠️ That subscription was deleted'.
    """
    if isinstance(target, CallbackQuery):
        await stale_redirect(target, SUBSCRIPTION_GONE, lambda: send_subscription_list(target, db))
        return
    await send_subscription_list(target, db, banner=SUBSCRIPTION_GONE_BANNER)


async def send_user_subscription_list(message: Message, db: Database, telegram_id: int) -> None:
    subs = await db.list_subscriptions_for_user(telegram_id)
    if not subs:
        await message.answer("📋 Subscriptions:\nYou don't have any subscriptions yet.")
        return

    base_tz = await resolve_report_timezone(db)
    tz_name = normalize_timezone_name(
        await db.get_effective_user_timezone(telegram_id, base_tz),
        base_tz,
    ) or base_tz
    today = datetime.now(parse_timezone(tz_name)).date()
    lines = ["📋 Subscriptions:", "Your plans:", ""]
    for idx, sub in enumerate(subs, 1):
        next_value = str(sub["next_charge_at"])
        lines.append(
            f"{idx}. <b>{escape_html(sub['name'])}</b> — next charge {_next_charge_html(next_value, tz_name, today)}"
        )
    await message.answer(
        "\n".join(lines),
        reply_markup=build_public_subscription_list_keyboard(subs),
    )


async def send_public_subscription_detail(
    target: Responder,
    db: Database,
    subscription_id: int,
) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return

    participants = await db.list_subscription_participants(subscription_id)
    open_cycles = await db.list_open_cycles(subscription_id)
    unpaid_overdue = []
    paid_due_map = set()
    user_id = None
    if isinstance(target, CallbackQuery) and target.from_user:
        user_id = target.from_user.id
    elif isinstance(target, Message) and target.from_user:
        user_id = target.from_user.id
    is_admin_view = bool(user_id is not None and await db.is_admin(user_id))

    admin_timezone = normalize_timezone_name(
        await db.get_effective_base_timezone(DEFAULT_REMINDER_TIMEZONE),
        DEFAULT_REMINDER_TIMEZONE,
    ) or DEFAULT_REMINDER_TIMEZONE
    reminder_timezone = admin_timezone
    if user_id is not None:
        reminder_timezone = normalize_timezone_name(
            await db.get_effective_user_timezone(user_id, admin_timezone),
            admin_timezone,
        ) or admin_timezone
    today = datetime.now(parse_timezone(reminder_timezone)).date()

    if user_id is not None:
        payments_for_open = await db.list_payments_for_cycles(subscription_id, open_cycles)
        paid_due_map = {
            (row["due_date"], row["paid_by_telegram_id"])
            for row in payments_for_open
            if row.get("paid_by_telegram_id") is not None
        }
        for due_value in open_cycles:
            try:
                due_date = datetime.strptime(due_value, "%Y-%m-%d").date()
            except ValueError:
                continue
            if due_date < today and (due_value, user_id) not in paid_due_map:
                unpaid_overdue.append(due_value)

    next_charge_value = str(subscription["next_charge_at"])
    if user_id is not None and (next_charge_value, user_id) in paid_due_map:
        period_days = int(subscription.get("period_days") or 30)
        monthly_anchor_day = normalize_monthly_anchor_day(subscription.get("monthly_anchor_day"))
        try:
            current_due = datetime.strptime(next_charge_value, "%Y-%m-%d").date()
            next_charge_value = calculate_next_charge_date(
                current_due,
                period_days,
                monthly_anchor_day,
            ).isoformat()
        except ValueError:
            pass
    payment_mode = normalize_payment_mode(subscription.get("payment_mode"))
    split_share_base = _split_share_base(subscription, participants)
    per_person = subscription["amount"] / split_share_base
    person_by_telegram: dict[int, Dict[str, object]] = {
        int(person["telegram_id"]): person
        for person in participants
        if person.get("telegram_id") is not None
    }

    admin_base_time = parse_time_string(
        await db.get_effective_base_reminder_time(),
    ).strftime("%H:%M")
    subscription_override = normalize_time_string(subscription.get("reminder_time"))
    user_sub_override = None
    user_base_time = admin_base_time
    if user_id is not None:
        user_sub_override = normalize_time_string(
            await db.get_user_subscription_setting(user_id, subscription_id, "reminder_time")
        )
        user_base_time = parse_time_string(
            await db.get_effective_user_base_reminder_time(user_id, admin_base_time),
            admin_base_time,
        ).strftime("%H:%M")
    default_reminder_time = subscription_override or user_base_time
    reminder_time_display = (
        f"<code>{escape_html(user_sub_override or default_reminder_time)}</code> ({escape_html(reminder_timezone)})"
    )
    if not user_sub_override:
        reminder_time_display += " · default"

    default_target_currency = str(
        subscription.get("base_currency") or subscription.get("currency") or "RUB"
    ).strip().upper()
    user_target_currency = ""
    if user_id is not None:
        user_target_currency = str(
            await db.get_user_subscription_setting(
                user_id,
                subscription_id,
                "target_currency",
            ) or ""
        ).strip().upper()
    if user_target_currency:
        my_currency_display = user_target_currency
    else:
        my_currency_display = f"{default_target_currency} (default)"

    my_amount_display = "n/a"
    if user_id is not None:
        current_person = person_by_telegram.get(int(user_id))
        if current_person:
            weight = int(current_person.get("share_weight") or 1)
            if payment_mode == PAYMENT_MODE_FIXED:
                fixed_amount = _effective_fixed_amount(
                    float(subscription.get("amount") or 0.0),
                    current_person.get("fixed_amount"),
                )
                my_amount_display = _money(fixed_amount * weight, subscription["currency"])
            else:
                my_amount_display = _money(per_person * weight, subscription["currency"])
    comment_value = (subscription.get("comment") or "").strip()
    payment_destination = await db.get_effective_subscription_payment_destination(subscription_id)
    payment_label = _subscription_payment_label(subscription, payment_destination)
    payment_details = str(payment_destination.get("details") or "").strip() if payment_destination else ""
    payment_link = str(payment_destination.get("payment_link") or "").strip() if payment_destination else ""

    # Member card: title, then one blockquote per section; admin viewers also
    # get the subscription amount, the users block and the reminder days.
    summary_lines = []
    if is_admin_view:
        summary_lines.append(_amount_line(subscription))
    summary_lines.extend(
        [
            f"💵 My amount: {my_amount_display} · reminders in {escape_html(my_currency_display)}",
            f"📅 Next charge: {_next_charge_line(next_charge_value, reminder_timezone, today)}",
            f"🔁 {_cadence_text(subscription)}",
        ]
    )
    quotes = [_quote(summary_lines)]
    if is_admin_view:
        quotes.append(_users_quote(subscription, participants))
    if unpaid_overdue:
        quotes.append(
            _quote(
                ["⚠️ Overdue", *(f"• <b>{tg_due(value, reminder_timezone, 'wD')}</b>" for value in unpaid_overdue)]
            )
        )
    reminder_line = f"⏰ {reminder_time_display}"
    if is_admin_view:
        offsets_text = format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))
        reminder_line += (
            f" · {escape_html(offsets_text)} · after due: {_on_off(subscription.get('remind_after_due'))}"
        )
    quotes.append(_quote([reminder_line]))
    quotes.append(
        _quote(_payment_info_lines(payment_label, payment_details, payment_link, comment_value, compact=True))
    )
    text = "\n".join([f"<b>🏷️ {escape_html(subscription['name'])}</b>", "", *quotes])
    await respond_with_markup(
        target,
        text,
        reply_markup=public_subscription_detail_keyboard(subscription_id),
    )


async def send_subscription_payment_report(
    target: Responder,
    db: Database,
    subscription_id: int,
) -> None:
    """'📊 <b>Reports · Netflix</b>' (WP8, app/ui/report_screens.py)."""
    from app.ui.report_screens import send_subscription_report  # it imports this module

    await send_subscription_report(target, db, subscription_id)


async def send_subscription_open_cycles(
    target: Responder,
    db: Database,
    subscription_id: int,
    notice: Optional[str] = None,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """'🗂 <b>Open cycles · Netflix</b> · 2' (WP8); ``notice`` is the old name of ``banner``."""
    from app.ui.report_screens import send_open_cycles  # it imports this module

    return await send_open_cycles(target, db, subscription_id, banner=banner or notice, toast=toast)


async def send_subscription_cycle_actions(
    target: Responder,
    db: Database,
    subscription_id: int,
    due_value: str,
    notice: Optional[str] = None,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """'🗂 <b>Cycle Thu, 01.10 · Netflix</b>' (WP8); ``notice`` is the old name of ``banner``."""
    from app.ui.report_screens import send_cycle_actions  # it imports this module

    return await send_cycle_actions(target, db, subscription_id, due_value, banner=banner or notice, toast=toast)


async def send_public_subscription_payment_report(
    target: Responder,
    db: Database,
    subscription_id: int,
    telegram_id: int,
) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return

    subs_for_user = await db.list_subscriptions_for_user(telegram_id)
    if not any(sub["id"] == subscription_id for sub in subs_for_user):
        await respond_with_markup(target, "You don't have access to this subscription.")
        return

    participants = await db.list_subscription_participants(subscription_id)
    user = next((p for p in participants if p["telegram_id"] == telegram_id), None)
    if not user:
        await respond_with_markup(target, "You don't have access to this subscription.")
        return
    text = await _build_subscription_payment_report_text(
        db,
        subscription,
        participants,
        scope="user",
        user_id=telegram_id,
        tz_name=await resolve_report_timezone(db),
    )
    await send_chunked_responder_text(
        target,
        assemble_payments_report([text]),
        reply_markup=public_subscription_report_keyboard(subscription_id),
    )


# A member overview collapses its on-track tail once it lists more than four subscriptions.
MEMBER_REPORT_COLLAPSE_MIN_TOTAL = 5
# An admin overview collapses its on-track tail once three or more subscriptions are green.
ADMIN_REPORT_COLLAPSE_MIN_GREEN = 3


async def send_public_user_payment_report(
    message: Message,
    db: Database,
    telegram_id: int,
) -> None:
    blocks = await _build_user_payment_blocks(db, telegram_id, await resolve_report_timezone(db))
    if not blocks:
        await message.answer("No payments to report yet.")
        return
    await send_chunked_responder_text(
        message,
        assemble_payments_report(blocks, collapse_min_total=MEMBER_REPORT_COLLAPSE_MIN_TOTAL),
    )


async def _build_user_payment_blocks(
    db: Database,
    telegram_id: int,
    tz_name: Optional[str] = DEFAULT_REMINDER_TIMEZONE,
) -> list[str]:
    subs = await db.list_subscriptions_for_user(telegram_id)
    if not subs:
        return []
    blocks: list[str] = []
    for sub in subs:
        participants = await db.list_subscription_participants(sub["id"])
        user = next((p for p in participants if p["telegram_id"] == telegram_id), None)
        if not user:
            continue
        block = await _build_subscription_payment_report_text(
            db,
            sub,
            participants,
            scope="user",
            user_id=telegram_id,
            tz_name=tz_name,
        )
        blocks.append(block)
    return blocks


async def resolve_report_timezone(db: Database) -> str:
    """The base timezone that decides which calendar day counts as "today" in reports."""
    return normalize_timezone_name(
        await db.get_effective_base_timezone(DEFAULT_REMINDER_TIMEZONE),
        DEFAULT_REMINDER_TIMEZONE,
    ) or DEFAULT_REMINDER_TIMEZONE


def payments_report_header(attention_count: int, title: str = "Reports") -> str:
    """``'📊 <b>Reports</b> · 1 needs attention'``; ``title`` (HTML) names the scope:
    ``'Reports · Netflix'`` or ``'Reports · Anna Petrova'``."""
    fact = ""
    if attention_count > 0:
        verb = "needs" if attention_count == 1 else "need"
        fact = f"{attention_count} {verb} attention"
    return screen_title("📊", title, fact)


def _is_attention_block(block: str) -> bool:
    return block.startswith("🔴")


def _collapse_report_blocks(blocks: Sequence[str], limit: int = MAX_TELEGRAM_MESSAGE_LEN) -> list[str]:
    """Wrap on-track blocks in expandable quotes, each small enough to stay whole."""

    def wrap(items: Sequence[str]) -> str:
        return BLOCKQUOTE_OPEN + "\n\n".join(items) + BLOCKQUOTE_CLOSE

    quotes: list[str] = []
    current: list[str] = []
    for block in blocks:
        if len(wrap([block])) > limit:
            if current:
                quotes.append(wrap(current))
                current = []
            quotes.append(block)
            continue
        if current and len(wrap(current + [block])) > limit:
            quotes.append(wrap(current))
            current = []
        current.append(block)
    if current:
        quotes.append(wrap(current))
    return quotes


def assemble_payments_report(
    blocks: Sequence[str],
    *,
    collapse_min_green: Optional[int] = None,
    collapse_min_total: Optional[int] = None,
) -> str:
    """Header plus blocks, red ones first; the green tail collapses only next to a red block."""
    attention = [block for block in blocks if _is_attention_block(block)]
    on_track = [block for block in blocks if not _is_attention_block(block)]
    collapse = bool(attention) and (
        (collapse_min_green is not None and len(on_track) >= collapse_min_green)
        or (collapse_min_total is not None and len(blocks) >= collapse_min_total)
    )
    sections = [payments_report_header(len(attention)), *attention]
    sections.extend(_collapse_report_blocks(on_track) if collapse else on_track)
    return "\n\n".join(sections)


def _format_due_distance(due_date: date, today: date) -> str:
    """``'in 2 days'`` | ``'tomorrow'`` | ``'today'`` | ``'overdue by 31 days'``."""
    return days_left_phrase((due_date - today).days)


def _format_report_distance(due_value: str, due_date: date, today: date, tz_name: Optional[str]) -> str:
    """Live "in N d" for future dates; overdue text stays server-side so the word is never lost."""
    distance = _format_due_distance(due_date, today)
    if due_date > today:
        return tg_due(due_value, tz_name, "r", distance)
    return distance


def _format_report_due(due_value: str, due_date: date, today: date, tz_name: Optional[str]) -> str:
    date_html = tg_due(due_value, tz_name, "wd")
    if due_date < today:
        date_html = f"<b>{date_html}</b>"
    return f"{date_html} ({_format_report_distance(due_value, due_date, today, tz_name)})"


def _build_report_schedule_line(
    last_paid_value: Optional[str],
    next_charge: Optional[date],
    today: date,
    tz_name: Optional[str] = DEFAULT_REMINDER_TIMEZONE,
    *,
    extra: str = "",
) -> str:
    if last_paid_value and next_charge:
        next_value = next_charge.isoformat()
        return (
            f"Paid {tg_due(last_paid_value, tz_name, 'wd')} → "
            f"next {tg_due(next_value, tz_name, 'wd')} "
            f"({_format_report_distance(next_value, next_charge, today, tz_name)}){extra}"
        )
    if next_charge:
        next_value = next_charge.isoformat()
        return (
            f"Next charge: {tg_due(next_value, tz_name, 'wd')} "
            f"({_format_report_distance(next_value, next_charge, today, tz_name)}){extra}"
        )
    if last_paid_value:
        return f"Paid {tg_due(last_paid_value, tz_name, 'wd')}{extra}"
    return ""


def _resolve_report_cycle_participants(
    cycle_state: Dict[str, object],
    live_participants: Sequence[Dict[str, object]],
) -> list[Dict[str, object]]:
    cycle_participants = list(cycle_state.get("participants") or [])
    snapshot_ready = bool(cycle_state.get("snapshot_ready"))
    if not cycle_participants and not snapshot_ready:
        cycle_participants = list(live_participants)
    return cycle_participants


async def _build_subscription_payment_report_text(
    db: Database,
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
    *,
    scope: str,
    user_id: Optional[int] = None,
    tz_name: Optional[str] = DEFAULT_REMINDER_TIMEZONE,
) -> str:
    today = datetime.now(parse_timezone(tz_name)).date()
    subscription_id = int(subscription["id"])

    open_cycles = await db.list_open_cycles(subscription_id)
    payments_for_open = await db.list_payments_for_cycles(subscription_id, open_cycles)
    paid_by_due: Dict[str, set[int]] = {}
    for row in payments_for_open:
        payer_id = row.get("paid_by_telegram_id")
        if payer_id is None:
            continue
        paid_by_due.setdefault(str(row.get("due_date") or ""), set()).add(int(payer_id))
    cycle_state_map = await db.list_cycle_participants_for_due_dates(subscription_id, open_cycles)

    try:
        next_charge = datetime.strptime(str(subscription["next_charge_at"]), "%Y-%m-%d").date()
    except (KeyError, TypeError, ValueError):
        next_charge = None

    safe_name = escape_html(str(subscription["name"]))

    if scope == "user":
        if user_id is None:
            raise ValueError("user_id is required when scope is 'user'")

        last_paid_value = await db.get_latest_paid_due_for_user(subscription_id, user_id)

        unpaid: list[str] = []
        upcoming_paid = False
        for due_value in open_cycles:
            try:
                due = datetime.strptime(due_value, "%Y-%m-%d").date()
            except ValueError:
                continue
            cycle_participants = _resolve_report_cycle_participants(
                cycle_state_map.get(due_value) or {},
                participants,
            )
            participant_ids = {int(p.get("telegram_id") or 0) for p in cycle_participants}
            if user_id not in participant_ids:
                continue
            if user_id in paid_by_due.get(due_value, set()):
                if due >= today:
                    upcoming_paid = True
                continue
            if due <= today:
                unpaid.append(_format_report_due(due_value, due, today, tz_name))

        if next_charge and last_paid_value == next_charge.isoformat():
            last_paid_value = None
        lines = [f"{'🔴' if unpaid else '🟢'} <b>{safe_name}</b>"]
        schedule_line = _build_report_schedule_line(
            last_paid_value,
            next_charge,
            today,
            tz_name,
            extra=f"{SEP}✅ paid" if upcoming_paid else "",
        )
        if schedule_line:
            lines.append(schedule_line)
        if unpaid:
            lines.append("⚠️ Unpaid: " + ", ".join(unpaid))
        return "\n".join(lines)

    latest_closed = await db.get_latest_closed_cycle(subscription_id)
    last_paid_value = str(latest_closed["due_date"]) if latest_closed else None

    paid_progress = ""
    if next_charge:
        next_value = next_charge.isoformat()
        if next_value in open_cycles:
            cycle_participants = _resolve_report_cycle_participants(
                cycle_state_map.get(next_value) or {},
                participants,
            )
            participant_ids = {
                int(p.get("telegram_id") or 0)
                for p in cycle_participants
                if int(p.get("telegram_id") or 0) > 0
            }
            if participant_ids:
                paid_count = len(participant_ids & paid_by_due.get(next_value, set()))
                if paid_count:
                    paid_progress = f"{SEP}{paid_count} of {len(participant_ids)} paid"

    unpaid_lines: list[str] = []
    for due_value in open_cycles:
        try:
            due = datetime.strptime(due_value, "%Y-%m-%d").date()
        except ValueError:
            continue
        if due > today:
            continue
        cycle_participants = _resolve_report_cycle_participants(
            cycle_state_map.get(due_value) or {},
            participants,
        )
        names_by_id: Dict[int, str] = {}
        for person in cycle_participants:
            telegram_id = int(person.get("telegram_id") or 0)
            if telegram_id > 0:
                names_by_id[telegram_id] = str(person.get("full_name") or f"User {telegram_id}")
        unpaid_ids = set(names_by_id) - paid_by_due.get(due_value, set())
        if not unpaid_ids:
            continue
        names = ", ".join(
            escape_html(format_display_name(names_by_id[telegram_id]))
            for telegram_id in sorted(unpaid_ids, key=lambda tid: names_by_id[tid].lower())
        )
        distance = "due today" if due == today else _format_due_distance(due, today)
        unpaid_lines.append(f"⚠️ <b>{tg_due(due_value, tz_name, 'wd')}</b>{SEP}{distance}: {names}")

    lines = [f"{'🔴' if unpaid_lines else '🟢'} <b>{safe_name}</b>"]
    schedule_line = _build_report_schedule_line(
        last_paid_value,
        next_charge,
        today,
        tz_name,
        extra=paid_progress,
    )
    if schedule_line:
        lines.append(schedule_line)
    lines.extend(unpaid_lines)
    return "\n".join(lines)


# --------------------------------------------------- admin subscription card
#
# Block layout: a header line, then one block per section ('📅 <b>Schedule</b>'
# plus indented 'Label: value' lines). A block that does not apply is left out.

CARD_FOLD_MIN = 6  # participants / unpaid names fold into an expandable quote above 5
CARD_FOLD_BUDGET = 1200  # UTF-16 units per folded list, so the card stays far below 4096
CARD_COMMENT_FOLD_LEN = 500
# The admin card shows only the start of the requisites and the link; “💳 Payment” has them in full.
CARD_DETAIL_SHOWN_MAX = 60
CARD_LINK_SHOWN_MAX = 80
PAYMENT_INFO_BUTTON_TEXT = "💳 Payment"


def _ordinal(day: int) -> str:
    """``1st``, ``2nd``, ``3rd``, ``4th``, ``11th``, ``21st``, ``22nd``, ``31st``."""
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _period_days_of(subscription: Dict[str, object]) -> int:
    try:
        return int(subscription.get("period_days") or 30)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 30


def _cadence_short(subscription: Dict[str, object]) -> str:
    """``'monthly'`` or ``'every 30 days'`` (the header fact)."""
    period = _period_days_of(subscription)
    if period == MONTHLY_PERIOD_SENTINEL:
        return "monthly"
    return f"every {format_days(period)}"


def _repeats_text(subscription: Dict[str, object]) -> str:
    """``'every month on the 15th'`` or ``'every 30 days'``."""
    period = _period_days_of(subscription)
    if period != MONTHLY_PERIOD_SENTINEL:
        return f"every {format_days(period)}"
    anchor = normalize_monthly_anchor_day(subscription.get("monthly_anchor_day"))
    if anchor is None:
        try:
            anchor = datetime.strptime(str(subscription.get("next_charge_at")), "%Y-%m-%d").day
        except (TypeError, ValueError):
            return "every month"
    return f"every month on the {_ordinal(anchor)}"


def _due_phrase_html(due_value: str, tz_name: str, today: date, *, bold: bool = False) -> str:
    """``'<tg-time …> (in 5 days)'`` / ``'(today)'`` / ``'(tomorrow)'`` / ``'(overdue by 9 days)'``.

    Two or more days ahead the relative part is a live entity; today, tomorrow
    and overdue stay plain words, so no client renders "in 3 hours".
    """
    date_html = tg_due(due_value, tz_name, "wD")
    if bold:
        date_html = f"<b>{date_html}</b>"
    try:
        due = datetime.strptime(due_value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return date_html
    days = (due - today).days
    phrase = days_left_phrase(days)
    if days >= 2:
        phrase = tg_due(due_value, tz_name, "r", phrase)
    return f"{date_html} ({phrase})"


def _card_schedule_block(subscription: Dict[str, object], tz_name: str, today: date) -> list[str]:
    currency = str(subscription.get("currency") or "").strip().upper()
    shown_in = str(subscription.get("base_currency") or currency).strip().upper() or currency
    next_charge = _due_phrase_html(str(subscription.get("next_charge_at")), tz_name, today, bold=True)
    lines = [
        f"Next charge: {next_charge}",
        f"Repeats: {_repeats_text(subscription)}",
        f"Shown to members in: {escape_html(shown_in)}" if shown_in != currency else None,
    ]
    return ["📅 <b>Schedule</b>", *(sub_line(line) for line in lines if line)]


def _card_participants_block(
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
) -> list[str]:
    if not participants:
        return ["👥 <b>Participants</b>", sub_line("No one yet — nobody gets reminders.")]
    currency = str(subscription.get("currency") or "").strip().upper()
    amount = float(subscription.get("amount") or 0.0)  # type: ignore[arg-type]
    payment_mode = normalize_payment_mode(subscription.get("payment_mode"))
    per_share = amount / _split_share_base(subscription, participants)
    if payment_mode == PAYMENT_MODE_FIXED:
        total = _sum_users_total(subscription, participants)
        summary = f"Fixed per user{SEP}total <b>{format_money(total, currency)}</b>"
    else:
        share_base = _split_share_base(subscription, participants)
        summary = f"Split by shares{SEP}{format_shares(share_base)} × <b>≈ {format_money(per_share, currency)}</b>"
    rows: list[str] = []
    for person in participants:
        weight = int(person.get("share_weight") or 1)  # type: ignore[arg-type]
        weight_text = f" ×{weight}" if weight > 1 else ""
        if payment_mode == PAYMENT_MODE_FIXED:
            value = _effective_fixed_amount(amount, person.get("fixed_amount")) * weight
            amount_text = f"<b>{format_money(value, currency)}</b>"
        else:
            amount_text = f"<b>≈ {format_money(per_share * weight, currency)}</b>"
        rows.append(f"{escape_html(str(person.get('full_name') or 'Unknown'))}{weight_text} — {amount_text}")
    lines = [f"👥 <b>Participants: {len(participants)}</b>", sub_line(summary)]
    if len(rows) >= CARD_FOLD_MIN:
        lines.append(expandable([f"{BULLET} {row}" for row in rows], CARD_FOLD_BUDGET, "participant"))
    else:
        lines.extend(bullet(row) for row in rows)
    return lines


def _card_cycle_block(
    current_cycle: Optional[Dict[str, object]],
    open_cycles_count: int,
    tz_name: str,
    today: date,
) -> Optional[list[str]]:
    """The oldest open cycle: due date, payment progress, who has not paid."""
    if not current_cycle:
        return None
    paid = int(current_cycle.get("paid") or 0)  # type: ignore[arg-type]
    total = int(current_cycle.get("total") or 0)  # type: ignore[arg-type]
    unpaid_names = [escape_html(name) for name in current_cycle.get("unpaid_names") or []]  # type: ignore[union-attr]
    due_html = _due_phrase_html(str(current_cycle["due_value"]), tz_name, today)
    lines = ["🧾 <b>Current cycle</b>", sub_line(f"Due: {due_html}")]
    if total:
        progress = "all paid" if paid >= total else f"{paid} of {total} paid"
        lines.append(sub_line(f"{progress_bar(paid, total)} {progress}"))
    if unpaid_names and len(unpaid_names) < CARD_FOLD_MIN:
        lines.append(sub_line(f"Unpaid: {', '.join(unpaid_names)}"))
    elif unpaid_names:
        lines.append(sub_line(f"Unpaid: <b>{len(unpaid_names)}</b>"))
        lines.append(expandable([f"{BULLET} {name}" for name in unpaid_names], CARD_FOLD_BUDGET, "user"))
    if open_cycles_count > 1:
        lines.append(sub_line(f"Open cycles: <b>{open_cycles_count}</b>"))
    return lines


def _card_reminders_block(subscription: Dict[str, object], reminder_time: str) -> list[str]:
    offsets_text = format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))
    from app.ui.keyboards import after_due_line

    return [
        "🔔 <b>Reminders</b>",
        sub_line(
            f"<b>{escape_html(reminder_time)}</b> in each member's timezone"
            f"{SEP}{escape_html(offsets_text)}{SEP}{after_due_line(bool(subscription.get('remind_after_due')))}"
        ),
    ]


def _card_payment_block(
    payment_label: str,
    payment_details: str,
    payment_link: str,
    comment_value: str,
) -> list[str]:
    """Method, the first requisites line, the link and the comment (skipped when it repeats the requisites).

    The card is a hub, not the place for the full requisites: the first line
    and the link are cut short (the full text is in “💳 Payment”), so long
    one-line requisites never push the card over Telegram's limit.
    """
    lines = ["💳 <b>Payment</b>", sub_line(f"Payment method: {escape_html(payment_label)}")]
    detail_lines = [line.strip() for line in payment_details.splitlines() if line.strip()]
    if detail_lines:
        first_detail = cut_visible(detail_lines[0], CARD_DETAIL_SHOWN_MAX)
        line = f"<code>{escape_html(first_detail)}</code>"
        if first_detail != detail_lines[0] or len(detail_lines) > 1:
            line += f" (more in “{PAYMENT_INFO_BUTTON_TEXT}”)"
        lines.append(sub_line(line))
    if payment_link:
        lines.append(sub_line(f"🔗 Link: {escape_html(cut_visible(payment_link, CARD_LINK_SHOWN_MAX))}"))
    comment = " ".join(comment_value.split())
    if comment and comment != " ".join(payment_details.split()):
        if len(comment) > CARD_COMMENT_FOLD_LEN:
            lines.append(sub_line("📝 Comment"))
            lines.append(expandable([escape_html(comment)], CARD_FOLD_BUDGET, "line"))
        else:
            lines.append(sub_line(f"📝 {escape_html(comment)}"))
    return lines


def card_payment_method_label(
    destination: Optional[Dict[str, object]],
    default_destination_id: Optional[int],
) -> str:
    """``'⭐ Sber · RUB'`` for the default method, ``'Sber · RUB'`` otherwise, ``'not set'`` without one."""
    if not destination:
        return "not set"
    try:
        is_default = default_destination_id is not None and int(destination.get("id")) == int(default_destination_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        is_default = False
    return payment_method_label(destination.get("title"), destination.get("currency"), default=is_default)


def _build_subscription_detail_text(
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
    base_time: str,
    base_timezone: str,
    open_cycles_count: int,
    payment_label: str,
    payment_details: str,
    payment_link: str,
    *,
    current_cycle: Optional[Dict[str, object]] = None,
    today: Optional[date] = None,
) -> str:
    """Admin card: '🏷️ <b>Netflix</b> · <b>15.99 USD</b> · monthly', then the blocks.

    Schedule, Participants, Current cycle (only with an open cycle), Reminders
    and Payment. ``payment_label`` is the method as shown ('⭐ Sber · RUB').
    """
    today = today or datetime.now(parse_timezone(base_timezone)).date()
    currency = str(subscription.get("currency") or "").strip().upper()
    try:
        amount: Optional[float] = float(subscription.get("amount"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        amount = None
    header = screen_title(
        "🏷️",
        escape_html(str(subscription.get("name") or "")),
        f"<b>{format_money(amount, currency)}</b>",
        _cadence_short(subscription),
    )
    reminder_time = normalize_time_string(subscription.get("reminder_time")) or base_time
    return join_blocks(
        header,
        _card_schedule_block(subscription, base_timezone, today),
        _card_participants_block(subscription, participants),
        _card_cycle_block(current_cycle, open_cycles_count, base_timezone, today),
        _card_reminders_block(subscription, reminder_time),
        _card_payment_block(
            payment_label,
            payment_details,
            payment_link,
            str(subscription.get("comment") or "").strip(),
        ),
    )


async def build_subscription_card(
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
    close: bool = False,
) -> Optional[Tuple[str, InlineKeyboardMarkup]]:
    """The admin card's text and keyboard, or ``None`` when the subscription is gone.

    ``close=True`` is the card opened as a new message from a push or report.
    """
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    participants = await db.list_subscription_participants(subscription_id)
    destination = await db.get_effective_subscription_payment_destination(subscription_id)
    default_destination_id = await db.get_default_payment_destination_id()
    base_time = parse_time_string(await db.get_effective_base_reminder_time()).strftime("%H:%M")
    base_timezone = await resolve_report_timezone(db)
    today = datetime.now(parse_timezone(base_timezone)).date()
    cycles = await _open_cycle_states(db, subscription, participants)
    text = _build_subscription_detail_text(
        subscription,
        participants,
        base_time,
        base_timezone,
        len(cycles),
        card_payment_method_label(destination, default_destination_id),
        str(destination.get("details") or "").strip() if destination else "",
        str(destination.get("payment_link") or "").strip() if destination else "",
        current_cycle=cycles[0] if cycles else None,
        today=today,
    )
    markup = subscription_detail_keyboard(subscription_id, participant_count=len(participants), close=close)
    return with_banner(banner, text), markup


async def show_card_after_prompt(
    message: Message,
    db: Database,
    subscription_id: int,
    flow_data: Dict[str, object],
    *,
    banner: str,
) -> None:
    """A typed value was saved: edit the card the prompt came from, with a result banner.

    Call after ``prompts.finish_prompt`` (it returns ``flow_data``). When the
    origin screen cannot be edited, the card is sent as a new message.
    """
    from app.ui.prompts import origin_of  # prompts imports this module

    origin = origin_of(flow_data)
    close = origin is not None and is_spawned_screen(*origin)
    card = await build_subscription_card(db, subscription_id, banner=banner, close=close)
    if card is None:
        await send_subscription_list(message, db, banner=SUBSCRIPTION_GONE_BANNER)
        return
    text, markup = card
    if origin is not None and message.bot is not None:
        if await edit_in_place(message.bot, origin[0], origin[1], text, markup):
            return
    await message.answer(text, reply_markup=markup)


async def send_subscription_detail(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """Show the admin card: edit on a tap, a new message for a typed message.

    A deleted subscription redraws the list instead (``show_subscription_gone``).
    The copy opened from a push (``remember_spawned``) keeps its [✖️ Close].
    """
    card = await build_subscription_card(db, subscription_id, banner=banner, close=spawned_screen_of(target))
    if card is None:
        await show_subscription_gone(target, db)
        return None
    text, markup = card
    return await respond_with_markup(target, text, reply_markup=markup, toast=toast)


async def send_member_list(
    target: Responder,
    db: Database,
    *,
    page: int = 1,
    focus_friend_id: Optional[int] = None,
) -> None:
    friends = await db.list_friends()
    if not friends:
        await respond_with_markup(
            target,
            "👥 Users:\nNo users yet. Use <code>➕ Add user</code> to create one.",
            reply_markup=build_members_list_keyboard([], page=1, total_pages=1, total_users=0),
        )
        return

    sorted_friends = sorted(
        friends,
        key=lambda item: (str(item.get("full_name") or "").casefold(), int(item.get("id") or 0)),
    )

    per_page = 6
    total_users = len(sorted_friends)
    total_pages = max(1, (total_users + per_page - 1) // per_page)
    if focus_friend_id is not None:
        for idx, friend in enumerate(sorted_friends):
            if int(friend.get("id") or 0) == int(focus_friend_id):
                page = idx // per_page + 1
                break
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    page_friends = sorted_friends[start : start + per_page]

    lines = ["👥 Users:", "Choose a user to manage:"]
    for idx, friend in enumerate(page_friends, start + 1):
        status_suffix = " (pending)" if friend.get("telegram_id") is None else ""
        lines.append(f"{idx}. <b>{escape_html(friend['full_name'])}</b>{status_suffix}")

    await respond_with_markup(
        target,
        "\n".join(lines),
        reply_markup=build_members_list_keyboard(
            page_friends,
            page=page,
            total_pages=total_pages,
            total_users=total_users,
        ),
    )


async def send_member_detail(target: Responder, db: Database, friend_id: int) -> None:
    friend = await db.get_friend(friend_id)
    if not friend:
        await respond_with_markup(target, "User not found.")
        return
    telegram_id = friend.get("telegram_id")
    is_pending = telegram_id is None
    subs = await db.list_subscriptions_for_user(telegram_id)
    default_balance_currency = str(await db.get_setting("target_currency") or "RUB").strip().upper() or "RUB"
    balance_currency = str(friend.get("balance_currency") or "").strip().upper()
    if len(balance_currency) != 3 or not balance_currency.isalpha():
        friend = {**friend, "balance_currency": default_balance_currency}
    text = _build_user_info_text(
        friend,
        subs,
        include_telegram_id=True,
        title="👤 User Info:",
    )
    if is_pending:
        invite_expires_at = str(friend.get("invite_expires_at") or "").strip()
        pending_lines = ["Status: Awaiting authorization"]
        if invite_expires_at:
            pending_lines.append(f"Invite expires at (UTC): <code>{escape_html(invite_expires_at)}</code>")
        text = f"{text}\n\n" + "\n".join(pending_lines)
    else:
        assigned_destination_id = await db.get_user_payment_destination_id(int(telegram_id))
        effective_destination = await db.get_effective_payment_destination_for_user(int(telegram_id))
        if effective_destination:
            payment_label = (
                f"{effective_destination['title']} ({effective_destination['currency']})"
                if assigned_destination_id is not None
                else f"Default ({effective_destination['title']} · {effective_destination['currency']})"
            )
        else:
            payment_label = "Not configured"
        text = f"{text}\n\nPayment method: {escape_html(payment_label)}"
    await respond_with_markup(
        target,
        text,
        reply_markup=member_detail_keyboard(friend_id, is_pending=is_pending),
    )


async def send_public_account_detail(message: Message, db: Database, telegram_id: int) -> None:
    friend = await db.get_friend_by_telegram(telegram_id)
    if not friend:
        await message.answer(
            "👤 Account:\nYour profile is not set up yet. Ask an admin to add you to the users list.",
            reply_markup=public_reply_keyboard(),
        )
        return

    default_balance_currency = str(await db.get_setting("target_currency") or "RUB").strip().upper() or "RUB"
    balance_currency = str(friend.get("balance_currency") or "").strip().upper()
    if len(balance_currency) != 3 or not balance_currency.isalpha():
        friend = {**friend, "balance_currency": default_balance_currency}

    subscriptions = await db.list_subscriptions_for_user(telegram_id)
    destinations = await db.list_payment_destinations()
    default_destination_id = await db.get_default_payment_destination_id()
    default_destination = (
        next((item for item in destinations if int(item.get("id") or 0) == default_destination_id), None)
        if default_destination_id is not None
        else None
    )
    payment_label = "Not configured"
    if destinations:
        payment_label = "Choose when topping up"
        if default_destination:
            payment_label += f" (default: {default_destination['title']} · {default_destination['currency']})"
    await message.answer(
        (
            _build_user_info_text(
            friend,
            subscriptions,
            include_telegram_id=False,
            title="👤 Account:",
            )
            + f"\n\nTop-up methods: {escape_html(payment_label)}"
        ),
        reply_markup=public_account_keyboard(),
    )


async def send_member_report(target: Responder, db: Database, friend_id: int) -> None:
    friend = await db.get_friend(friend_id)
    if not friend:
        await respond_with_markup(target, "User not found.")
        return
    if friend.get("telegram_id") is None:
        await respond_with_markup(
            target,
            "This user has not authorized their Telegram account yet.",
            reply_markup=member_report_keyboard(friend_id),
        )
        return
    blocks = await _build_user_payment_blocks(db, friend["telegram_id"], await resolve_report_timezone(db))
    if not blocks:
        await respond_with_markup(
            target,
            "No payments to report yet.",
            reply_markup=member_report_keyboard(friend_id),
        )
        return
    await send_chunked_responder_text(
        target,
        assemble_payments_report(blocks, collapse_min_total=MEMBER_REPORT_COLLAPSE_MIN_TOTAL),
        reply_markup=member_report_keyboard(friend_id),
    )


async def send_reminder_settings(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """🔔 Reminders (WP6, app/ui/reminder_screens.py); a deleted subscription shows the list."""
    from app.ui.reminder_screens import send_reminders_screen  # it imports this module

    return await send_reminders_screen(target, db, subscription_id, banner=banner, toast=toast)


async def send_participants_settings(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """👥 Participants (WP5, app/ui/participant_screens.py); a deleted subscription shows the list."""
    from app.ui.participant_screens import send_participants_screen  # it imports this module

    return await send_participants_screen(target, db, subscription_id, banner=banner, toast=toast)


async def send_reminder_send_menu(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """📬 Send now (WP6, app/ui/reminder_screens.py); a deleted subscription shows the list."""
    from app.ui.reminder_screens import send_send_now_screen  # it imports this module

    return await send_send_now_screen(target, db, subscription_id, toast=toast)


async def send_settings_tests_menu(target: Responder, db: Database) -> None:
    text = "🧪 Tests:\nChoose a test action."
    await respond_with_markup(
        target,
        text,
        reply_markup=tests_menu_keyboard(),
    )


async def send_test_user_list(
    target: Responder,
    db: Database,
    *,
    page: int = 1,
) -> None:
    friends = [friend for friend in await db.list_friends() if friend.get("telegram_id") is not None]
    if not friends:
        await respond_with_markup(
            target,
            "🧪 Tests:\nNo linked users yet.",
            reply_markup=settings_tests_keyboard([], page=1, total_pages=1, total_users=0),
        )
        return

    sorted_friends = sorted(
        friends,
        key=lambda item: (str(item.get("full_name") or "").casefold(), int(item.get("id") or 0)),
    )
    per_page = 6
    total_users = len(sorted_friends)
    total_pages = max(1, (total_users + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    page_friends = sorted_friends[start : start + per_page]

    lines = ["🧪 Tests:", "Choose a user to send the test message:"]
    for idx, friend in enumerate(page_friends, start + 1):
        lines.append(f"{idx}. <b>{escape_html(str(friend['full_name']))}</b>")
    await respond_with_markup(
        target,
        "\n".join(lines),
        reply_markup=settings_tests_keyboard(
            page_friends,
            page=page,
            total_pages=total_pages,
            total_users=total_users,
        ),
    )


# ------------------------------------------------- Pricing & schedule (WP4)
#
# '💰 <b>Pricing · Netflix</b>' with Amount, Shown to members in, Next charge
# and Repeats. Every sub-screen (currency pickers, typed prompts, the
# next-charge conflict question) comes back here with a result banner.


def subscription_currency_of(subscription: Dict[str, object]) -> str:
    return str(subscription.get("currency") or "").strip().upper()


def subscription_shown_in_of(subscription: Dict[str, object]) -> str:
    """The currency members see their share in: ``base_currency``, else the subscription's."""
    currency = subscription_currency_of(subscription)
    return str(subscription.get("base_currency") or currency).strip().upper() or currency


def pricing_text(subscription: Dict[str, object], tz_name: str, today: date) -> str:
    currency = subscription_currency_of(subscription)
    try:
        amount: Optional[float] = float(subscription.get("amount"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        amount = None
    next_charge = _due_phrase_html(str(subscription.get("next_charge_at")), tz_name, today, bold=True)
    return join_blocks(
        screen_title("💰", f"Pricing{SEP}{escape_html(str(subscription.get('name') or ''))}"),
        [
            f"Amount: <b>{format_money(amount, currency)}</b>",
            f"Shown to members in: {escape_html(subscription_shown_in_of(subscription))}",
            f"Next charge: {next_charge}",
            f"Repeats: {_repeats_text(subscription)}",
        ],
    )


async def build_pricing_screen(
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
) -> Optional[Tuple[str, InlineKeyboardMarkup]]:
    """Pricing's text and keyboard, or ``None`` when the subscription is gone."""
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    tz_name = await resolve_report_timezone(db)
    today = datetime.now(parse_timezone(tz_name)).date()
    text = with_banner(banner, pricing_text(subscription, tz_name, today))
    return text, pricing_settings_keyboard(subscription_id)


async def send_pricing_settings(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """Show Pricing (edit on a tap); a deleted subscription redraws the list instead."""
    screen = await build_pricing_screen(db, subscription_id, banner=banner)
    if screen is None:
        await show_subscription_gone(target, db)
        return None
    text, markup = screen
    return await respond_with_markup(target, text, reply_markup=markup, toast=toast)


async def show_pricing_after_prompt(
    message: Message,
    db: Database,
    subscription_id: int,
    flow_data: Dict[str, object],
    *,
    banner: str,
) -> None:
    """A typed value was saved: edit the screen the prompt came from into Pricing with a banner.

    Call after ``prompts.finish_prompt``. When that screen cannot be edited,
    Pricing is sent as a new message; a deleted subscription shows the list.
    """
    from app.ui.prompts import origin_of  # prompts imports this module

    screen = await build_pricing_screen(db, subscription_id, banner=banner)
    if screen is None:
        await send_subscription_list(message, db, banner=SUBSCRIPTION_GONE_BANNER)
        return
    text, markup = screen
    origin = origin_of(flow_data)
    if origin is not None and message.bot is not None:
        if await edit_in_place(message.bot, origin[0], origin[1], text, markup):
            return
    await message.answer(text, reply_markup=markup)


async def send_subscription_more(target: Responder, db: Database, subscription_id: int) -> Optional[Message]:
    """The legacy 'more' screen is gone (Delete and Reports are on the card): redraw the card."""
    return await send_subscription_detail(target, db, subscription_id)


async def send_subscription_payment_info(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """💳 Payment of one subscription (WP7, app/ui/subscription_payment_screens.py)."""
    from app.ui.subscription_payment_screens import send_payment_info_screen  # it imports this module

    return await send_payment_info_screen(target, db, subscription_id, banner=banner, toast=toast)


async def send_subscription_user_amounts(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """💵 Amount per user (WP5, app/ui/participant_screens.py); a deleted subscription shows the list."""
    from app.ui.participant_screens import send_user_amounts_screen  # it imports this module

    return await send_user_amounts_screen(target, db, subscription_id, banner=banner, toast=toast)


async def send_participants_editor(
    callback: CallbackQuery,
    db: Database,
    subscription_id: int,
    *,
    page: int = 1,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """👥 Choose people, the participant picker (WP5, app/ui/participant_screens.py)."""
    from app.ui.participant_screens import send_picker_screen  # it imports this module

    return await send_picker_screen(callback, db, subscription_id, page=page, toast=toast)


async def start_subscription_edit_flow(
    callback: CallbackQuery,
    state: FSMContext,
    subscription_id: int,
    next_state: State,
    prompt: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    origin_screen: Optional[str] = None,
    extra_rows: Sequence[Sequence[object]] = (),
    data: Optional[Dict[str, object]] = None,
) -> Optional[Message]:
    """Ask for a typed value of a subscription field through ``prompts.ask``.

    The prompt is a new '✏️ …' message with quick-value rows above a single
    [✖️ Cancel]; the previous flow is wound down first. ``reply_markup`` (old
    call shape) contributes its rows except its Cancel. ``origin_screen``
    names the screen to redraw on success ('pricing:<id>', 'participants:<id>',
    'reminders:<id>', 'payment:<id>'; default 'card:<id>'); the tapped message
    is the origin. ``edit_subscription_id`` is stored for the input handler.
    """
    from app.ui.prompts import ask  # prompts imports this module

    rows = [list(row) for row in extra_rows if row]
    if reply_markup is not None:
        for row in reply_markup.inline_keyboard:
            kept = [button for button in row if button.callback_data != _CANCEL_CALLBACK]
            if kept:
                rows.append(kept)
    return await ask(
        callback,
        state,
        next_state,
        prompt,
        origin_screen=origin_screen or f"card:{int(subscription_id)}",
        extra_rows=rows,
        data={**dict(data or {}), "edit_subscription_id": int(subscription_id)},
    )


async def get_edit_subscription_id(state: FSMContext) -> Optional[int]:
    data = await state.get_data()
    subscription_id = data.get("edit_subscription_id")
    if subscription_id is None:
        return None
    return int(subscription_id)


async def require_edit_subscription_id(message: Message, state: FSMContext) -> Optional[int]:
    subscription_id = await get_edit_subscription_id(state)
    if subscription_id is None:
        from app.ui.prompts import reset_flow_state, wind_down  # prompts imports this module

        old_data = await reset_flow_state(state)
        if message.bot is not None:
            await wind_down(message.bot, message.chat.id, old_data)
        await message.answer(PROMPT_OUT_OF_DATE_TEXT)
        return None
    return subscription_id


# Questions shared by the creation wizard and the field-edit prompts. A
# ``current`` value is named in the question and marked ✅ on its button.
PROMPT_CURRENCIES = CURRENCY_OPTIONS
PERIOD_MONTHLY_NOTE = "Monthly = the same day each month."
PAYMENT_MODE_LINES = (
    "Split by shares — the amount is divided by weights.",
    "Fixed per user — everyone pays a set amount (by default the full amount).",
)


def _period_label(period: Optional[int]) -> str:
    if period == MONTHLY_PERIOD_SENTINEL:
        return "monthly"
    return f"every {format_days(int(period or 0))}"


def currency_prompt(
    current: Optional[str] = None,
    *,
    cancel_callback: str = _CANCEL_CALLBACK,
) -> Tuple[str, InlineKeyboardMarkup]:
    now = f" Now {escape_html(current.upper())}." if current else ""
    options = PROMPT_CURRENCIES  # the one currency option list (keyboards.CURRENCY_OPTIONS)
    return (
        f"✏️ Currency?{now} Tap one or type three letters.",
        build_currency_keyboard(options, selected=current, cancel_callback=cancel_callback),
    )


def period_prompt(
    current: Optional[int] = None,
    *,
    suggested: Optional[int] = None,
    cancel_callback: str = _CANCEL_CALLBACK,
) -> Tuple[str, InlineKeyboardMarkup]:
    """'✏️ How often?' with day presets and Monthly.

    ``current`` (days or MONTHLY_PERIOD_SENTINEL) is named in the question and
    marked ✅; without it, ``suggested`` is marked ✅ (the wizard suggests Monthly).
    """
    now = f" Now {_period_label(current)}." if current is not None else ""
    selected = current if current is not None else suggested
    return (
        f"✏️ How often?{now} Tap one or send a number of days.\n{PERIOD_MONTHLY_NOTE}",
        build_period_keyboard(selected=selected, cancel_callback=cancel_callback),
    )


def share_limit_prompt(
    *,
    everyone_selected: bool = False,
    cancel_callback: str = _CANCEL_CALLBACK,
) -> Tuple[str, InlineKeyboardMarkup]:
    return (
        "✏️ How many people share it? Tap “Everyone” to split among all participants.",
        build_share_limit_keyboard(
            everyone_selected=everyone_selected,
            cancel_callback=cancel_callback,
        ),
    )


def payment_mode_prompt(
    current: Optional[str] = None,
    *,
    cancel_callback: str = _CANCEL_CALLBACK,
) -> Tuple[str, InlineKeyboardMarkup]:
    return (
        "\n".join(["✏️ How are amounts split?", *PAYMENT_MODE_LINES]),
        build_creation_payment_mode_keyboard(
            selected=current,
            cancel_callback=cancel_callback,
        ),
    )
