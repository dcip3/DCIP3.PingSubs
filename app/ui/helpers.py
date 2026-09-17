from __future__ import annotations

from datetime import date, datetime
from typing import Dict, Optional, Sequence, Tuple, Union

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from app.core.constants import (
    DEFAULT_CURRENCIES,
    MONTHLY_PERIOD_SENTINEL,
    PAYMENT_MODE_FIXED,
)
from app.storage.db import Database
from app.services import normalize_payment_mode, parse_fixed_amount
from app.ui.keyboards import (
    admin_reply_keyboard,
    build_members_list_keyboard,
    build_participants_keyboard,
    build_currency_keyboard,
    build_creation_payment_mode_keyboard,
    build_period_keyboard,
    build_public_subscription_list_keyboard,
    public_account_keyboard,
    build_share_limit_keyboard,
    build_subscription_list_keyboard,
    dialog_cancel_inline_keyboard,
    member_detail_keyboard,
    member_report_keyboard,
    participants_settings_keyboard,
    pricing_settings_keyboard,
    subscription_user_amounts_keyboard,
    public_subscription_detail_keyboard,
    public_subscription_report_keyboard,
    public_reply_keyboard,
    reminder_settings_keyboard,
    reminder_send_targets_keyboard,
    settings_tests_keyboard,
    subscription_cycle_actions_keyboard,
    subscription_open_cycles_keyboard,
    subscription_more_keyboard,
    subscription_payment_info_keyboard,
    subscription_report_keyboard,
    subscription_detail_keyboard,
    tests_menu_keyboard,
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
    due_status_html,
    tg_due,
)
from app.ui.text import escape_html, format_display_name

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


def _reminder_summary(reminder_time: str, tz_name: str, subscription: Dict[str, object]) -> str:
    """Time, timezone, reminder days and the post-due flag on one line (no emoji: each card adds its own)."""
    offsets_text = format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))
    return (
        f"{escape_html(reminder_time)} {escape_html(tz_name)} · {escape_html(offsets_text)}"
        f" · after due: {_on_off(subscription.get('remind_after_due'))}"
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
        audience = int(sub["participant_count"] or 0)
        if audience == 0:
            audience_text = "no users"
        else:
            audience_text = f"{audience} user" if audience == 1 else f"{audience} users"
        lines.append(
            f"{idx}. <b>{escape_html(sub['name'])}</b> — "
            f"{float(sub['amount']):.2f} {escape_html(sub['currency'])} · {audience_text}"
        )

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
        tz_name=await resolve_report_timezone(db),
    )
    await send_chunked_responder_text(
        target,
        assemble_payments_report([text]),
        reply_markup=subscription_report_keyboard(subscription_id),
    )


async def send_subscription_open_cycles(
    target: Responder,
    db: Database,
    subscription_id: int,
    notice: Optional[str] = None,
) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return

    open_cycles = await db.list_open_cycles(subscription_id)
    if not open_cycles:
        text = "🗂 Open cycles:\nNo open cycles for this subscription."
        if notice:
            text = f"{escape_html(notice)}\n\n{text}"
        await respond_with_markup(
            target,
            text,
            reply_markup=subscription_open_cycles_keyboard(subscription_id),
        )
        return

    cycle_state_map = await db.list_cycle_participants_for_due_dates(subscription_id, open_cycles)
    payments = await db.list_payments_for_cycles(subscription_id, open_cycles)
    live_participants = await db.list_subscription_participants(subscription_id)

    paid_by_due: dict[str, set[int]] = {}
    for row in payments:
        due_value = str(row.get("due_date") or "")
        payer_id = row.get("paid_by_telegram_id")
        if payer_id is None:
            continue
        paid_by_due.setdefault(due_value, set()).add(int(payer_id))

    tz_name = await resolve_report_timezone(db)
    today = datetime.now(parse_timezone(tz_name)).date()
    lines = [
        "🗂 Open cycles:",
        f"Subscription: <b>{escape_html(subscription['name'])}</b>",
        "",
    ]
    if notice:
        lines[0:0] = [escape_html(notice), ""]
    for idx, due_value in enumerate(open_cycles, 1):
        summary = _build_open_cycle_summary(
            subscription,
            due_value,
            today,
            cycle_state_map,
            live_participants,
            paid_by_due,
        )

        lines.extend(
            [
                f"{idx}. <b>{tg_due(due_value, tz_name, 'wD')}</b> — {due_status_html(due_value, tz_name, today)}",
                f"💰 Amount: {_money(float(summary['amount']), summary['currency'])}",
                f"👥 Users: {int(summary['users_count'])} | "
                f"✅ Paid: {int(summary['paid_count'])}/{int(summary['users_count'])}",
                f"📦 Snapshot flags: users={int(bool(summary['snapshot_ready']))} "
                f"settings={int(bool(summary['settings_snapshot_ready']))}",
                "",
            ]
        )

    await send_chunked_responder_text(
        target,
        "\n".join(lines).rstrip(),
        reply_markup=subscription_open_cycles_keyboard(subscription_id, open_cycles),
    )


