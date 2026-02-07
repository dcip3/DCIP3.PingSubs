from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Dict, Optional

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError

from app.ui.keyboards import (
    build_batch_payment_confirmation_keyboard,
    build_payment_confirmation_keyboard,
    build_test_payment_confirmation_keyboard,
)
from app.core.constants import MONTHLY_PERIOD_SENTINEL
from app.core.reminders import (
    DEFAULT_REMINDER_TIMEZONE,
    calculate_next_charge_date,
    format_due_date,
    normalize_monthly_anchor_day,
    normalize_time_string,
    normalize_timezone_name,
    parse_offsets,
    parse_time_string,
    parse_timezone,
)
from app.ui.text import escape_html
from app.storage.db import Database


class CurrencyConverter:
    def __init__(self, target_currency: str = "RUB") -> None:
        self.target_currency = target_currency.upper()
        self._cache: Dict[tuple[str, str], tuple[float, datetime]] = {}
        self._session: Optional[aiohttp.ClientSession] = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def convert(self, amount: float, source_currency: str) -> float:
        return await self.convert_to(amount, source_currency, self.target_currency)

    async def convert_to(self, amount: float, source_currency: str, target_currency: str) -> float:
        currency = source_currency.upper()
        target = target_currency.upper()
        if currency == target:
            return amount

        rate = await self._get_rate(currency, target)
        return amount * rate

    async def _get_rate(self, source_currency: str, target_currency: str) -> float:
        now = datetime.now(timezone.utc)
        cache_key = (source_currency, target_currency)
        cached = self._cache.get(cache_key)
        if cached and (now - cached[1]) < timedelta(hours=6):
            return cached[0]

        session = await self._ensure_session()
        url = f"https://open.er-api.com/v6/latest/{source_currency}"
        async with session.get(url, timeout=15) as response:
            response.raise_for_status()
            payload = await response.json()
            rates = payload.get("rates") or {}
            result = rates.get(target_currency)
            if result is None:
                raise RuntimeError("Currency API did not return a conversion rate")

        self._cache[cache_key] = (result, now)
        return result

    def set_target_currency(self, target_currency: str) -> None:
        self.target_currency = target_currency.upper()
        self._cache.clear()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session


def calculate_share_base(item: Dict[str, object], participants: list[Dict[str, object]]) -> int:
    share_limit = item.get("share_limit")
    if share_limit:
        return int(share_limit)
    if not participants:
        return 1
    return sum(int(p.get("share_weight") or 1) for p in participants)


def format_converted_amount(amount: float, currency: str, rounding_mode: str) -> str:
    if rounding_mode == "precise":
        return f"{amount:.2f} {currency}"
    value = Decimal(str(amount))
    if rounding_mode == "floor":
        rounded = value.to_integral_value(rounding=ROUND_FLOOR)
    elif rounding_mode == "ceil":
        rounded = value.to_integral_value(rounding=ROUND_CEILING)
    else:
        rounded = value.to_integral_value(rounding=ROUND_HALF_UP)
    return f"{int(rounded)} {currency}"


def _parse_due_date(raw_value: str) -> date:
    return datetime.strptime(raw_value, "%Y-%m-%d").date()


def _format_context_text(due_date: date, today: date) -> str:
    due_display = format_due_date(due_date.isoformat())
    if due_date > today:
        days_left = (due_date - today).days
        return f"Due in {days_left} day(s) ({due_display})."
    if due_date == today:
        return f"Due today ({due_display})."
    days_overdue = (today - due_date).days
    return f"Overdue by {days_overdue} day(s) (due {due_display})."


def _format_status_and_date(due_date: date, today: date) -> tuple[str, str]:
    due_display = format_due_date(due_date.isoformat())
    if due_date > today:
        days_left = (due_date - today).days
        return f"Due in {days_left} day(s)", due_display
    if due_date == today:
        return "Due today", due_display
    days_overdue = (today - due_date).days
    return f"Overdue by {days_overdue} day(s)", due_display


