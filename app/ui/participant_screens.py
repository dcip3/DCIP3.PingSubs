"""Participants subtree of the admin subscription card (WP5).

* Participants: '👥 <b>Participants · Netflix</b> · 3', the mode line, one
  bullet per person (folded above 5), the mode radio and [◀️ To card].
* Choose people: the picker, '👥 <b>Choose people · Netflix</b> · 2 of 5',
  one 🟢/⚪ toggle per signed-in user (plus ×N in split mode), 20 per page.
* Amount per user: subscription amount, assigned total, one button per
  person ('Alice Smith · 10.00 ✏️'), [✏️ Set all].

Prompts started from these screens come back here with a result banner.
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Sequence, Tuple

from aiogram.types import InlineKeyboardMarkup, Message

from app.core.constants import PAYMENT_MODE_FIXED
from app.services import normalize_payment_mode, parse_fixed_amount
from app.storage.db import Database
from app.ui.helpers import (
    CARD_FOLD_BUDGET,
    CARD_FOLD_MIN,
    SUBSCRIPTION_GONE_BANNER,
    _effective_fixed_amount,
    _split_share_base,
    _sum_users_total,
    edit_in_place,
    respond_with_markup,
    send_subscription_list,
    show_subscription_gone,
)
from app.ui.keyboards import (
    CLOSE_CALLBACK,
    PARTICIPANTS_PAGE_SIZE,
    build_participants_keyboard,
    participants_settings_keyboard,
    subscription_user_amounts_keyboard,
    user_amount_all_confirm_keyboard,
)
from app.ui.states import Responder
from app.ui.style import BULLET, SEP, expandable, join_blocks, title as screen_title, with_banner
from app.ui.text import escape_html, format_money, format_number, format_shares, plural

Screen = Tuple[str, InlineKeyboardMarkup]

PARTICIPANTS_EMPTY_LINE = "No one yet — nobody gets reminders."
MODE_HINT = "Each person's amount changes with the mode."
# One hint sentence, built from these parts (principle hints-last).
PICKER_HINT = "Tap a name to add or remove"
PICKER_WEIGHT_HINT = "×N to change a share (1–5)"
PICKER_UNSIGNED_HINT = "people who have not signed in are not listed"
# The chosen people are named in full above the (cut) name buttons; past this many they fold.
PICKER_CHOSEN_INLINE_MAX = 5
PICKER_CHOSEN_FOLD_BUDGET = 1200
PICKER_EMPTY_LINE = "No users yet. Add people in “👥 Users” first."
PICKER_NOBODY_SIGNED_IN_LINE = "No one has signed in yet. People appear here once they sign in."
USER_AMOUNTS_HINT = "✏️ marks a set amount; others pay the full subscription amount."
USER_AMOUNTS_TOTAL_WARNING = "⚠️ The total differs from the subscription."
SET_ALL_CONSEQUENCE = "Their own amounts are replaced."

AMOUNT_HINT = "⚠️ Need a number, for example <code>300</code>."
AMOUNT_NEGATIVE_HINT = "⚠️ Need 0 or more, for example <code>300</code>."
SET_ALL_AMOUNT_HINT = "⚠️ Need a number, for example <code>300</code>."
SHARE_COUNT_HINT = "⚠️ Need a whole number, for example <code>3</code>."
PERSON_LEFT_BANNER = "⚠️ That person is no longer in this subscription"
USER_AMOUNT_OUT_OF_DATE_TOAST = "List updated"
# Set-all amounts travel in the confirm button as cents; keep them far below 64 bytes.
MAX_SET_ALL_AMOUNT = 1_000_000_000


# ------------------------------------------------------------------ helpers


def _name(subscription: Dict[str, object]) -> str:
    return escape_html(str(subscription.get("name") or ""))


def _currency(subscription: Dict[str, object]) -> str:
    return str(subscription.get("currency") or "").strip().upper()


def _amount(subscription: Dict[str, object]) -> float:
    try:
        value = float(subscription.get("amount") or 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _is_fixed(subscription: Dict[str, object]) -> bool:
    return normalize_payment_mode(subscription.get("payment_mode")) == PAYMENT_MODE_FIXED


def _differs(left: float, right: float) -> bool:
    return round(left, 2) != round(right, 2)


def people(count: int) -> str:
    """``'1 person'`` / ``'3 people'``."""
    return plural(count, "person", "people")


def person_name(person: Dict[str, object]) -> str:
    """The person's full name as HTML (escaped, spaces collapsed)."""
    return escape_html(" ".join(str(person.get("full_name") or "Unknown").split()))


