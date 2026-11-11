from __future__ import annotations

import re
from datetime import date, datetime
from typing import Dict, Iterable, Optional, Sequence
from urllib.parse import urlsplit

from aiogram.types import (
    CopyTextButton,
    DisabledButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core.constants import MONTHLY_PERIOD_SENTINEL, PAYMENT_MODE_FIXED, PAYMENT_MODE_SPLIT
from app.ui.keyboard_fit import fit_label, label_units, row_budget
from app.ui.text import escape_html, format_days, format_money, format_number
from app.ui.style import SEP
from app.ui.states import (
    CycleAction,
    DueDateChangeAction,
    MemberAction,
    ParticipantAction,
    PaymentDestinationAction,
    ReminderAction,
    ReminderSendAction,
    ReminderSendConfirmAction,
    SubscriptionAction,
    TestListAction,
    TestPaidAction,
    TestSendAction,
    TopUpAction,
)

# A maximal run of digits with single spaces or dashes between them. The run is
# taken whole (no backtracking into a shorter tail) and then filtered by digit
# count, so a grouped 20-digit bank account never yields a 16-digit "card".
DIGIT_RUN_RE = re.compile(r"(?<!\w)\d(?:[ \-]?\d)*(?![ \-]?\d)(?!\w)")
CARD_NUMBER_MIN_DIGITS = 13
CARD_NUMBER_MAX_DIGITS = 19
# An IBAN-like token (compact "DE89370400440532013000" or grouped "DE89 3704 0044 ...").
IBAN_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}(?![A-Z0-9])", re.IGNORECASE)
COPY_TEXT_MAX_LEN = 256
PAYMENT_LINK_WEB_SCHEMES = {"http", "https"}
PAYMENT_LINK_TG_SCHEME = "tg"


def extract_copyable_requisite(details: str) -> tuple[str, str] | None:
    """Pick the one thing worth a copy button out of free-form payment details.

    Returns ``(button_label, text_to_copy)``: the first card-like number (digits
    only) wins; otherwise, when an IBAN-like token is present, the whole
    details text trimmed to Telegram's copy limit. ``None`` means no button.
    """
    text = (details or "").strip()
    if not text:
        return None
    iban_spans = [match.span() for match in IBAN_RE.finditer(text)]
    # Blank out IBANs before the card search so a grouped IBAN's digit tail is
    # not mistaken for a card number.
    masked = list(text)
    for start, end in iban_spans:
        masked[start:end] = " " * (end - start)
    masked_text = "".join(masked)
    for match in DIGIT_RUN_RE.finditer(masked_text):
        digits = re.sub(r"\D", "", match.group(0))
        if CARD_NUMBER_MIN_DIGITS <= len(digits) <= CARD_NUMBER_MAX_DIGITS:
            return f"📋 Copy card number ···{digits[-4:]}", digits
    if iban_spans:
        return "📋 Copy details", text[:COPY_TEXT_MAX_LEN]
    return None


def is_openable_payment_link(link: str) -> bool:
    """True when the link is safe to put into an inline ``url=`` button.

    Telegram rejects the whole message (BUTTON_URL_INVALID) for a malformed
    button URL, so anything doubtful stays a plain text line instead.
    """
    link = (link or "").strip()
    if not link or any(ch.isspace() or ord(ch) < 32 for ch in link):
        return False
    parts = urlsplit(link)
    scheme = parts.scheme.lower()
    if scheme in PAYMENT_LINK_WEB_SCHEMES:
        return bool(parts.netloc)
    if scheme == PAYMENT_LINK_TG_SCHEME:
        return bool(parts.netloc or parts.path)
    return False


def payment_requisite_rows(payment_details: str = "", payment_link: str = "") -> list[list[InlineKeyboardButton]]:
    """Rows that go above the "Paid" buttons: copy-requisite and open-link, when available."""
    rows: list[list[InlineKeyboardButton]] = []
    requisite = extract_copyable_requisite(payment_details)
    if requisite is not None:
        label, copy_value = requisite
        rows.append([InlineKeyboardButton(text=label, copy_text=CopyTextButton(text=copy_value))])
    link = (payment_link or "").strip()
    if is_openable_payment_link(link):
        rows.append([InlineKeyboardButton(text="🔗 Open payment link", url=link)])
    return rows


COMMON_TIMEZONES = (
    "Europe/Moscow",
    "Europe/London",
    "Europe/Berlin",
    "Asia/Dubai",
    "Asia/Almaty",
    "Asia/Tokyo",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "UTC",
)


# ------------------------------------------------------------------ vocabulary
#
# One name per concept (principles: exit-verbs, same-name-everywhere). Ways
# back are never styled; only the destructive verb of a confirmation is red.

BACK_BUTTON_TEXT = "◀️ Back"
CLOSE_BUTTON_TEXT = "✖️ Close"
CANCEL_BUTTON_TEXT = "✖️ Cancel"
KEEP_BUTTON_TEXT = "Keep"
REFRESH_BUTTON_TEXT = "🔄 Refresh"
TO_CARD_TEXT = "◀️ To card"
TO_LIST_TEXT = "◀️ To list"
TO_USERS_TEXT = "◀️ To users"
TO_REMINDERS_TEXT = "◀️ To reminders"
TO_PAYMENT_TEXT = "◀️ To payment"
TO_PENDING_TEXT = "◀️ To pending"

CLOSE_CALLBACK = "menu:close"
CANCEL_CALLBACK = "dialog:cancel"
# Callbacks whose only job is to delete their own message: their nav button is "✖️ Close".
CLOSE_CALLBACKS = frozenset({CLOSE_CALLBACK})

SELECTED_PREFIX = "✅ "
TOGGLE_ON = "🟢"
TOGGLE_OFF = "⚪"
DEFAULT_METHOD_MARK = "⭐ "

# Reply-menu labels. Clients keep showing an old reply keyboard until a new one
# arrives, so every label ever shipped keeps a handler: the current ones below
# plus LEGACY_MENU_ALIASES. MENU_BUTTON_TEXTS is what the menu-reset middleware
# recognises as "the user left the pending input".
MENU_USERS = "👥 Users"
MENU_SUBSCRIPTIONS = "📋 Subscriptions"
MENU_PAYMENT = "💳 Payment"
MENU_REPORTS = "📊 Reports"
MENU_SETTINGS = "⚙️ Settings"
MENU_ACCOUNT = "👤 Account"
MENU_HELP = "❓ Help"
LEGACY_MENU_ALIASES = frozenset({"💳 Payment methods", "📊 Payments report", "➕ Top up", "➕ Add subscription"})
MENU_LABELS = (
    MENU_USERS,
    MENU_SUBSCRIPTIONS,
    MENU_PAYMENT,
    MENU_REPORTS,
    MENU_SETTINGS,
    MENU_ACCOUNT,
    MENU_HELP,
)
MENU_BUTTON_TEXTS = frozenset(MENU_LABELS) | LEGACY_MENU_ALIASES


# --------------------------------------------------------------- reply menus


def _reply_keyboard(*rows: Sequence[str]) -> ReplyKeyboardMarkup:
    """A menu that stays under the input field (``is_persistent``) and sized to fit."""
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=True,
        keyboard=[[KeyboardButton(text=label) for label in row] for row in rows],
    )


def admin_reply_keyboard() -> ReplyKeyboardMarkup:
    return _reply_keyboard(
        (MENU_USERS, MENU_SUBSCRIPTIONS),
        (MENU_PAYMENT, MENU_REPORTS),
        (MENU_SETTINGS, MENU_HELP),
    )


def public_reply_keyboard() -> ReplyKeyboardMarkup:
    """The menu of a linked member. Never send it to an admin: it replaces their menu."""
    return _reply_keyboard(
        (MENU_ACCOUNT, MENU_SUBSCRIPTIONS),
        (MENU_REPORTS, MENU_SETTINGS),
        (MENU_HELP,),
    )


def guest_reply_keyboard() -> ReplyKeyboardMarkup:
    """The reduced menu of someone without access (a stranger, a failed sign-in link)."""
    return _reply_keyboard((MENU_HELP,))


# ---------------------------------------------------------------- nav helpers


def nav_button(callback_data: str, text: str | None = None) -> InlineKeyboardButton:
    """The way back of a screen, named by what it does; never styled.

    ``menu:close`` (delete this message) is always "✖️ Close"; any other
    callback is "◀️ Back" unless ``text`` names the target ("◀️ To card").
    """
    if callback_data in CLOSE_CALLBACKS:
        label = CLOSE_BUTTON_TEXT
    else:
        label = text or BACK_BUTTON_TEXT
    return InlineKeyboardButton(text=label, callback_data=callback_data)


def nav_row(
    callback_data: str,
    text: str | None = None,
    *,
    refresh_callback: str | None = None,
) -> list[InlineKeyboardButton]:
    """The last row of a keyboard: the way back first, then an optional "🔄 Refresh".

    A long target ("◀️ To reminders") does not fit half a row, so do not pair
    it with Refresh; tests catch that through ``keyboard_problems``.
    """
    row = [nav_button(callback_data, text)]
    if refresh_callback:
        row.append(InlineKeyboardButton(text=REFRESH_BUTTON_TEXT, callback_data=refresh_callback))
    return row


def cancel_button(callback_data: str = CANCEL_CALLBACK, *, text: str = CANCEL_BUTTON_TEXT) -> InlineKeyboardButton:
    """The "✖️ Cancel" of prompts, wizards and confirmations ("Keep" via ``text``); never red."""
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def confirm_row(
    cancel_callback: str,
    verb_label: str,
    yes_callback: str,
    *,
    cancel_text: str = CANCEL_BUTTON_TEXT,
) -> list[InlineKeyboardButton]:
    """``[✖️ Cancel][🗑 Delete]``: the safe answer on the left, the red bare verb on the right."""
    return [
        cancel_button(cancel_callback, text=cancel_text),
        InlineKeyboardButton(text=verb_label, callback_data=yes_callback, style="danger"),
    ]


