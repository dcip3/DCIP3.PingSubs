"""Admin 💳 Payment section: the methods root, a method's card, the delete
question, the "New payment method" wizard steps and the field-edit prompts.

Every ``*_screen`` coroutine returns ``(text, markup)`` so a handler can show
it on a callback (edit in place), as a new message, or on the screen a prompt
came from. The pure ``*_text`` builders are what the tests pin.

Navigation: the root is a top-level screen (no nav row); a method's card
returns to it with "◀️ Back"; the delete question answers [✖️ Cancel][🗑 Delete].
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, Tuple

from aiogram.types import InlineKeyboardMarkup

from app.storage.db import Database
from app.ui.keyboards import (
    is_openable_payment_link,
    payment_destination_delete_keyboard,
    payment_destination_detail_keyboard,
    payment_destinations_keyboard,
)
from app.ui.style import SEP, expandable, join_blocks, sub_line, title, with_banner
from app.ui.text import escape_html, format_users, plural

Screen = Tuple[str, InlineKeyboardMarkup]

PAYMENT_EMOJI = "💳"
METHOD_EMOJI = "🏦"
ROOT_EMPTY_TEXT = "No payment methods yet. Add one so members know how to pay."
ROOT_HINT = "Members pay to these; ⭐ is the default."
ROOT_HINT_NO_DEFAULT = "Members pay to these; open one to make it the default."
DEFAULT_METHOD_LINE = "⭐ Default method"
LINK_NOT_OPENABLE_LINE = "⚠️ The link is not a web or Telegram link, so members get no button."
METHOD_GONE_BANNER = "⚠️ This payment method was deleted"

DETAILS_FOLD_LINES = 4  # more requisites lines than this fold into an expandable quote
DETAILS_FOLD_BUDGET = 2000  # UTF-16 units of folded requisites, so a card stays far below 4096
TITLE_MAX_LEN = 64
DETAILS_MAX_LEN = 1500
LINK_MAX_LEN = 500

WIZARD_STEPS = 4
WIZARD_TITLE_KEY = "payment_destination_title"
WIZARD_CURRENCY_KEY = "payment_destination_currency"
WIZARD_DETAILS_KEY = "payment_destination_details"

TITLE_QUESTION = "✏️ Title? For example <code>Main RUB card</code>."
CURRENCY_QUESTION = "✏️ Currency? Tap one or send three letters."
DETAILS_QUESTION = (
    "✏️ Payment details? Several lines are fine. A card number or IBAN gets a copy button for members."
)
LINK_QUESTION = "✏️ Payment link? Optional."

TEXT_HINT = "⚠️ Send text, for example <code>Main RUB card</code>."
TITLE_TOO_LONG_HINT = f"⚠️ Keep the title under {TITLE_MAX_LEN} characters, for example <code>Main RUB card</code>."
CURRENCY_HINT = "⚠️ Need three letters, for example <code>USD</code>."
DETAILS_HINT = "⚠️ Send the details as text, for example <code>Card 2200 1234 5678 9012</code>."
DETAILS_TOO_LONG_HINT = f"⚠️ Keep the details under {DETAILS_MAX_LEN} characters."
LINK_HINT = "⚠️ Links must start with https:// or tg:// — send it again to keep it."
LINK_TOO_LONG_HINT = f"⚠️ Keep the link under {LINK_MAX_LEN} characters."
LINK_TEXT_HINT = "⚠️ Send the link as text, for example <code>https://pay.example.com</code>."


# ------------------------------------------------------------------ pieces


def method_title(destination: Mapping[str, Any]) -> str:
    """The method's title as plain text (not escaped)."""
    return str(destination.get("title") or "").strip() or f"Method {destination.get('id')}"


def method_currency(destination: Mapping[str, Any]) -> str:
    return str(destination.get("currency") or "").strip().upper()


def _detail_lines(details: object) -> list[str]:
    return [line.strip() for line in str(details or "").splitlines() if line.strip()]


def details_block(details: object) -> list[str] | str:
    """'📝 <b>Details</b>' with one <code> line per requisites line; more than 4 lines fold."""
    lines = _detail_lines(details)
    if not lines:
        return "📝 Details: none"
    code_lines = [f"<code>{escape_html(line)}</code>" for line in lines]
    if len(lines) > DETAILS_FOLD_LINES:
        return [
            f"📝 <b>Details</b>{SEP}{plural(len(lines), 'line')}",
            expandable(code_lines, DETAILS_FOLD_BUDGET, "line"),
        ]
    return ["📝 <b>Details</b>", *(sub_line(line) for line in code_lines)]


