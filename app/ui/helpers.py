from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional, Sequence, Tuple, Union

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from app.core.constants import (
    DATE_INPUT_FORMAT,
    DEFAULT_CURRENCIES,
    MONTHLY_PERIOD_SENTINEL,
    PAYMENT_MODE_FIXED,
    PAYMENT_MODE_SPLIT,
)
from app.storage.db import Database
from app.ui.keyboards import (
    admin_reply_keyboard,
    build_members_list_keyboard,
    build_participants_keyboard,
    build_currency_keyboard,
    build_creation_payment_mode_keyboard,
    build_period_keyboard,
    build_public_subscription_list_keyboard,
    build_share_limit_keyboard,
    build_subscription_list_keyboard,
    dialog_keyboard,
    member_detail_keyboard,
    member_report_keyboard,
    pricing_settings_keyboard,
    subscription_user_amounts_keyboard,
    public_subscription_detail_keyboard,
    public_subscription_report_keyboard,
    reminder_settings_keyboard,
    reminder_send_targets_keyboard,
    settings_tests_keyboard,
    subscription_report_keyboard,
    subscription_detail_keyboard,
    tests_menu_keyboard,
    test_reminder_targets_keyboard,
)
from app.ui.states import Responder, SubscriptionAction
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
)
from app.ui.text import escape_html, format_display_name

MAX_TELEGRAM_MESSAGE_LEN = 3900


def _normalize_payment_mode(raw_value: object) -> str:
    value = str(raw_value or PAYMENT_MODE_SPLIT).strip().lower()
    if value == PAYMENT_MODE_FIXED:
        return PAYMENT_MODE_FIXED
    return PAYMENT_MODE_SPLIT


def _to_fixed_amount(raw_value: object) -> Optional[float]:
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


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
    payment_mode = _normalize_payment_mode(subscription.get("payment_mode"))
    split_share_base = _split_share_base(subscription, participants)
    per_share = float(subscription.get("amount") or 0.0) / split_share_base
    total = 0.0
    for person in participants:
        if payment_mode == PAYMENT_MODE_FIXED:
            fixed_amount = _to_fixed_amount(person.get("fixed_amount"))
            if fixed_amount is not None:
                total += fixed_amount
                continue
        weight = int(person.get("share_weight") or 1)
        total += per_share * weight
    return total


def _format_iso_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime(DATE_INPUT_FORMAT)
    except ValueError:
        return value


