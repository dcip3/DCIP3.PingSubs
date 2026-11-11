"""Payment subtree of the admin subscription card (WP7).

* Payment (sub:paymentinfo): '💳 <b>Payment · Netflix</b>', the method
  ('Payment method: ⭐ Sber · RUB (default)'), its requisites in <code> (folded above
  4 lines), the link and the reminder comment; [🏦 Method][📝 Comment] /
  [◀️ To card].
* Payment method (sub:paymentmethod): '🏦 <b>Payment method · Netflix</b>',
  'Now: Default (⭐ Sber · RUB)'; Default is a radio option like any method;
  [◀️ To payment]. Picking one returns to Payment with a banner and a toast.
* The comment prompt ('✏️ Comment for Netflix reminders? …') comes back to
  Payment with '✅ Comment saved' / '✅ Comment cleared'.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from aiogram.types import InlineKeyboardMarkup, Message

from app.storage.db import Database
from app.ui.helpers import (
    SUBSCRIPTION_GONE_BANNER,
    edit_in_place,
    respond_with_markup,
    send_subscription_list,
    show_subscription_gone,
)
from app.ui.keyboards import (
    DEFAULT_METHOD_OPTION,
    NO_DEFAULT_METHOD_NAME,
    payment_method_choice_banner,
    payment_method_label,
    subscription_payment_destination_keyboard,
    subscription_payment_info_keyboard,
)
from app.ui.states import Responder
from app.ui.style import SEP, expandable, join_blocks, title as screen_title, toast_from_html, with_banner
from app.ui.text import escape_html

Screen = Tuple[str, InlineKeyboardMarkup]

PAYMENT_EMOJI = "💳"
METHOD_EMOJI = "🏦"
METHOD_NOT_SET_LINE = "Payment method: not set — members see no payment details."
NOT_SET_TAIL = " — members see no payment details."
NO_METHODS_LINE = "No payment methods yet. Add one in “💳 Payment”."
COMMENT_NONE_LINE = "📝 Comment: none"

DETAILS_FOLD_LINES = 4  # more requisites lines than this fold into an expandable quote
DETAILS_FOLD_BUDGET = 2000  # UTF-16 units of folded requisites
COMMENT_FOLD_LEN = 500  # longer comments fold (principle split-long-messages)
COMMENT_FOLD_BUDGET = 1200  # UTF-16 units of a folded comment, so the screen stays below 4096
COMMENT_SHOWN_MAX = 1000  # an older, longer comment is cut to this before folding
COMMENT_MAX_LEN = 1000  # new comments; they travel in every reminder
COMMENT_PROMPT_PREVIEW = 80  # characters of the current comment quoted in the prompt
COMMENT_CLEAR_WORDS = frozenset({"clear", "none", "-"})

COMMENT_SAVED_BANNER = "✅ Comment saved"
COMMENT_CLEARED_BANNER = "✅ Comment cleared"
COMMENT_TEXT_HINT = "⚠️ Send text, for example <code>Pay by the 5th</code>."
COMMENT_TOO_LONG_HINT = f"⚠️ Keep the comment under {COMMENT_MAX_LEN} characters."


def _name(subscription: Mapping[str, Any]) -> str:
    return escape_html(str(subscription.get("name") or ""))


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _id_or_none(value: object) -> Optional[int]:
    try:
        return int(value) if value is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass
class PaymentChoice:
    """What a subscription pays to: the effective method and whether it follows the default."""

    destination: Optional[Dict[str, Any]]
    uses_default: bool
    default_destination_id: Optional[int]
    selected_destination_id: Optional[int]

    @property
    def is_default_method(self) -> bool:
        return (
            self.destination is not None
            and self.default_destination_id is not None
            and _id_or_none(self.destination.get("id")) == self.default_destination_id
        )

    def method_label(self) -> str:
        """``'⭐ Sber · RUB'`` (plain text), ``''`` without a method."""
        if self.destination is None:
            return ""
        return payment_method_label(
            self.destination.get("title"), self.destination.get("currency"), default=self.is_default_method
        )


async def payment_choice(db: Database, subscription: Mapping[str, Any]) -> PaymentChoice:
    """The method the subscription's members pay to (a deleted own method falls back to the default)."""
    selected = _id_or_none(subscription.get("payment_destination_id"))
    own = await db.get_payment_destination(selected) if selected is not None else None
    default_id = await db.get_default_payment_destination_id()
    if own:
        return PaymentChoice(own, False, default_id, selected)
    default = await db.get_payment_destination(default_id) if default_id is not None else None
    return PaymentChoice(default, True, default_id, selected)


# -------------------------------------------------------------------- Payment


def _detail_lines(details: object) -> list[str]:
    return [line.strip() for line in str(details or "").splitlines() if line.strip()]


def requisites_lines(details: object) -> list[str]:
    """One <code> line per requisites line; more than 4 lines fold into an expandable quote."""
    code_lines = [f"<code>{escape_html(line)}</code>" for line in _detail_lines(details)]
    if len(code_lines) > DETAILS_FOLD_LINES:
        return [expandable(code_lines, DETAILS_FOLD_BUDGET, "line")]
    return code_lines


def comment_lines(comment: object) -> list[str]:
    """'📝 Comment: Pay by the 5th', 'Comment: none', or a folded quote above 500 characters."""
    value = str(comment or "").strip()
    if not value:
        return [COMMENT_NONE_LINE]
    if len(value) <= COMMENT_FOLD_LEN:
        return [f"📝 Comment: {escape_html(value)}"]
    lines = [escape_html(line.strip()) for line in _cut(value, COMMENT_SHOWN_MAX).splitlines() if line.strip()]
    return ["📝 Comment", expandable(lines, COMMENT_FOLD_BUDGET, "line")]


