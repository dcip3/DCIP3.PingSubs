"""Top-up flow (WP14): the member's method picker, amount prompt and request
screen, the pushes about a decision, and the admin's review and pending list.

Pure ``*_text`` builders take plain dicts (rows from ``topup_requests`` joined
with the member) so the tests can pin them without a database; the
``*_screen`` coroutines read what they need and return ``(text, markup)``.

Navigation: the picker edits 👤 Account and goes back to it ("◀️ Back"); from
a push it is a new message with [✖️ Close]. The request screen ends with
[✖️ Cancel]; once sent it becomes a final state with no buttons. The admin
review push has no way back (its decision makes it final); opened from the
pending list it returns there ("◀️ To pending").
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from aiogram.types import InlineKeyboardMarkup

from app.core.reminders import parse_timezone, tg_time
from app.services import _render_payment_details_lines
from app.storage.db import Database
from app.ui.keyboards import (
    PENDING_TOPUPS_BUTTONS_MAX,
    extract_copyable_requisite,
    is_openable_payment_link,
    payment_method_label,
    topup_destination_keyboard,
    topup_moment,
    topup_pending_requests_keyboard,
    topup_request_review_keyboard,
    topup_request_submit_keyboard,
)
from app.ui.member_screens import balance_of, default_balance_currency, first_name_of
from app.ui.style import SEP, join_blocks, title, utf16_len, with_banner
from app.ui.text import escape_html, format_money, format_relative_past

Screen = Tuple[str, InlineKeyboardMarkup]

TOPUP_TITLE = title("➕", "Top up")
PICKER_HINT = "Pick where you will send the money."
NO_METHODS_LINE = "No payment methods yet. Ask the admin."
METHOD_GONE_TOAST = "Payment method was deleted"
AMOUNT_HINT = "⚠️ Need an amount above zero, for example <code>1000</code>."
AMOUNT_TEXT_HINT = "⚠️ Send the amount as text, for example <code>1000</code>."
CREATE_FAILED_TEXT = "⚠️ Could not create the request. Try again."
SUBMIT_FAILED_ALERT = "Could not send the request. Try again."
I_PAID_HINT = "Then tap “✅ I paid”."
TOPUP_CANCELLED_TOAST = "Top-up cancelled"
SENT_TOAST = "⏳ Sent for approval"
NOT_YOURS_ALERT = "This request belongs to another user"
ALREADY_SENT = "Already sent for approval"
ALREADY_APPROVED = "Already approved"
ALREADY_REJECTED = "Already rejected"
APPROVED_TOAST = "✅ Top-up approved"
REJECTED_TOAST = "❌ Top-up rejected"
MISMATCH_TOAST = "The balance is in another currency"
PENDING_HINT = "Tap a request to approve or reject it."
PENDING_EMPTY = "No top-ups waiting."
REJECTED_PUSH_HINT = "If you did send the money, contact the admin."

# Requisites echoed on the request screen stay far below Telegram's 4096 limit.
DETAILS_BUDGET_UTF16 = 3000


# ------------------------------------------------------------------ basics


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def request_money(request: Mapping[str, Any], *, bold: bool = False) -> str:
    return format_money(request.get("amount"), str(request.get("currency") or "").strip().upper(), bold=bold)


def request_method(request: Mapping[str, Any]) -> str:
    """The method title the request was made for (plain text, not escaped)."""
    return _clean(request.get("destination_title")) or "the payment method"


def request_name(request: Mapping[str, Any]) -> str:
    return _clean(request.get("full_name")) or "Unknown"


def _local(moment: datetime, tz_name: Optional[str]) -> datetime:
    return moment.astimezone(parse_timezone(tz_name))


def _currency_of(value: object) -> str:
    return str(value or "").strip().upper()


def currency_mismatch(request: Mapping[str, Any]) -> Optional[str]:
    """The member's balance currency when it blocks approval (the DB rule), else None.

    A balance above zero in another valid currency blocks it; an empty or
    broken balance simply takes the top-up's currency.
    """
    try:
        balance = float(request.get("balance") or 0.0)
    except (TypeError, ValueError):
        balance = 0.0
    balance_currency = _currency_of(request.get("balance_currency"))
    if balance <= 0 or len(balance_currency) != 3 or not balance_currency.isalpha():
        return None
    return balance_currency if balance_currency != _currency_of(request.get("currency")) else None


# ------------------------------------------------------------------ picker


@dataclass(frozen=True)
class TopUpPicker:
    friend: Dict[str, Any]
    balance: float
    currency: str
    # The methods offered, the member's own (or the default) first.
    methods: Tuple[Dict[str, Any], ...]
    # True when the list is limited to the balance currency (the balance is above zero).
    filtered: bool


def picker_methods(
    destinations: Sequence[Dict[str, Any]],
    *,
    balance: float,
    currency: str,
    preferred_id: Optional[int],
) -> Tuple[list[Dict[str, Any]], bool]:
    """The methods a member may top up through, and whether they were filtered.

    With money on the balance only methods in its currency work (approval
    refuses others). An empty balance takes any currency, so every method is
    offered. The member's own method (or the default) comes first.
    """
    filtered = balance > 0
    methods = [
        item for item in destinations if not filtered or _currency_of(item.get("currency")) == currency
    ]
    methods.sort(key=lambda item: 0 if preferred_id is not None and int(item["id"]) == preferred_id else 1)
    return methods, filtered


async def topup_picker(db: Database, telegram_id: int) -> Optional[TopUpPicker]:
    """What the member's top-up starts from, or None when this account is not linked."""
    friend = await db.get_friend_by_telegram(telegram_id)
    if not friend:
        return None
    balance, currency = balance_of(friend, await default_balance_currency(db))
    preferred = await db.get_effective_payment_destination_for_user(telegram_id)
    methods, filtered = picker_methods(
        await db.list_payment_destinations(),
        balance=balance,
        currency=currency,
        preferred_id=int(preferred["id"]) if preferred else None,
    )
    return TopUpPicker(dict(friend), balance, currency, tuple(methods), filtered)