async def send_subscription_cycle_actions(
    target: Responder,
    db: Database,
    subscription_id: int,
    due_value: str,
    notice: Optional[str] = None,
) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return

    open_cycles = await db.list_open_cycles(subscription_id)
    if due_value not in open_cycles:
        await respond_with_markup(
            target,
            "This cycle is no longer open.",
            reply_markup=subscription_open_cycles_keyboard(subscription_id, open_cycles),
        )
        return

    cycle_state_map = await db.list_cycle_participants_for_due_dates(subscription_id, [due_value])
    payments = await db.list_payments_for_cycles(subscription_id, [due_value])
    live_participants = await db.list_subscription_participants(subscription_id)

    paid_by_due: dict[str, set[int]] = {}
    for row in payments:
        payer_id = row.get("paid_by_telegram_id")
        if payer_id is None:
            continue
        paid_by_due.setdefault(str(row.get("due_date") or ""), set()).add(int(payer_id))

    tz_name = await resolve_report_timezone(db)
    today = datetime.now(parse_timezone(tz_name)).date()
    summary = _build_open_cycle_summary(
        subscription,
        due_value,
        today,
        cycle_state_map,
        live_participants,
        paid_by_due,
    )
    unpaid_count = max(int(summary["users_count"]) - int(summary["paid_count"]), 0)

    body = (
        "🗂 Open cycle:\n"
        f"Subscription: <b>{escape_html(subscription['name'])}</b>\n"
        f"Cycle: <b>{tg_due(due_value, tz_name, 'wD')}</b>\n"
        f"Status: {due_status_html(due_value, tz_name, today)}\n"
        f"💰 Amount: {_money(float(summary['amount']), summary['currency'])}\n"
        f"👥 Users: {int(summary['users_count'])} | "
        f"✅ Paid: {int(summary['paid_count'])}/{int(summary['users_count'])}\n"
        f"📦 Snapshot flags: users={int(bool(summary['snapshot_ready']))} "
        f"settings={int(bool(summary['settings_snapshot_ready']))}\n"
        "\n"
        "♻️ Recreate cycle resets this cycle to the current users/settings and clears its payment marks.\n"
        f"✅ Force close marks <b>{unpaid_count}</b> unpaid user(s) as paid and closes the cycle."
    )
    text = body if not notice else f"{escape_html(notice)}\n\n{body}"
    await respond_with_markup(
        target,
        text,
        reply_markup=subscription_cycle_actions_keyboard(subscription_id, due_value),
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


def payments_report_header(attention_count: int) -> str:
    if attention_count <= 0:
        return "📊 Payments report"
    verb = "needs" if attention_count == 1 else "need"
    return f"📊 Payments report · <b>{attention_count} {verb} attention</b>"


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
    if due_date > today:
        return f"in {(due_date - today).days} d"
    if due_date == today:
        return "today"
    return f"{(today - due_date).days} d overdue"


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
            extra=" ✅" if upcoming_paid else "",
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
                    paid_progress = f" · {paid_count}/{len(participant_ids)} paid"

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
        unpaid_lines.append(f"⚠️ {_format_report_due(due_value, due, today, tz_name)}: {names}")

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


def _share_details(
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
) -> tuple[int, str]:
    payment_mode = normalize_payment_mode(subscription.get("payment_mode"))
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
    open_cycles_count: int,
    payment_label: str,
    payment_details: str,
    payment_link: str,
) -> str:
    """Admin card: title, then one blockquote per section and the open-cycle count."""
    comment_value = (subscription.get("comment") or "").strip()
    today = datetime.now(parse_timezone(base_timezone)).date()
    override_time = normalize_time_string(subscription.get("reminder_time"))
    reminder_time = override_time or base_time

    quotes = [
        _quote(
            [
                _amount_line(subscription),
                f"📅 Next charge: {_next_charge_line(str(subscription['next_charge_at']), base_timezone, today)}",
                f"🔁 {_cadence_text(subscription)}",
            ]
        ),
        _users_quote(subscription, participants),
        _quote([f"🔔 {_reminder_summary(reminder_time, base_timezone, subscription)}"]),
        _quote(_payment_info_lines(payment_label, payment_details, payment_link, comment_value, compact=True)),
    ]
    return "\n".join(
        [
            f"<b>🏷️ {escape_html(subscription['name'])}</b>",
            "",
            *quotes,
            f"🗂 Open cycles: <b>{open_cycles_count}</b>",
        ]
    )