async def respond_with_markup(
    target: Responder,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=reply_markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
        await target.answer()
    else:
        await target.answer(text, reply_markup=reply_markup)


def _split_plain_block(block: str, limit: int) -> list[str]:
    parts: list[str] = []
    remaining = block
    while remaining:
        parts.append(remaining[:limit])
        remaining = remaining[limit:]
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


def split_text_chunks(text: str, limit: int = MAX_TELEGRAM_MESSAGE_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]

    blocks = text.split("\n\n")
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
            await target.answer("Unable to show the report right now.", show_alert=True)
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


async def send_subscription_list(target: Responder, db: Database) -> None:
    subs = await db.list_subscriptions()
    if not subs:
        await respond_with_markup(
            target,
            "📋 Subscriptions:\nNo subscriptions yet. Use <code>➕ New subscription</code> below to create one.",
            reply_markup=build_subscription_list_keyboard([]),
        )
        return

    lines = [
        "📋 Subscriptions:",
        "Choose a subscription to manage:",
        "",
    ]
    for idx, sub in enumerate(subs, 1):
        audience = sub["participant_count"]
        audience_text = f"{audience} user(s)" if audience else "no users"
        lines.extend(
            [
                f"{idx}. <code>{escape_html(sub['name'])}</code>",
                f"Amount: <code>{sub['amount']:.2f} {escape_html(sub['currency'])}</code>",
                f"Users: <code>{escape_html(audience_text)}</code>",
                "",
            ]
        )
    if lines and not lines[-1]:
        lines.pop()

    await respond_with_markup(
        target,
        "\n".join(lines),
        reply_markup=build_subscription_list_keyboard(subs),
    )


async def send_user_subscription_list(message: Message, db: Database, telegram_id: int) -> None:
    subs = await db.list_subscriptions_for_user(telegram_id)
    if not subs:
        await message.answer("📋 Subscriptions:\nYou don't have any subscriptions yet.")
        return

    lines = ["📋 Subscriptions:", "Your plans:", ""]
    for idx, sub in enumerate(subs, 1):
        due = _format_iso_date(sub["next_charge_at"])
        lines.extend(
            [
                f"{idx}. <code>{escape_html(sub['name'])}</code>",
                f"Amount: <code>{sub['amount']:.2f} {escape_html(sub['currency'])}</code>",
                f"Next charge: <code>{escape_html(due)}</code>",
                "",
            ]
        )
    if lines and not lines[-1]:
        lines.pop()
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
    next_charge = _format_iso_date(next_charge_value)
    payment_mode = _normalize_payment_mode(subscription.get("payment_mode"))
    _, share_text = _share_details(subscription, participants)
    split_share_base = _split_share_base(subscription, participants)
    per_person = subscription["amount"] / split_share_base
    users_total = _sum_users_total(subscription, participants)
    person_by_telegram: dict[int, Dict[str, object]] = {
        int(person["telegram_id"]): person
        for person in participants
        if person.get("telegram_id") is not None
    }

    if participants:
        participants_lines = []
        for p in participants:
            weight = int(p.get("share_weight") or 1)
            weight_text = f" (x{weight})" if weight > 1 else ""
            fixed_amount = _to_fixed_amount(p.get("fixed_amount"))
            amount_text = ""
            if payment_mode == PAYMENT_MODE_FIXED and fixed_amount is not None:
                amount_text = f" — {fixed_amount:.2f} {escape_html(subscription['currency'])}"
            participants_lines.append(
                f"• <code>{escape_html(p['full_name'])}{weight_text}{amount_text}</code>"
            )
        participants_text = "\n".join(participants_lines)
    else:
        participants_text = "<code>No users yet.</code>"

    if subscription["period_days"] == MONTHLY_PERIOD_SENTINEL:
        cadence = "every month on the same calendar day"
    else:
        cadence = f"every {subscription['period_days']} days"

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
    if user_sub_override:
        reminder_time_display = user_sub_override
    else:
        reminder_time_display = f"Default ({default_reminder_time})"

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
        my_currency_display = f"Default ({default_target_currency})"

    my_amount_display = "n/a"
    if user_id is not None:
        current_person = person_by_telegram.get(int(user_id))
        if current_person:
            if payment_mode == PAYMENT_MODE_FIXED:
                fixed_amount = _to_fixed_amount(current_person.get("fixed_amount"))
                if fixed_amount is not None:
                    my_amount_display = f"{fixed_amount:.2f} {subscription['currency']}"
                else:
                    weight = int(current_person.get("share_weight") or 1)
                    my_amount_display = f"{(per_person * weight):.2f} {subscription['currency']}"
            else:
                weight = int(current_person.get("share_weight") or 1)
                my_amount_display = f"{(per_person * weight):.2f} {subscription['currency']}"
    offsets_text = format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))
    overdue_text = "enabled" if subscription.get("remind_after_due") else "disabled"
    comment_value = (subscription.get("comment") or "").strip()
    comment_line = f"📝 Comment: <code>{escape_html(comment_value)}</code>" if comment_value else ""

    if unpaid_overdue:
        overdue_lines = "\n".join(f"• <code>{_format_iso_date(value)}</code>" for value in unpaid_overdue)
        overdue_block = f"⚠️ Overdue Cycles:\n{overdue_lines}"
    else:
        overdue_block = ""

    sections = [
        "Subscription Info:",
        f"🏷️ Name: <code>{escape_html(subscription['name'])}</code>",
        f"💰 Amount: <code>{subscription['amount']:.2f} {escape_html(subscription['currency'])}</code>",
        f"💱 My currency: <code>{escape_html(my_currency_display)}</code>",
        f"💵 My amount: <code>{escape_html(my_amount_display)}</code>",
        "",
        "Cycle Info:",
        f"📅 Next charge: <code>{escape_html(next_charge)}</code>",
        f"🔁 Cadence: <code>{escape_html(cadence)}</code>",
    ]
    if is_admin_view:
        sections.insert(5, f"💳 Payment mode: <code>{'Fixed per user' if payment_mode == PAYMENT_MODE_FIXED else 'Split by shares'}</code>")
        if payment_mode == PAYMENT_MODE_FIXED:
            sections.insert(6, f"👥 Users total: <code>{users_total:.2f} {escape_html(subscription['currency'])}</code>")
        else:
            sections.insert(6, f"👥 Per share: <code>≈ {per_person:.2f} {escape_html(subscription['currency'])}</code>")
            sections.insert(7, f"➗ Split mode: <code>{escape_html(share_text)}</code>")
    if overdue_block:
        sections.extend(["", overdue_block])
    if comment_line:
        sections.extend(["", comment_line])
    sections.extend(
        [
            "",
            "Reminder Info:",
            f"⏰ Time: <code>{escape_html(reminder_time_display)} ({escape_html(reminder_timezone)})</code>",
        ]
    )
    if is_admin_view:
        sections.extend(
            [
                f"🔔 Days: <code>{escape_html(offsets_text)}</code>",
                f"📣 Post-due: <code>{escape_html(overdue_text)}</code>",
                "",
                f"Users ({len(participants)}):",
                participants_text,
            ]
        )
    text = "\n".join(sections)
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
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return

    participants = await db.list_subscription_participants(subscription_id)
    if not participants:
        await respond_with_markup(
            target,
        "No users to report yet.",
            reply_markup=subscription_report_keyboard(subscription_id),
        )
        return
    text = await _build_subscription_payment_report_text(
        db,
        subscription,
        participants,
        scope="all",
    )
    await send_chunked_responder_text(
        target,
        text,
        reply_markup=subscription_report_keyboard(subscription_id),
    )


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
    )
    await send_chunked_responder_text(
        target,
        text,
        reply_markup=public_subscription_report_keyboard(subscription_id),
    )


