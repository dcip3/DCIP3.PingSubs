from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.ui.states import MemberAction, ParticipantAction, ReminderAction, ReminderSendAction, SubscriptionAction


def admin_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="👥 Users"), KeyboardButton(text="📋 Subscriptions")],
            [KeyboardButton(text="📊 Payments report"), KeyboardButton(text="⚙️ Settings")],
        ],
    )


def public_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="📋 Subscriptions"), KeyboardButton(text="📊 Payments report")],
        ],
    )


def dialog_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[[KeyboardButton(text="Cancel")]],
    )


def build_subscription_list_keyboard(subs: Sequence[Dict[str, object]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="➕ New subscription",
        callback_data=SubscriptionAction(action="create", subscription_id=0).pack(),
    )
    for sub in subs:
        label = f"{sub['name']} ({sub['amount']:.2f} {sub['currency']})"
        builder.button(
            text=label,
            callback_data=SubscriptionAction(action="open", subscription_id=sub["id"]).pack(),
        )
    builder.adjust(1)
    return builder.as_markup()


def build_members_list_keyboard(friends: Sequence[Dict[str, object]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="➕ Add user",
        callback_data=MemberAction(action="add", friend_id=0).pack(),
    )
    for friend in friends:
        label = friend["full_name"]
        builder.button(
            text=label,
            callback_data=MemberAction(action="open", friend_id=friend["id"]).pack(),
        )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def member_detail_keyboard(friend_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✏️ Rename",
        callback_data=MemberAction(action="rename", friend_id=friend_id).pack(),
    )
    builder.button(
        text="📊 Payments report",
        callback_data=MemberAction(action="report", friend_id=friend_id).pack(),
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


def member_delete_confirm_keyboard(friend_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Yes, delete",
        callback_data=MemberAction(action="confirm_delete", friend_id=friend_id).pack(),
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


def build_public_subscription_list_keyboard(subs: Sequence[Dict[str, object]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for sub in subs:
        label = f"{sub['name']} ({sub['amount']:.2f} {sub['currency']})"
        builder.button(
            text=label,
            callback_data=SubscriptionAction(action="open_public", subscription_id=sub["id"]).pack(),
        )
    builder.adjust(1)
    return builder.as_markup()


def subscription_detail_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Rename", callback_data=SubscriptionAction(action="rename", subscription_id=subscription_id).pack())
    builder.button(text="💰 Pricing", callback_data=SubscriptionAction(action="pricing", subscription_id=subscription_id).pack())
    builder.button(text="📅 Next charge", callback_data=SubscriptionAction(action="duedate", subscription_id=subscription_id).pack())
    builder.button(text="🔁 Period", callback_data=SubscriptionAction(action="period", subscription_id=subscription_id).pack())
    builder.button(text="🔔 Reminders", callback_data=SubscriptionAction(action="reminders", subscription_id=subscription_id).pack())
    builder.button(text="👥 Users", callback_data=SubscriptionAction(action="participants", subscription_id=subscription_id).pack())
    builder.button(text="➗ Split limit", callback_data=SubscriptionAction(action="share", subscription_id=subscription_id).pack())
    builder.button(text="📊 Payments report", callback_data=SubscriptionAction(action="report", subscription_id=subscription_id).pack())
    builder.button(text="🗑 Delete", callback_data=SubscriptionAction(action="delete", subscription_id=subscription_id).pack())
    builder.adjust(2, 2, 2, 2, 2)
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
        text="⏰ Reminder time",
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
    return build_back_keyboard(subscription_id, "open")


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


def reminder_send_targets_keyboard(
    subscription_id: int,
    participants: Sequence[Dict[str, object]],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📬 All",
        callback_data=ReminderSendAction(subscription_id=subscription_id, telegram_id=0).pack(),
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
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="open", subscription_id=subscription_id).pack(),
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def build_participants_keyboard(friends: Sequence[Dict[str, object]], subscription_id: int) -> InlineKeyboardMarkup:
    rows = []
    for friend in friends:
        prefix = "✅" if friend["is_member"] else "☐"
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
                callback_data=SubscriptionAction(action="open", subscription_id=subscription_id).pack(),
            ),
            InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_currency_keyboard(options: Iterable[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code in options:
        builder.button(text=code.upper(), callback_data=f"currency:{code.upper()}")
    builder.adjust(3)
    return builder.as_markup()


def admin_settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💱 Base currency", callback_data="settings:currency")
    builder.button(text="🔢 Rounding", callback_data="settings:rounding")
    builder.button(text="🔔 Notifications", callback_data="settings:notifications")
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def settings_currency_keyboard(options: Iterable[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code in options:
        builder.button(text=code.upper(), callback_data=f"settings_currency:{code.upper()}")
    builder.button(text="Other", callback_data="settings:currency_other")
    builder.adjust(3, 2)
    builder.row(
        InlineKeyboardButton(text="⬅️ Back", callback_data="settings:menu"),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def settings_rounding_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    def mark(mode: str, label: str) -> str:
        return f"✅ {label}" if current_mode == mode else f"☐ {label}"

    builder = InlineKeyboardBuilder()
    builder.button(text=mark("precise", "With cents"), callback_data="settings_rounding:precise")
    builder.button(text=mark("floor", "Round down"), callback_data="settings_rounding:floor")
    builder.button(text=mark("round", "Round correctly"), callback_data="settings_rounding:round")
    builder.button(text=mark("ceil", "Round up"), callback_data="settings_rounding:ceil")
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
    def mark(enabled: bool, label: str) -> str:
        return f"✅ {label}" if enabled else f"☐ {label}"

    builder = InlineKeyboardBuilder()
    builder.button(
        text=mark(reminders_enabled, "Payment reminders"),
        callback_data="settings_notify:reminders",
    )
    builder.button(
        text=mark(paid_enabled, "Payment recorded"),
        callback_data="settings_notify:paid",
    )
    builder.button(
        text=mark(closed_enabled, "All paid (cycle closed)"),
        callback_data="settings_notify:closed",
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="⬅️ Back", callback_data="settings:menu"),
        InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
    )
    return builder.as_markup()


def build_period_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for days in (7, 14, 30, 90):
        builder.button(text=f"{days} d", callback_data=f"period:{days}")
    builder.button(text="Monthly", callback_data="period:month")
    builder.adjust(2)
    return builder.as_markup()


def build_share_limit_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Split across all", callback_data="sharelimit:all")
    builder.adjust(1)
    return builder.as_markup()


def build_payment_confirmation_keyboard(subscription_id: int, due_date: date | str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    due_str = due_date if isinstance(due_date, str) else due_date.isoformat()
    builder.button(
        text="✅ Paid",
        callback_data=ReminderAction(subscription_id=subscription_id, due_date=due_str).pack(),
    )
    builder.adjust(1)
    return builder.as_markup()