def radio_label(text: str, selected: bool) -> str:
    """A picker option; the current one is marked with "✅ " (colour is not shown everywhere)."""
    return f"{SELECTED_PREFIX}{text}" if selected else text


def toggle_label(text: str, on: bool) -> str:
    """``'📣 After due date · 🟢'`` / ``'… · ⚪'``: the state is on the button."""
    return f"{text}{SEP}{TOGGLE_ON if on else TOGGLE_OFF}"


def pair_rows(buttons: Iterable[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    """Siblings two per row, in order; a label too long for half a row gets a row of its own."""
    half = row_budget(2)
    rows: list[list[InlineKeyboardButton]] = []
    pending: InlineKeyboardButton | None = None
    for button in buttons:
        if label_units(button.text) > half:
            if pending is not None:
                rows.append([pending])
                pending = None
            rows.append([button])
        elif pending is None:
            pending = button
        else:
            rows.append([pending, button])
            pending = None
    if pending is not None:
        rows.append([pending])
    return rows


def payment_method_label(title: object, currency: object = "", *, default: bool = False) -> str:
    """``'Sber · RUB'``, or ``'⭐ Sber · RUB'`` for the default method (plain text, not escaped)."""
    name = str(title or "").strip()
    code = str(currency or "").strip().upper()
    label = f"{name}{SEP}{code}" if name and code else name or code
    return f"{DEFAULT_METHOD_MARK}{label}" if default else label


def default_method_value(default_target: Optional[Dict[str, object]]) -> str:
    """``'Default (⭐ Sber · RUB)'`` / ``'Default (not set)'``: the Default choice of a payment-method picker."""
    if not default_target:
        return "Default (not set)"
    return f"Default ({payment_method_label(default_target.get('title'), default_target.get('currency'), default=True)})"


def default_method_option_label(default_target: Optional[Dict[str, object]], selected: bool) -> str:
    """``'✅ Default (⭐ Sber · RUB)'`` fit to a full row: the title is cut, ``' · RUB)'`` kept."""
    if not default_target:
        return radio_label("Default (not set)", selected)
    name = " ".join(str(default_target.get("title") or "").split())
    code = str(default_target.get("currency") or "").strip().upper()
    head = radio_label(f"Default ({DEFAULT_METHOD_MARK}{name}", selected)
    return fit_label(head, 1, tail=f"{SEP}{code})" if code else ")")


def payment_method_choice_banner(value: str) -> str:
    """``'✅ Payment method · <b>Default (⭐ Sber · RUB)</b>'``: one banner for every payment-method picker.

    The toast is its first line (``toast_from_html``), so users, subscriptions
    and the member card all say the same thing.
    """
    return f"✅ Payment method{SEP}<b>{escape_html(value)}</b>"


def dialog_cancel_inline_button() -> InlineKeyboardButton:
    """The prompt's "✖️ Cancel" (callback ``dialog:cancel``, kept for old messages)."""
    return cancel_button()


def dialog_cancel_inline_keyboard() -> InlineKeyboardMarkup:
    """Cancel-only keyboard for text prompts.

    A message carries one reply_markup: with the inline [✖️ Cancel] there is
    no ForceReply, so prompts never set ``force_reply`` (it was a dead extra).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[[dialog_cancel_inline_button()]],
    )


def pagination_row(
    page: int,
    total_pages: int,
    *,
    previous_callback: str,
    next_callback: str,
) -> list[InlineKeyboardButton]:
    """A stable three-button row: ◀️, "n / N" counter, ▶️.

    Arrows that cannot move further are rendered disabled instead of being
    dropped, so the row keeps its shape and the counter never jumps.
    """
    if page > 1:
        previous_button = InlineKeyboardButton(text="◀️", callback_data=previous_callback)
    else:
        previous_button = InlineKeyboardButton(text="◀️", disabled=DisabledButton())
    if page < total_pages:
        next_button = InlineKeyboardButton(text="▶️", callback_data=next_callback)
    else:
        next_button = InlineKeyboardButton(text="▶️", disabled=DisabledButton())
    return [
        previous_button,
        InlineKeyboardButton(text=f"{page} / {total_pages}", disabled=DisabledButton()),
        next_button,
    ]


SUBSCRIPTION_LIST_PAGE_SIZE = 20
SUBSCRIPTION_LIST_PAGE_PREFIX = "sublist:page:"
NEW_SUBSCRIPTION_TEXT = "➕ New subscription"
NO_PARTICIPANTS_TAIL = "no participants"


def subscription_list_label(sub: Dict[str, object]) -> str:
    """One list row: ``'⚠️ Netflix · 15.99 USD'``, ``'Spotify · 299.00 RUB'``, ``'👥 iCloud · no participants'``.

    The name is cut to the row with ``…``; the amount (or the state) is kept whole.
    """
    name = " ".join(str(sub.get("name") or "").split())
    if not int(sub.get("participant_count") or 0):
        return fit_label(f"👥 {name}", 1, tail=f"{SEP}{NO_PARTICIPANTS_TAIL}")
    try:
        amount: float | None = float(sub.get("amount"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        amount = None
    tail = f"{SEP}{format_money(amount, str(sub.get('currency') or '').upper())}"
    prefix = "⚠️ " if sub.get("unpaid") else ""
    return fit_label(f"{prefix}{name}", 1, tail=tail)


def build_subscription_list_keyboard(
    subs: Sequence[Dict[str, object]],
    *,
    page: int = 1,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    """[➕ New subscription] (primary only while the list is empty), one row per item, paging last.

    The list is a top-level screen: no Back/Close row (the reply menu is there).
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=NEW_SUBSCRIPTION_TEXT,
                callback_data=SubscriptionAction(action="create", subscription_id=0).pack(),
                style=None if subs else "primary",
            )
        ]
    ]
    for sub in subs:
        rows.append(
            [
                InlineKeyboardButton(
                    text=subscription_list_label(sub),
                    callback_data=SubscriptionAction(action="open", subscription_id=int(sub["id"])).pack(),
                )
            ]
        )
    if total_pages > 1:
        rows.append(
            pagination_row(
                page,
                total_pages,
                previous_callback=f"{SUBSCRIPTION_LIST_PAGE_PREFIX}{page - 1}",
                next_callback=f"{SUBSCRIPTION_LIST_PAGE_PREFIX}{page + 1}",
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------------ Users section
#
# Users list (top-level: no nav row), member card (hub, ≤6 rows), and the
# card's sub-screens, which return to it with "◀️ To card".

MEMBERS_PER_PAGE = 6
ADD_USER_TEXT = "➕ Add user"
NOT_SIGNED_IN_MARK = "⏳ "
NOT_SIGNED_IN_TAIL = f"{SEP}not signed in"
UNPAID_LIST_MARK = "⚠️ "
UNPAID_LIST_TAIL = f"{SEP}unpaid"
# Every currency picker offers the same codes in the same order, two per row
# (``currency_option_rows``); anything else is typed via "✏️ Other currency".
CURRENCY_OPTIONS = ("RUB", "USD", "EUR", "TRY")
MEMBER_BALANCE_CURRENCIES = CURRENCY_OPTIONS


def currency_option_rows(buttons: Sequence[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    """Currency options two per row, the one layout of every currency picker."""
    return pair_rows(buttons)


def _member_callback(action: str, friend_id: int) -> str:
    return MemberAction(action=action, friend_id=friend_id).pack()


def member_list_button_label(friend: Dict[str, object]) -> str:
    """``'Anna Petrova'``, ``'⚠️ Anna Petrova · unpaid'`` or ``'⏳ Ivan Sidorov · not signed in'``.

    The name is cut, the state tail kept (principle list-rows-and-paging).
    """
    name = str(friend.get("full_name") or "").strip() or "Unknown"
    if friend.get("telegram_id") is None:
        return fit_label(f"{NOT_SIGNED_IN_MARK}{name}", 1, tail=NOT_SIGNED_IN_TAIL)
    if friend.get("unpaid"):
        return fit_label(f"{UNPAID_LIST_MARK}{name}", 1, tail=UNPAID_LIST_TAIL)
    return fit_label(name, 1)


def build_members_list_keyboard(
    friends: Sequence[Dict[str, object]],
    *,
    page: int = 1,
    total_pages: int = 1,
    total_users: int = 0,
) -> InlineKeyboardMarkup:
    """[➕ Add user] (primary only while there are no users), one user per row, paging; no nav row."""
    is_empty = not friends and total_users <= 0
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=ADD_USER_TEXT,
                callback_data=_member_callback("add", 0),
                style="primary" if is_empty else None,
            )
        ]
    ]
    for friend in friends:
        rows.append(
            [
                InlineKeyboardButton(
                    text=member_list_button_label(friend),
                    callback_data=_member_callback("open", int(friend["id"])),
                )
            ]
        )
    if total_pages > 1:
        rows.append(
            pagination_row(
                page,
                total_pages,
                previous_callback=_member_callback("page", page - 1),
                next_callback=_member_callback("page", page + 1),
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def member_detail_keyboard(
    friend_id: int,
    *,
    is_pending: bool = False,
    spawned: bool = False,
) -> InlineKeyboardMarkup:
    """The member card: [💰 Balance][📊 Reports] / [🏦 Method][✏️ Rename] / [🔗 New sign-in link]
    (pending only) / [🗑 Delete] / [◀️ To list]. ``spawned`` (opened as a new message from an
    alert) ends with [✖️ Close] instead."""
    actions = [
        InlineKeyboardButton(text="💰 Balance", callback_data=_member_callback("balance", friend_id)),
        InlineKeyboardButton(text="📊 Reports", callback_data=_member_callback("report", friend_id)),
    ]
    if not is_pending:
        actions.append(
            InlineKeyboardButton(text="🏦 Method", callback_data=_member_callback("payment_destination", friend_id))
        )
    actions.append(InlineKeyboardButton(text="✏️ Rename", callback_data=_member_callback("rename", friend_id)))
    rows = pair_rows(actions)
    if is_pending:
        rows.append(
            [InlineKeyboardButton(text="🔗 New sign-in link", callback_data=_member_callback("invite", friend_id))]
        )
    rows.append([InlineKeyboardButton(text="🗑 Delete", callback_data=_member_callback("delete", friend_id))])
    if spawned:
        rows.append(nav_row(CLOSE_CALLBACK))
    else:
        rows.append(nav_row(_member_callback("back", friend_id), TO_LIST_TEXT))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def member_card_nav_keyboard(friend_id: int) -> InlineKeyboardMarkup:
    """A dead end inside the card's subtree: only [◀️ To card]."""
    return InlineKeyboardMarkup(inline_keyboard=[nav_row(_member_callback("open", friend_id), TO_CARD_TEXT)])


def invite_link_keyboard(invite_link: str, friend_id: int | None = None) -> InlineKeyboardMarkup:
    """[📋 Copy link] (the one call to action) / [◀️ To card] for the sign-in link screen."""
    rows = [
        [
            InlineKeyboardButton(
                text="📋 Copy link",
                copy_text=CopyTextButton(text=invite_link[:COPY_TEXT_MAX_LEN]),
                style="primary",
            )
        ]
    ]
    if friend_id is not None:
        rows.append(nav_row(_member_callback("open", friend_id), TO_CARD_TEXT))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def member_invite_confirm_keyboard(friend_id: int) -> InlineKeyboardMarkup:
    """[✖️ Cancel][🔗 New link]: a new link makes the old one stop working."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            confirm_row(_member_callback("open", friend_id), "🔗 New link", _member_callback("invite_yes", friend_id))
        ]
    )


def member_balance_keyboard(friend_id: int) -> InlineKeyboardMarkup:
    """[✏️ Set amount][💱 Currency] / [◀️ To card]."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Set amount", callback_data=_member_callback("balance_set", friend_id)),
                InlineKeyboardButton(text="💱 Currency", callback_data=_member_callback("balance_currency", friend_id)),
            ],
            nav_row(_member_callback("open", friend_id), TO_CARD_TEXT),
        ]
    )