def _build_share_line(
    *,
    share_amount: float,
    weight: int,
    share_base: int,
    safe_currency: str,
    converted_base: Optional[float],
    rounding_mode: str,
    target_currency: str,
) -> tuple[str, str, float, Optional[float], Optional[str]]:
    weight_text = f"{weight}/{share_base}" if weight > 1 else f"1/{share_base}"
    share_amount_value = share_amount * weight
    amount_line = f"{share_amount_value:.2f} {safe_currency}"
    converted_value: Optional[float] = None
    converted_display: Optional[str] = None
    if converted_base is not None:
        converted = converted_base * weight
        converted_display = format_converted_amount(converted, target_currency, rounding_mode)
        converted_value = float(converted)
    return weight_text, amount_line, share_amount_value, converted_value, converted_display


def _build_reminder_message(
    *,
    person_name: str,
    subscription_name: str,
    status_text: str,
    due_date_text: str,
    share_text: str,
    amount_text: str,
    converted_text: Optional[str],
    comment: str,
    footer: str,
) -> str:
    blocks: list[list[str]] = [[f"{escape_html(person_name)},"]]
    blocks += _build_subscription_blocks(
        index=None,
        subscription_name=subscription_name,
        status_text=status_text,
        due_date_text=due_date_text,
        share_text=share_text,
        amount_text=amount_text,
        converted_text=converted_text,
        comment=comment,
    )
    if footer:
        blocks.append([footer])
    return "\n\n".join("\n".join(block) for block in blocks)


def _build_subscription_blocks(
    *,
    index: Optional[int],
    subscription_name: str,
    status_text: str,
    due_date_text: str,
    share_text: str,
    amount_text: str,
    converted_text: Optional[str],
    comment: str,
) -> list[list[str]]:
    if index is None:
        title = "🔔 Subscription Info:"
    else:
        title = f"🔔 Subscription {index} Info:"
    safe_name = escape_html(subscription_name)
    safe_status = escape_html(status_text)
    safe_due_date = escape_html(due_date_text)
    safe_share = escape_html(share_text)
    safe_amount = escape_html(amount_text)
    safe_converted = escape_html(converted_text) if converted_text else None
    safe_comment = escape_html(comment) if comment else ""
    blocks: list[list[str]] = [
        [
            title,
            f"🏷️ Name: <code>{safe_name}</code>",
            f"⏳ Status: <code>{safe_status}</code>",
            f"📅 Date: <code>{safe_due_date}</code>",
        ],
        [
            "💳 Payment:",
            f"👥 Share: <code>{safe_share}</code>",
            f"💰 Amount: <code>{safe_amount}</code>",
        ],
    ]
    if safe_converted:
        blocks[-1].append(f"≈ <code>{safe_converted}</code>")
    if safe_comment:
        blocks.append(["📝 Comment:", f"<code>{safe_comment}</code>"])
    return blocks


def _build_batch_reminder_message(
    *,
    person_name: str,
    items: list[dict[str, object]],
    total_text: Optional[str],
    footer: str,
) -> str:
    blocks: list[list[str]] = [[f"{escape_html(person_name)},"]]
    for idx, item in enumerate(items, 1):
        blocks += _build_subscription_blocks(
            index=idx,
            subscription_name=str(item["subscription_name"]),
            status_text=str(item["status_text"]),
            due_date_text=str(item["due_date_text"]),
            share_text=str(item["share_text"]),
            amount_text=str(item["amount_text"]),
            converted_text=item.get("converted_text"),
            comment=str(item.get("comment") or ""),
        )
    if total_text:
        blocks.append(
            [
                "==============================",
                "💳 Total:",
                f"💰 Amount: <code>{escape_html(total_text)}</code>",
            ]
        )
    if footer:
        blocks.append([footer])
    return "\n\n".join("\n".join(block) for block in blocks)


async def _send_message_with_retry(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    reply_markup: object | None,
    logger: logging.Logger,
    retries: int = 2,
    base_delay: float = 1.0,
) -> Optional[int]:
    attempt = 0
    while True:
        try:
            sent = await bot.send_message(chat_id, text, reply_markup=reply_markup)
            return sent.message_id
        except TelegramForbiddenError as exc:
            logger.warning(
                "Cannot send reminder to user %s: forbidden (%s)",
                chat_id,
                exc,
            )
            return None
        except TelegramBadRequest as exc:
            logger.warning(
                "Cannot send reminder to user %s: bad request (%s)",
                chat_id,
                exc,
            )
            return None
        except (TelegramNetworkError, asyncio.TimeoutError, aiohttp.ClientError) as exc:
            if attempt >= retries:
                logger.exception(
                    "Failed to send reminder to user %s after %s attempts: %s",
                    chat_id,
                    attempt + 1,
                    exc,
                )
                return None
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "Send reminder to user %s failed (%s). Retrying in %.1fs.",
                chat_id,
                exc,
                delay,
            )
            attempt += 1
            await asyncio.sleep(delay)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send reminder to user %s", chat_id)
            return None