# ------------------------------------------------------------- Participants


def participant_rows(subscription: Dict[str, object], participants: Sequence[Dict[str, object]]) -> list[str]:
    """``'Alice Smith ×2 — <b>≈ 8.00 USD</b>'`` (split) / ``'Bob — <b>10.00 USD</b>'`` (fixed)."""
    currency = _currency(subscription)
    amount = _amount(subscription)
    fixed = _is_fixed(subscription)
    per_share = amount / _split_share_base(subscription, participants)
    rows = []
    for person in participants:
        weight = int(person.get("share_weight") or 1)  # type: ignore[arg-type]
        weight_text = f" ×{weight}" if weight > 1 else ""
        if fixed:
            value = _effective_fixed_amount(amount, person.get("fixed_amount")) * weight
            amount_text = f"<b>{format_money(value, currency)}</b>"
        else:
            amount_text = f"<b>≈ {format_money(per_share * weight, currency)}</b>"
        rows.append(f"{person_name(person)}{weight_text} — {amount_text}")
    return rows


def participants_text(subscription: Dict[str, object], participants: Sequence[Dict[str, object]]) -> str:
    """The Participants screen text (see the module docstring)."""
    currency = _currency(subscription)
    amount = _amount(subscription)
    head = screen_title("👥", f"Participants{SEP}{_name(subscription)}", len(participants))
    warning = None
    if _is_fixed(subscription):
        mode_line = "Mode: fixed per user"
        if participants:
            total = _sum_users_total(subscription, participants)
            mode_line += f"{SEP}total <b>{format_money(total, currency)}</b>"
            if _differs(total, amount):
                warning = f"⚠️ Total differs from the subscription ({format_money(amount, currency)})."
    else:
        mode_line = "Mode: split by shares"
        if participants or int(subscription.get("share_limit") or 0):  # type: ignore[arg-type]
            mode_line += f"{SEP}{format_shares(_split_share_base(subscription, participants))}"
    if not participants:
        return join_blocks(head, [mode_line, PARTICIPANTS_EMPTY_LINE])
    rows = [f"{BULLET} {row}" for row in participant_rows(subscription, participants)]
    if len(rows) >= CARD_FOLD_MIN:
        rows = [expandable(rows, CARD_FOLD_BUDGET, "participant")]
    return join_blocks(head, [mode_line, warning, *rows], MODE_HINT)


async def build_participants_screen(
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
) -> Optional[Screen]:
    """Participants' text and keyboard, or ``None`` when the subscription is gone."""
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    participants = await db.list_subscription_participants(subscription_id)
    text = with_banner(banner, participants_text(subscription, participants))
    markup = participants_settings_keyboard(
        subscription_id,
        normalize_payment_mode(subscription.get("payment_mode")),
        participant_count=len(participants),
    )
    return text, markup


# ---------------------------------------------------------------- the picker


def picker_hint(*, fixed: bool, has_unsigned: bool) -> str:
    """One sentence: 'Tap a name to add or remove, ×N to change a share (1–5).'"""
    hint = PICKER_HINT
    if not fixed:
        hint += f", {PICKER_WEIGHT_HINT}"
    if has_unsigned:
        hint += f"; {PICKER_UNSIGNED_HINT}"
    return f"{hint}."


def _chosen_lines(chosen: Sequence[Dict[str, object]], *, fixed: bool) -> Optional[str]:
    """The chosen people named in full (the buttons cut long names): 'Chosen: Anna Petrova ×2, Boris ×1'."""
    if not chosen:
        return None
    names = []
    for friend in chosen:
        name = escape_html(" ".join(str(friend.get("full_name") or "Unknown").split()))
        names.append(name if fixed else f"{name} ×{int(friend.get('share_weight') or 1)}")  # type: ignore[call-overload]
    if len(names) <= PICKER_CHOSEN_INLINE_MAX:
        return f"Chosen: {', '.join(names)}"
    folded = expandable([f"{BULLET} {name}" for name in names], PICKER_CHOSEN_FOLD_BUDGET, "person", "people")
    return f"Chosen: <b>{len(names)}</b>\n{folded}"


