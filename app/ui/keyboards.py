from __future__ import annotations

import re
from datetime import date, datetime
from typing import Dict, Iterable, Sequence
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

from app.core.constants import PAYMENT_MODE_FIXED, PAYMENT_MODE_SPLIT
from app.ui.states import (
    CycleAction,
    DueDateChangeAction,
    MemberAction,
    ParticipantAction,
    PaymentDestinationAction,
    ReminderAction,
    ReminderSendAction,
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


def _format_cycle_button_label(due_value: str) -> str:
    try:
        return datetime.strptime(due_value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return due_value


def admin_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="👥 Users"), KeyboardButton(text="📋 Subscriptions")],
            [KeyboardButton(text="💳 Payment methods"), KeyboardButton(text="📊 Payments report")],
            [KeyboardButton(text="⚙️ Settings")],
        ],
    )


def public_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="👤 Account"), KeyboardButton(text="📋 Subscriptions")],
            [KeyboardButton(text="📊 Payments report"), KeyboardButton(text="⚙️ Settings")],
        ],
    )


def dialog_cancel_inline_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="Cancel", callback_data="dialog:cancel", style="danger")


def dialog_cancel_inline_keyboard(force_reply: bool = True) -> InlineKeyboardMarkup:
    """Cancel-only keyboard for text prompts.

    ``force_reply=True`` opens the reply box on the client, so it is only valid
    on a freshly sent prompt (``message.answer``); pass ``False`` when the
    keyboard is attached through ``edit_text``/``edit_reply_markup``.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[[dialog_cancel_inline_button()]],
        force_reply=True if force_reply else None,
    )


def _prompt_markup(builder: InlineKeyboardBuilder, force_reply: bool) -> InlineKeyboardMarkup:
    """Export a builder as markup; ``as_markup(force_reply=...)`` silently drops the flag."""
    return InlineKeyboardMarkup(
        inline_keyboard=builder.export(),
        force_reply=True if force_reply else None,
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


def build_subscription_list_keyboard(subs: Sequence[Dict[str, object]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="➕ New subscription",
        callback_data=SubscriptionAction(action="create", subscription_id=0).pack(),
        style="primary",
    )
    for sub in subs:
        label = f"{sub['name']} ({sub['amount']:.2f} {sub['currency']})"
        builder.button(
            text=label,
            callback_data=SubscriptionAction(action="open", subscription_id=sub["id"]).pack(),
        )
    builder.adjust(1)
    return builder.as_markup()


def build_members_list_keyboard(
    friends: Sequence[Dict[str, object]],
    *,
    page: int = 1,
    total_pages: int = 1,
    total_users: int = 0,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="➕ Add user",
                callback_data=MemberAction(action="add", friend_id=0).pack(),
                style="primary",
            )
        ]
    ]

    columns = 2 if total_users > 4 else 1
    friend_buttons = [
        InlineKeyboardButton(
            text=str(friend["full_name"]),
            callback_data=MemberAction(action="open", friend_id=int(friend["id"])).pack(),
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
                previous_callback=MemberAction(action="page", friend_id=page - 1).pack(),
                next_callback=MemberAction(action="page", friend_id=page + 1).pack(),
            )
        )

    rows.append([InlineKeyboardButton(text="✖️ Close", callback_data="menu:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def member_detail_keyboard(friend_id: int, *, is_pending: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✏️ Rename",
        callback_data=MemberAction(action="rename", friend_id=friend_id).pack(),
    )
    if is_pending:
        builder.button(
            text="🔗 Auth link",
            callback_data=MemberAction(action="invite", friend_id=friend_id).pack(),
        )
    builder.button(
        text="📊 Payments report",
        callback_data=MemberAction(action="report", friend_id=friend_id).pack(),
    )
    builder.button(
        text="💰 Balance",
        callback_data=MemberAction(action="balance", friend_id=friend_id).pack(),
    )
    builder.button(
        text="💳 Payment method",
        callback_data=MemberAction(action="payment_destination", friend_id=friend_id).pack(),
    )
    builder.button(
        text="🗑 Delete",
        callback_data=MemberAction(action="delete", friend_id=friend_id).pack(),
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=MemberAction(action="back", friend_id=friend_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def member_balance_keyboard(friend_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✏️ Set",
        callback_data=MemberAction(action="balance_set", friend_id=friend_id).pack(),
        style="primary",
    )
    builder.button(
        text="💱 Currency",
        callback_data=MemberAction(action="balance_currency", friend_id=friend_id).pack(),
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=MemberAction(action="open", friend_id=friend_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def member_balance_currency_keyboard(
    friend_id: int,
    current_currency: str,
) -> InlineKeyboardMarkup:
    options = ("USD", "EUR", "RUB")
    normalized_current = current_currency.upper()
    rows: list[list[InlineKeyboardButton]] = []
    option_buttons: list[InlineKeyboardButton] = []
    for code in options:
        is_active = code == normalized_current
        if is_active:
            option_buttons.append(
                InlineKeyboardButton(
                    text=code,
                    callback_data=f"member_balance_currency:{friend_id}:{code}",
                    style="success",
                )
            )
        else:
            option_buttons.append(
                InlineKeyboardButton(
                    text=code,
                    callback_data=f"member_balance_currency:{friend_id}:{code}",
                )
            )
    rows.append(option_buttons)
    rows.append(
        [
            InlineKeyboardButton(
                text="Other",
                callback_data=MemberAction(action="balance_currency_other", friend_id=friend_id).pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data=MemberAction(action="balance", friend_id=friend_id).pack(),
            ),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def member_delete_confirm_keyboard(friend_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🗑 Yes, delete",
        callback_data=MemberAction(action="confirm_delete", friend_id=friend_id).pack(),
        style="danger",
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=MemberAction(action="open", friend_id=friend_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def member_report_keyboard(friend_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=MemberAction(action="open", friend_id=friend_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def member_payment_destination_keyboard(
    friend_id: int,
    destinations: Sequence[Dict[str, object]],
    assigned_destination_id: int | None,
    default_destination_id: int | None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    default_label = "Default"
    if default_destination_id:
        default_target = next(
            (item for item in destinations if int(item.get("id") or 0) == default_destination_id),
            None,
        )
        if default_target:
            default_label = f"Default ({default_target['title']})"
    default_style = "success" if assigned_destination_id is None else None
    rows.append(
        [
            InlineKeyboardButton(
                text=default_label,
                callback_data=f"member_payment_destination_default:{friend_id}",
                style=default_style,
            )
        ]
    )
    for item in destinations:
        destination_id = int(item["id"])
        label = f"{item['title']} ({item['currency']})"
        style = "success" if assigned_destination_id == destination_id else None
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"member_payment_destination:{friend_id}:{destination_id}",
                    style=style,
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data=MemberAction(action="open", friend_id=friend_id).pack(),
            ),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_destinations_keyboard(
    destinations: Sequence[Dict[str, object]],
    default_destination_id: int | None,
    pending_count: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="➕ Add payment method",
                callback_data=PaymentDestinationAction(action="create", destination_id=0).pack(),
                style="primary",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"📥 Pending top-ups ({pending_count})",
                callback_data=TopUpAction(action="admin_list", request_id=0).pack(),
            )
        ],
    ]
    for item in destinations:
        destination_id = int(item["id"])
        title = str(item.get("title") or f"Method {destination_id}")
        currency = str(item.get("currency") or "RUB")
        prefix = "✅ " if default_destination_id == destination_id else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix}{title} ({currency})",
                    callback_data=PaymentDestinationAction(action="open", destination_id=destination_id).pack(),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="✖️ Close", callback_data="menu:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_destination_detail_keyboard(
    destination_id: int,
    *,
    is_default: bool,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✏️ Title",
        callback_data=PaymentDestinationAction(action="edit_title", destination_id=destination_id).pack(),
    )
    builder.button(
        text="💱 Currency",
        callback_data=PaymentDestinationAction(action="edit_currency", destination_id=destination_id).pack(),
    )
    builder.button(
        text="📝 Details",
        callback_data=PaymentDestinationAction(action="edit_details", destination_id=destination_id).pack(),
    )
    builder.button(
        text="🔗 Link",
        callback_data=PaymentDestinationAction(action="edit_link", destination_id=destination_id).pack(),
    )
    builder.button(
        text="⭐ Default" if is_default else "☆ Set default",
        callback_data=PaymentDestinationAction(action="set_default", destination_id=destination_id).pack(),
        style="success" if is_default else None,
    )
    builder.button(
        text="🗑 Delete",
        callback_data=PaymentDestinationAction(action="delete", destination_id=destination_id).pack(),
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=PaymentDestinationAction(action="menu", destination_id=0).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def payment_destination_delete_keyboard(destination_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🗑 Yes, delete",
        callback_data=PaymentDestinationAction(action="confirm_delete", destination_id=destination_id).pack(),
        style="danger",
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=PaymentDestinationAction(action="open", destination_id=destination_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


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


def subscription_detail_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✏️ Rename",
        callback_data=SubscriptionAction(action="rename", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="💰 Pricing & schedule",
        callback_data=SubscriptionAction(action="pricing", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="👥 Participants",
        callback_data=SubscriptionAction(action="participants", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="🔔 Reminders",
        callback_data=SubscriptionAction(action="reminders", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="💳 Payment",
        callback_data=SubscriptionAction(action="paymentinfo", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="📊 Reports & more",
        callback_data=SubscriptionAction(action="more", subscription_id=subscription_id).pack(),
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="back", subscription_id=subscription_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


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
        text="📊 Payments report",
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


def subscription_report_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    return build_back_keyboard(subscription_id, "more")


def subscription_open_cycles_keyboard(
    subscription_id: int,
    due_dates: Sequence[str] = (),
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for due_value in due_dates:
        builder.button(
            text=f"📅 {_format_cycle_button_label(due_value)}",
            callback_data=CycleAction(
                action="open",
                subscription_id=subscription_id,
                due_date=due_value,
            ).pack(),
        )
    if due_dates:
        builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="more", subscription_id=subscription_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def subscription_cycle_actions_keyboard(subscription_id: int, due_value: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="♻️ Recreate cycle",
        callback_data=CycleAction(
            action="recreate",
            subscription_id=subscription_id,
            due_date=due_value,
        ).pack(),
    )
    builder.button(
        text="✅ Force close",
        callback_data=CycleAction(
            action="force_close",
            subscription_id=subscription_id,
            due_date=due_value,
        ).pack(),
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="cycles", subscription_id=subscription_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def public_subscription_report_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    return build_back_keyboard(subscription_id, "open_public")


def reminder_settings_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="⏰ Reminder time",
        callback_data=SubscriptionAction(action="remindertime", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="🔔 Reminder days",
        callback_data=SubscriptionAction(action="reminderdays", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="📣 Post-due alerts",
        callback_data=SubscriptionAction(action="reminderloop", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="📬 Send reminders now",
        callback_data=SubscriptionAction(action="reminders_send", subscription_id=subscription_id).pack(),
        style="primary",
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="open", subscription_id=subscription_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def participants_settings_keyboard(
    subscription_id: int,
    current_mode: str,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="👥 Manage users",
            callback_data=SubscriptionAction(action="participants_manage", subscription_id=subscription_id).pack(),
        )
    )
    normalized_mode = (current_mode or PAYMENT_MODE_SPLIT).strip().lower()
    if normalized_mode == PAYMENT_MODE_SPLIT:
        split_button = InlineKeyboardButton(
            text="Split by shares",
            callback_data=f"sub_payment_mode:{subscription_id}:{PAYMENT_MODE_SPLIT}",
            style="success",
        )
    else:
        split_button = InlineKeyboardButton(
            text="Split by shares",
            callback_data=f"sub_payment_mode:{subscription_id}:{PAYMENT_MODE_SPLIT}",
        )
    if normalized_mode == PAYMENT_MODE_FIXED:
        fixed_button = InlineKeyboardButton(
            text="Fixed per user",
            callback_data=f"sub_payment_mode:{subscription_id}:{PAYMENT_MODE_FIXED}",
            style="success",
        )
    else:
        fixed_button = InlineKeyboardButton(
            text="Fixed per user",
            callback_data=f"sub_payment_mode:{subscription_id}:{PAYMENT_MODE_FIXED}",
        )
    builder.row(split_button, fixed_button)
    if normalized_mode == PAYMENT_MODE_FIXED:
        builder.row(
            InlineKeyboardButton(
                text="💵 Amount per user",
                callback_data=SubscriptionAction(action="useramounts", subscription_id=subscription_id).pack(),
            )
        )
    else:
        builder.row(
            InlineKeyboardButton(
                text="➗ Shares & split",
                callback_data=SubscriptionAction(action="share", subscription_id=subscription_id).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="open", subscription_id=subscription_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def subscription_more_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📊 Payments report",
        callback_data=SubscriptionAction(action="report", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="🗂 Open cycles",
        callback_data=SubscriptionAction(action="cycles", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="🗑 Delete",
        callback_data=SubscriptionAction(action="delete", subscription_id=subscription_id).pack(),
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="open", subscription_id=subscription_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def subscription_payment_info_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="💳 Payment method",
        callback_data=SubscriptionAction(action="paymentmethod", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="📝 Comment",
        callback_data=SubscriptionAction(action="comment", subscription_id=subscription_id).pack(),
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="open", subscription_id=subscription_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def subscription_payment_destination_keyboard(
    subscription_id: int,
    destinations: Sequence[Dict[str, object]],
    selected_destination_id: int | None,
    default_destination_id: int | None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    default_label = "Default"
    if default_destination_id is not None:
        default_target = next(
            (item for item in destinations if int(item.get("id") or 0) == default_destination_id),
            None,
        )
        if default_target:
            default_label = f"Default ({default_target['title']} · {default_target['currency']})"
    rows.append(
        [
            InlineKeyboardButton(
                text=default_label,
                callback_data=f"sub_payment_destination:{subscription_id}:default",
                style="success" if selected_destination_id is None else None,
            )
        ]
    )
    for item in destinations:
        destination_id = int(item["id"])
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{item['title']} ({item['currency']})",
                    callback_data=f"sub_payment_destination:{subscription_id}:{destination_id}",
                    style="success" if selected_destination_id == destination_id else None,
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data=SubscriptionAction(action="paymentinfo", subscription_id=subscription_id).pack(),
            ),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_reminder_time_keyboard(
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
                    callback_data=f"sub_remindertime:{subscription_id}:{time_value}",
                    style="success",
                )
            )
        else:
            preset_buttons.append(
                InlineKeyboardButton(
                    text=time_value,
                    callback_data=f"sub_remindertime:{subscription_id}:{time_value}",
                )
            )
    for index in range(0, len(preset_buttons), 2):
        rows.append(preset_buttons[index : index + 2])

    rows.append([InlineKeyboardButton(text="Other", callback_data=f"sub_remindertime_other:{subscription_id}")])
    if default_active:
        default_button = InlineKeyboardButton(
            text="Default",
            callback_data=SubscriptionAction(action="remindertime_default", subscription_id=subscription_id).pack(),
            style="success",
        )
    else:
        default_button = InlineKeyboardButton(
            text="Default",
            callback_data=SubscriptionAction(action="remindertime_default", subscription_id=subscription_id).pack(),
        )
    rows.append([default_button])
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data=SubscriptionAction(action="open", subscription_id=subscription_id).pack(),
            ),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
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
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📬 All",
        callback_data=ReminderSendAction(subscription_id=subscription_id, telegram_id=0).pack(),
        style="primary",
    )
    for person in participants:
        builder.button(
            text=str(person.get("full_name") or "Unknown"),
            callback_data=ReminderSendAction(
                subscription_id=subscription_id,
                telegram_id=int(person["telegram_id"]),
            ).pack(),
        )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="reminders", subscription_id=subscription_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


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
    builder = InlineKeyboardBuilder()
    builder.button(
        text="💰 Amount",
        callback_data=SubscriptionAction(action="amount", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="💱 Currency",
        callback_data=SubscriptionAction(action="currency", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="🌐 Convert currency",
        callback_data=SubscriptionAction(action="basecurrency", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="📅 Next charge",
        callback_data=SubscriptionAction(action="duedate", subscription_id=subscription_id).pack(),
    )
    builder.button(
        text="🔁 Period",
        callback_data=SubscriptionAction(action="period", subscription_id=subscription_id).pack(),
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="open", subscription_id=subscription_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def subscription_user_amounts_keyboard(
    subscription_id: int,
    participants: Sequence[Dict[str, object]],
    currency: str,
    subscription_amount: float,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for person in participants:
        name = str(person.get("full_name") or "Unknown")
        weight = int(person.get("share_weight") or 1)
        fixed_amount = person.get("fixed_amount")
        try:
            base_value = float(fixed_amount) if fixed_amount is not None else float(subscription_amount)
        except (TypeError, ValueError):
            base_value = float(subscription_amount)
        if weight > 1:
            amount_text = f"{base_value:.2f} (x{weight}) {currency.upper()}"
        else:
            amount_text = f"{base_value:.2f} {currency.upper()}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{name} — {amount_text}",
                    callback_data=f"sub_user_amount:{subscription_id}:{int(person['id'])}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✏️ Set all",
                callback_data=f"sub_user_amount_all:{subscription_id}",
                style="primary",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data=SubscriptionAction(action="participants", subscription_id=subscription_id).pack(),
            ),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_amount_clear_keyboard(
    subscription_id: int,
    friend_id: int,
    *,
    force_reply: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Clear",
        callback_data=f"sub_user_amount_clear:{subscription_id}:{friend_id}",
        style="danger",
    )
    builder.adjust(1)
    builder.row(dialog_cancel_inline_button())
    return _prompt_markup(builder, force_reply)


def subscription_base_currency_keyboard(
    subscription_id: int,
    current_currency: str,
) -> InlineKeyboardMarkup:
    options = ("USD", "EUR", "RUB")
    builder = InlineKeyboardBuilder()
    for code in options:
        is_active = code == current_currency.upper()
        text = f"{'✅ ' if is_active else ''}{code}"
        if is_active:
            builder.button(
                text=text,
                callback_data=f"sub_base_currency:{subscription_id}:{code}",
                style="success",
            )
        else:
            builder.button(
                text=text,
                callback_data=f"sub_base_currency:{subscription_id}:{code}",
            )
    builder.button(
        text="Other",
        callback_data=SubscriptionAction(action="basecurrency_other", subscription_id=subscription_id).pack(),
    )
    builder.adjust(3, 1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="pricing", subscription_id=subscription_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def build_participants_keyboard(friends: Sequence[Dict[str, object]], subscription_id: int) -> InlineKeyboardMarkup:
    rows = []
    for friend in friends:
        prefix = "✅" if friend["is_member"] else "☐"
        if friend["is_member"]:
            toggle_button = InlineKeyboardButton(
                text=f"{prefix} {friend['full_name']}",
                callback_data=ParticipantAction(
                    action="toggle",
                    subscription_id=subscription_id,
                    friend_id=friend["id"],
                ).pack(),
                style="success",
            )
        else:
            toggle_button = InlineKeyboardButton(
                text=f"{prefix} {friend['full_name']}",
                callback_data=ParticipantAction(
                    action="toggle",
                    subscription_id=subscription_id,
                    friend_id=friend["id"],
                ).pack(),
            )
        if friend["is_member"]:
            weight_value = int(friend.get("share_weight") or 1)
            weight_button = InlineKeyboardButton(
                text=f"x{weight_value}",
                callback_data=ParticipantAction(
                    action="weight",
                    subscription_id=subscription_id,
                    friend_id=friend["id"],
                ).pack(),
            )
            rows.append([toggle_button, weight_button])
        else:
            rows.append([toggle_button])
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data=SubscriptionAction(action="participants", subscription_id=subscription_id).pack(),
            ),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_currency_keyboard(options: Iterable[str], *, force_reply: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code in options:
        builder.button(text=code.upper(), callback_data=f"currency:{code.upper()}")
    builder.adjust(3)
    builder.row(dialog_cancel_inline_button())
    return _prompt_markup(builder, force_reply)


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
    options = ("USD", "EUR", "RUB")
    normalized_current = current_currency.upper()
    rows: list[list[InlineKeyboardButton]] = []
    option_buttons: list[InlineKeyboardButton] = []
    for code in options:
        is_active = code == normalized_current
        if is_active:
            option_buttons.append(
                InlineKeyboardButton(
                    text=code,
                    callback_data=f"sub_currency:{subscription_id}:{code}",
                    style="success",
                )
            )
        else:
            option_buttons.append(
                InlineKeyboardButton(
                    text=code,
                    callback_data=f"sub_currency:{subscription_id}:{code}",
                )
            )
    rows.append(option_buttons)
    rows.append([InlineKeyboardButton(text="Other", callback_data=f"sub_currency_other:{subscription_id}")])
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data=SubscriptionAction(action="pricing", subscription_id=subscription_id).pack(),
            ),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
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


def build_period_keyboard(*, force_reply: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for days in (7, 14, 30, 90):
        builder.button(text=f"{days} d", callback_data=f"period:{days}")
    builder.button(text="Monthly", callback_data="period:month")
    builder.adjust(2)
    builder.row(dialog_cancel_inline_button())
    return _prompt_markup(builder, force_reply)


def build_share_limit_keyboard(*, force_reply: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Split across all", callback_data="sharelimit:all")
    builder.adjust(1)
    builder.row(dialog_cancel_inline_button())
    return _prompt_markup(builder, force_reply)


def build_creation_payment_mode_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Split by shares", callback_data=f"create_payment_mode:{PAYMENT_MODE_SPLIT}")
    builder.button(text="Fixed per user", callback_data=f"create_payment_mode:{PAYMENT_MODE_FIXED}")
    builder.adjust(2)
    builder.row(dialog_cancel_inline_button())
    return builder.as_markup()


def build_due_date_change_keyboard(
    subscription_id: int,
    old_due: str,
    new_due: str,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔀 Move cycle (keep payments)",
        callback_data=DueDateChangeAction(
            action="move",
            subscription_id=subscription_id,
            old_due=old_due,
            new_due=new_due,
        ).pack(),
    )
    builder.button(
        text="🆕 New cycle (request payments)",
        callback_data=DueDateChangeAction(
            action="new",
            subscription_id=subscription_id,
            old_due=old_due,
            new_due=new_due,
        ).pack(),
    )
    builder.adjust(1)
    builder.row(dialog_cancel_inline_button())
    return builder.as_markup()


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
    *,
    force_reply: bool = False,
) -> InlineKeyboardMarkup:
    """Comment prompt keyboard; ``force_reply`` only when sent as a fresh message (never on edit paths)."""
    builder = InlineKeyboardBuilder()
    if has_comment:
        builder.row(
            InlineKeyboardButton(
                text="Clear",
                callback_data=SubscriptionAction(action="comment_clear", subscription_id=subscription_id).pack(),
            )
        )
    builder.row(
        dialog_cancel_inline_button(),
    )
    builder.row(
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return _prompt_markup(builder, force_reply)