def topup_picker_text(picker: TopUpPicker, *, banner: Optional[str] = None) -> str:
    """'➕ <b>Top up</b>', the balance, then what to do (or why there is nothing to pick)."""
    balance_line = f"Balance: {format_money(picker.balance, picker.currency, bold=True)}"
    if picker.methods:
        body = f"{balance_line}\n{PICKER_HINT}"
    elif picker.filtered:
        body = f"{balance_line}\nNo payment method in {escape_html(picker.currency)} yet. Ask the admin."
    else:
        body = f"{balance_line}\n{NO_METHODS_LINE}"
    return with_banner(banner, join_blocks(TOPUP_TITLE, body))


def topup_picker_screen(picker: TopUpPicker, *, close: bool = False, banner: Optional[str] = None) -> Screen:
    return topup_picker_text(picker, banner=banner), topup_destination_keyboard(picker.methods, close=close)


def method_mismatch_toast(destination: Mapping[str, Any], balance_currency: str) -> str:
    """``'Sber takes EUR; your balance is RUB'``."""
    method = _clean(destination.get("title")) or "This method"
    return f"{method} takes {_currency_of(destination.get('currency'))}; your balance is {balance_currency}"


def amount_prompt_text(currency: str) -> str:
    return f"✏️ How much to top up, {escape_html(currency)}? For example <code>1000</code>."


def rounded_banner(amount: float, currency: str) -> str:
    """Shown on the request screen when the typed amount had more than 2 decimals."""
    return f"⚠️ Rounded to cents · {format_money(amount, currency, bold=True)}"


# ----------------------------------------------------------- request screen


def _capped_details(details: str) -> str:
    if utf16_len(details) <= DETAILS_BUDGET_UTF16:
        return details
    kept = ""
    for char in details:
        if utf16_len(kept + char) > DETAILS_BUDGET_UTF16 - 1:
            break
        kept += char
    return kept.rstrip() + "…"


def topup_request_text(request: Mapping[str, Any], *, banner: Optional[str] = None) -> str:
    """'💳 <b>Top-up · 1 000.00 RUB</b>', where to send it, then 'Then tap “✅ I paid”.'

    The requisites fold into an expandable quote when a copy button carries
    them (the reminders' rule), else they stay a <pre> block. An openable link
    is a button only; any other link is a text line.
    """
    details = _capped_details(str(request.get("destination_details") or "").strip())
    currency = _currency_of(request.get("currency"))
    method = escape_html(request_method(request))
    where = f"Send to {method} ({escape_html(currency)})" if currency else f"Send to {method}"
    body = [where + (":" if details else ".")]
    body.extend(_render_payment_details_lines(details, copyable=extract_copyable_requisite(details) is not None))
    link = str(request.get("destination_link") or "").strip()
    if link and not is_openable_payment_link(link):
        body.append(f"🔗 Link: {escape_html(link)}")
    head = title("💳", f"Top-up{SEP}{request_money(request)}")
    return with_banner(banner, join_blocks(head, body, I_PAID_HINT))