def link_block(link: object) -> list[str]:
    """'🔗 Link: …', plus a warning when members get no button for it."""
    value = str(link or "").strip()
    if not value:
        return ["🔗 Link: none"]
    lines = [f"🔗 Link: {escape_html(value)}"]
    if not is_openable_payment_link(value):
        lines.append(LINK_NOT_OPENABLE_LINE)
    return lines


# -------------------------------------------------------------------- root


def payment_root_text(
    destinations: Sequence[Mapping[str, Any]],
    default_destination_id: Optional[int],
    pending_count: int,
) -> str:
    """'💳 <b>Payment</b>', what waits, the methods count with the default, a hint."""
    head = title(PAYMENT_EMOJI, "Payment")
    waiting = f"📥 Top-ups waiting: <b>{pending_count}</b>" if pending_count > 0 else None
    if not destinations:
        return join_blocks(head, [waiting] if waiting else None, ROOT_EMPTY_TEXT)
    default = next(
        (item for item in destinations if default_destination_id is not None and int(item["id"]) == default_destination_id),
        None,
    )
    default_part = f"default {escape_html(method_title(default))}" if default else "no default"
    methods_line = f"🏦 Methods: <b>{len(destinations)}</b>{SEP}{default_part}"
    hint = ROOT_HINT if default else ROOT_HINT_NO_DEFAULT
    return join_blocks(head, [waiting, methods_line], hint)


async def payment_root_screen(db: Database, *, banner: Optional[str] = None) -> Screen:
    destinations = await db.list_payment_destinations()
    default_destination_id = await db.get_default_payment_destination_id()
    pending_count = len(await db.list_pending_topup_requests())
    text = payment_root_text(destinations, default_destination_id, pending_count)
    markup = payment_destinations_keyboard(destinations, default_destination_id, pending_count)
    return with_banner(banner, text), markup


# ------------------------------------------------------------- method card


def method_detail_text(
    destination: Mapping[str, Any],
    *,
    is_default: bool,
    assigned_count: int,
) -> str:
    """'🏦 <b>Sber</b> · RUB', default and users, the requisites, the link."""
    head = title(METHOD_EMOJI, escape_html(method_title(destination)), escape_html(method_currency(destination)))
    facts = [
        DEFAULT_METHOD_LINE if is_default else None,
        f"Assigned to users: <b>{assigned_count}</b>" if assigned_count > 0 else None,
    ]
    return join_blocks(
        head,
        [fact for fact in facts if fact],
        details_block(destination.get("details")),
        link_block(destination.get("payment_link")),
    )


async def method_detail_screen(
    db: Database,
    destination_id: int,
    *,
    banner: Optional[str] = None,
) -> Optional[Screen]:
    """The method's card, or None when it was deleted."""
    destination = await db.get_payment_destination(destination_id)
    if not destination:
        return None
    is_default = await db.get_default_payment_destination_id() == destination_id
    assigned = await db.list_payment_destination_assignees(destination_id)
    text = method_detail_text(destination, is_default=is_default, assigned_count=len(assigned))
    markup = payment_destination_detail_keyboard(destination_id, is_default=is_default)
    return with_banner(banner, text), markup


# ------------------------------------------------------------------ delete


def delete_consequence(
    *,
    is_default: bool,
    is_only: bool,
    assigned_count: int,
    default_destination: Optional[Mapping[str, Any]],
) -> str:
    """One line saying what happens to the people and subscriptions on this method.

    Deleting clears the default and the users' own picks of this method;
    subscriptions that point at it fall back to the default method. No other
    method becomes the default by itself.
    """
    if is_only:
        return "Members see no payment details until you add a new method."
    if is_default:
        return "It is the default, so anyone without their own method sees none until you make another one default."
    if default_destination is not None:
        target = f"the default method ({escape_html(method_title(default_destination))})"
        if assigned_count > 0:
            return f"Its {format_users(assigned_count)} and any subscriptions on it switch to {target}."
        return f"Anything that uses it switches to {target}."
    if assigned_count > 0:
        return f"Its {format_users(assigned_count)} and any subscriptions on it are left without a payment method."
    return "Anything that uses it is left without a payment method."


def method_delete_text(
    destination: Mapping[str, Any],
    *,
    is_default: bool,
    is_only: bool,
    assigned_count: int,
    default_destination: Optional[Mapping[str, Any]],
) -> str:
    question = f"⚠️ <b>Delete {escape_html(method_title(destination))}?</b>"
    consequence = delete_consequence(
        is_default=is_default,
        is_only=is_only,
        assigned_count=assigned_count,
        default_destination=default_destination,
    )
    return f"{question}\n{consequence}"