def member_balance_currency_keyboard(
    friend_id: int,
    current_currency: str,
) -> InlineKeyboardMarkup:
    """[✅ RUB][USD] / [EUR][TRY] (radio, no colour) / [✏️ Other currency] / [◀️ Back] to the balance."""
    normalized_current = (current_currency or "").strip().upper()
    options = [
        InlineKeyboardButton(
            text=radio_label(code, code == normalized_current),
            callback_data=f"member_balance_currency:{friend_id}:{code}",
        )
        for code in MEMBER_BALANCE_CURRENCIES
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            *currency_option_rows(options),
            [
                InlineKeyboardButton(
                    text="✏️ Other currency",
                    callback_data=_member_callback("balance_currency_other", friend_id),
                )
            ],
            nav_row(_member_callback("balance", friend_id)),
        ]
    )


def member_delete_confirm_keyboard(friend_id: int) -> InlineKeyboardMarkup:
    """[✖️ Cancel][🗑 Delete]: Cancel returns to the card."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            confirm_row(_member_callback("open", friend_id), "🗑 Delete", _member_callback("confirm_delete", friend_id))
        ]
    )


def member_report_keyboard(friend_id: int) -> InlineKeyboardMarkup:
    return member_card_nav_keyboard(friend_id)


def member_payment_destination_keyboard(
    friend_id: int,
    destinations: Sequence[Dict[str, object]],
    assigned_destination_id: int | None,
    default_destination_id: int | None,
) -> InlineKeyboardMarkup:
    """[✅ Default (⭐ Sber · RUB)] / one radio row per method ('⭐ Sber · RUB') / [◀️ To card]."""
    rows: list[list[InlineKeyboardButton]] = []
    default_target = next(
        (item for item in destinations if int(item.get("id") or 0) == (default_destination_id or 0)),
        None,
    )
    default_selected = assigned_destination_id is None
    default_label = default_method_option_label(default_target, default_selected)
    rows.append(
        [
            InlineKeyboardButton(
                text=default_label,
                callback_data=f"member_payment_destination_default:{friend_id}",
            )
        ]
    )
    for item in destinations:
        destination_id = int(item["id"])
        method = payment_method_label(item.get("title"), "", default=destination_id == default_destination_id)
        label = fit_label(
            radio_label(method, assigned_destination_id == destination_id),
            1,
            tail=f"{SEP}{str(item.get('currency') or '').strip().upper()}",
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"member_payment_destination:{friend_id}:{destination_id}",
                )
            ]
        )
    rows.append(nav_row(_member_callback("open", friend_id), TO_CARD_TEXT))
    return InlineKeyboardMarkup(inline_keyboard=rows)


ADD_PAYMENT_METHOD_TEXT = "➕ Add payment method"
PENDING_TOPUPS_TEXT = "📥 Pending top-ups"
MAKE_DEFAULT_TEXT = "⭐ Make default"
CLEAR_LINK_TEXT = "🗑 Clear link"
SKIP_TEXT = "⏭ Skip"
# New raw callbacks of the one-message "New payment method" wizard.
PAYMENT_WIZARD_CURRENCY_PREFIX = "paydest_wiz_cur:"
PAYMENT_WIZARD_SKIP_CALLBACK = "paydest_wiz_skip"
PAYMENT_WIZARD_CURRENCIES = CURRENCY_OPTIONS


def _paydest_callback(action: str, destination_id: int = 0) -> str:
    return PaymentDestinationAction(action=action, destination_id=destination_id).pack()


def payment_method_button_label(item: Dict[str, object], default_destination_id: int | None) -> str:
    """``'⭐ Sber · RUB'`` / ``'Revolut · EUR'``: the title is cut, the currency tail is kept."""
    destination_id = int(item.get("id") or 0)
    is_default = default_destination_id is not None and destination_id == default_destination_id
    name = payment_method_label(item.get("title") or f"Method {destination_id}", "", default=is_default)
    currency = str(item.get("currency") or "").strip().upper()
    return fit_label(name, 1, tail=f"{SEP}{currency}" if currency else "")


def payment_destinations_keyboard(
    destinations: Sequence[Dict[str, object]],
    default_destination_id: int | None,
    pending_count: int,
) -> InlineKeyboardMarkup:
    """The 💳 Payment root (top level, no nav row).

    [📥 Pending top-ups (2)] on top (primary) only while some wait / one row per
    method ('⭐ Sber · RUB') / [➕ Add payment method], primary only while there
    is no method and nothing else is due.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if pending_count > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{PENDING_TOPUPS_TEXT} ({pending_count})",
                    callback_data=TopUpAction(action="admin_list", request_id=0).pack(),
                    style="primary",
                )
            ]
        )
    for item in destinations:
        rows.append(
            [
                InlineKeyboardButton(
                    text=payment_method_button_label(item, default_destination_id),
                    callback_data=_paydest_callback("open", int(item["id"])),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=ADD_PAYMENT_METHOD_TEXT,
                callback_data=_paydest_callback("create"),
                style="primary" if not destinations and pending_count <= 0 else None,
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_destination_detail_keyboard(
    destination_id: int,
    *,
    is_default: bool,
) -> InlineKeyboardMarkup:
    """[✏️ Title][💱 Currency] / [📝 Details][🔗 Link] / [⭐ Make default] (not default) / [🗑 Delete] / [◀️ Back]."""
    fields = [
        InlineKeyboardButton(text="✏️ Title", callback_data=_paydest_callback("edit_title", destination_id)),
        InlineKeyboardButton(text="💱 Currency", callback_data=_paydest_callback("edit_currency", destination_id)),
        InlineKeyboardButton(text="📝 Details", callback_data=_paydest_callback("edit_details", destination_id)),
        InlineKeyboardButton(text="🔗 Link", callback_data=_paydest_callback("edit_link", destination_id)),
    ]
    rows = pair_rows(fields)
    if not is_default:
        rows.append(
            [InlineKeyboardButton(text=MAKE_DEFAULT_TEXT, callback_data=_paydest_callback("set_default", destination_id))]
        )
    rows.append([InlineKeyboardButton(text="🗑 Delete", callback_data=_paydest_callback("delete", destination_id))])
    rows.append(nav_row(_paydest_callback("menu")))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_destination_delete_keyboard(destination_id: int) -> InlineKeyboardMarkup:
    """[✖️ Cancel][🗑 Delete]: Cancel returns to the method, Delete (red) keeps its old callback."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            confirm_row(
                _paydest_callback("open", destination_id),
                "🗑 Delete",
                _paydest_callback("confirm_delete", destination_id),
            )
        ]
    )


def payment_wizard_keyboard(step: int) -> InlineKeyboardMarkup:
    """The "New payment method" wizard: quick values above [✖️ Cancel].

    Step 2 offers [RUB][USD] / [EUR][TRY], step 4 offers [⏭ Skip].
    """
    rows: list[list[InlineKeyboardButton]] = []
    if step == 2:
        rows.extend(
            currency_option_rows(
                [
                    InlineKeyboardButton(text=code, callback_data=f"{PAYMENT_WIZARD_CURRENCY_PREFIX}{code}")
                    for code in PAYMENT_WIZARD_CURRENCIES
                ]
            )
        )
    elif step == 4:
        rows.append([InlineKeyboardButton(text=SKIP_TEXT, callback_data=PAYMENT_WIZARD_SKIP_CALLBACK)])
    rows.append([cancel_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_link_clear_rows(destination_id: int) -> list[list[InlineKeyboardButton]]:
    """The quick-value row of the link prompt: [🗑 Clear link] (sits above ✖️ Cancel)."""
    return [[InlineKeyboardButton(text=CLEAR_LINK_TEXT, callback_data=_paydest_callback("clear_link", destination_id))]]


def topup_request_submit_keyboard(
    request_id: int,
    payment_details: str = "",
    payment_link: str = "",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in payment_requisite_rows(payment_details, payment_link):
        builder.row(*row)
    builder.row(
        InlineKeyboardButton(
            text="✅ I paid",
            callback_data=TopUpAction(action="submit", request_id=request_id).pack(),
            style="success",
        )
    )
    builder.row(InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"))
    return builder.as_markup()


def topup_pending_requests_keyboard(requests: Sequence[Dict[str, object]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in requests:
        request_id = int(item["id"])
        title = str(item.get("full_name") or f"User {item.get('friend_id')}")
        amount = float(item.get("amount") or 0.0)
        currency = str(item.get("currency") or "RUB")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{title} · {amount:.2f} {currency}",
                    callback_data=TopUpAction(action="admin_open", request_id=request_id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data=PaymentDestinationAction(action="menu", destination_id=0).pack(),
            ),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topup_request_review_keyboard(request_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Approve",
        callback_data=TopUpAction(action="approve", request_id=request_id).pack(),
        style="success",
    )
    builder.button(
        text="✖️ Reject",
        callback_data=TopUpAction(action="reject", request_id=request_id).pack(),
        style="danger",
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=TopUpAction(action="admin_list", request_id=0).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def topup_destination_keyboard(
    destinations: Sequence[Dict[str, object]],
    default_destination_id: int | None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in destinations:
        destination_id = int(item["id"])
        prefix = "⭐ " if default_destination_id == destination_id else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix}{item['title']} ({item['currency']})",
                    callback_data=f"topup_destination:{destination_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="✖️ Close", callback_data="menu:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def public_account_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✏️ Rename",
        callback_data="public_account:rename",
    )
    builder.button(
        text="➕ Top up",
        callback_data=TopUpAction(action="start", request_id=0).pack(),
        style="primary",
    )
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"))
    return builder.as_markup()


# One name per concept on every screen (same-name-everywhere): the display
# currency is "🌐 Shown in" and the reminder time "⏰ Reminder time" for admins
# and members alike; "💱 Currency" is only the billed or stored currency.
SHOWN_IN_TEXT = "🌐 Shown in"
REMINDER_TIME_TEXT = "⏰ Reminder time"


def build_public_subscription_list_keyboard(subs: Sequence[Dict[str, object]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for sub in subs:
        label = str(sub["name"])
        builder.button(
            text=label,
            callback_data=SubscriptionAction(action="open_public", subscription_id=sub["id"]).pack(),
        )
    builder.adjust(1)
    return builder.as_markup()


def _sub_button(text: str, action: str, subscription_id: int, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=SubscriptionAction(action=action, subscription_id=subscription_id).pack(),
        style=style,
    )


ADD_PARTICIPANTS_TEXT = "👥 Add participants"


def subscription_detail_keyboard(
    subscription_id: int,
    *,
    participant_count: int | None = None,
    close: bool = False,
) -> InlineKeyboardMarkup:
    """The subscription hub card (at most 6 rows with nav).

    [👥 Add participants] leads, styled primary, only while nobody is in the
    subscription. ``close=True`` is the card opened as a NEW message from a
    push or report: its only nav is [✖️ Close]; otherwise [◀️ To list].
    """
    rows: list[list[InlineKeyboardButton]] = []
    if participant_count == 0:
        rows.append([_sub_button(ADD_PARTICIPANTS_TEXT, "participants_manage", subscription_id, "primary")])
    rows.extend(
        [
            [
                _sub_button("💰 Pricing", "pricing", subscription_id),
                _sub_button("👥 Participants", "participants", subscription_id),
            ],
            [
                _sub_button("🔔 Reminders", "reminders", subscription_id),
                _sub_button("💳 Payment", "paymentinfo", subscription_id),
            ],
            [
                _sub_button("📊 Reports", "report", subscription_id),
                _sub_button("🗂 Open cycles", "cycles", subscription_id),
            ],
            [
                _sub_button("✏️ Rename", "rename", subscription_id),
                _sub_button("🗑 Delete", "delete", subscription_id),
            ],
        ]
    )
    if close:
        rows.append([nav_button(CLOSE_CALLBACK)])
    else:
        back = SubscriptionAction(action="back", subscription_id=subscription_id).pack()
        rows.append([nav_button(back, TO_LIST_TEXT)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_delete_confirm_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    """[✖️ Cancel] back to the card, [🗑 Delete] (red) on the existing confirm callback."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            confirm_row(
                SubscriptionAction(action="open", subscription_id=subscription_id).pack(),
                "🗑 Delete",
                SubscriptionAction(action="confirm_delete", subscription_id=subscription_id).pack(),
            )
        ]
    )