async def send_public_user_payment_report(
    message: Message,
    db: Database,
    telegram_id: int,
) -> None:
    blocks = await _build_user_payment_blocks(db, telegram_id)
    if not blocks:
        await message.answer("No payments to report yet.")
        return
    await send_chunked_responder_text(message, "\n\n".join(blocks))


async def _build_user_payment_blocks(db: Database, telegram_id: int) -> list[str]:
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
        )
        blocks.append(block)
    return blocks


async def _build_subscription_payment_report_text(
    db: Database,
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
    *,
    scope: str,
    user_id: Optional[int] = None,
) -> str:
    today = datetime.today().date()
    months = [f"{today.year:04d}-{month:02d}" for month in range(1, 13)]
    month_labels = [
        datetime.strptime(month_value, "%Y-%m").strftime("%b")[0] for month_value in months
    ]

    subscription_id = int(subscription["id"])
    due_date = datetime.strptime(subscription["next_charge_at"], "%Y-%m-%d").date()
    await db.ensure_cycle(subscription_id, due_date)
    period_days = int(subscription.get("period_days") or 30)
    monthly_anchor_day = normalize_monthly_anchor_day(subscription.get("monthly_anchor_day"))
    while due_date < today:
        due_date = calculate_next_charge_date(due_date, period_days, monthly_anchor_day)
        await db.ensure_cycle(subscription_id, due_date)

    open_cycles = await db.list_open_cycles(subscription_id)
    open_cycle_dates = [
        datetime.strptime(value, "%Y-%m-%d").date() for value in open_cycles
    ]
    payments_for_open = await db.list_payments_for_cycles(subscription_id, open_cycles)
    paid_due_map = {
        (row["due_date"], row["paid_by_telegram_id"])
        for row in payments_for_open
        if row.get("paid_by_telegram_id") is not None
    }

    paid_rows = await db.list_payment_activity_by_due_month(subscription_id, today.year)
    paid_month_map = {
        (row["paid_by_telegram_id"], row["month"])
        for row in paid_rows
        if row.get("paid_by_telegram_id") is not None
    }

    if scope == "user":
        if user_id is None:
            raise ValueError("user_id is required when scope is 'user'")
        filtered = [p for p in participants if p["telegram_id"] == user_id]
    else:
        filtered = list(participants)

    names = [escape_html(format_display_name(person.get("full_name"))) for person in filtered]

    name_width = max(len("Name"), max(len(name) for name in names))
    header = f"{'Name':<{name_width}} | " + " ".join(month_labels)
    lines = [header]

    for person, display_name in zip(filtered, names):
        payer_id = person["telegram_id"]
        squares = ""
        for month in months:
            month_overdue = False
            for open_due in open_cycle_dates:
                if open_due.strftime("%Y-%m") != month:
                    continue
                if open_due < today and (open_due.isoformat(), payer_id) not in paid_due_map:
                    month_overdue = True
                    break
            if month_overdue:
                squares += "🟥"
            elif (payer_id, month) in paid_month_map:
                squares += "🟩"
            else:
                squares += "⬜"
        lines.append(f"{display_name:<{name_width}} | {squares}")

    return (
        "📊 Payments report:\n"
        f"Name: <code>{escape_html(subscription['name'])}</code>\n"
        + "<pre>" + "\n".join(lines) + "</pre>"
    )


