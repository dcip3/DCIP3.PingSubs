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

from app.core.constants import DATE_INPUT_FORMAT, DEFAULT_CURRENCIES, MONTHLY_PERIOD_SENTINEL
from app.storage.db import Database
from app.ui.keyboards import (
    admin_reply_keyboard,
    build_members_list_keyboard,
    build_participants_keyboard,
    build_currency_keyboard,
    build_period_keyboard,
    build_public_subscription_list_keyboard,
    build_share_limit_keyboard,
    build_subscription_list_keyboard,
    dialog_keyboard,
    member_detail_keyboard,
    member_report_keyboard,
    pricing_settings_keyboard,
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
    normalize_time_string,
    normalize_timezone_name,
    parse_offsets,
    parse_time_string,
    parse_timezone,
)
from app.ui.text import escape_html, format_display_name

MAX_TELEGRAM_MESSAGE_LEN = 3900


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
            "No subscriptions yet. Use “➕ New subscription” below to create one.",
            reply_markup=build_subscription_list_keyboard([]),
        )
        return

    lines = [
        "Choose a subscription to manage it. Use “➕ New subscription” below to add another plan:"
    ]
    for idx, sub in enumerate(subs, 1):
        audience = sub["participant_count"]
        audience_text = f"{audience} user(s)" if audience else "no users"
        lines.append(
            f"{idx}. {escape_html(sub['name'])} — {sub['amount']:.2f} {escape_html(sub['currency'])} | {audience_text}"
        )

    await respond_with_markup(
        target,
        "\n".join(lines),
        reply_markup=build_subscription_list_keyboard(subs),
    )


async def send_user_subscription_list(message: Message, db: Database, telegram_id: int) -> None:
    subs = await db.list_subscriptions_for_user(telegram_id)
    if not subs:
        await message.answer("You don't have any subscriptions yet.")
        return

    lines = ["Your subscriptions:"]
    for idx, sub in enumerate(subs, 1):
        due = _format_iso_date(sub["next_charge_at"])
        lines.append(
            f"{idx}. {escape_html(sub['name'])} — {sub['amount']:.2f} {escape_html(sub['currency'])} | {due}"
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
        try:
            current_due = datetime.strptime(next_charge_value, "%Y-%m-%d").date()
            next_charge_value = calculate_next_charge_date(current_due, period_days).isoformat()
        except ValueError:
            pass
    next_charge = _format_iso_date(next_charge_value)
    share_base, share_text = _share_details(subscription, participants)
    per_person = subscription["amount"] / share_base

    if participants:
        participants_lines = []
        for p in participants:
            weight = int(p.get("share_weight") or 1)
            weight_text = f" (x{weight})" if weight > 1 else ""
            participants_lines.append(
                f"       {escape_html(p['full_name'])}{weight_text}"
            )
        participants_text = "\n".join(participants_lines)
    else:
        participants_text = "       No users yet."

    if subscription["period_days"] == MONTHLY_PERIOD_SENTINEL:
        cadence = "every month on the same calendar day"
    else:
        cadence = f"every {subscription['period_days']} days"

    admin_base_time = parse_time_string(
        await db.get_effective_base_reminder_time(),
    ).strftime("%H:%M")
    subscription_override = normalize_time_string(subscription.get("reminder_time"))
    reminder_time = subscription_override or admin_base_time
    reminder_source = "admin base time"
    if user_id is not None:
        user_sub_override = normalize_time_string(
            await db.get_user_subscription_setting(user_id, subscription_id, "reminder_time")
        )
        user_base_override = normalize_time_string(
            await db.get_user_setting(user_id, "base_reminder_time")
        )
        if user_sub_override:
            reminder_time = user_sub_override
            reminder_source = "your subscription override"
        elif subscription_override:
            reminder_time = subscription_override
            reminder_source = "subscription override by admin"
        elif user_base_override:
            reminder_time = user_base_override
            reminder_source = "your base time"
    elif subscription_override:
        reminder_source = "subscription override"
    offsets_text = format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))
    overdue_text = "enabled" if subscription.get("remind_after_due") else "disabled"
    comment_value = (subscription.get("comment") or "").strip()
    comment_block = ""
    if comment_value:
        comment_block = f"📝 <b>Comment</b>:\n       {escape_html(comment_value)}\n"

    if unpaid_overdue:
        overdue_lines = "\n".join(f"       {_format_iso_date(value)}" for value in unpaid_overdue)
        overdue_block = f"⚠️ <b>Overdue</b>:\n{overdue_lines}\n"
    else:
        overdue_block = ""

    text = (
        f"<b>{escape_html(subscription['name'])}</b>\n"
        f"💰 <b>Amount</b>:\n"
        f"       {subscription['amount']:.2f} {escape_html(subscription['currency'])}\n"
        f"       ≈ {per_person:.2f} {escape_html(subscription['currency'])} per share, {share_text}\n"
        f"{overdue_block}"
        f"{comment_block}"
        "📅 <b>Next charge</b>:\n"
        f"       {next_charge}\n"
        f"       {cadence}\n"
        "🔔 <b>Reminders</b>:\n"
        f"       {reminder_time} ({reminder_timezone}, {reminder_source})\n"
        f"       Days: {offsets_text}\n"
        f"       Post-due: {overdue_text}\n"
        f"👥 <b>Users</b> ({len(participants)}):\n"
        f"{participants_text}"
    )
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
    while due_date < today:
        due_date = calculate_next_charge_date(due_date, period_days)
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

    return f"{escape_html(subscription['name'])}:\n" + "<pre>" + "\n".join(lines) + "</pre>"