def public_subscription_detail_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="💱 My currency",
        callback_data=SubscriptionAction(action="public_currency", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="⏰ My reminder time",
        callback_data=SubscriptionAction(action="public_remindertime", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="📊 Reports",
        callback_data=SubscriptionAction(action="public_report", subscription_id=subscription_id).pack(),
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="public_back", subscription_id=subscription_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


# Reports and cycles (WP8): the subscription report, open cycles, cycle actions
# and their confirmations, and the admin's global 📊 Reports.
RECREATE_CYCLE_TEXT = "♻️ Recreate"
CLOSE_CYCLE_TEXT = "🔒 Close cycle"
CYCLE_RECREATE_YES = "recreate_yes"
CYCLE_FORCE_CLOSE_YES = "force_close_yes"
# Admin 📊 Reports: at most this many '🔴 Netflix' buttons under the report.
ADMIN_REPORT_BUTTONS_MAX = 8


def _cycle_callback(action: str, subscription_id: int, due_value: str) -> str:
    return CycleAction(action=action, subscription_id=subscription_id, due_date=due_value).pack()


def cycle_short_date(due_value: str, today: date | None = None) -> str:
    """``'01.10'``; ``'01.10.2025'`` when the cycle is in another year than ``today``."""
    try:
        due = datetime.strptime(due_value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return str(due_value)
    if today is not None and due.year != today.year:
        return due.strftime("%d.%m.%Y")
    return due.strftime("%d.%m")


def cycle_progress_words(paid: int, total: int) -> str:
    """``'1 of 3 paid'`` / ``'all paid'`` / ``'no participants'``."""
    if total <= 0:
        return "no participants"
    if paid >= total:
        return "all paid"
    return f"{paid} of {total} paid"


def open_cycle_button_label(cycle: Dict[str, object] | str, today: date | None = None) -> str:
    """``'📅 01.10 · 1 of 3 paid'`` (a bare due-date string gives ``'📅 01.10'``)."""
    if isinstance(cycle, str):
        return f"📅 {cycle_short_date(cycle, today)}"
    short = cycle_short_date(str(cycle.get("due_value") or ""), today)
    words = cycle_progress_words(int(cycle.get("paid") or 0), int(cycle.get("total") or 0))  # type: ignore[call-overload]
    return f"📅 {short}{SEP}{words}"


def build_back_keyboard(subscription_id: int, back_action: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action=back_action, subscription_id=subscription_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def subscription_report_keyboard(
    subscription_id: int,
    unpaid_count: int = 0,
    *,
    empty: bool = False,
) -> InlineKeyboardMarkup:
    """[📬 Remind unpaid (2)] (primary, only while someone is overdue) / [◀️ To card].

    ``empty`` (no participants): [👥 Add participants] (primary) instead, as in Send now.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if empty:
        rows.append([_sub_button(ADD_PARTICIPANTS_TEXT, "participants_manage", subscription_id, "primary")])
    elif unpaid_count > 0:
        rows.append([_sub_button(remind_unpaid_label(unpaid_count), "reminders_send", subscription_id, "primary")])
    rows.append([nav_button(SubscriptionAction(action="open", subscription_id=subscription_id).pack(), TO_CARD_TEXT)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_open_cycles_keyboard(
    subscription_id: int,
    cycles: Sequence[Dict[str, object] | str] = (),
    *,
    today: date | None = None,
) -> InlineKeyboardMarkup:
    """One '📅 01.10 · 1 of 3 paid' per open cycle (cycle:open, unchanged) / [◀️ To card]."""
    rows: list[list[InlineKeyboardButton]] = []
    for cycle in cycles:
        due_value = cycle if isinstance(cycle, str) else str(cycle.get("due_value") or "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=open_cycle_button_label(cycle, today),
                    callback_data=_cycle_callback("open", subscription_id, due_value),
                )
            ]
        )
    rows.append([nav_button(SubscriptionAction(action="open", subscription_id=subscription_id).pack(), TO_CARD_TEXT)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_cycle_actions_keyboard(subscription_id: int, due_value: str) -> InlineKeyboardMarkup:
    """[♻️ Recreate][🔒 Close cycle] (each opens its confirmation) / [◀️ To list] (open cycles)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=RECREATE_CYCLE_TEXT,
                    callback_data=_cycle_callback("recreate", subscription_id, due_value),
                ),
                InlineKeyboardButton(
                    text=CLOSE_CYCLE_TEXT,
                    callback_data=_cycle_callback("force_close", subscription_id, due_value),
                ),
            ],
            [nav_button(SubscriptionAction(action="cycles", subscription_id=subscription_id).pack(), TO_LIST_TEXT)],
        ]
    )


def cycle_recreate_confirm_keyboard(subscription_id: int, due_value: str) -> InlineKeyboardMarkup:
    """[✖️ Cancel] back to the cycle, [♻️ Recreate] (red, cycle:recreate_yes)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            confirm_row(
                _cycle_callback("open", subscription_id, due_value),
                RECREATE_CYCLE_TEXT,
                _cycle_callback(CYCLE_RECREATE_YES, subscription_id, due_value),
            )
        ]
    )


def cycle_close_confirm_keyboard(subscription_id: int, due_value: str) -> InlineKeyboardMarkup:
    """[✖️ Cancel] back to the cycle, [🔒 Close cycle] (red, cycle:force_close_yes)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            confirm_row(
                _cycle_callback("open", subscription_id, due_value),
                CLOSE_CYCLE_TEXT,
                _cycle_callback(CYCLE_FORCE_CLOSE_YES, subscription_id, due_value),
            )
        ]
    )