def topup_request_screen(request: Mapping[str, Any], *, banner: Optional[str] = None) -> Screen:
    markup = topup_request_submit_keyboard(
        int(request["id"]),
        payment_details=str(request.get("destination_details") or ""),
        payment_link=str(request.get("destination_link") or ""),
    )
    return topup_request_text(request, banner=banner), markup


def submitted_text(request: Mapping[str, Any], tz_name: Optional[str]) -> str:
    """The request screen's final state: no requisites, no buttons."""
    moment = topup_moment(dict(request))
    sent = ""
    if moment is not None:
        sent = f"{SEP}sent {tg_time(moment, 't', _local(moment, tz_name).strftime('%H:%M'))}"
    head = title("⏳", f"Top-up sent for approval{SEP}{request_money(request)}")
    body = f"{escape_html(request_method(request))}{sent}.\nYou will get a message when the admin confirms."
    return join_blocks(head, body)


def approved_push_text(request: Mapping[str, Any], balance: float, currency: str) -> str:
    """To the member: '✅ <b>Top-up approved · 1 000.00 RUB</b>' and the new balance."""
    head = title("✅", f"Top-up approved{SEP}{request_money(request)}")
    return join_blocks(head, f"Balance: {format_money(balance, currency, bold=True)}")


def rejected_push_text(request: Mapping[str, Any]) -> str:
    head = title("❌", f"Top-up rejected{SEP}{request_money(request)}")
    return join_blocks(head, f"{escape_html(request_method(request))}. {REJECTED_PUSH_HINT}")


def member_request_final_text(request: Mapping[str, Any], tz_name: Optional[str], default_currency: str) -> str:
    """What an old request screen becomes once the request left 'new'."""
    status = str(request.get("status") or "")
    if status == "approved":
        balance, currency = balance_of(dict(request), default_currency)
        return approved_push_text(request, balance, currency)
    if status == "rejected":
        return rejected_push_text(request)
    return submitted_text(request, tz_name)


def member_already_toast(request: Mapping[str, Any]) -> str:
    status = str(request.get("status") or "")
    if status == "approved":
        return ALREADY_APPROVED
    if status == "rejected":
        return ALREADY_REJECTED
    return ALREADY_SENT


# -------------------------------------------------------------- admin review


def member_link(request: Mapping[str, Any]) -> str:
    """``'<a href="tg://user?id=123">Ivan Petrov</a> · <code>123</code>'``."""
    name = escape_html(request_name(request))
    telegram_id = request.get("telegram_id")
    if telegram_id is None:
        return name
    return f'<a href="tg://user?id={int(telegram_id)}">{name}</a>{SEP}<code>{int(telegram_id)}</code>'


