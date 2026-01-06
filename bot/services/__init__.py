from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Dict, Optional

import aiohttp
from aiogram import Bot

from bot.keyboards import build_payment_confirmation_keyboard
from bot.core.reminders import (
    REMINDER_TIMEZONE,
    calculate_next_charge_date,
    format_due_date,
    parse_offsets,
    parse_time_string,
)
from bot.text import escape_html
from bot.storage.db import Database


class CurrencyConverter:
    def __init__(self, target_currency: str = "RUB") -> None:
        self.target_currency = target_currency.upper()
        self._cache: Dict[str, tuple[float, datetime]] = {}
        self._session: Optional[aiohttp.ClientSession] = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def convert(self, amount: float, source_currency: str) -> float:
        currency = source_currency.upper()
        if currency == self.target_currency:
            return amount

        rate = await self._get_rate(currency)
        return amount * rate

    async def _get_rate(self, source_currency: str) -> float:
        now = datetime.now(timezone.utc)
        cached = self._cache.get(source_currency)
        if cached and (now - cached[1]) < timedelta(hours=6):
            return cached[0]

        session = await self._ensure_session()
        url = f"https://open.er-api.com/v6/latest/{source_currency}"
        async with session.get(url, timeout=15) as response:
            response.raise_for_status()
            payload = await response.json()
            rates = payload.get("rates") or {}
            result = rates.get(self.target_currency)
            if result is None:
                raise RuntimeError("Currency API did not return a conversion rate")

        self._cache[source_currency] = (result, now)
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


async def _ensure_open_cycles(
    db: Database,
    subscription: Dict[str, object],
    today: date,
) -> list[date]:
    due_date = _parse_due_date(str(subscription["next_charge_at"]))
    subscription_id = int(subscription["id"])
    await db.ensure_cycle(subscription_id, due_date)

    period_days = int(subscription.get("period_days") or 30)
    updated = False
    if today > due_date:
        while due_date < today:
            due_date = calculate_next_charge_date(due_date, period_days)
            await db.ensure_cycle(subscription_id, due_date)
            updated = True
    if updated:
        await db.update_subscription_fields(subscription_id, next_charge_at=due_date)

    open_cycle_values = await db.list_open_cycles(subscription_id)
    return [_parse_due_date(value) for value in open_cycle_values]