async def _ensure_open_cycles(
    db: Database,
    subscription: Dict[str, object],
    today: date,
) -> list[date]:
    due_date = _parse_due_date(str(subscription["next_charge_at"]))
    subscription_id = int(subscription["id"])
    await db.ensure_cycle(subscription_id, due_date)

    period_days = int(subscription.get("period_days") or 30)
    monthly_anchor_day = normalize_monthly_anchor_day(subscription.get("monthly_anchor_day"))
    if period_days == MONTHLY_PERIOD_SENTINEL and monthly_anchor_day is None:
        monthly_anchor_day = due_date.day
        await db.update_subscription_fields(subscription_id, monthly_anchor_day=monthly_anchor_day)
    updated = False
    if today > due_date:
        while due_date < today:
            due_date = calculate_next_charge_date(due_date, period_days, monthly_anchor_day)
            await db.ensure_cycle(subscription_id, due_date)
            updated = True
    if updated:
        await db.update_subscription_fields(subscription_id, next_charge_at=due_date)

    open_cycle_values = await db.list_open_cycles(subscription_id)
    return [_parse_due_date(value) for value in open_cycle_values]


async def _auto_mark_admin_payments(
    db: Database,
    subscription_id: int,
    participant_ids_by_cycle: dict[date, set[int]],
    notify_admin_reminders: bool,
) -> None:
    if notify_admin_reminders:
        return
    admin_ids = set(await db.list_admin_ids())
    if not admin_ids:
        return
    for cycle_due, participant_ids in participant_ids_by_cycle.items():
        target_ids = admin_ids.intersection(participant_ids)
        if not target_ids:
            continue
        for admin_id in target_ids:
            await db.log_payment(subscription_id, cycle_due, admin_id)