def _share_details(
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
) -> tuple[int, str]:
    payment_mode = _normalize_payment_mode(subscription.get("payment_mode"))
    if payment_mode == PAYMENT_MODE_FIXED:
        return 1, "fixed per user"

    share_limit = int(subscription.get("share_limit") or 0)
    if share_limit:
        return share_limit, f"split into {share_limit} share(s)"

    if not participants:
        return 1, "waiting for users"

    total_shares = sum(int(p.get("share_weight") or 1) for p in participants)
    if total_shares == len(participants):
        return total_shares, f"split across {len(participants)} user(s)"
    return total_shares, f"split across {total_shares} share(s) across {len(participants)} user(s)"


def _build_subscription_detail_text(
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
    base_time: str,
    base_timezone: str,
) -> str:
    payment_mode = _normalize_payment_mode(subscription.get("payment_mode"))
    split_share_base = _split_share_base(subscription, participants)
    _, share_text = _share_details(subscription, participants)
    per_person = subscription["amount"] / split_share_base
    users_total = _sum_users_total(subscription, participants)
    comment_value = (subscription.get("comment") or "").strip()
    comment_line = f"📝 Comment: <code>{escape_html(comment_value)}</code>" if comment_value else ""

    if participants:
        participants_lines = []
        for p in participants:
            weight = int(p.get("share_weight") or 1)
            weight_text = f" (x{weight})" if weight > 1 else ""
            fixed_amount = _to_fixed_amount(p.get("fixed_amount"))
            amount_text = ""
            if payment_mode == PAYMENT_MODE_FIXED and fixed_amount is not None:
                amount_text = f" — {fixed_amount:.2f} {escape_html(subscription['currency'])}"
            participants_lines.append(
                f"• <code>{escape_html(p['full_name'])}{weight_text}{amount_text}</code>"
            )
        participants_text = "\n".join(participants_lines)
    else:
        participants_text = "<code>No users yet.</code>"

    if subscription["period_days"] == MONTHLY_PERIOD_SENTINEL:
        cadence = "every month on the same calendar day"
    else:
        cadence = f"every {subscription['period_days']} days"

    override_time = normalize_time_string(subscription.get("reminder_time"))
    if override_time:
        reminder_time = f"{override_time} ({base_timezone})"
    else:
        reminder_time = f"{base_time} ({base_timezone})"
    offsets_text = format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))
    overdue_text = "enabled" if subscription.get("remind_after_due") else "disabled"

    lines = [
        "Subscription Info:",
        f"🏷️ Name: <code>{escape_html(subscription['name'])}</code>",
        f"💰 Amount: <code>{subscription['amount']:.2f} {escape_html(subscription['currency'])}</code>",
        f"💱 Base currency: <code>{escape_html(str(subscription.get('base_currency') or subscription['currency']).upper())}</code>",
        f"💳 Payment mode: <code>{'Fixed per user' if payment_mode == PAYMENT_MODE_FIXED else 'Split by shares'}</code>",
        "",
        "Cycle Info:",
        f"📅 Next charge: <code>{_format_iso_date(subscription['next_charge_at'])}</code>",
        f"🔁 Cadence: <code>{escape_html(cadence)}</code>",
        "",
        "Reminder Info:",
        f"⏰ Time: <code>{escape_html(reminder_time)}</code>",
        f"🔔 Days: <code>{escape_html(offsets_text)}</code>",
        f"📣 Post-due: <code>{escape_html(overdue_text)}</code>",
    ]
    if payment_mode == PAYMENT_MODE_FIXED:
        lines.insert(5, f"👥 Users total: <code>{users_total:.2f} {escape_html(subscription['currency'])}</code>")
    else:
        lines.insert(5, f"👥 Per share: <code>≈ {per_person:.2f} {escape_html(subscription['currency'])}</code>")
        lines.insert(6, f"➗ Split mode: <code>{escape_html(share_text)}</code>")
    if comment_line:
        lines.extend(["", comment_line])
    lines.extend(["", f"Users ({len(participants)}):", participants_text])
    return "\n".join(lines)