def payment_info_text(subscription: Mapping[str, Any], choice: PaymentChoice) -> str:
    """'💳 <b>Payment · Netflix</b>', the method with requisites and link, then the comment."""
    destination = choice.destination
    if destination is None:
        method_block = [METHOD_NOT_SET_LINE]
    else:
        default_note = " (default)" if choice.uses_default else ""
        method_block = [f"Payment method: {escape_html(choice.method_label())}{default_note}"]
        method_block.extend(requisites_lines(destination.get("details")))
        link = str(destination.get("payment_link") or "").strip()
        if link:
            method_block.append(f"🔗 Link: {escape_html(link)}")
    return join_blocks(
        screen_title(PAYMENT_EMOJI, f"Payment{SEP}{_name(subscription)}"),
        method_block,
        comment_lines(subscription.get("comment")),
    )


async def build_payment_info_screen(
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
) -> Optional[Screen]:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    choice = await payment_choice(db, subscription)
    text = with_banner(banner, payment_info_text(subscription, choice))
    return text, subscription_payment_info_keyboard(subscription_id)


async def _show(target: Responder, db: Database, screen: Optional[Screen], *, toast: Optional[str]) -> Optional[Message]:
    if screen is None:
        await show_subscription_gone(target, db)
        return None
    text, markup = screen
    return await respond_with_markup(target, text, reply_markup=markup, toast=toast)


async def send_payment_info_screen(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """Show Payment (edit on a tap); a deleted subscription redraws the list instead."""
    return await _show(target, db, await build_payment_info_screen(db, subscription_id, banner=banner), toast=toast)


async def show_payment_after_prompt(
    message: Message,
    db: Database,
    subscription_id: int,
    flow_data: Mapping[str, Any],
    *,
    banner: str,
) -> None:
    """A typed value was saved: edit the screen the prompt came from into Payment with a banner.

    Call after ``prompts.finish_prompt``. When that screen cannot be edited,
    Payment is sent as a new message; a deleted subscription shows the list.
    """
    from app.ui.prompts import origin_of  # prompts imports helpers

    screen = await build_payment_info_screen(db, subscription_id, banner=banner)
    if screen is None:
        await send_subscription_list(message, db, banner=SUBSCRIPTION_GONE_BANNER)
        return
    text, markup = screen
    origin = origin_of(flow_data)
    if origin is not None and message.bot is not None:
        if await edit_in_place(message.bot, origin[0], origin[1], text, markup):
            return
    await message.answer(text, reply_markup=markup)


# ------------------------------------------------------------- Payment method


def current_method_text(choice: PaymentChoice) -> str:
    """``'Default (⭐ Sber · RUB)'``, ``'Revolut · EUR'`` or ``'Default (not set)'`` (plain text)."""
    if choice.uses_default:
        return f"{DEFAULT_METHOD_OPTION} ({choice.method_label() or NO_DEFAULT_METHOD_NAME})"
    return choice.method_label()


def payment_method_text(subscription: Mapping[str, Any], choice: PaymentChoice, *, has_methods: bool) -> str:
    """'🏦 <b>Payment method · Netflix</b>' and 'Now: …'; the empty state points at “💳 Payment”."""
    head = screen_title(METHOD_EMOJI, f"Payment method{SEP}{_name(subscription)}")
    if not has_methods:
        return join_blocks(head, NO_METHODS_LINE)
    now = f"Now: {escape_html(current_method_text(choice))}"
    if choice.destination is None:
        now += NOT_SET_TAIL
    return join_blocks(head, now)


async def build_payment_method_screen(db: Database, subscription_id: int) -> Optional[Screen]:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    destinations: Sequence[Dict[str, Any]] = await db.list_payment_destinations()
    choice = await payment_choice(db, subscription)
    text = payment_method_text(subscription, choice, has_methods=bool(destinations))
    markup = subscription_payment_destination_keyboard(
        subscription_id,
        destinations,
        None if choice.uses_default else choice.selected_destination_id,
        choice.default_destination_id,
    )
    return text, markup


async def send_payment_method_screen(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    toast: Optional[str] = None,
) -> Optional[Message]:
    return await _show(target, db, await build_payment_method_screen(db, subscription_id), toast=toast)


def method_banner(choice: PaymentChoice) -> str:
    """'✅ Payment method · <b>Revolut · EUR</b>' or '… · <b>Default (⭐ Sber · RUB)</b>' (as for a user)."""
    return payment_method_choice_banner(current_method_text(choice))


def method_toast(choice: PaymentChoice) -> str:
    """The banner's first line: '✅ Payment method · Default (⭐ Sber · RUB)'."""
    return toast_from_html(method_banner(choice))


# -------------------------------------------------------------------- Comment


def comment_prompt_text(subscription: Mapping[str, Any]) -> str:
    """'✏️ Comment for Netflix reminders? Now: “Pay by the 5th”.' plus 'Send the new text.'

    The current comment is quoted on one line and cut, never in <code>.
    """
    current = " ".join(str(subscription.get("comment") or "").split())
    now = f"“{escape_html(_cut(current, COMMENT_PROMPT_PREVIEW))}”" if current else "none"
    return f"✏️ Comment for {_name(subscription)} reminders? Now: {now}.\nSend the new text."