async def _run_reminder_pass(
    bot: Bot,
    db: Database,
    converter: CurrencyConverter,
    *,
    subscription_id: Optional[int],
    now: datetime,
    rounding_mode: str,
    enforce_time: bool,
    register_reminders: bool,
    target_telegram_id: Optional[int] = None,
    record_manual_suppressions: bool = False,
) -> int:
    logger = logging.getLogger("reminder_runner")
    if now.tzinfo is None:
        now_utc = now.replace(tzinfo=timezone.utc)
    else:
        now_utc = now.astimezone(timezone.utc)
    admin_timezone_name = normalize_timezone_name(
        await db.get_effective_base_timezone(DEFAULT_REMINDER_TIMEZONE),
        DEFAULT_REMINDER_TIMEZONE,
    ) or DEFAULT_REMINDER_TIMEZONE
    admin_timezone = parse_timezone(admin_timezone_name)
    admin_now = now_utc.astimezone(admin_timezone)
    today = admin_now.date()
    current_time = admin_now.time()
    notify_admin_reminders = await db.get_setting_bool("notify_admin_reminders", True)
    admin_ids: set[int] = set(await db.list_admin_ids()) if notify_admin_reminders else set()
    admin_base_time = parse_time_string(await db.get_effective_base_reminder_time()).strftime("%H:%M")
    sent_count = 0
    pending: dict[tuple[int, str], list[dict[str, object]]] = {}

    subscriptions = await db.fetch_subscriptions_for_reminders()
    for item in subscriptions:
        if subscription_id is not None and item.get("id") != subscription_id:
            continue
        raw_name = str(item.get("name", ""))
        raw_currency = str(item.get("currency", ""))
        raw_comment = str(item.get("comment", "")).strip()
        safe_name = escape_html(raw_name)
        safe_currency = escape_html(raw_currency)
        subscription_override_time = normalize_time_string(item.get("reminder_time"))
        try:
            _parse_due_date(item["next_charge_at"])
        except (KeyError, ValueError):
            continue

        participants = item.get("participants", [])
        offsets = parse_offsets(item.get("reminder_offsets"))
        open_cycles = await _ensure_open_cycles(db, item, today)
        cycle_due_values = [cycle.isoformat() for cycle in open_cycles]
        cycle_participants_state = await db.list_cycle_participants_for_due_dates(
            int(item["id"]),
            cycle_due_values,
        )
        participants_by_cycle: dict[date, list[dict[str, object]]] = {}
        participant_ids_by_cycle: dict[date, set[int]] = {}
        for cycle_due in open_cycles:
            due_key = cycle_due.isoformat()
            cycle_state = cycle_participants_state.get(due_key) or {}
            cycle_participants = list(cycle_state.get("participants") or [])
            snapshot_ready = bool(cycle_state.get("snapshot_ready"))
            if not cycle_participants and not snapshot_ready:
                cycle_participants = participants
            if target_telegram_id is not None:
                cycle_participants = [
                    person
                    for person in cycle_participants
                    if int(person.get("telegram_id") or 0) == target_telegram_id
                ]
            participants_by_cycle[cycle_due] = cycle_participants
            participant_ids_by_cycle[cycle_due] = {
                int(person.get("telegram_id") or 0)
                for person in cycle_participants
                if int(person.get("telegram_id") or 0) > 0
            }
        await _auto_mark_admin_payments(
            db,
            int(item["id"]),
            participant_ids_by_cycle,
            notify_admin_reminders,
        )
        payment_rows = await db.list_payments_for_cycles(item["id"], cycle_due_values)
        paid_map = {
            (row["due_date"], row["paid_by_telegram_id"])
            for row in payment_rows
            if row.get("paid_by_telegram_id") is not None
        }
        has_cycle_participants = any(participants_by_cycle.values())
        if target_telegram_id is not None and not has_cycle_participants:
            continue
        if has_cycle_participants:
            queued_events_for_admin: dict[tuple[date, int], int] = {}
            user_base_time_cache: dict[int, str] = {}
            user_subscription_time_cache: dict[int, Optional[str]] = {}
            user_timezone_cache: dict[int, str] = {}
            user_time_now_cache: dict[int, tuple[date, time]] = {}
            user_suppressed_cache: dict[tuple[str, date], set[int]] = {}
            user_currency_cache: dict[int, str] = {}
            converted_share_cache: dict[tuple[str, int], Optional[float]] = {}
            source_currency = raw_currency.upper()
            for cycle_due in open_cycles:
                cycle_participants = participants_by_cycle.get(cycle_due, [])
                if not cycle_participants:
                    continue
                share_base = calculate_share_base(item, cycle_participants)
                share_amount = item["amount"] / share_base
                due_key = cycle_due.isoformat()
                for person in cycle_participants:
                    telegram_id = int(person.get("telegram_id") or 0)
                    if telegram_id <= 0:
                        continue
                    if (due_key, telegram_id) in paid_map:
                        continue
                    if telegram_id not in user_subscription_time_cache:
                        raw_user_sub_time = await db.get_user_subscription_setting(
                            telegram_id,
                            int(item["id"]),
                            "reminder_time",
                        )
                        user_subscription_time_cache[telegram_id] = normalize_time_string(raw_user_sub_time)

                    effective_time = user_subscription_time_cache[telegram_id]
                    if effective_time is None:
                        if subscription_override_time:
                            effective_time = subscription_override_time
                        else:
                            base_time = user_base_time_cache.get(telegram_id)
                            if base_time is None:
                                base_time = parse_time_string(
                                    await db.get_effective_user_base_reminder_time(
                                        telegram_id,
                                        admin_base_time,
                                    ),
                                    admin_base_time,
                                ).strftime("%H:%M")
                                user_base_time_cache[telegram_id] = base_time
                            effective_time = base_time

                    effective_timezone = user_timezone_cache.get(telegram_id)
                    if effective_timezone is None:
                        raw_user_timezone = await db.get_effective_user_timezone(
                            telegram_id,
                            admin_timezone_name,
                        )
                        effective_timezone = normalize_timezone_name(
                            raw_user_timezone,
                            admin_timezone_name,
                        ) or admin_timezone_name
                        user_timezone_cache[telegram_id] = effective_timezone
                    local_today, local_current_time = user_time_now_cache.get(telegram_id, (today, current_time))
                    if telegram_id not in user_time_now_cache:
                        local_now = now_utc.astimezone(parse_timezone(effective_timezone))
                        local_today, local_current_time = local_now.date(), local_now.time()
                        user_time_now_cache[telegram_id] = (local_today, local_current_time)

                    if enforce_time and local_current_time < parse_time_string(effective_time, admin_base_time):
                        continue

                    event_offsets = {
                        offset
                        for offset in offsets
                        if cycle_due + timedelta(days=offset) == local_today
                    }
                    if item.get("remind_after_due") and local_today > cycle_due:
                        event_offsets.add((local_today - cycle_due).days)
                    if not event_offsets:
                        continue

                    if register_reminders:
                        suppression_key = (due_key, local_today)
                        suppressed_ids = user_suppressed_cache.get(suppression_key)
                        if suppressed_ids is None:
                            suppressed_ids = set(
                                await db.list_reminder_suppressed_users(
                                    int(item["id"]),
                                    cycle_due,
                                    local_today,
                                )
                            )
                            user_suppressed_cache[suppression_key] = suppressed_ids
                        if telegram_id in suppressed_ids:
                            continue

                    target_currency = user_currency_cache.get(telegram_id)
                    if target_currency is None:
                        target_currency = await db.get_effective_target_currency(
                            telegram_id,
                            converter.target_currency,
                        )
                        user_currency_cache[telegram_id] = target_currency

                    converted_cache_key = (target_currency, share_base)
                    converted_base = converted_share_cache.get(converted_cache_key)
                    if converted_cache_key not in converted_share_cache:
                        if source_currency == target_currency:
                            converted_base = None
                        else:
                            try:
                                converted_base = await converter.convert_to(
                                    share_amount,
                                    source_currency,
                                    target_currency,
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "Conversion failed for subscription %s and currency %s: %s",
                                    item["id"],
                                    target_currency,
                                    exc,
                                )
                                converted_base = None
                        converted_share_cache[converted_cache_key] = converted_base

                    weight = int(person.get("share_weight") or 1)
                    share_text, amount_text, share_amount_value, converted_value, converted_display = _build_share_line(
                        share_amount=share_amount,
                        weight=weight,
                        share_base=share_base,
                        safe_currency=raw_currency,
                        converted_base=converted_base,
                        rounding_mode=rounding_mode,
                        target_currency=target_currency,
                    )
                    base_amount_value: Optional[float] = converted_value
                    if base_amount_value is None and source_currency == target_currency:
                        base_amount_value = float(share_amount_value)
                    status_text, due_date_text = _format_status_and_date(cycle_due, local_today)
                    for event_offset in sorted(event_offsets):
                        if register_reminders:
                            if not await db.register_user_reminder_if_new(
                                int(item["id"]),
                                cycle_due,
                                event_offset,
                                telegram_id,
                            ):
                                continue

                        key = (telegram_id, effective_time)
                        pending.setdefault(key, []).append(
                            {
                                "person_name": str(person.get("full_name") or f"User {telegram_id}"),
                                "subscription_id": int(item["id"]),
                                "due_date": cycle_due,
                                "subscription_name": raw_name,
                                "subscription_label": str(item.get("name", "")),
                                "status_text": status_text,
                                "due_date_text": due_date_text,
                                "share_text": share_text,
                                "amount_text": amount_text,
                                "converted_text": converted_display,
                                "comment": raw_comment,
                                "share_amount_value": share_amount_value,
                                "share_currency": str(item["currency"]),
                                "converted_value": converted_value,
                                "base_amount_value": base_amount_value,
                                "target_currency": target_currency,
                                "sent_on": local_today,
                            }
                        )
                        queued_events_for_admin[(cycle_due, event_offset)] = len(cycle_participants)

            if admin_ids and queued_events_for_admin:
                admin_comment = f"\nComment: {escape_html(raw_comment)}" if raw_comment else ""
                for (due_date_value, event_offset), queued_count in sorted(queued_events_for_admin.items()):
                    if register_reminders:
                        if not await db.register_reminder_if_new(
                            int(item["id"]),
                            due_date_value,
                            event_offset,
                        ):
                            continue
                    context_text = _format_context_text(due_date_value, today)
                    admin_note = (
                        f"🔔 {safe_name}\n"
                        f"{context_text}\n"
                        f"Total: {item['amount']:.2f} {safe_currency} | Users: {queued_count}"
                        f"{admin_comment}"
                    )
                    event_participant_ids = participant_ids_by_cycle.get(due_date_value, set())
                    for admin_id in admin_ids:
                        if admin_id in event_participant_ids:
                            continue
                        sent = await _send_message_with_retry(
                            bot,
                            admin_id,
                            admin_note,
                            reply_markup=None,
                            logger=logger,
                        )
                        if sent is not None:
                            sent_count += 1
            continue

        if not admin_ids:
            continue

        dispatch_time = subscription_override_time or admin_base_time
        if enforce_time and current_time < parse_time_string(dispatch_time, admin_base_time):
            continue
        admin_comment = f"\nComment: {escape_html(raw_comment)}" if raw_comment else ""
        for cycle_due in open_cycles:
            event_offsets = {
                offset
                for offset in offsets
                if cycle_due + timedelta(days=offset) == today
            }
            if item.get("remind_after_due") and today > cycle_due:
                event_offsets.add((today - cycle_due).days)
            for event_offset in sorted(event_offsets):
                if register_reminders:
                    if not await db.register_reminder_if_new(int(item["id"]), cycle_due, event_offset):
                        continue
                context_text = _format_context_text(cycle_due, today)
                admin_note = (
                    f"🔔 {safe_name}\n"
                    f"{context_text}\n"
                    "No users are assigned yet. Add them via 📋 Subscriptions."
                    f"{admin_comment}"
                )
                for admin_id in admin_ids:
                    sent = await _send_message_with_retry(
                        bot,
                        admin_id,
                        admin_note,
                        reply_markup=None,
                        logger=logger,
                    )
                    if sent is not None:
                        sent_count += 1

    for (telegram_id, _reminder_time), items in pending.items():
        if not items:
            continue
        person_name = str(items[0].get("person_name") or "")
        if len(items) == 1:
            item = items[0]
            message_text = _build_reminder_message(
                person_name=person_name,
                subscription_name=str(item["subscription_name"]),
                status_text=str(item["status_text"]),
                due_date_text=str(item["due_date_text"]),
                share_text=str(item["share_text"]),
                amount_text=str(item["amount_text"]),
                converted_text=item.get("converted_text"),
                comment=str(item.get("comment") or ""),
                footer="Tap “Paid” when the bill is covered.",
            )
            keyboard = build_payment_confirmation_keyboard(
                int(item["subscription_id"]),
                item["due_date"],
            )
            message_id = await _send_message_with_retry(
                bot,
                telegram_id,
                message_text,
                reply_markup=keyboard,
                logger=logger,
            )
            if message_id is not None:
                sent_count += 1
                await db.record_reminder_message(
                    subscription_id=int(item["subscription_id"]),
                    due_date=item["due_date"],
                    telegram_id=telegram_id,
                    message_id=message_id,
                    share_amount=float(item["share_amount_value"]),
                    share_currency=str(item["share_currency"]),
                    converted_amount=item.get("converted_value"),
                    converted_currency=str(item.get("target_currency")) if item.get("converted_text") else None,
                    converted_display=item.get("converted_text"),
                )
                if record_manual_suppressions:
                    await db.register_reminder_suppression(
                        int(item["subscription_id"]),
                        item["due_date"],
                        telegram_id,
                        item["sent_on"],
                    )
            continue

        total_value = 0.0
        total_ready = True
        for item in items:
            base_value = item.get("base_amount_value")
            if base_value is None:
                total_ready = False
                break
            total_value += float(base_value)
        total_text = None
        if total_ready:
            total_currency = str(items[0].get("target_currency") or converter.target_currency)
            total_text = format_converted_amount(total_value, total_currency, rounding_mode)

        message_text = _build_batch_reminder_message(
            person_name=person_name,
            items=items,
            total_text=total_text,
            footer="Tap “Paid” when the bill is covered.",
        )
        keyboard = build_batch_payment_confirmation_keyboard(items)
        message_id = await _send_message_with_retry(
            bot,
            telegram_id,
            message_text,
            reply_markup=keyboard,
            logger=logger,
        )
        if message_id is not None:
            sent_count += 1
            if record_manual_suppressions:
                for item in items:
                    await db.register_reminder_suppression(
                        int(item["subscription_id"]),
                        item["due_date"],
                        telegram_id,
                        item["sent_on"],
                    )

    return sent_count