def picker_text(
    subscription: Dict[str, object],
    selected: int,
    total: int,
    *,
    has_unsigned: bool = False,
    chosen: Sequence[Dict[str, object]] = (),
) -> str:
    """'👥 <b>Choose people · Netflix</b> · 2 of 5', who is chosen (in full), one hint; an empty state without users."""
    name = f"Choose people{SEP}{_name(subscription)}"
    if total == 0:
        line = PICKER_NOBODY_SIGNED_IN_LINE if has_unsigned else PICKER_EMPTY_LINE
        return join_blocks(screen_title("👥", name), line)
    fixed = _is_fixed(subscription)
    return join_blocks(
        screen_title("👥", name, f"{selected} of {total}"),
        _chosen_lines(chosen, fixed=fixed),
        picker_hint(fixed=fixed, has_unsigned=has_unsigned),
    )


def picker_pages(count: int) -> int:
    return max(1, math.ceil(count / PARTICIPANTS_PAGE_SIZE))


def picker_page_of(friends: Sequence[Dict[str, object]], friend_id: int) -> int:
    """The page that lists ``friend_id`` (1 when it is not listed)."""
    for index, friend in enumerate(friends):
        if int(friend["id"]) == int(friend_id):  # type: ignore[call-overload]
            return index // PARTICIPANTS_PAGE_SIZE + 1
    return 1


async def build_picker_screen(
    db: Database,
    subscription_id: int,
    *,
    page: int = 1,
    friend_id: Optional[int] = None,
    close: bool = False,
) -> Optional[Screen]:
    """The picker on ``page`` (or on the page that lists ``friend_id``); ``None`` when the subscription is gone.

    ``close``: the copy opened as a new message from a push, ending in [✖️ Close].
    """
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    friends = await db.list_friends_with_membership(subscription_id)
    has_unsigned = any(friend.get("telegram_id") is None for friend in await db.list_friends())
    total_pages = picker_pages(len(friends))
    if friend_id is not None:
        page = picker_page_of(friends, friend_id)
    page = max(1, min(int(page), total_pages))
    start = (page - 1) * PARTICIPANTS_PAGE_SIZE
    chosen = [friend for friend in friends if friend.get("is_member")]
    text = picker_text(subscription, len(chosen), len(friends), has_unsigned=has_unsigned, chosen=chosen)
    markup = build_participants_keyboard(
        friends[start : start + PARTICIPANTS_PAGE_SIZE],
        subscription_id,
        payment_mode=normalize_payment_mode(subscription.get("payment_mode")),
        page=page,
        total_pages=total_pages,
        close=close,
    )
    return text, markup


def _is_close_copy(target: Responder) -> bool:
    """A tap on the picker copy opened from a push: its keyboard ends in [✖️ Close]."""
    message = getattr(target, "message", None)
    markup = getattr(message, "reply_markup", None)
    if not isinstance(markup, InlineKeyboardMarkup) or not markup.inline_keyboard:
        return False
    return any(button.callback_data == CLOSE_CALLBACK for button in markup.inline_keyboard[-1])


# ----------------------------------------------------------- Amount per user


def user_amounts_text(subscription: Dict[str, object], participants: Sequence[Dict[str, object]]) -> str:
    currency = _currency(subscription)
    amount = _amount(subscription)
    total = _sum_users_total(subscription, participants)
    lines = [
        f"Subscription: <b>{format_money(amount, currency)}</b>",
        f"Assigned total: <b>{format_money(total, currency)}</b>",
        USER_AMOUNTS_TOTAL_WARNING if participants and _differs(total, amount) else None,
    ]
    return join_blocks(
        screen_title("💵", f"Amount per user{SEP}{_name(subscription)}"),
        lines,
        USER_AMOUNTS_HINT if participants else PARTICIPANTS_EMPTY_LINE,
    )