async def _auto_mark_admin_payments(
    db: Database,
    subscription_id: int,
    open_cycles: list[date],
    participant_ids: set[int],
    notify_admin_reminders: bool,
) -> None:
    if notify_admin_reminders:
        return
    admin_ids = set(await db.list_admin_ids())
    target_ids = admin_ids.intersection(participant_ids)
    if not target_ids:
        return
    for cycle_due in open_cycles:
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
) -> int:
    logger = logging.getLogger("reminder_runner")
    today = now.date()
    current_time = now.time()
    notify_admin_reminders = await db.get_setting_bool("notify_admin_reminders", True)
    admin_ids: set[int] = set(await db.list_admin_ids()) if notify_admin_reminders else set()
    sent_count = 0

    subscriptions = await db.fetch_subscriptions_for_reminders()
    for item in subscriptions:
        if subscription_id is not None and item.get("id") != subscription_id:
            continue
        safe_name = escape_html(item.get("name", ""))
        safe_currency = escape_html(item.get("currency", ""))
        try:
            _parse_due_date(item["next_charge_at"])
        except (KeyError, ValueError):
            continue

        reminder_time = parse_time_string(item.get("reminder_time"))
        if enforce_time and current_time < reminder_time:
            continue

        participants = item.get("participants", [])
        participant_ids = {p["telegram_id"] for p in participants}
        offsets = parse_offsets(item.get("reminder_offsets"))
        open_cycles = await _ensure_open_cycles(db, item, today)
        await _auto_mark_admin_payments(db, int(item["id"]), open_cycles, participant_ids, notify_admin_reminders)
        due_dates = [cycle.isoformat() for cycle in open_cycles]
        payment_rows = await db.list_payments_for_cycles(item["id"], due_dates)
        paid_map = {
            (row["due_date"], row["paid_by_telegram_id"])
            for row in payment_rows
            if row.get("paid_by_telegram_id") is not None
        }

        async def dispatch(context_text: str, due_date_value: date) -> None:
            nonlocal sent_count
            admin_note: Optional[str] = None
            if participants:
                due_key = due_date_value.isoformat()
                unpaid = [
                    person for person in participants
                    if (due_key, person["telegram_id"]) not in paid_map
                ]
                if not unpaid:
                    return
                share_base = calculate_share_base(item, participants)
                share_amount = item["amount"] / share_base
                try:
                    share_amount_rub = await converter.convert(share_amount, item["currency"])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Conversion failed for subscription %s: %s", item["id"], exc)
                    share_amount_rub = None

                keyboard = build_payment_confirmation_keyboard(item["id"], due_date_value)
                for person in unpaid:
                    weight = int(person.get("share_weight") or 1)
                    weight_text = f"{weight}/{share_base}" if weight > 1 else f"1/{share_base}"
                    share_line = (
                        f"Your share ({weight_text}): {share_amount * weight:.2f} {safe_currency}"
                    )
                    if share_amount_rub is not None:
                        converted = share_amount_rub * weight
                        share_line += (
                            f" (≈ {format_converted_amount(converted, converter.target_currency, rounding_mode)})"
                        )
                    message_text = (
                        f"{escape_html(person['full_name'])},\n"
                        f"🔔 {context_text}\n"
                        f"💳 {share_line}\n"
                        "Tap “Paid” when the bill is covered."
                    )
                    try:
                        await bot.send_message(
                            person["telegram_id"],
                            message_text,
                            reply_markup=keyboard,
                        )
                        sent_count += 1
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to send reminder to user %s",
                            person["telegram_id"],
                        )

                if admin_ids:
                    admin_note = (
                        f"{context_text}\n"
                        f"Total: {item['amount']:.2f} {safe_currency} | Members: {len(participants)}"
                    )
            elif admin_ids:
                admin_note = (
                    f"{context_text}\n"
                    "No members are assigned yet. Add them via 📋 Subscriptions."
                )

            if admin_note and admin_ids:
                for admin_id in admin_ids:
                    if admin_id in participant_ids:
                        continue
                    with contextlib.suppress(Exception):
                        await bot.send_message(admin_id, admin_note)
                        sent_count += 1

        for cycle_due in open_cycles:
            due_display = format_due_date(cycle_due.isoformat())
            for offset in offsets:
                target_date = cycle_due + timedelta(days=offset)
                if target_date != today:
                    continue
                if register_reminders:
                    if not await db.register_reminder_if_new(item["id"], cycle_due, offset):
                        continue

                if target_date < cycle_due:
                    days_left = (cycle_due - today).days
                    context = (
                        f"'{safe_name}' is due in {days_left} day(s) "
                        f"({due_display})."
                    )
                elif target_date == cycle_due:
                    context = f"'{safe_name}' is due today ({due_display})."
                else:
                    days_overdue = (today - cycle_due).days
                    context = (
                        f"'{safe_name}' was due {days_overdue} day(s) ago "
                        f"({due_display})."
                    )
                await dispatch(context, cycle_due)

            if item.get("remind_after_due") and today > cycle_due:
                overdue_offset = (today - cycle_due).days
                if register_reminders:
                    if not await db.register_reminder_if_new(item["id"], cycle_due, overdue_offset):
                        continue
                context = (
                    f"'{safe_name}' is overdue by {overdue_offset} day(s) "
                    f"(was due {due_display})."
                )
                await dispatch(context, cycle_due)

    return sent_count


async def send_reminders_now(
    bot: Bot,
    db: Database,
    converter: CurrencyConverter,
    subscription_id: Optional[int] = None,
    rounding_mode: str = "precise",
) -> int:
    now = datetime.now(REMINDER_TIMEZONE)
    return await _run_reminder_pass(
        bot,
        db,
        converter,
        subscription_id=subscription_id,
        now=now,
        rounding_mode=rounding_mode,
        enforce_time=False,
        register_reminders=False,
    )


async def reminder_worker(
    bot: Bot,
    db: Database,
    converter: CurrencyConverter,
    interval_seconds: int = 3600,
    rounding_mode: str = "precise",
) -> None:
    logger = logging.getLogger("reminder_worker")
    try:
        while True:
            await _run_reminder_pass(
                bot,
                db,
                converter,
                subscription_id=None,
                now=datetime.now(REMINDER_TIMEZONE),
                rounding_mode=rounding_mode,
                enforce_time=True,
                register_reminders=True,
            )

            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:  # pragma: no cover - service shutdown
        logger.info("Reminder worker stopped")
        raise