async def send_subscription_detail(target: Responder, db: Database, subscription_id: int) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return

    participants = await db.list_subscription_participants(subscription_id)
    open_cycles = await db.list_open_cycles(subscription_id)
    payment_destination = await db.get_effective_subscription_payment_destination(subscription_id)
    base_time = parse_time_string(await db.get_effective_base_reminder_time()).strftime("%H:%M")
    base_timezone = normalize_timezone_name(
        await db.get_effective_base_timezone(DEFAULT_REMINDER_TIMEZONE),
        DEFAULT_REMINDER_TIMEZONE,
    ) or DEFAULT_REMINDER_TIMEZONE
    text = _build_subscription_detail_text(
        subscription,
        participants,
        base_time,
        base_timezone,
        len(open_cycles),
        _subscription_payment_label(subscription, payment_destination),
        str(payment_destination.get("details") or "").strip() if payment_destination else "",
        str(payment_destination.get("payment_link") or "").strip() if payment_destination else "",
    )

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
        reminder_line = (
            f"<code>{escape_html(override_time)}</code> ({escape_html(base_timezone)}, subscription override)"
        )
    else:
        reminder_line = f"default (<code>{escape_html(base_time)}</code> {escape_html(base_timezone)})"
    offsets_text = format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))
    text = (
        "🔔 Reminders:\n\n"
        f"⏰ Time: {reminder_line}\n"
        f"🔔 Days: {escape_html(offsets_text)}\n"
        f"📣 Post-due alerts: {_on_off(subscription.get('remind_after_due'))}"
    )

    await respond_with_markup(
        target,
        text,
        reply_markup=reminder_settings_keyboard(subscription_id),
    )