async def build_user_amounts_screen(
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
) -> Optional[Screen]:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    participants = await db.list_subscription_participants(subscription_id)
    text = with_banner(banner, user_amounts_text(subscription, participants))
    markup = subscription_user_amounts_keyboard(subscription_id, participants, _currency(subscription), _amount(subscription))
    return text, markup


def fixed_amount_of(person: Dict[str, object]) -> Optional[float]:
    """The person's own amount, or ``None`` when they pay the subscription amount."""
    return parse_fixed_amount(person.get("fixed_amount"))


def user_amount_prompt_text(subscription: Dict[str, object], person: Dict[str, object]) -> str:
    own = fixed_amount_of(person)
    now = f"{format_number(own)} (set)" if own is not None else f"{format_number(_amount(subscription))} (default)"
    return (
        f"✏️ Amount for {person_name(person)}, {escape_html(_currency(subscription))}? Now {now}.\n"
        "Send <code>0</code> or more."
    )


def set_all_prompt_text(subscription: Dict[str, object], count: int) -> str:
    return f"✏️ One amount for all {people(count)} in {_name(subscription)}, {escape_html(_currency(subscription))}?"


def set_all_question(amount: float, currency: str, count: int) -> str:
    return f"⚠️ <b>Set {format_money(amount, currency)} for all {people(count)}?</b>\n{SET_ALL_CONSEQUENCE}"


def user_amount_banner(person: Dict[str, object], amount: float, currency: str) -> str:
    return f"✅ {person_name(person)}{SEP}<b>{format_money(amount, currency)}</b>"


def user_amount_default_banner(person: Dict[str, object]) -> str:
    return f"✅ {person_name(person)} pays the default"


def set_all_banner(amount: float, currency: str) -> str:
    return f"✅ All set to <b>{format_money(amount, currency)}</b>"


def build_set_all_confirm(subscription: Dict[str, object], amount: float, count: int) -> Screen:
    cents = int(round(amount * 100))
    return (
        set_all_question(amount, _currency(subscription), count),
        user_amount_all_confirm_keyboard(int(subscription["id"]), cents),  # type: ignore[call-overload]
    )


# --------------------------------------------------------------- share count


def share_count_prompt_text(subscription: Dict[str, object], participants: Sequence[Dict[str, object]]) -> str:
    limit = int(subscription.get("share_limit") or 0)  # type: ignore[arg-type]
    if limit:
        now = str(limit)
    elif participants:
        now = f"everyone ({format_shares(_split_share_base(subscription, participants))})"
    else:
        now = "everyone"
    return f"✏️ How many people share {_name(subscription)}? Now {now}.\nTap “Everyone” or send a number."


def share_count_banner(share_limit: Optional[int]) -> str:
    return f"✅ Share count{SEP}<b>{share_limit if share_limit else 'everyone'}</b>"


# -------------------------------------------------------------- show / send


async def show_screen(target: Responder, db: Database, screen: Optional[Screen], *, toast: Optional[str] = None) -> Optional[Message]:
    """Show a built screen (edit on a tap); ``None`` means the subscription is gone: the list instead."""
    if screen is None:
        await show_subscription_gone(target, db)
        return None
    text, markup = screen
    return await respond_with_markup(target, text, reply_markup=markup, toast=toast)


async def show_after_prompt(
    message: Message,
    db: Database,
    origin: Optional[Tuple[int, int]],
    screen: Optional[Screen],
) -> None:
    """A typed value was handled: edit the screen the prompt came from, else send it as a new message."""
    if screen is None:
        await send_subscription_list(message, db, banner=SUBSCRIPTION_GONE_BANNER)
        return
    text, markup = screen
    if origin is not None and message.bot is not None:
        if await edit_in_place(message.bot, origin[0], origin[1], text, markup):
            return
    await message.answer(text, reply_markup=markup)


async def send_participants_screen(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    return await show_screen(target, db, await build_participants_screen(db, subscription_id, banner=banner), toast=toast)


async def send_picker_screen(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    page: int = 1,
    friend_id: Optional[int] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    screen = await build_picker_screen(
        db, subscription_id, page=page, friend_id=friend_id, close=_is_close_copy(target)
    )
    return await show_screen(target, db, screen, toast=toast)


async def send_user_amounts_screen(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    return await show_screen(target, db, await build_user_amounts_screen(db, subscription_id, banner=banner), toast=toast)