async def send_subscription_detail(target: Responder, db: Database, subscription_id: int) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return

    participants = await db.list_subscription_participants(subscription_id)
    base_time = parse_time_string(await db.get_effective_base_reminder_time()).strftime("%H:%M")
    base_timezone = normalize_timezone_name(
        await db.get_effective_base_timezone(DEFAULT_REMINDER_TIMEZONE),
        DEFAULT_REMINDER_TIMEZONE,
    ) or DEFAULT_REMINDER_TIMEZONE
    text = _build_subscription_detail_text(subscription, participants, base_time, base_timezone)

    await respond_with_markup(
        target,
        text,
        reply_markup=subscription_detail_keyboard(subscription_id),
    )


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
        lines.append(f"{idx}. <code>{escape_html(friend['full_name'])}</code>")
    if total_pages > 1:
        lines.append(f"Page: <code>{page}/{total_pages}</code>")

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
    subs = await db.list_subscriptions_for_user(friend["telegram_id"])
    if subs:
        sub_lines = "\n".join(f"• <code>{escape_html(sub['name'])}</code>" for sub in subs)
    else:
        sub_lines = "<code>No subscriptions yet.</code>"
    text = (
        "User Info:\n"
        f"Name: <code>{escape_html(friend['full_name'])}</code>\n"
        f"🆔 Telegram ID: <code>{friend['telegram_id']}</code>\n\n"
        "Subscriptions:\n"
        f"{sub_lines}"
    )
    await respond_with_markup(
        target,
        text,
        reply_markup=member_detail_keyboard(friend_id),
    )


async def send_member_report(target: Responder, db: Database, friend_id: int) -> None:
    friend = await db.get_friend(friend_id)
    if not friend:
        await respond_with_markup(target, "User not found.")
        return
    blocks = await _build_user_payment_blocks(db, friend["telegram_id"])
    if not blocks:
        await respond_with_markup(
            target,
            "No payments to report yet.",
            reply_markup=member_report_keyboard(friend_id),
        )
        return
    await send_chunked_responder_text(
        target,
        "\n\n".join(blocks),
        reply_markup=member_report_keyboard(friend_id),
    )


async def send_reminder_settings(target: Responder, db: Database, subscription_id: int) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return

    base_time = parse_time_string(await db.get_effective_base_reminder_time()).strftime("%H:%M")
    base_timezone = normalize_timezone_name(
        await db.get_effective_base_timezone(DEFAULT_REMINDER_TIMEZONE),
        DEFAULT_REMINDER_TIMEZONE,
    ) or DEFAULT_REMINDER_TIMEZONE
    override_time = normalize_time_string(subscription.get("reminder_time"))
    if override_time:
        reminder_line = f"{override_time} ({base_timezone}, subscription override)"
    else:
        reminder_line = f"default ({base_time} {base_timezone})"
    offsets_text = format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))
    overdue_text = "enabled" if subscription.get("remind_after_due") else "disabled"
    text = (
        "🔔 Reminders:\n"
        f"🏷️ Name: <code>{escape_html(subscription['name'])}</code>\n"
        "\n"
        f"⏰ Time: <code>{escape_html(reminder_line)}</code>\n"
        f"🔔 Days: <code>{escape_html(offsets_text)}</code>\n"
        f"📣 Post-due alerts: <code>{escape_html(overdue_text)}</code>"
    )

    await respond_with_markup(
        target,
        text,
        reply_markup=reminder_settings_keyboard(subscription_id),
    )