def new_subscription_keyboard() -> InlineKeyboardMarkup:
    """[➕ New subscription] (primary): the forward button of an empty 📊 Reports."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=NEW_SUBSCRIPTION_TEXT,
                    callback_data=SubscriptionAction(action="create", subscription_id=0).pack(),
                    style="primary",
                )
            ]
        ]
    )


def admin_report_keyboard(subs: Sequence[Dict[str, object]]) -> InlineKeyboardMarkup | None:
    """One '🔴 Netflix' per subscription that needs attention (at most 8).

    Each opens the admin card as a NEW message ending in [✖️ Close]
    (sub:open_new). The report is a top-level screen: no nav row. None when
    nothing needs attention.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=fit_label(f"🔴 {' '.join(str(sub.get('name') or '').split())}", 1),
                callback_data=SubscriptionAction(action="open_new", subscription_id=int(sub["id"])).pack(),  # type: ignore[call-overload]
            )
        ]
        for sub in list(subs)[:ADMIN_REPORT_BUTTONS_MAX]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def public_subscription_report_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    return build_back_keyboard(subscription_id, "open_public")


# Reminders subtree (WP6): Reminders, Reminder time, the Days prompt and Send
# now with its confirmation. Callbacks are the shipped ones; the confirmation's
# Send is the new ReminderSendConfirmAction ('remsendok') and the Days quick
# value the raw 'remdays_default:<sub>'.
REMINDER_TIME_BUTTON_TEXT = REMINDER_TIME_TEXT
REMINDER_DAYS_TEXT = "📅 Days"
AFTER_DUE_TEXT = "📣 After due date"
AFTER_DUE_ON_TEXT = "daily until paid"


def after_due_line(on: bool) -> str:
    """``'After due date: daily until paid'`` / ``'… off'``: one wording for the card, the screen and the toast."""
    return f"After due date: {AFTER_DUE_ON_TEXT if on else 'off'}"

SEND_NOW_TEXT = "📬 Send now"
SEND_TEXT = "📬 Send"
REMIND_UNPAID_TEXT = "📬 Remind unpaid"
REMINDER_DAYS_DEFAULT_PREFIX = "remdays_default:"
SEND_STATUS_UNPAID = "unpaid"
SEND_STATUS_PAID = "paid"


def reminders_callback(subscription_id: int) -> str:
    return SubscriptionAction(action="reminders", subscription_id=subscription_id).pack()


def reminder_send_callback(subscription_id: int) -> str:
    return SubscriptionAction(action="reminders_send", subscription_id=subscription_id).pack()


def reminder_settings_keyboard(subscription_id: int, *, after_due: bool = False) -> InlineKeyboardMarkup:
    """[⏰ Reminder time] / [📅 Days] / [📣 After due date · 🟢] / [📬 Send now] / [◀️ To card].

    "⏰ Reminder time" is the one name of the setting, too wide to share a row.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_sub_button(REMINDER_TIME_BUTTON_TEXT, "remindertime", subscription_id)],
            [_sub_button(REMINDER_DAYS_TEXT, "reminderdays", subscription_id)],
            [_sub_button(toggle_label(AFTER_DUE_TEXT, after_due), "reminderloop", subscription_id)],
            [_sub_button(SEND_NOW_TEXT, "reminders_send", subscription_id)],
            [
                nav_button(
                    SubscriptionAction(action="open", subscription_id=subscription_id).pack(),
                    TO_CARD_TEXT,
                )
            ],
        ]
    )


def reminder_days_default_label(offsets_text: str) -> str:
    """``'↺ 1 day before + on the day'`` from the display text of the default offsets."""
    return f"↺ {offsets_text.replace(', ', ' + ')}"


def reminder_days_quick_rows(subscription_id: int, offsets_text: str) -> list[list[InlineKeyboardButton]]:
    """The Days prompt's quick value, above its [✖️ Cancel]."""
    return [
        [
            InlineKeyboardButton(
                text=reminder_days_default_label(offsets_text),
                callback_data=f"{REMINDER_DAYS_DEFAULT_PREFIX}{subscription_id}",
            )
        ]
    ]


def remind_unpaid_label(count: int) -> str:
    return f"{REMIND_UNPAID_TEXT} ({count})"


def reminder_send_person_label(person: Dict[str, object]) -> str:
    """``'⚠️ Alice Smith · unpaid'`` / ``'✅ Bob · paid'`` / ``'Carol'``; only the name is cut."""
    name = " ".join(str(person.get("full_name") or "Unknown").split())
    status = person.get("status", SEND_STATUS_UNPAID)
    if status == SEND_STATUS_UNPAID:
        return fit_label(f"⚠️ {name}", 1, tail=f"{SEP}unpaid")
    if status == SEND_STATUS_PAID:
        return fit_label(f"✅ {name}", 1, tail=f"{SEP}paid")
    return fit_label(name, 1)


def reminder_send_empty_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    """Send now with no participants: [👥 Add participants] / [◀️ To reminders]."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_sub_button(ADD_PARTICIPANTS_TEXT, "participants_manage", subscription_id, "primary")],
            [nav_button(reminders_callback(subscription_id), TO_REMINDERS_TEXT)],
        ]
    )


def reminder_send_confirm_keyboard(subscription_id: int, telegram_id: int) -> InlineKeyboardMarkup:
    """[✖️ Cancel → Send now][📬 Send (red)]; ``telegram_id`` 0 means every unpaid person."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            confirm_row(
                reminder_send_callback(subscription_id),
                SEND_TEXT,
                ReminderSendConfirmAction(subscription_id=subscription_id, telegram_id=telegram_id).pack(),
            )
        ]
    )


# Participants subtree (WP5): the Participants screen, the people picker,
# Amount per user and its prompts. Selection is a ✅ prefix or a 🟢/⚪ toggle,
# never a colour; the only coloured buttons are the empty-state call to action
# and the red verb of the Set-all confirmation.
CHOOSE_PEOPLE_TEXT = "👥 Choose people"
SHARE_COUNT_TEXT = "➗ Share count"
AMOUNT_PER_USER_TEXT = "💵 Amount per user"
SET_ALL_TEXT = "✏️ Set all"
SET_AMOUNT_MARK = " ✏️"
PARTICIPANTS_PAGE_SIZE = 20
PARTICIPANTS_PAGE_PREFIX = "spart_page:"
USER_AMOUNT_ALL_YES_PREFIX = "sub_user_amount_all_yes:"


def _participants_back(subscription_id: int) -> InlineKeyboardButton:
    return nav_button(SubscriptionAction(action="participants", subscription_id=subscription_id).pack())