def _share_details(
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
) -> tuple[int, str]:
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
    share_base, share_text = _share_details(subscription, participants)
    per_person = subscription["amount"] / share_base
    comment_value = (subscription.get("comment") or "").strip()
    comment_block = ""
    if comment_value:
        comment_block = f"📝 <b>Comment</b>:\n       {escape_html(comment_value)}\n"

    if participants:
        participants_lines = []
        for p in participants:
            weight = int(p.get("share_weight") or 1)
            weight_text = f" (x{weight})" if weight > 1 else ""
            participants_lines.append(
                f"       {escape_html(p['full_name'])}{weight_text}"
            )
        participants_text = "\n".join(participants_lines)
    else:
        participants_text = "       No users yet."

    if subscription["period_days"] == MONTHLY_PERIOD_SENTINEL:
        cadence = "every month on the same calendar day"
    else:
        cadence = f"every {subscription['period_days']} days"

    override_time = normalize_time_string(subscription.get("reminder_time"))
    if override_time:
        reminder_time = f"{override_time} ({base_timezone}, subscription override)"
    else:
        reminder_time = f"{base_time} ({base_timezone}, admin base time)"
    offsets_text = format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))
    overdue_text = "enabled" if subscription.get("remind_after_due") else "disabled"

    return (
        f"<b>{escape_html(subscription['name'])}</b>\n"
        f"💰 <b>Amount</b>:\n"
        f"       {subscription['amount']:.2f} {escape_html(subscription['currency'])}\n"
        f"       ≈ {per_person:.2f} {escape_html(subscription['currency'])} per share, {share_text}\n"
        f"{comment_block}"
        f"📅 <b>Next charge</b>:\n"
        f"       {_format_iso_date(subscription['next_charge_at'])}\n"
        f"       {cadence}\n"
        f"🔔 <b>Reminders</b>:\n"
        f"       {reminder_time}\n"
        f"       Days: {offsets_text}\n"
        f"       Post-due: {overdue_text}\n"
        f"👥 <b>Users</b> ({len(participants)}):\n"
        f"{participants_text}"
    )


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


