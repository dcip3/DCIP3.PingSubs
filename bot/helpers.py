from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional, Sequence, Tuple, Union

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from constants import DATE_INPUT_FORMAT, DEFAULT_CURRENCIES, MONTHLY_PERIOD_SENTINEL
from database import Database
from bot.keyboards import (
    admin_reply_keyboard,
    build_participants_keyboard,
    build_currency_keyboard,
    build_period_keyboard,
    build_public_subscription_list_keyboard,
    build_share_limit_keyboard,
    build_subscription_list_keyboard,
    dialog_keyboard,
    pricing_settings_keyboard,
    public_subscription_detail_keyboard,
    public_subscription_report_keyboard,
    reminder_settings_keyboard,
    subscription_report_keyboard,
    subscription_detail_keyboard,
)
from bot.states import Responder, SubscriptionAction
from bot.reminders import calculate_next_charge_date, format_offsets_for_display, parse_offsets


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
        audience_text = f"{audience} member(s)" if audience else "no members"
        lines.append(
            f"{idx}. {sub['name']} — {sub['amount']:.2f} {sub['currency']} | {audience_text}"
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
            f"{idx}. {sub['name']} — {sub['amount']:.2f} {sub['currency']} | {due}"
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
    today = datetime.today().date()
    open_cycles = await db.list_open_cycles(subscription_id)
    unpaid_overdue = []
    user_id = None
    if isinstance(target, CallbackQuery) and target.from_user:
        user_id = target.from_user.id
    elif isinstance(target, Message) and target.from_user:
        user_id = target.from_user.id

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

    next_charge = _format_iso_date(subscription["next_charge_at"])
    share_base, share_text = _share_details(subscription, participants)
    per_person = subscription["amount"] / share_base

    if participants:
        participants_lines = []
        for p in participants:
            weight = int(p.get("share_weight") or 1)
            weight_text = f" (x{weight})" if weight > 1 else ""
            participants_lines.append(
                f"       {p['full_name']}{weight_text}"
            )
        participants_text = "\n".join(participants_lines)
    else:
        participants_text = "       No members yet."

    if subscription["period_days"] == MONTHLY_PERIOD_SENTINEL:
        cadence = "every month on the same calendar day"
    else:
        cadence = f"every {subscription['period_days']} days"

    reminder_time = (subscription.get("reminder_time") or "16:00").strip() or "16:00"
    offsets_text = format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))
    overdue_text = "enabled" if subscription.get("remind_after_due") else "disabled"

    if unpaid_overdue:
        overdue_lines = "\n".join(f"       {_format_iso_date(value)}" for value in unpaid_overdue)
        overdue_block = f"⚠️ <b>Overdue</b>:\n{overdue_lines}\n"
    else:
        overdue_block = ""

    text = (
        f"<b>{subscription['name']}</b>\n"
        f"💰 <b>Amount</b>:\n"
        f"       {subscription['amount']:.2f} {subscription['currency']}\n"
        f"       ≈ {per_person:.2f} {subscription['currency']} per share, {share_text}\n"
        f"{overdue_block}"
        "📅 <b>Next charge</b>:\n"
        f"       {next_charge}\n"
        f"       {cadence}\n"
        "🔔 <b>Reminders</b>:\n"
        f"       {reminder_time} MSK\n"
        f"       Days: {offsets_text}\n"
        f"       Post-due: {overdue_text}\n"
        f"👥 <b>Members</b> ({len(participants)}):\n"
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
            "No members to report yet.",
            reply_markup=subscription_report_keyboard(subscription_id),
        )
        return

    today = datetime.today().date()
    months = [f"{today.year:04d}-{month:02d}" for month in range(1, 13)]
    month_labels = [
        datetime.strptime(month_value, "%Y-%m").strftime("%b")[0] for month_value in months
    ]
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

    names = []
    for person in participants:
        full_name = (person["full_name"] or "Unknown").strip()
        parts = [part for part in full_name.split() if part]
        first = parts[0] if parts else "Unknown"
        last_initial = f" {parts[1][0].upper()}." if len(parts) > 1 else ""
        names.append(f"{first}{last_initial}")
    name_width = max(len("Name"), max(len(name) for name in names))
    header = f"{'Name':<{name_width}} | " + " ".join(month_labels)
    lines = [header]

    for person, display_name in zip(participants, names):
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

    text = f"{subscription['name']}:\n" + "<pre>" + "\n".join(lines) + "</pre>"
    await respond_with_markup(
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

    today = datetime.today().date()
    months = [f"{today.year:04d}-{month:02d}" for month in range(1, 13)]
    month_labels = [
        datetime.strptime(month_value, "%Y-%m").strftime("%b")[0] for month_value in months
    ]

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

    full_name = (user["full_name"] or "Unknown").strip()
    parts = [part for part in full_name.split() if part]
    first = parts[0] if parts else "Unknown"
    last_initial = f" {parts[1][0].upper()}." if len(parts) > 1 else ""
    display_name = f"{first}{last_initial}"
    name_width = max(len("Name"), len(display_name))
    header = f"{'Name':<{name_width}} | " + " ".join(month_labels)

    squares = ""
    for month in months:
        month_overdue = False
        for open_due in open_cycle_dates:
            if open_due.strftime("%Y-%m") != month:
                continue
            if open_due < today and (open_due.isoformat(), telegram_id) not in paid_due_map:
                month_overdue = True
                break
        if month_overdue:
            squares += "🟥"
        elif (telegram_id, month) in paid_month_map:
            squares += "🟩"
        else:
            squares += "⬜"

    lines = [header, f"{display_name:<{name_width}} | {squares}"]
    text = f"{subscription['name']}:\n" + "<pre>" + "\n".join(lines) + "</pre>"
    await respond_with_markup(
        target,
        text,
        reply_markup=public_subscription_detail_keyboard(subscription_id),
    )


async def send_public_user_payment_report(
    message: Message,
    db: Database,
    telegram_id: int,
) -> None:
    subs = await db.list_subscriptions_for_user(telegram_id)
    if not subs:
        await message.answer("You don't have any subscriptions yet.")
        return

    today = datetime.today().date()
    months = [f"{today.year:04d}-{month:02d}" for month in range(1, 13)]
    month_labels = [
        datetime.strptime(month_value, "%Y-%m").strftime("%b")[0] for month_value in months
    ]
    header = "Months: " + " ".join(month_labels)

    blocks: list[str] = []
    for sub in subs:
        participants = await db.list_subscription_participants(sub["id"])
        user = next((p for p in participants if p["telegram_id"] == telegram_id), None)
        if not user:
            continue

        due_date = datetime.strptime(sub["next_charge_at"], "%Y-%m-%d").date()
        await db.ensure_cycle(sub["id"], due_date)
        period_days = int(sub.get("period_days") or 30)
        while due_date < today:
            due_date = calculate_next_charge_date(due_date, period_days)
            await db.ensure_cycle(sub["id"], due_date)

        open_cycles = await db.list_open_cycles(sub["id"])
        open_cycle_dates = [
            datetime.strptime(value, "%Y-%m-%d").date() for value in open_cycles
        ]
        payments_for_open = await db.list_payments_for_cycles(sub["id"], open_cycles)
        paid_due_map = {
            (row["due_date"], row["paid_by_telegram_id"])
            for row in payments_for_open
            if row.get("paid_by_telegram_id") is not None
        }
        paid_rows = await db.list_payment_activity_by_due_month(sub["id"], today.year)
        paid_month_map = {
            (row["paid_by_telegram_id"], row["month"])
            for row in paid_rows
            if row.get("paid_by_telegram_id") is not None
        }

        full_name = (user["full_name"] or "Unknown").strip()
        parts = [part for part in full_name.split() if part]
        first = parts[0] if parts else "Unknown"
        last_initial = f" {parts[1][0].upper()}." if len(parts) > 1 else ""
        display_name = f"{first}{last_initial}"

        squares = ""
        for month in months:
            month_overdue = False
            for open_due in open_cycle_dates:
                if open_due.strftime("%Y-%m") != month:
                    continue
                if open_due < today and (open_due.isoformat(), telegram_id) not in paid_due_map:
                    month_overdue = True
                    break
            if month_overdue:
                squares += "🟥"
            elif (telegram_id, month) in paid_month_map:
                squares += "🟩"
            else:
                squares += "⬜"

        block = f"{sub['name']}:\n<pre>{header}\n{display_name} | {squares}</pre>"
        blocks.append(block)

    if not blocks:
        await message.answer("No payments to report yet.")
        return

    await message.answer("\n\n".join(blocks))


def _share_details(
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
) -> tuple[int, str]:
    share_limit = int(subscription.get("share_limit") or 0)
    if share_limit:
        return share_limit, f"split into {share_limit} share(s)"

    if not participants:
        return 1, "waiting for members"

    total_shares = sum(int(p.get("share_weight") or 1) for p in participants)
    if total_shares == len(participants):
        return total_shares, f"split across {len(participants)} member(s)"
    return total_shares, f"split across {total_shares} share(s) across {len(participants)} member(s)"


def _build_subscription_detail_text(
    subscription: Dict[str, object],
    participants: Sequence[Dict[str, object]],
) -> str:
    share_base, share_text = _share_details(subscription, participants)
    per_person = subscription["amount"] / share_base

    if participants:
        participants_lines = []
        for p in participants:
            weight = int(p.get("share_weight") or 1)
            weight_text = f" (x{weight})" if weight > 1 else ""
            participants_lines.append(
                f"       {p['full_name']}{weight_text}"
            )
        participants_text = "\n".join(participants_lines)
    else:
        participants_text = "       No members yet."

    if subscription["period_days"] == MONTHLY_PERIOD_SENTINEL:
        cadence = "every month on the same calendar day"
    else:
        cadence = f"every {subscription['period_days']} days"

    reminder_time = (subscription.get("reminder_time") or "16:00").strip() or "16:00"
    offsets_text = format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))
    overdue_text = "enabled" if subscription.get("remind_after_due") else "disabled"

    return (
        f"<b>{subscription['name']}</b>\n"
        f"💰 <b>Amount</b>:\n"
        f"       {subscription['amount']:.2f} {subscription['currency']}\n"
        f"       ≈ {per_person:.2f} {subscription['currency']} per share, {share_text}\n"
        f"📅 <b>Charge date</b>:\n"
        f"       {_format_iso_date(subscription['next_charge_at'])}\n"
        f"       {cadence}\n"
        f"🔔 <b>Reminders</b>:\n"
        f"       {reminder_time} MSK\n"
        f"       Days: {offsets_text}\n"
        f"       Post-due: {overdue_text}\n"
        f"👥 <b>Members</b> ({len(participants)}):\n"
        f"{participants_text}"
    )