def participants_settings_keyboard(
    subscription_id: int,
    current_mode: str,
    *,
    participant_count: int | None = None,
) -> InlineKeyboardMarkup:
    """[👥 Choose people] / [✅ Split by shares][Fixed per user] / [➗ Share count | 💵 Amount per user] / [◀️ To card].

    While nobody is in the subscription the first button is the call to action
    (primary) and reads "👥 Add participants", as on the card. The mode radio keeps ``sub_payment_mode:<id>:<mode>``;
    a label too wide for half a row gets a row of its own.
    """
    mode = (current_mode or PAYMENT_MODE_SPLIT).strip().lower()
    if mode != PAYMENT_MODE_FIXED:
        mode = PAYMENT_MODE_SPLIT
    # Empty: the same CTA as on the card and in Send now, "👥 Add participants".
    empty = participant_count == 0
    rows: list[list[InlineKeyboardButton]] = [
        [
            _sub_button(
                ADD_PARTICIPANTS_TEXT if empty else CHOOSE_PEOPLE_TEXT,
                "participants_manage",
                subscription_id,
                "primary" if empty else None,
            )
        ]
    ]
    rows.extend(
        pair_rows(
            [
                InlineKeyboardButton(
                    text=radio_label(SPLIT_BY_SHARES_LABEL, mode == PAYMENT_MODE_SPLIT),
                    callback_data=f"sub_payment_mode:{subscription_id}:{PAYMENT_MODE_SPLIT}",
                ),
                InlineKeyboardButton(
                    text=radio_label(FIXED_PER_USER_LABEL, mode == PAYMENT_MODE_FIXED),
                    callback_data=f"sub_payment_mode:{subscription_id}:{PAYMENT_MODE_FIXED}",
                ),
            ]
        )
    )
    if mode == PAYMENT_MODE_FIXED:
        rows.append([_sub_button(AMOUNT_PER_USER_TEXT, "useramounts", subscription_id)])
    else:
        rows.append([_sub_button(SHARE_COUNT_TEXT, "share", subscription_id)])
    rows.append([nav_button(SubscriptionAction(action="open", subscription_id=subscription_id).pack(), TO_CARD_TEXT)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Subscription payment info, method and comment (WP7).
SUB_METHOD_TEXT = "🏦 Method"
SUB_COMMENT_TEXT = "📝 Comment"
CLEAR_COMMENT_TEXT = "🗑 Clear comment"
SUB_PAYMENT_DESTINATION_PREFIX = "sub_payment_destination:"
DEFAULT_METHOD_OPTION = "Default"
NO_DEFAULT_METHOD_NAME = "not set"


def subscription_payment_info_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    """💳 Payment of one subscription: [🏦 Method][📝 Comment] / [◀️ To card]."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _sub_button(SUB_METHOD_TEXT, "paymentmethod", subscription_id),
                _sub_button(SUB_COMMENT_TEXT, "comment", subscription_id),
            ],
            nav_row(SubscriptionAction(action="open", subscription_id=subscription_id).pack(), TO_CARD_TEXT),
        ]
    )


def _destination_id(item: Dict[str, object]) -> int | None:
    try:
        return int(item.get("id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def subscription_method_option_label(
    item: Dict[str, object],
    *,
    selected: bool,
    is_default: bool,
) -> str:
    """``'✅ ⭐ Sber · RUB'``: the title is cut to the row, the currency is kept whole."""
    title = " ".join(str(item.get("title") or "").split())
    currency = str(item.get("currency") or "").strip().upper()
    head = radio_label(f"{DEFAULT_METHOD_MARK if is_default else ''}{title}", selected)
    return fit_label(head, 1, tail=f"{SEP}{currency}" if currency else "")


def subscription_payment_destination_keyboard(
    subscription_id: int,
    destinations: Sequence[Dict[str, object]],
    selected_destination_id: int | None,
    default_destination_id: int | None,
) -> InlineKeyboardMarkup:
    """🏦 Payment method picker: [✅ Default (⭐ Sber · RUB)] / one row per method (⭐ = default) / [◀️ To payment].

    The current choice carries "✅ " (Default when ``selected_destination_id``
    is None or names a method that no longer exists). With no methods at all
    only the way back is left: Default would mean nothing. Callbacks are the
    old ``sub_payment_destination:<sub>:default|<id>``.
    """
    back = nav_row(SubscriptionAction(action="paymentinfo", subscription_id=subscription_id).pack(), TO_PAYMENT_TEXT)
    if not destinations:
        return InlineKeyboardMarkup(inline_keyboard=[back])
    known_ids = {_destination_id(item) for item in destinations}
    uses_default = selected_destination_id is None or selected_destination_id not in known_ids
    default_target = next(
        (item for item in destinations if default_destination_id is not None and _destination_id(item) == default_destination_id),
        None,
    )
    default_label = default_method_option_label(default_target, uses_default)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=default_label,
                callback_data=f"{SUB_PAYMENT_DESTINATION_PREFIX}{subscription_id}:default",
            )
        ]
    ]
    for item in destinations:
        destination_id = _destination_id(item)
        if destination_id is None:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=subscription_method_option_label(
                        item,
                        selected=not uses_default and selected_destination_id == destination_id,
                        is_default=destination_id == default_destination_id,
                    ),
                    callback_data=f"{SUB_PAYMENT_DESTINATION_PREFIX}{subscription_id}:{destination_id}",
                )
            ]
        )
    rows.append(back)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_reminder_time_keyboard(
    subscription_id: int,
    current_time: str,
    default_time: str,
    *,
    has_override: bool | None = None,
) -> InlineKeyboardMarkup:
    """[✅ Default (10:00)] / presets two per row / [✏️ Other time] / [◀️ To reminders].

    Default is selected only while the subscription has no own time
    (``has_override`` False), even when an own time equals the default; a
    preset is selected when it is that own time. ``has_override=None``
    guesses from ``current != default`` (old call shape).
    """
    current = (current_time or "").strip()
    default = (default_time or "").strip()
    if has_override is None:
        has_override = current != default
    rows: list[list[InlineKeyboardButton]] = [
        [_sub_button(radio_label(f"Default ({default})", not has_override), "remindertime_default", subscription_id)]
    ]
    rows.extend(
        pair_rows(
            InlineKeyboardButton(
                text=radio_label(value, has_override and value == current),
                callback_data=f"sub_remindertime:{subscription_id}:{value}",
            )
            for value in TIME_PRESETS
        )
    )
    rows.append([InlineKeyboardButton(text=OTHER_TIME_TEXT, callback_data=f"sub_remindertime_other:{subscription_id}")])
    rows.append([nav_button(reminders_callback(subscription_id), TO_REMINDERS_TEXT)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def public_subscription_reminder_time_keyboard(
    subscription_id: int,
    current_time: str,
    default_time: str,
) -> InlineKeyboardMarkup:
    presets = ("09:00", "12:00", "16:00", "20:00")
    normalized_current = current_time.strip()
    normalized_default = default_time.strip()
    default_active = normalized_current == normalized_default
    rows: list[list[InlineKeyboardButton]] = []
    preset_buttons: list[InlineKeyboardButton] = []

    for time_value in presets:
        is_active = (time_value == normalized_current) and not default_active
        if is_active:
            preset_buttons.append(
                InlineKeyboardButton(
                    text=time_value,
                    callback_data=f"public_sub_remindertime:{subscription_id}:{time_value}",
                    style="success",
                )
            )
        else:
            preset_buttons.append(
                InlineKeyboardButton(
                    text=time_value,
                    callback_data=f"public_sub_remindertime:{subscription_id}:{time_value}",
                )
            )
    for index in range(0, len(preset_buttons), 2):
        rows.append(preset_buttons[index : index + 2])

    rows.append([InlineKeyboardButton(text="Other", callback_data=f"public_sub_remindertime_other:{subscription_id}")])
    if default_active:
        default_button = InlineKeyboardButton(
            text="Default",
            callback_data=SubscriptionAction(
                action="public_remindertime_default",
                subscription_id=subscription_id,
            ).pack(),
            style="success",
        )
    else:
        default_button = InlineKeyboardButton(
            text="Default",
            callback_data=SubscriptionAction(
                action="public_remindertime_default",
                subscription_id=subscription_id,
            ).pack(),
        )
    rows.append([default_button])
    rows.append(
        [
            InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="open_public", subscription_id=subscription_id).pack(),
            ),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminder_send_targets_keyboard(
    subscription_id: int,
    participants: Sequence[Dict[str, object]],
) -> InlineKeyboardMarkup:
    """[📬 Remind unpaid (2)] (primary, only when someone is unpaid) / one row per person / [◀️ To reminders].

    ``participants`` carry ``full_name``, ``telegram_id`` and ``status``
    (SEND_STATUS_UNPAID — the default —, SEND_STATUS_PAID, or None when the
    person is in no open cycle). People without a Telegram id are left out.
    Every button opens the confirmation; a paid row only answers with a toast.
    """
    people = [person for person in participants if int(person.get("telegram_id") or 0) > 0]
    unpaid = sum(1 for person in people if person.get("status", SEND_STATUS_UNPAID) == SEND_STATUS_UNPAID)
    rows: list[list[InlineKeyboardButton]] = []
    if unpaid:
        rows.append(
            [
                InlineKeyboardButton(
                    text=remind_unpaid_label(unpaid),
                    callback_data=ReminderSendAction(subscription_id=subscription_id, telegram_id=0).pack(),
                    style="primary",
                )
            ]
        )
    for person in people:
        rows.append(
            [
                InlineKeyboardButton(
                    text=reminder_send_person_label(person),
                    callback_data=ReminderSendAction(
                        subscription_id=subscription_id,
                        telegram_id=int(person["telegram_id"]),
                    ).pack(),
                )
            ]
        )
    rows.append([nav_button(reminders_callback(subscription_id), TO_REMINDERS_TEXT)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_tests_keyboard(
    friends: Sequence[Dict[str, object]],
    *,
    page: int = 1,
    total_pages: int = 1,
    total_users: int = 0,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    columns = 2 if total_users > 4 else 1
    friend_buttons = [
        InlineKeyboardButton(
            text=str(friend.get("full_name") or "Unknown"),
            callback_data=TestSendAction(telegram_id=int(friend["telegram_id"])).pack(),
        )
        for friend in friends
    ]
    for index in range(0, len(friend_buttons), columns):
        rows.append(friend_buttons[index : index + columns])

    if total_pages > 1:
        rows.append(
            pagination_row(
                page,
                total_pages,
                previous_callback=TestListAction(action="page", page=page - 1).pack(),
                next_callback=TestListAction(action="page", page=page + 1).pack(),
            )
        )

    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Back", callback_data="settings:tests"),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)

OTHER_CURRENCY_TEXT = "✏️ Other currency"
SUBSCRIPTION_CURRENCY_OPTIONS = CURRENCY_OPTIONS
SHOWN_IN_CURRENCY_OPTIONS = CURRENCY_OPTIONS
MOVE_CYCLE_TEXT = "🔀 Move cycle"
NEW_CYCLE_TEXT = "🆕 New cycle"


def tests_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📬 Send test reminders", callback_data="tests:send", style="primary")
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="⬅️ Back", callback_data="settings:menu"),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def pricing_settings_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    """[💰 Amount][💱 Currency] / [🌐 Shown in][📅 Next charge] / [🔁 Period] / [◀️ To card]."""

    def action(label: str, name: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=label,
            callback_data=SubscriptionAction(action=name, subscription_id=subscription_id).pack(),
        )

    rows = pair_rows(
        [
            action("💰 Amount", "amount"),
            action("💱 Currency", "currency"),
            action(SHOWN_IN_TEXT, "basecurrency"),
            action("📅 Next charge", "duedate"),
            action("🔁 Period", "period"),
        ]
    )
    rows.append([nav_button(SubscriptionAction(action="open", subscription_id=subscription_id).pack(), TO_CARD_TEXT)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_amount_button_label(person: Dict[str, object], subscription_amount: float) -> str:
    """``'Alice Smith · 10.00 ✏️'`` (a set amount) / ``'Bob · 15.99'`` / ``'Boris · 10.00 ×2'``.

    The amount is what one share costs that person; ``×N`` is their weight, so
    the row reads as what they pay (the card shows the product). The name is
    cut to the row, the tail is kept whole.
    """
    name = " ".join(str(person.get("full_name") or "Unknown").split())
    weight = int(person.get("share_weight") or 1)  # type: ignore[arg-type]
    fixed_amount = person.get("fixed_amount")
    is_set = False
    try:
        value = float(fixed_amount) if fixed_amount is not None else float(subscription_amount)  # type: ignore[arg-type]
        is_set = fixed_amount is not None and value >= 0
    except (TypeError, ValueError):
        value = float(subscription_amount)
    if value < 0:
        value, is_set = float(subscription_amount), False
    tail = f"{SEP}{format_number(value)}"
    if weight > 1:
        tail += f" ×{weight}"
    if is_set:
        tail += SET_AMOUNT_MARK
    return fit_label(name, 1, tail=tail)


def subscription_user_amounts_keyboard(
    subscription_id: int,
    participants: Sequence[Dict[str, object]],
    currency: str,
    subscription_amount: float,
) -> InlineKeyboardMarkup:
    """One person per row, then [✏️ Set all] (only with people), then [◀️ Back] (to Participants). No colour."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=user_amount_button_label(person, subscription_amount),
                callback_data=f"sub_user_amount:{subscription_id}:{int(person['id'])}",  # type: ignore[call-overload]
            )
        ]
        for person in participants
    ]
    if participants:
        rows.append([InlineKeyboardButton(text=SET_ALL_TEXT, callback_data=f"sub_user_amount_all:{subscription_id}")])
    else:
        # Never a dead end: the way forward is to add people.
        rows.append([_sub_button(ADD_PARTICIPANTS_TEXT, "participants_manage", subscription_id, "primary")])
    rows.append([_participants_back(subscription_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_amount_clear_keyboard(
    subscription_id: int,
    friend_id: int,
) -> InlineKeyboardMarkup:
    """The per-user amount prompt with a set amount: one row [✖️ Cancel][↺ Use default] (no colour).

    "Use default" keeps the old ``sub_user_amount_clear:<id>:<friend>`` callback.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                dialog_cancel_inline_button(),
                InlineKeyboardButton(
                    text=USE_DEFAULT_TEXT,
                    callback_data=f"sub_user_amount_clear:{subscription_id}:{friend_id}",
                ),
            ]
        ],
    )


def user_amount_all_confirm_keyboard(subscription_id: int, cents: int) -> InlineKeyboardMarkup:
    """[✖️ Cancel] back to Amount per user, [✏️ Set all] (red) applying the amount in the question."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            confirm_row(
                SubscriptionAction(action="useramounts", subscription_id=subscription_id).pack(),
                SET_ALL_TEXT,
                f"{USER_AMOUNT_ALL_YES_PREFIX}{int(subscription_id)}:{int(cents)}",
            )
        ]
    )