async def send_member_list(target: Responder, db: Database) -> None:
    friends = await db.list_friends()
    if not friends:
        await respond_with_markup(
            target,
            "No users yet. Use “➕ Add user” to create one.",
            reply_markup=build_members_list_keyboard([]),
        )
        return
    lines = ["Choose a user to manage:"]
    for idx, friend in enumerate(friends, 1):
        lines.append(f"{idx}. {escape_html(friend['full_name'])}")
    await respond_with_markup(
        target,
        "\n".join(lines),
        reply_markup=build_members_list_keyboard(friends),
    )


async def send_member_detail(target: Responder, db: Database, friend_id: int) -> None:
    friend = await db.get_friend(friend_id)
    if not friend:
        await respond_with_markup(target, "User not found.")
        return
    subs = await db.list_subscriptions_for_user(friend["telegram_id"])
    if subs:
        sub_lines = "\n".join(f"       {escape_html(sub['name'])}" for sub in subs)
    else:
        sub_lines = "       No subscriptions yet."
    text = (
        f"<b>{escape_html(friend['full_name'])}</b>\n"
        f"Telegram ID: {friend['telegram_id']}\n"
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
        f"<b>{escape_html(subscription['name'])}</b>\n"
        "🔔 <b>Reminders</b>:\n"
        f"       ⏰ Time: {reminder_line}\n"
        f"       🔔 Days: {offsets_text}\n"
        f"       📣 Post-due alerts: {overdue_text}"
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
            "No users are assigned yet. Add them via 📋 Subscriptions.",
            reply_markup=reminder_settings_keyboard(subscription_id),
        )
        return

    text = (
        f"<b>{escape_html(subscription['name'])}</b>\n"
        "Choose who should receive the reminder."
    )
    await respond_with_markup(
        target,
        text,
        reply_markup=reminder_send_targets_keyboard(subscription_id, participants),
    )


async def send_settings_tests_menu(target: Responder, db: Database) -> None:
    text = "Tests:"
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
            "No subscriptions yet. Create one first.",
            reply_markup=settings_tests_keyboard([]),
        )
        return

    text = "Choose a subscription to send test reminders:"
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
        f"<b>{escape_html(subscription['name'])}</b>\n"
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
    share_base, share_text = _share_details(subscription, participants)
    per_person = subscription["amount"] / share_base

    text = (
        f"<b>Pricing</b> for {escape_html(subscription['name'])}:\n"
        f"      {subscription['amount']:.2f} {escape_html(subscription['currency'])}\n"
        f"      ≈ {per_person:.2f} {escape_html(subscription['currency'])} per share, {share_text}"
    )

    await respond_with_markup(
        target,
        text,
        reply_markup=pricing_settings_keyboard(subscription_id),
    )


async def send_participants_editor(callback: CallbackQuery, db: Database, subscription_id: int) -> None:
    friends = await db.list_friends_with_membership(subscription_id)
    if not friends:
        builder = InlineKeyboardBuilder()
        builder.button(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="open", subscription_id=subscription_id).pack(),
        )
        await callback.message.edit_text(
            "No users in the database yet. Add someone first with “👥 Users”.",
            reply_markup=builder.as_markup(),
        )
        await callback.answer()
        return

    keyboard = build_participants_keyboard(friends, subscription_id)
    selected = sum(1 for friend in friends if friend["is_member"])
    total_shares = sum(int(friend.get("share_weight") or 1) for friend in friends if friend["is_member"])
    text = (
        "Toggle users for this subscription.\n"
        "Tap xN to change share weight (1-5).\n"
        f"Currently selected: {selected} | Total shares: {total_shares}"
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
        'Choose a currency or type your own (3 letters):',
        build_currency_keyboard(DEFAULT_CURRENCIES),
    )


def period_prompt() -> Tuple[str, InlineKeyboardMarkup]:
    return (
        'Repeat period in days (default 30). Choose a preset or send your own number:',
        build_period_keyboard(),
    )


def share_limit_prompt() -> Tuple[str, InlineKeyboardMarkup]:
    return (
        'Send the number of users who split this subscription or tap “Split across all”.',
        build_share_limit_keyboard(),
    )