async def send_reminders_now(
    bot: Bot,
    db: Database,
    converter: CurrencyConverter,
    subscription_id: Optional[int] = None,
    rounding_mode: str = "precise",
    target_telegram_id: Optional[int] = None,
    suppress_auto_today: bool = True,
) -> int:
    now = datetime.now(timezone.utc)
    return await _run_reminder_pass(
        bot,
        db,
        converter,
        subscription_id=subscription_id,
        now=now,
        rounding_mode=rounding_mode,
        enforce_time=False,
        register_reminders=False,
        target_telegram_id=target_telegram_id,
        record_manual_suppressions=suppress_auto_today,
    )


async def send_test_reminders(
    bot: Bot,
    db: Database,
    converter: CurrencyConverter,
    subscription_id: int,
    rounding_mode: str = "precise",
    target_telegram_id: Optional[int] = None,
) -> int:
    logger = logging.getLogger("test_reminder_sender")
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return 0

    participants = await db.list_subscription_participants(subscription_id)
    target_participants = participants
    if target_telegram_id is not None:
        target_participants = [
            person for person in participants
            if person.get("telegram_id") == target_telegram_id
        ]
    if not target_participants:
        return 0

    try:
        due_date = _parse_due_date(str(subscription["next_charge_at"]))
    except (KeyError, ValueError):
        return 0

    raw_name = str(subscription.get("name", ""))
    raw_currency = str(subscription.get("currency", ""))
    raw_comment = str(subscription.get("comment", "")).strip()
    source_currency = raw_currency.upper()
    admin_timezone_name = normalize_timezone_name(
        await db.get_effective_base_timezone(DEFAULT_REMINDER_TIMEZONE),
        DEFAULT_REMINDER_TIMEZONE,
    ) or DEFAULT_REMINDER_TIMEZONE
    today = datetime.now(parse_timezone(admin_timezone_name)).date()
    share_base = calculate_share_base(subscription, participants)
    share_amount = subscription["amount"] / share_base

    sent_count = 0
    target_currency_cache: dict[int, str] = {}
    converted_share_cache: dict[str, Optional[float]] = {}
    for person in target_participants:
        telegram_id = int(person["telegram_id"])
        target_currency = target_currency_cache.get(telegram_id)
        if target_currency is None:
            target_currency = await db.get_effective_target_currency(telegram_id, converter.target_currency)
            target_currency_cache[telegram_id] = target_currency

        converted_base = converted_share_cache.get(target_currency)
        if target_currency not in converted_share_cache:
            if source_currency == target_currency:
                converted_base = None
            else:
                try:
                    converted_base = await converter.convert_to(share_amount, source_currency, target_currency)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Conversion failed for test reminder %s and currency %s: %s",
                        subscription_id,
                        target_currency,
                        exc,
                    )
                    converted_base = None
            converted_share_cache[target_currency] = converted_base

        weight = int(person.get("share_weight") or 1)
        share_text, amount_text, _, _, converted_display = _build_share_line(
            share_amount=share_amount,
            weight=weight,
            share_base=share_base,
            safe_currency=raw_currency,
            converted_base=converted_base,
            rounding_mode=rounding_mode,
            target_currency=target_currency,
        )
        status_text, due_date_text = _format_status_and_date(due_date, today)
        message_text = _build_reminder_message(
            person_name=str(person["full_name"]),
            subscription_name=raw_name,
            status_text=status_text,
            due_date_text=due_date_text,
            share_text=share_text,
            amount_text=amount_text,
            converted_text=converted_display,
            comment=raw_comment,
            footer="This is a test reminder. Tapping “Paid” will not record anything.",
        )
        keyboard = build_test_payment_confirmation_keyboard(subscription_id, due_date)
        message_id = await _send_message_with_retry(
            bot,
            telegram_id,
            message_text,
            reply_markup=keyboard,
            logger=logger,
        )
        if message_id is not None:
            sent_count += 1

    return sent_count


async def reminder_worker(
    bot: Bot,
    db: Database,
    converter: CurrencyConverter,
    interval_seconds: int = 3600,
    rounding_mode: str = "precise",
) -> None:
    logger = logging.getLogger("reminder_worker")
    poll_interval = max(5.0, min(float(interval_seconds), 60.0))
    try:
        while True:
            started = asyncio.get_running_loop().time()
            await _run_reminder_pass(
                bot,
                db,
                converter,
                subscription_id=None,
                now=datetime.now(timezone.utc),
                rounding_mode=rounding_mode,
                enforce_time=True,
                register_reminders=True,
            )
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(1.0, poll_interval - elapsed))
    except asyncio.CancelledError:  # pragma: no cover - service shutdown
        logger.info("Reminder worker stopped")
        raise