def subscription_base_currency_keyboard(
    subscription_id: int,
    current_currency: str,
) -> InlineKeyboardMarkup:
    """'🌐 Shown in' picker: [✅ RUB][USD] / [EUR][TRY] / [✏️ Other currency] / [◀️ Back] (to Pricing)."""
    current = (current_currency or "").strip().upper()
    buttons = [
        InlineKeyboardButton(
            text=radio_label(code, code == current),
            callback_data=f"sub_base_currency:{subscription_id}:{code}",
        )
        for code in SHOWN_IN_CURRENCY_OPTIONS
    ]
    rows = currency_option_rows(buttons)
    rows.append(
        [
            InlineKeyboardButton(
                text=OTHER_CURRENCY_TEXT,
                callback_data=SubscriptionAction(action="basecurrency_other", subscription_id=subscription_id).pack(),
            )
        ]
    )
    rows.append([nav_button(SubscriptionAction(action="pricing", subscription_id=subscription_id).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def participant_toggle_label(friend: Dict[str, object], buttons_in_row: int = 1) -> str:
    """``'Alice Smith · 🟢'`` (in the subscription) / ``'Carol · ⚪'``; only the name is cut."""
    name = " ".join(str(friend.get("full_name") or "Unknown").split())
    on = bool(friend.get("is_member"))
    return fit_label(name, buttons_in_row, tail=f"{SEP}{TOGGLE_ON if on else TOGGLE_OFF}")


def build_participants_keyboard(
    friends: Sequence[Dict[str, object]],
    subscription_id: int,
    *,
    payment_mode: str = PAYMENT_MODE_SPLIT,
    page: int = 1,
    total_pages: int = 1,
    close: bool = False,
) -> InlineKeyboardMarkup:
    """The people picker: [Alice Smith · 🟢][×2] / [Carol · ⚪], paging, then [◀️ Back] (to Participants).

    ``friends`` is the page to show. The ``×N`` share button sits next to a
    person who is in, and only in split mode. Callbacks ``spart:*`` are
    unchanged; paging is ``spart_page:<sub>:<n>``. ``close`` is the copy
    opened as a new message from a push: it ends in [✖️ Close] instead.
    """
    split = (payment_mode or PAYMENT_MODE_SPLIT).strip().lower() != PAYMENT_MODE_FIXED
    rows: list[list[InlineKeyboardButton]] = []
    for friend in friends:
        with_weight = split and bool(friend.get("is_member"))
        toggle_button = InlineKeyboardButton(
            text=participant_toggle_label(friend, 2 if with_weight else 1),
            callback_data=ParticipantAction(
                action="toggle",
                subscription_id=subscription_id,
                friend_id=int(friend["id"]),  # type: ignore[call-overload]
            ).pack(),
        )
        if not with_weight:
            rows.append([toggle_button])
            continue
        weight_button = InlineKeyboardButton(
            text=f"×{int(friend.get('share_weight') or 1)}",  # type: ignore[call-overload]
            callback_data=ParticipantAction(
                action="weight",
                subscription_id=subscription_id,
                friend_id=int(friend["id"]),  # type: ignore[call-overload]
            ).pack(),
        )
        rows.append([toggle_button, weight_button])
    if total_pages > 1:
        rows.append(
            pagination_row(
                page,
                total_pages,
                previous_callback=f"{PARTICIPANTS_PAGE_PREFIX}{subscription_id}:{page - 1}",
                next_callback=f"{PARTICIPANTS_PAGE_PREFIX}{subscription_id}:{page + 1}",
            )
        )
    rows.append([nav_button(CLOSE_CALLBACK) if close else _participants_back(subscription_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _option_rows(buttons: Sequence[InlineKeyboardButton], *, max_per_row: int = 4) -> list[list[InlineKeyboardButton]]:
    """Short picker options ``max_per_row`` to a row when every label fits; else in pairs (2×2)."""
    if all(label_units(button.text) <= row_budget(max_per_row) for button in buttons):
        return [list(buttons[start:start + max_per_row]) for start in range(0, len(buttons), max_per_row)]
    return pair_rows(buttons)


def build_currency_keyboard(
    options: Iterable[str],
    *,
    selected: str | None = None,
    cancel_callback: str = CANCEL_CALLBACK,
) -> InlineKeyboardMarkup:
    """Currency quick picks (``currency:XXX``, unchanged) above [✖️ Cancel]; the current one gets ✅."""
    current = (selected or "").strip().upper()
    buttons = [
        InlineKeyboardButton(text=radio_label(code.upper(), code.upper() == current), callback_data=f"currency:{code.upper()}")
        for code in options
    ]
    rows = currency_option_rows(buttons)
    rows.append([cancel_button(cancel_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⏰ Base time", callback_data="settings:time")
    builder.button(text="🌍 Timezone", callback_data="settings:timezone")
    builder.button(text="🔢 Rounding", callback_data="settings:rounding")
    builder.button(text="🔔 Notifications", callback_data="settings:notifications")
    builder.button(text="🧪 Tests", callback_data="settings:tests")
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def settings_time_keyboard(current_time: str) -> InlineKeyboardMarkup:
    presets = ("09:00", "12:00", "16:00", "20:00")
    builder = InlineKeyboardBuilder()
    for time_value in presets:
        is_active = time_value == current_time
        text = f"{'✅ ' if is_active else ''}{time_value}"
        if is_active:
            builder.button(text=text, callback_data=f"settings_time:{time_value}", style="success")
        else:
            builder.button(text=text, callback_data=f"settings_time:{time_value}")
    builder.button(text="Other", callback_data="settings:time_other")
    builder.adjust(2, 2, 1)
    builder.row(
        InlineKeyboardButton(text="⬅️ Back", callback_data="settings:menu"),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def settings_timezone_keyboard(current_timezone: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    zone_buttons: list[InlineKeyboardButton] = []
    for zone in COMMON_TIMEZONES:
        is_active = zone == current_timezone
        text = f"{'✅ ' if is_active else ''}{zone}"
        if is_active:
            zone_buttons.append(
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"settings_timezone:{zone}",
                    style="success",
                )
            )
        else:
            zone_buttons.append(
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"settings_timezone:{zone}",
                )
            )
    for index in range(0, len(zone_buttons), 2):
        rows.append(zone_buttons[index : index + 2])
    rows.append([InlineKeyboardButton(text="Other", callback_data="settings:timezone_other")])
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Back", callback_data="settings:menu"),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


OTHER_TIME_TEXT = "✏️ Other time"
USE_DEFAULT_TEXT = "↺ Use default"
TIME_PRESETS = ("09:00", "12:00", "16:00", "20:00")


def public_settings_keyboard(
    current_time: str,
    current_timezone: str,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⏰ Base time", callback_data="public_settings:time")
    builder.button(text="🌍 Timezone", callback_data="public_settings:timezone")
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def public_subscription_currency_keyboard(
    subscription_id: int,
    current_currency: str,
    default_currency: str,
) -> InlineKeyboardMarkup:
    options = ("USD", "EUR", "RUB")
    normalized_current = current_currency.upper()
    normalized_default = default_currency.upper()
    default_active = normalized_current == normalized_default
    rows: list[list[InlineKeyboardButton]] = []
    option_buttons: list[InlineKeyboardButton] = []
    for code in options:
        is_active = (code == normalized_current) and not default_active
        if is_active:
            option_buttons.append(
                InlineKeyboardButton(
                    text=code,
                    callback_data=f"public_sub_currency:{subscription_id}:{code}",
                    style="success",
                )
            )
        else:
            option_buttons.append(
                InlineKeyboardButton(
                    text=code,
                    callback_data=f"public_sub_currency:{subscription_id}:{code}",
                )
            )
    rows.append(option_buttons)
    rows.append([InlineKeyboardButton(text="Other", callback_data=f"public_sub_currency_other:{subscription_id}")])
    if default_active:
        default_button = InlineKeyboardButton(
            text="Default",
            callback_data=SubscriptionAction(
                action="public_currency_default",
                subscription_id=subscription_id,
            ).pack(),
            style="success",
        )
    else:
        default_button = InlineKeyboardButton(
            text="Default",
            callback_data=SubscriptionAction(
                action="public_currency_default",
                subscription_id=subscription_id,
            ).pack(),
        )
    rows.append([default_button])
    rows.append(
        [
            InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="open_public", subscription_id=subscription_id).pack(),
            ),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_currency_keyboard(
    subscription_id: int,
    current_currency: str,
) -> InlineKeyboardMarkup:
    """'💱 Currency' picker: [✅ RUB][USD] / [EUR][TRY] / [✏️ Other currency] / [◀️ Back]."""
    current = (current_currency or "").strip().upper()
    buttons = [
        InlineKeyboardButton(
            text=radio_label(code, code == current),
            callback_data=f"sub_currency:{subscription_id}:{code}",
        )
        for code in SUBSCRIPTION_CURRENCY_OPTIONS
    ]
    rows = currency_option_rows(buttons)
    rows.append([InlineKeyboardButton(text=OTHER_CURRENCY_TEXT, callback_data=f"sub_currency_other:{subscription_id}")])
    rows.append([nav_button(SubscriptionAction(action="pricing", subscription_id=subscription_id).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def public_settings_time_keyboard(current_time: str, default_time: str) -> InlineKeyboardMarkup:
    presets = ("09:00", "12:00", "16:00", "20:00")
    normalized_current = current_time.strip()
    normalized_default = default_time.strip()
    default_active = normalized_current == normalized_default
    rows: list[list[InlineKeyboardButton]] = []
    preset_buttons: list[InlineKeyboardButton] = []
    for time_value in presets:
        is_active = (time_value == normalized_current) and not default_active
        text = f"{'✅ ' if is_active else ''}{time_value}"
        if is_active:
            preset_buttons.append(
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"public_settings_time:{time_value}",
                    style="success",
                )
            )
        else:
            preset_buttons.append(
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"public_settings_time:{time_value}",
                )
            )
    for index in range(0, len(preset_buttons), 2):
        rows.append(preset_buttons[index : index + 2])
    rows.append([InlineKeyboardButton(text="Other", callback_data="public_settings:time_other")])
    if default_active:
        default_button = InlineKeyboardButton(
            text="Default",
            callback_data="public_settings:time_reset",
            style="success",
        )
    else:
        default_button = InlineKeyboardButton(text="Default", callback_data="public_settings:time_reset")
    rows.append([default_button])
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Back", callback_data="public_settings:menu"),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def public_settings_timezone_keyboard(current_timezone: str, default_timezone: str) -> InlineKeyboardMarkup:
    normalized_current = current_timezone.strip()
    normalized_default = default_timezone.strip()
    default_active = normalized_current == normalized_default
    rows: list[list[InlineKeyboardButton]] = []
    zone_buttons: list[InlineKeyboardButton] = []
    for zone in COMMON_TIMEZONES:
        is_active = (zone == normalized_current) and not default_active
        text = f"{'✅ ' if is_active else ''}{zone}"
        if is_active:
            zone_buttons.append(
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"public_settings_timezone:{zone}",
                    style="success",
                )
            )
        else:
            zone_buttons.append(
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"public_settings_timezone:{zone}",
                )
            )
    for index in range(0, len(zone_buttons), 2):
        rows.append(zone_buttons[index : index + 2])
    rows.append([InlineKeyboardButton(text="Other", callback_data="public_settings:timezone_other")])
    if default_active:
        default_button = InlineKeyboardButton(
            text="Default",
            callback_data="public_settings:timezone_reset",
            style="success",
        )
    else:
        default_button = InlineKeyboardButton(text="Default", callback_data="public_settings:timezone_reset")
    rows.append([default_button])
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Back", callback_data="public_settings:menu"),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_rounding_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    options = (
        ("precise", "With cents"),
        ("floor", "Round down"),
        ("round", "Round correctly"),
        ("ceil", "Round up"),
    )
    for mode, label in options:
        is_active = current_mode == mode
        text = f"{'✅' if is_active else '☐'} {label}"
        if is_active:
            builder.button(text=text, callback_data=f"settings_rounding:{mode}", style="success")
        else:
            builder.button(text=text, callback_data=f"settings_rounding:{mode}")
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="⬅️ Back", callback_data="settings:menu"),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def settings_notifications_keyboard(
    reminders_enabled: bool,
    paid_enabled: bool,
    closed_enabled: bool,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    options = (
        (reminders_enabled, "Payment reminders", "settings_notify:reminders"),
        (paid_enabled, "Payment recorded", "settings_notify:paid"),
        (closed_enabled, "All paid (cycle closed)", "settings_notify:closed"),
    )
    for is_enabled, label, callback_data in options:
        text = f"{'✅' if is_enabled else '☐'} {label}"
        if is_enabled:
            builder.button(text=text, callback_data=callback_data, style="success")
        else:
            builder.button(text=text, callback_data=callback_data)
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="⬅️ Back", callback_data="settings:menu"),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


PERIOD_PRESET_DAYS = (7, 14, 30, 90)
MONTHLY_LABEL = "Monthly"
EVERYONE_LABEL = "Everyone"
SPLIT_BY_SHARES_LABEL = "Split by shares"
FIXED_PER_USER_LABEL = "Fixed per user"


def build_period_keyboard(
    *,
    selected: int | None = None,
    cancel_callback: str = CANCEL_CALLBACK,
) -> InlineKeyboardMarkup:
    """[7 days][14 days] / [30 days][90 days] / [Monthly] / [✖️ Cancel] (``period:N`` / ``period:month``).

    ``selected`` is a day count or ``MONTHLY_PERIOD_SENTINEL``; that option gets ✅.
    """
    presets = [
        InlineKeyboardButton(text=radio_label(format_days(days), selected == days), callback_data=f"period:{days}")
        for days in PERIOD_PRESET_DAYS
    ]
    rows = pair_rows(presets)
    rows.append(
        [
            InlineKeyboardButton(
                text=radio_label(MONTHLY_LABEL, selected == MONTHLY_PERIOD_SENTINEL),
                callback_data="period:month",
            )
        ]
    )
    rows.append([cancel_button(cancel_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_share_limit_keyboard(
    *,
    everyone_selected: bool = False,
    cancel_callback: str = CANCEL_CALLBACK,
) -> InlineKeyboardMarkup:
    """[Everyone] (``sharelimit:all``) above [✖️ Cancel]."""
    rows = [
        [InlineKeyboardButton(text=radio_label(EVERYONE_LABEL, everyone_selected), callback_data="sharelimit:all")],
        [cancel_button(cancel_callback)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_creation_payment_mode_keyboard(
    *,
    selected: str | None = None,
    cancel_callback: str = CANCEL_CALLBACK,
) -> InlineKeyboardMarkup:
    """[Split by shares][Fixed per user] (``create_payment_mode:*``) above [✖️ Cancel]."""
    buttons = [
        InlineKeyboardButton(
            text=radio_label(SPLIT_BY_SHARES_LABEL, selected == PAYMENT_MODE_SPLIT),
            callback_data=f"create_payment_mode:{PAYMENT_MODE_SPLIT}",
        ),
        InlineKeyboardButton(
            text=radio_label(FIXED_PER_USER_LABEL, selected == PAYMENT_MODE_FIXED),
            callback_data=f"create_payment_mode:{PAYMENT_MODE_FIXED}",
        ),
    ]
    rows = pair_rows(buttons)
    rows.append([cancel_button(cancel_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_due_date_change_keyboard(
    subscription_id: int,
    old_due: str,
    new_due: str,
) -> InlineKeyboardMarkup:
    """[🔀 Move cycle] / [🆕 New cycle] / [✖️ Cancel] (back to Pricing); the text explains both."""

    def choice(label: str, action: str) -> list[InlineKeyboardButton]:
        data = DueDateChangeAction(action=action, subscription_id=subscription_id, old_due=old_due, new_due=new_due)
        return [InlineKeyboardButton(text=label, callback_data=data.pack())]

    return InlineKeyboardMarkup(
        inline_keyboard=[
            choice(MOVE_CYCLE_TEXT, "move"),
            choice(NEW_CYCLE_TEXT, "new"),
            [cancel_button(SubscriptionAction(action="pricing", subscription_id=subscription_id).pack())],
        ]
    )


def build_payment_confirmation_keyboard(
    subscription_id: int,
    due_date: date | str,
    payment_details: str = "",
    payment_link: str = "",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in payment_requisite_rows(payment_details, payment_link):
        builder.row(*row)
    due_str = due_date if isinstance(due_date, str) else due_date.isoformat()
    builder.row(
        InlineKeyboardButton(
            text="✅ Paid",
            callback_data=ReminderAction(subscription_id=subscription_id, due_date=due_str).pack(),
            style="success",
        )
    )
    return builder.as_markup()


def build_batch_payment_confirmation_keyboard(
    items: Sequence[Dict[str, object]],
    payment_details: str = "",
    payment_link: str = "",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in payment_requisite_rows(payment_details, payment_link):
        builder.row(*row)
    for item in items:
        due_date = item.get("due_date")
        due_str = due_date if isinstance(due_date, str) else due_date.isoformat()
        label = str(item.get("subscription_label") or item.get("subscription_name") or "Subscription")
        due_hint = str(item.get("due_date_text") or due_str)
        builder.row(
            InlineKeyboardButton(
                text=f"✅ {label} · {due_hint}",
                callback_data=ReminderAction(subscription_id=int(item["subscription_id"]), due_date=due_str).pack(),
                style="success",
            )
        )
    return builder.as_markup()


def build_test_payment_confirmation_keyboard(
    subscription_id: int,
    due_date: date | str,
    payment_details: str = "",
    payment_link: str = "",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in payment_requisite_rows(payment_details, payment_link):
        builder.row(*row)
    due_str = due_date if isinstance(due_date, str) else due_date.isoformat()
    builder.row(
        InlineKeyboardButton(
            text="✅ Paid",
            callback_data=TestPaidAction(subscription_id=subscription_id, due_date=due_str).pack(),
            style="success",
        )
    )
    return builder.as_markup()


def comment_edit_keyboard(
    subscription_id: int,
    has_comment: bool,
) -> InlineKeyboardMarkup:
    """The comment prompt: [🗑 Clear comment] (only while there is one) above [✖️ Cancel].

    No "✖️ Close": it deleted the prompt but left the input armed. "🗑 Clear
    comment" is too wide to share a row with Cancel, so it sits above it like
    any quick value.
    """
    rows = comment_quick_rows(subscription_id, has_comment)
    rows.append([dialog_cancel_inline_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def comment_quick_rows(subscription_id: int, has_comment: bool) -> list[list[InlineKeyboardButton]]:
    """The quick value above the comment prompt's Cancel: [🗑 Clear comment] (``sub:comment_clear``)."""
    if not has_comment:
        return []
    return [[_sub_button(CLEAR_COMMENT_TEXT, "comment_clear", subscription_id)]]