def _balance_line(request: Mapping[str, Any], mismatch: Optional[str]) -> str:
    try:
        balance = float(request.get("balance") or 0.0)
    except (TypeError, ValueError):
        balance = 0.0
    if mismatch:
        return f"Balance: {format_money(balance, mismatch)}"
    currency = _currency_of(request.get("currency"))
    try:
        amount = float(request.get("amount") or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    now = max(balance, 0.0)
    return f"Balance: {format_money(now, currency)} → {format_money(now + amount, currency, bold=True)} after approval"


def review_text(request: Mapping[str, Any], *, tz_name: Optional[str], more_waiting: int = 0) -> str:
    """'📥 <b>Top-up request · 1 000.00 RUB</b>': who, method, the balance after, when; no requisites."""
    mismatch = currency_mismatch(request)
    method = payment_method_label(request_method(request), request.get("currency"))
    facts = [f"From: {member_link(request)}", f"Payment method: {escape_html(method)}"]
    facts.append(_balance_line(request, mismatch))
    moment = topup_moment(dict(request))
    if moment is not None:
        local = _local(moment, tz_name)
        absolute = tg_time(moment, "wdt", local.strftime("%a, %d.%m.%Y %H:%M"))
        facts.append(f"Sent: {absolute} ({tg_time(moment, 'r', format_relative_past(moment))})")
    warning = None
    if mismatch:
        first = escape_html(first_name_of(request_name(request)))
        warning = (
            f"⚠️ {first}'s balance is in {escape_html(mismatch)} — fix it in “👥 Users” → "
            f"{escape_html(request_name(request))} → “💰 Balance” first."
        )
    more = f"📥 <b>{more_waiting}</b> more waiting" if more_waiting > 0 else None
    head = title("📥", f"Top-up request{SEP}{request_money(request)}")
    return join_blocks(head, facts, warning, more)


async def review_screen(
    db: Database,
    request: Mapping[str, Any],
    *,
    tz_name: Optional[str],
    from_list: bool,
) -> Screen:
    pending = await db.list_pending_topup_requests()
    more = sum(1 for item in pending if int(item["id"]) != int(request["id"]))
    markup = topup_request_review_keyboard(
        int(request["id"]),
        friend_id=int(request["friend_id"]) if request.get("friend_id") else None,
        from_list=from_list,
        can_approve=currency_mismatch(request) is None,
    )
    return review_text(request, tz_name=tz_name, more_waiting=more), markup


def reject_confirm_text(request: Mapping[str, Any]) -> str:
    first = escape_html(first_name_of(request_name(request)))
    return f"⚠️ <b>Reject {request_money(request)} from {first}?</b>\n{first} is told the top-up was rejected."


def decision_line(request: Mapping[str, Any], default_currency: str) -> str:
    """The fact under a decided review: the member as a tg://user link with the id, then the new balance."""
    person = member_link(request)
    if str(request.get("status") or "") != "approved":
        return person
    balance, currency = balance_of(dict(request), default_currency)
    return f"{person}\nBalance now: {format_money(balance, currency, bold=True)}"


def review_final_text(
    request: Mapping[str, Any],
    default_currency: str,
    *,
    decided_by: Optional[str] = None,
) -> str:
    """A decided review push: '✅ <b>Top-up approved · …</b>' / '❌ <b>Top-up rejected · …</b>', no buttons.

    The title says who decided: '· by you', or '· by Anna' (``decided_by``)
    on another admin's copy, so with several admins every push names the
    decider. The member stays a tg://user link with the id.
    """
    approved = str(request.get("status") or "") == "approved"
    verb = "approved" if approved else "rejected"
    by = f"by {escape_html(decided_by)}" if decided_by else "by you"
    head = title("✅" if approved else "❌", f"Top-up {verb}{SEP}{request_money(request)}", by)
    return join_blocks(head, decision_line(request, default_currency))


def decided_banner(request: Mapping[str, Any]) -> str:
    """The pending list's banner after a decision made from it."""
    approved = str(request.get("status") or "") == "approved"
    verb = "approved" if approved else "rejected"
    return f"{'✅' if approved else '❌'} Top-up {verb}{SEP}{request_money(request, bold=True)}{SEP}{escape_html(request_name(request))}"


def admin_already_toast(request: Mapping[str, Any], decided_by: Optional[str]) -> str:
    """'Already approved by Anna' for another admin's decision; plain 'Already approved' for one's own."""
    status = str(request.get("status") or "")
    if status not in {"approved", "rejected"}:
        return "Request already handled"
    toast = ALREADY_APPROVED if status == "approved" else ALREADY_REJECTED
    return f"{toast} by {decided_by}" if decided_by else toast


# ------------------------------------------------------------- pending list


def pending_list_text(requests: Sequence[Mapping[str, Any]], *, banner: Optional[str] = None) -> str:
    """'📥 <b>Pending top-ups</b> · 2' and the hint; the requests are the buttons."""
    count = len(requests)
    if not count:
        return with_banner(banner, join_blocks(title("📥", "Pending top-ups"), PENDING_EMPTY))
    body = PENDING_HINT
    if count > PENDING_TOPUPS_BUTTONS_MAX:
        body += f"\nShowing the oldest {PENDING_TOPUPS_BUTTONS_MAX}."
    return with_banner(banner, join_blocks(title("📥", "Pending top-ups", count), body))


async def pending_list_screen(db: Database, *, banner: Optional[str] = None) -> Screen:
    requests = await db.list_pending_topup_requests()
    return pending_list_text(requests, banner=banner), topup_pending_requests_keyboard(requests)