async def send_reminder_send_menu(target: Responder, db: Database, subscription_id: int) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return

    participants = await db.list_subscription_participants(subscription_id)
    if not participants:
        await respond_with_markup(
            target,
            "📬 Send reminders now:\nNo users are assigned yet. Add them via <code>📋 Subscriptions</code>.",
            reply_markup=reminder_settings_keyboard(subscription_id),
        )
        return

    text = (
        f"📬 Send reminders now:\n🏷️ Name: <code>{escape_html(subscription['name'])}</code>\n"
        "\n"
        "Choose who should receive the reminder."
    )
    await respond_with_markup(
        target,
        text,
        reply_markup=reminder_send_targets_keyboard(subscription_id, participants),
    )


async def send_settings_tests_menu(target: Responder, db: Database) -> None:
    text = "🧪 Tests:\nChoose a test action."
    await respond_with_markup(
        target,
        text,
        reply_markup=tests_menu_keyboard(),
    )


async def send_test_subscription_list(target: Responder, db: Database) -> None:
    subs = await db.list_subscriptions()
    if not subs:
        await respond_with_markup(
            target,
            "🧪 Tests:\nNo subscriptions yet. Create one first.",
            reply_markup=settings_tests_keyboard([]),
        )
        return

    text = "🧪 Tests:\nChoose a subscription to send test reminders:"
    await respond_with_markup(
        target,
        text,
        reply_markup=settings_tests_keyboard(subs),
    )


async def send_test_reminder_targets(target: Responder, db: Database, subscription_id: int) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        subs = await db.list_subscriptions()
        await respond_with_markup(
            target,
            "This subscription no longer exists.",
            reply_markup=settings_tests_keyboard(subs),
        )
        return

    participants = await db.list_subscription_participants(subscription_id)
    if not participants:
        subs = await db.list_subscriptions()
        await respond_with_markup(
            target,
            "No users are assigned yet. Add them via 📋 Subscriptions.",
            reply_markup=settings_tests_keyboard(subs),
        )
        return

    text = (
        f"🧪 Test reminder send:\n🏷️ Name: <code>{escape_html(subscription['name'])}</code>\n"
        "\n"
        "Choose who should receive the test reminder."
    )
    await respond_with_markup(
        target,
        text,
        reply_markup=test_reminder_targets_keyboard(subscription_id, participants),
    )


async def send_pricing_settings(target: Responder, db: Database, subscription_id: int) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return
    participants = await db.list_subscription_participants(subscription_id)
    payment_mode = _normalize_payment_mode(subscription.get("payment_mode"))
    users_total = _sum_users_total(subscription, participants)
    mode_label = "Fixed per user" if payment_mode == PAYMENT_MODE_FIXED else "Split by shares"

    text = (
        "💰 Pricing:\n"
        f"🏷️ Name: <code>{escape_html(subscription['name'])}</code>\n"
        "\n"
        f"💰 Amount: <code>{subscription['amount']:.2f} {escape_html(subscription['currency'])}</code>\n"
        f"💱 Base currency: <code>{escape_html(str(subscription.get('base_currency') or subscription['currency']).upper())}</code>\n"
        f"💳 Payment mode: <code>{mode_label}</code>\n"
        f"👥 Users total: <code>{users_total:.2f} {escape_html(subscription['currency'])}</code>"
    )

    await respond_with_markup(
        target,
        text,
        reply_markup=pricing_settings_keyboard(subscription_id),
    )