async def send_participants_settings(target: Responder, db: Database, subscription_id: int) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return

    participants = await db.list_subscription_participants(subscription_id)
    payment_mode = normalize_payment_mode(subscription.get("payment_mode"))
    total_shares = sum(int(person.get("share_weight") or 1) for person in participants)
    if payment_mode == PAYMENT_MODE_FIXED:
        mode_details = "per-user amounts"
    else:
        _, share_text = _share_details(subscription, participants)
        mode_details = share_text

    text = (
        "👥 Participants:\n\n"
        f"💳 Payment mode: {'Fixed per user' if payment_mode == PAYMENT_MODE_FIXED else 'Split by shares'}\n"
        f"👥 Users: {len(participants)}\n"
        f"➗ Shares: {total_shares}\n"
        f"ℹ️ Mode details: {escape_html(mode_details)}"
    )
    await respond_with_markup(
        target,
        text,
        reply_markup=participants_settings_keyboard(subscription_id, payment_mode),
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
        f"📬 Send reminders now:\n🏷️ Name: <b>{escape_html(subscription['name'])}</b>\n"
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


async def send_pricing_settings(target: Responder, db: Database, subscription_id: int) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return
    if subscription["period_days"] == MONTHLY_PERIOD_SENTINEL:
        cadence = "every month"
    else:
        cadence = f"every {subscription['period_days']} days"
    tz_name = await resolve_report_timezone(db)
    text = (
        "💰 Pricing & schedule:\n\n"
        f"💰 Amount: {_money(float(subscription['amount']), subscription['currency'])}\n"
        f"🌐 Convert currency: {escape_html(str(subscription.get('base_currency') or subscription['currency']).upper())}\n"
        f"📅 Next charge: {tg_due(str(subscription['next_charge_at']), tz_name, 'wD')}\n"
        f"🔁 Period: {escape_html(cadence)}"
    )

    await respond_with_markup(
        target,
        text,
        reply_markup=pricing_settings_keyboard(subscription_id),
    )


async def send_subscription_more(target: Responder, db: Database, subscription_id: int) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return

    open_cycles = await db.list_open_cycles(subscription_id)
    text = (
        "📊 Reports & more:\n\n"
        f"🗂 Open cycles: {len(open_cycles)}"
    )

    await respond_with_markup(
        target,
        text,
        reply_markup=subscription_more_keyboard(subscription_id),
    )


async def send_subscription_payment_info(
    target: Responder,
    db: Database,
    subscription_id: int,
) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return
    payment_destination = await db.get_effective_subscription_payment_destination(subscription_id)
    payment_label = _subscription_payment_label(subscription, payment_destination)
    payment_details = str(payment_destination.get("details") or "").strip() if payment_destination else ""
    payment_link = str(payment_destination.get("payment_link") or "").strip() if payment_destination else ""
    comment_value = str(subscription.get("comment") or "").strip()
    text = "\n".join(_payment_info_lines(payment_label, payment_details, payment_link, comment_value))
    await respond_with_markup(
        target,
        text,
        reply_markup=subscription_payment_info_keyboard(subscription_id),
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
    assigned_total = _sum_users_total(subscription, participants)
    text = (
        "👥 Amount per user:\n"
        f"Subscription: {_money(float(subscription['amount']), subscription['currency'])}\n"
        f"Assigned total: {_money(assigned_total, subscription['currency'])}\n"
        "\n"
        "Select a user to set or clear their fixed amount.\n"
        "Use <code>Set all</code> to apply one amount to everyone."
    )
    await respond_with_markup(
        target,
        text,
        reply_markup=subscription_user_amounts_keyboard(
            subscription_id,
            participants,
            str(subscription["currency"]),
            float(subscription.get("amount") or 0.0),
        ),
    )


async def send_participants_editor(callback: CallbackQuery, db: Database, subscription_id: int) -> None:
    friends = await db.list_friends_with_membership(subscription_id)
    if not friends:
        builder = InlineKeyboardBuilder()
        builder.button(
            text="⬅️ Back",
            callback_data=SubscriptionAction(action="participants", subscription_id=subscription_id).pack(),
        )
        await callback.message.edit_text(
            "👥 Users:\nNo users in the database yet. Add someone first with <code>👥 Manage users</code>.",
            reply_markup=builder.as_markup(),
        )
        await callback.answer()
        return

    keyboard = build_participants_keyboard(friends, subscription_id)
    selected = sum(1 for friend in friends if friend["is_member"])
    total_shares = sum(int(friend.get("share_weight") or 1) for friend in friends if friend["is_member"])
    text = (
        "👥 Users:\n"
        f"Selected: {selected}\n"
        f"Total shares: {total_shares}\n"
        "\n"
        "Tap <code>xN</code> to change share weight (1-5)."
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


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
    markup = reply_markup or dialog_cancel_inline_keyboard()
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