async def method_delete_screen(db: Database, destination_id: int) -> Optional[Screen]:
    destination = await db.get_payment_destination(destination_id)
    if not destination:
        return None
    destinations = await db.list_payment_destinations()
    default_id = await db.get_default_payment_destination_id()
    default_destination = await db.get_payment_destination(default_id) if default_id is not None else None
    assigned = await db.list_payment_destination_assignees(destination_id)
    text = method_delete_text(
        destination,
        is_default=default_id == destination_id,
        is_only=all(int(item["id"]) == destination_id for item in destinations),
        assigned_count=len(assigned),
        default_destination=default_destination,
    )
    return text, payment_destination_delete_keyboard(destination_id)


def deleted_banner(destination: Mapping[str, Any]) -> str:
    return f"✅ {escape_html(method_title(destination))} deleted"


# ------------------------------------------------------------------ wizard


def _short(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def wizard_answer_lines(data: Mapping[str, Any]) -> list[str]:
    """The answers given so far, as indented 'Label: value' lines."""
    lines: list[str] = []
    if data.get(WIZARD_TITLE_KEY):
        lines.append(f"Title: {escape_html(data[WIZARD_TITLE_KEY])}")
    if data.get(WIZARD_CURRENCY_KEY):
        lines.append(f"Currency: {escape_html(data[WIZARD_CURRENCY_KEY])}")
    detail_lines = _detail_lines(data.get(WIZARD_DETAILS_KEY))
    if detail_lines:
        more = f"{SEP}{plural(len(detail_lines), 'line')}" if len(detail_lines) > 1 else ""
        lines.append(f"Details: <code>{escape_html(_short(detail_lines[0]))}</code>{more}")
    return [sub_line(line) for line in lines]


WIZARD_QUESTIONS = {1: TITLE_QUESTION, 2: CURRENCY_QUESTION, 3: DETAILS_QUESTION, 4: LINK_QUESTION}


def payment_wizard_text(data: Mapping[str, Any], step: int, hint: Optional[str] = None) -> str:
    """'➕ <b>New payment method</b> · step 2 of 4', the answers, then '✏️ question' (+ '⚠️ hint')."""
    question = WIZARD_QUESTIONS[step]
    if hint:
        question = f"{question}\n{hint}"
    return join_blocks(
        title("➕", "New payment method", f"step {step} of {WIZARD_STEPS}"),
        wizard_answer_lines(data),
        question,
    )


def added_banner(destination_title: str, *, is_default: bool) -> str:
    banner = f"✅ <b>{escape_html(destination_title)} added</b>"
    return f"{banner}{SEP}it is the default method" if is_default else banner


# ------------------------------------------------------------ field edits


def title_prompt_text(destination: Mapping[str, Any]) -> str:
    name = escape_html(method_title(destination))
    return f"✏️ New title for {name}? Now: {name}."


def currency_prompt_text(destination: Mapping[str, Any], assigned_count: int) -> str:
    name = escape_html(method_title(destination))
    who = f"{format_users(assigned_count)} and pending top-ups" if assigned_count > 0 else "Balances and pending top-ups"
    return (
        f"✏️ New currency for {name}? Now {escape_html(method_currency(destination))}.\n"
        f"⚠️ {who} keep their amounts; nothing is converted."
    )


def details_prompt_text(destination: Mapping[str, Any]) -> str:
    name = escape_html(method_title(destination))
    lines = _detail_lines(destination.get("details"))
    if not lines:
        return f"✏️ New details for {name}? Now: none."
    code_lines = [f"<code>{escape_html(line)}</code>" for line in lines]
    if len(lines) > DETAILS_FOLD_LINES:
        return f"✏️ New details for {name}? Now:\n" + expandable(code_lines, DETAILS_FOLD_BUDGET, "line")
    return f"✏️ New details for {name}? Now:\n" + "\n".join(code_lines)


def link_prompt_text(destination: Mapping[str, Any]) -> str:
    name = escape_html(method_title(destination))
    link = str(destination.get("payment_link") or "").strip()
    if not link:
        return f"✏️ New link for {name}? Now: none."
    return f"✏️ New link for {name}? Now: {escape_html(link)}"


def field_banner(field: str, value: str) -> str:
    """The result banner after a field edit: '✅ Title · <b>Main card</b>'."""
    if field == "title":
        return f"✅ Title{SEP}<b>{escape_html(value)}</b>"
    if field == "currency":
        return f"✅ Currency{SEP}<b>{escape_html(value)}</b>"
    if field == "details":
        return "✅ Details updated"
    if not value:
        return "✅ Link cleared"
    return f"✅ Link{SEP}<b>{escape_html(value)}</b>"


def default_banner(destination: Mapping[str, Any]) -> str:
    return f"✅ Default method{SEP}<b>{escape_html(method_title(destination))}</b>"