async def send_subscription_user_amounts(
    target: Responder,
    db: Database,
    subscription_id: int,
) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return
    participants = await db.list_subscription_participants(subscription_id)
    payment_mode = _normalize_payment_mode(subscription.get("payment_mode"))
    assigned_total = sum(
        amount
        for amount in (_to_fixed_amount(person.get("fixed_amount")) for person in participants)
        if amount is not None
    )
    missing_count = sum(
        1 for person in participants if _to_fixed_amount(person.get("fixed_amount")) is None
    )
    mode_label = "Fixed per user" if payment_mode == PAYMENT_MODE_FIXED else "Split by shares"
    text = (
        "👥 Amount per user:\n"
        f"🏷️ Name: <code>{escape_html(subscription['name'])}</code>\n"
        f"💳 Mode: <code>{mode_label}</code>\n"
        f"💰 Subscription: <code>{subscription['amount']:.2f} {escape_html(subscription['currency'])}</code>\n"
        f"👥 Assigned total: <code>{assigned_total:.2f} {escape_html(subscription['currency'])}</code>\n"
        f"⚠️ Missing amounts: <code>{missing_count}</code>\n\n"
        "Select a user to set or clear their fixed amount."
    )
    await respond_with_markup(
        target,
        text,
        reply_markup=subscription_user_amounts_keyboard(
            subscription_id,
            participants,
            str(subscription["currency"]),
        ),
    )


async def send_participants_editor(callback: CallbackQuery, db: Database, subscription_id: int) -> None:
    friends = await db.list_friends_with_membership(subscription_id)
    if not friends:
        builder = InlineKeyboardBuilder()
        builder.button(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="open", subscription_id=subscription_id).pack(),
            style="primary",
        )
        await callback.message.edit_text(
            "👥 Users:\nNo users in the database yet. Add someone first with <code>👥 Users</code>.",
            reply_markup=builder.as_markup(),
        )
        await callback.answer()
        return

    keyboard = build_participants_keyboard(friends, subscription_id)
    selected = sum(1 for friend in friends if friend["is_member"])
    total_shares = sum(int(friend.get("share_weight") or 1) for friend in friends if friend["is_member"])
    text = (
        "👥 Users:\n"
        f"👥 Selected: <code>{selected}</code>\n"
        f"➗ Total shares: <code>{total_shares}</code>\n"
        "Tap <code>xN</code> to change share weight (1-5)."
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


def add_cancel_button(
    subscription_id: int,
    base_markup: Optional[InlineKeyboardMarkup] = None,
) -> InlineKeyboardMarkup:
    cancel_button = InlineKeyboardButton(
        text="✖ Cancel",
        callback_data=SubscriptionAction(action="cancel_edit", subscription_id=subscription_id).pack(),
    )
    rows = list(base_markup.inline_keyboard) if base_markup else []
    rows.append([cancel_button])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_subscription_edit_flow(
    callback: CallbackQuery,
    state: FSMContext,
    subscription_id: int,
    next_state: State,
    prompt: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    await state.set_state(next_state)
    await state.update_data(edit_subscription_id=int(subscription_id))
    await callback.answer()
    markup = add_cancel_button(subscription_id, reply_markup)
    await callback.message.answer(prompt, reply_markup=markup)


async def get_edit_subscription_id(state: FSMContext) -> Optional[int]:
    data = await state.get_data()
    subscription_id = data.get("edit_subscription_id")
    if subscription_id is None:
        return None
    return int(subscription_id)


async def require_edit_subscription_id(message: Message, state: FSMContext) -> Optional[int]:
    subscription_id = await get_edit_subscription_id(state)
    if subscription_id is None:
        await message.answer(
            "Session expired. Open 📋 Subscriptions and select the item again.",
            reply_markup=admin_reply_keyboard(),
        )
        await state.clear()
        return None
    return subscription_id


def currency_prompt() -> Tuple[str, InlineKeyboardMarkup]:
    return (
        "💱 Currency:\nChoose a currency or type your own (3 letters).",
        build_currency_keyboard(DEFAULT_CURRENCIES),
    )


def period_prompt() -> Tuple[str, InlineKeyboardMarkup]:
    return (
        "🔁 Period:\nRepeat period in days (default 30). Choose a preset or send your own number.",
        build_period_keyboard(),
    )


def share_limit_prompt() -> Tuple[str, InlineKeyboardMarkup]:
    return (
        "➗ Split limit:\nSend the number of users who split this subscription or tap “Split across all”.",
        build_share_limit_keyboard(),
    )


def payment_mode_prompt() -> Tuple[str, InlineKeyboardMarkup]:
    return (
        "💳 Payment mode:\nChoose how user amounts will be calculated.",
        build_creation_payment_mode_keyboard(),
    )