async def send_subscription_detail(target: Responder, db: Database, subscription_id: int) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return

    participants = await db.list_subscription_participants(subscription_id)
    text = _build_subscription_detail_text(subscription, participants)

    await respond_with_markup(
        target,
        text,
        reply_markup=subscription_detail_keyboard(subscription_id),
    )


async def send_reminder_settings(target: Responder, db: Database, subscription_id: int) -> None:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await respond_with_markup(target, "This subscription no longer exists.")
        return

    reminder_time = (subscription.get("reminder_time") or "16:00").strip() or "16:00"
    offsets_text = format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))
    overdue_text = "enabled" if subscription.get("remind_after_due") else "disabled"
    text = (
        f"<b>{subscription['name']}</b>\n"
        "🔔 <b>Reminders</b>:\n"
        f"       ⏰ Time: {reminder_time} MSK\n"
        f"       🔔 Days: {offsets_text}\n"
        f"       📣 Post-due alerts: {overdue_text}"
    )

    await respond_with_markup(
        target,
        text,
        reply_markup=reminder_settings_keyboard(subscription_id),
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
        f"<b>Pricing</b> for {subscription['name']}:\n"
        f"      {subscription['amount']:.2f} {subscription['currency']}\n"
        f"      ≈ {per_person:.2f} {subscription['currency']} per share, {share_text}"
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
            "No friends in the database yet. Add someone first with “👤 Add member”.",
            reply_markup=builder.as_markup(),
        )
        await callback.answer()
        return

    keyboard = build_participants_keyboard(friends, subscription_id)
    selected = sum(1 for friend in friends if friend["is_member"])
    total_shares = sum(int(friend.get("share_weight") or 1) for friend in friends if friend["is_member"])
    text = (
        "Toggle members for this subscription.\n"
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
        'Send the number of members who split this subscription or tap “Split across all”.',
        build_share_limit_keyboard(),
    )
