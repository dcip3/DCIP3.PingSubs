from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Dict, Optional

import aiohttp
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

from app.ui.keyboards import (
    build_batch_payment_confirmation_keyboard,
    build_payment_confirmation_keyboard,
    extract_copyable_requisite,
)
from app.core.constants import (
    MONTHLY_PERIOD_SENTINEL,
    PAYMENT_MODE_FIXED,
    PAYMENT_MODE_SPLIT,
)
from app.core.reminders import (
    DEFAULT_REMINDER_TIMEZONE,
    calculate_next_charge_date,
    due_status_html,
    format_due_date,
    normalize_monthly_anchor_day,
    normalize_time_string,
    normalize_timezone_name,
    parse_offsets,
    parse_time_string,
    parse_timezone,
    tg_due,
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


def normalize_payment_mode(raw_value: object) -> str:
    value = str(raw_value or PAYMENT_MODE_SPLIT).strip().lower()
    if value == PAYMENT_MODE_FIXED:
        return PAYMENT_MODE_FIXED
    return PAYMENT_MODE_SPLIT


def parse_fixed_amount(raw_value: object) -> Optional[float]:
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


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


def _format_context_text(due_date: date, today: date, tz_name: Optional[str] = None) -> str:
    """HTML status line for admin notes: live relative status plus the due date."""
    due_value = due_date.isoformat()
    return f"{due_status_html(due_value, tz_name, today)} ({tg_due(due_value, tz_name)})."


def _format_status_and_date(due_date: date, today: date) -> tuple[str, str]:
    due_display = format_due_date(due_date.isoformat())
    if due_date > today:
        days_left = (due_date - today).days
        return f"Due in {days_left} day(s)", due_display
    if due_date == today:
        return "Due today", due_display
    days_overdue = (today - due_date).days
    return f"Overdue by {days_overdue} day(s)", due_display


def _build_reminder_message(
    *,
    person_name: str,
    subscription_name: str,
    status_text: str,
    due_date_text: str,
    amount_text: str,
    converted_text: Optional[str],
    payment_label: str,
    payment_details: str,
    payment_link: str,
    comment: str,
    footer: str,
    due_value: Optional[str] = None,
    tz_name: Optional[str] = None,
    today: Optional[date] = None,
    copyable: bool = False,
) -> str:
    blocks: list[list[str]] = [[f"{escape_html(person_name)},"]]
    blocks += _build_subscription_blocks(
        index=None,
        subscription_name=subscription_name,
        status_text=status_text,
        due_date_text=due_date_text,
        amount_text=amount_text,
        converted_text=converted_text,
        payment_label=payment_label,
        payment_details=payment_details,
        payment_link=payment_link,
        comment=comment,
        due_value=due_value,
        tz_name=tz_name,
        today=today,
        copyable=copyable,
    )
    if footer:
        blocks.append([footer])
    return "\n\n".join("\n".join(block) for block in blocks)


def _render_payment_details_lines(payment_details: str, *, copyable: bool = False) -> list[str]:
    """Single place that renders the payment details block of a reminder.

    ``copyable`` says that the keyboard of this very message carries a copy
    button for these details (card number or IBAN); then the details are
    folded into an expandable quote and the button is the primary way to grab
    them. Otherwise the monospace block stays, so the text is still easy to
    select and copy by hand. The caller decides, because only it knows which
    keyboard (if any) goes with the text.
    """
    if not payment_details:
        return []
    safe_details = escape_html(payment_details)
    if copyable:
        return [f"<blockquote expandable>{safe_details}</blockquote>"]
    return [f"<pre>{safe_details}</pre>"]


def _build_subscription_blocks(
    *,
    index: Optional[int],
    subscription_name: str,
    status_text: str,
    due_date_text: str,
    amount_text: str,
    converted_text: Optional[str],
    payment_label: str,
    payment_details: str,
    payment_link: str,
    comment: str,
    due_value: Optional[str] = None,
    tz_name: Optional[str] = None,
    today: Optional[date] = None,
    details_shown_above: bool = False,
    copyable: bool = False,
) -> list[list[str]]:
    """Render one subscription of a reminder as blank-line separated blocks.

    ``status_text``/``due_date_text`` are the plain fallbacks (also used for
    button labels). When ``due_value`` (ISO ``YYYY-MM-DD``) is given, the status
    and date lines become live Telegram date-time entities localized to the
    reader, with ``today`` taken in ``tz_name`` unless passed explicitly.
    ``details_shown_above`` skips the payment details because an earlier item
    of the same message already rendered the identical text. ``copyable``
    marks details that the message keyboard offers via a copy button.
    """
    safe_name = escape_html(subscription_name)
    if index is None:
        title = f"<b>🔔 {safe_name}</b>"
    else:
        title = f"<b>{index}. {safe_name}</b>"
    if due_value:
        if today is None:
            today = datetime.now(parse_timezone(tz_name)).date()
        status_html = due_status_html(due_value, tz_name, today)
        date_html = tg_due(due_value, tz_name, "wD")
    else:
        status_html = escape_html(status_text)
        date_html = escape_html(due_date_text)
    safe_amount = escape_html(amount_text)
    safe_converted = escape_html(converted_text) if converted_text else None
    safe_payment_label = escape_html(payment_label) if payment_label else "not set"
    safe_payment_link = escape_html(payment_link) if payment_link else ""
    safe_comment = escape_html(comment) if comment else ""
    amount_line = f"💵 My amount: <b>{safe_amount}</b>"
    if safe_converted:
        amount_line += f" ≈ <b>{safe_converted}</b>"
    method_line = f"🏦 Method: {safe_payment_label}"
    if details_shown_above and payment_details:
        method_line += " <i>(details above)</i>"
    blocks: list[list[str]] = [
        [
            title,
            f"⏳ Status: <b>{status_html}</b>",
            f"📅 Date: <b>{date_html}</b>",
        ],
        [
            "💳 Payment:",
            amount_line,
            method_line,
        ],
    ]
    if not details_shown_above:
        blocks[-1] += _render_payment_details_lines(payment_details, copyable=copyable)
    if safe_payment_link:
        blocks[-1].append(f"🔗 Link: {safe_payment_link}")
    if safe_comment:
        blocks.append([f"<blockquote>📝 {safe_comment}</blockquote>"])
    return blocks


def _build_batch_reminder_message(
    *,
    person_name: str,
    items: list[dict[str, object]],
    total_text: Optional[str],
    footer: str,
    tz_name: Optional[str] = None,
    today: Optional[date] = None,
) -> str:
    blocks: list[list[str]] = [[f"{escape_html(person_name)},"]]
    rendered_details: set[str] = set()
    for idx, item in enumerate(items, 1):
        item_due_value = item.get("due_value")
        item_today = item.get("today")
        item_details = str(item.get("payment_details") or "").strip()
        details_shown_above = bool(item_details) and item_details in rendered_details
        if item_details:
            rendered_details.add(item_details)
        blocks += _build_subscription_blocks(
            index=idx,
            subscription_name=str(item["subscription_name"]),
            status_text=str(item["status_text"]),
            due_date_text=str(item["due_date_text"]),
            amount_text=str(item["amount_text"]),
            converted_text=item.get("converted_text"),
            payment_label=str(item.get("payment_label") or ""),
            payment_details=str(item.get("payment_details") or ""),
            payment_link=str(item.get("payment_link") or ""),
            comment=str(item.get("comment") or ""),
            due_value=str(item_due_value) if item_due_value else None,
            tz_name=str(item.get("tz_name") or tz_name or "") or None,
            today=item_today if isinstance(item_today, date) else today,
            details_shown_above=details_shown_above,
            copyable=bool(item.get("copyable")),
        )
    if total_text:
        blocks.append([f"💰 <b>Total: {escape_html(total_text)}</b>"])
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
    flood_waits = 0
    while True:
        try:
            sent = await bot.send_message(chat_id, text, reply_markup=reply_markup)
            return sent.message_id
        except TelegramRetryAfter as exc:
            if flood_waits >= 3:
                logger.warning("Giving up on reminder to user %s after repeated flood waits", chat_id)
                return None
            flood_waits += 1
            delay = float(exc.retry_after) + 1.0
            logger.warning("Telegram asked to wait %.0fs before sending to user %s", delay, chat_id)
            await asyncio.sleep(delay)
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


async def _close_fully_paid_cycles(
    db: Database,
    subscription: Dict[str, object],
    open_cycles: list[date],
    participants_by_cycle: dict[date, list[dict[str, object]]],
) -> None:
    if not open_cycles:
        return

    subscription_id = int(subscription["id"])
    due_values = [due.isoformat() for due in open_cycles]
    payment_rows = await db.list_payments_for_cycles(subscription_id, due_values)
    paid_by_due: dict[str, set[int]] = {}
    for row in payment_rows:
        payer_id = row.get("paid_by_telegram_id")
        due_value = str(row.get("due_date") or "")
        if payer_id is None or not due_value:
            continue
        paid_by_due.setdefault(due_value, set()).add(int(payer_id))

    next_charge_value = str(subscription.get("next_charge_at") or "")
    period_days = int(subscription.get("period_days") or 30)
    monthly_anchor_day = normalize_monthly_anchor_day(subscription.get("monthly_anchor_day"))

    for cycle_due in sorted(open_cycles):
        participant_ids = {
            int(person.get("telegram_id") or 0)
            for person in participants_by_cycle.get(cycle_due, [])
            if int(person.get("telegram_id") or 0) > 0
        }
        if not participant_ids:
            continue
        due_value = cycle_due.isoformat()
        paid_ids = paid_by_due.get(due_value, set())
        if not participant_ids.issubset(paid_ids):
            continue

        await db.close_cycle(subscription_id, cycle_due)
        if next_charge_value == due_value:
            next_due = calculate_next_charge_date(cycle_due, period_days, monthly_anchor_day)
            await db.update_subscription_fields(subscription_id, next_charge_at=next_due)
            next_charge_value = next_due.isoformat()
            subscription["next_charge_at"] = next_charge_value


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
    auto_paid_notifications: list[dict[str, object]] = []

    subscriptions = await db.fetch_subscriptions_for_reminders()
    for item in subscriptions:
        if subscription_id is not None and item.get("id") != subscription_id:
            continue
        raw_name = str(item.get("name", ""))
        raw_currency = str(item.get("currency", ""))
        raw_base_currency = str(item.get("base_currency") or converter.target_currency).upper()
        raw_payment_mode = normalize_payment_mode(item.get("payment_mode"))
        raw_comment = str(item.get("comment", "")).strip()
        current_payment_destination = await db.get_effective_subscription_payment_destination(int(item["id"]))
        payment_destination_raw_id = item.get("payment_destination_id")
        try:
            current_payment_destination_id = (
                int(payment_destination_raw_id) if payment_destination_raw_id is not None else None
            )
        except (TypeError, ValueError):
            current_payment_destination_id = None
        current_payment_label = "not set"
        current_payment_details = ""
        current_payment_link = ""
        if current_payment_destination:
            label_base = f"{current_payment_destination['title']} · {current_payment_destination['currency']}"
            current_payment_label = (
                label_base if current_payment_destination_id is not None else f"Default ({label_base})"
            )
            current_payment_details = str(current_payment_destination.get("details") or "").strip()
            current_payment_link = str(current_payment_destination.get("payment_link") or "").strip()
        safe_name = escape_html(raw_name)
        default_subscription_override_time = normalize_time_string(item.get("reminder_time"))
        default_offsets = parse_offsets(item.get("reminder_offsets"))
        default_remind_after_due = bool(item.get("remind_after_due"))
        try:
            _parse_due_date(item["next_charge_at"])
        except (KeyError, ValueError):
            continue

        participants = item.get("participants", [])
        open_cycles = await _ensure_open_cycles(db, item, today)
        cycle_due_values = [cycle.isoformat() for cycle in open_cycles]
        cycle_participants_state = await db.list_cycle_participants_for_due_dates(
            int(item["id"]),
            cycle_due_values,
        )
        participants_by_cycle: dict[date, list[dict[str, object]]] = {}
        participant_ids_by_cycle: dict[date, set[int]] = {}
        cycle_meta_by_due: dict[date, dict[str, object]] = {}
        for cycle_due in open_cycles:
            due_key = cycle_due.isoformat()
            cycle_state = cycle_participants_state.get(due_key) or {}
            cycle_participants = list(cycle_state.get("participants") or [])
            snapshot_ready = bool(cycle_state.get("snapshot_ready"))
            settings_snapshot_ready = bool(cycle_state.get("settings_snapshot_ready"))
            if not cycle_participants and not snapshot_ready:
                cycle_participants = participants

            amount_source = cycle_state.get("amount") if settings_snapshot_ready else item.get("amount")
            try:
                cycle_amount = float(amount_source)
            except (TypeError, ValueError):
                cycle_amount = float(item["amount"])
            currency_source = cycle_state.get("currency") if settings_snapshot_ready else raw_currency
            cycle_currency = str(currency_source or raw_currency).upper()
            base_currency_source = cycle_state.get("base_currency") if settings_snapshot_ready else raw_base_currency
            cycle_base_currency = str(base_currency_source or raw_base_currency).upper()
            payment_mode_source = cycle_state.get("payment_mode") if settings_snapshot_ready else raw_payment_mode
            cycle_payment_mode = normalize_payment_mode(payment_mode_source)
            share_limit_source = cycle_state.get("share_limit") if settings_snapshot_ready else item.get("share_limit")
            try:
                cycle_share_limit = int(share_limit_source) if share_limit_source is not None else None
            except (TypeError, ValueError):
                cycle_share_limit = None
            if cycle_share_limit is not None and cycle_share_limit <= 0:
                cycle_share_limit = None
            cycle_comment = str(
                cycle_state.get("comment") if settings_snapshot_ready else raw_comment
            ).strip()
            cycle_offsets_raw = cycle_state.get("reminder_offsets") if settings_snapshot_ready else item.get("reminder_offsets")
            cycle_offsets = parse_offsets(cycle_offsets_raw)
            remind_after_due_source = cycle_state.get("remind_after_due") if settings_snapshot_ready else item.get("remind_after_due")
            cycle_remind_after_due = bool(remind_after_due_source)
            cycle_reminder_time_raw = cycle_state.get("reminder_time") if settings_snapshot_ready else item.get("reminder_time")
            cycle_override_time = normalize_time_string(cycle_reminder_time_raw)
            cycle_payment_label = (
                str(cycle_state.get("payment_destination_title") or "").strip()
                if settings_snapshot_ready
                else current_payment_label
            )
            cycle_payment_details = (
                str(cycle_state.get("payment_destination_details") or "").strip()
                if settings_snapshot_ready
                else current_payment_details
            )
            cycle_payment_link = (
                str(cycle_state.get("payment_destination_link") or "").strip()
                if settings_snapshot_ready
                else current_payment_link
            )
            cycle_meta_by_due[cycle_due] = {
                "settings_snapshot_ready": settings_snapshot_ready,
                "amount": cycle_amount,
                "currency": cycle_currency,
                "base_currency": cycle_base_currency,
                "payment_label": cycle_payment_label,
                "payment_details": cycle_payment_details,
                "payment_link": cycle_payment_link,
                "payment_mode": cycle_payment_mode,
                "share_limit": cycle_share_limit,
                "comment": cycle_comment,
                "offsets": cycle_offsets,
                "remind_after_due": cycle_remind_after_due,
                "reminder_time": cycle_override_time,
            }
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
            queued_events_for_admin: dict[tuple[date, int], dict[str, object]] = {}
            frozen_due_dates: set[date] = set()
            user_base_time_cache: dict[int, str] = {}
            user_subscription_time_cache: dict[int, Optional[str]] = {}
            user_timezone_cache: dict[int, str] = {}
            user_time_now_cache: dict[int, tuple[date, time]] = {}
            user_suppressed_cache: dict[tuple[str, date], set[int]] = {}
            user_currency_cache: dict[tuple[int, str], str] = {}
            converted_share_cache: dict[tuple[str, str, float], Optional[float]] = {}
            user_balance_cache: dict[int, float] = {}
            user_balance_currency_cache: dict[int, str] = {}
            balance_conversion_cache: dict[tuple[str, str, float], Optional[float]] = {}
            for cycle_due in open_cycles:
                cycle_participants = participants_by_cycle.get(cycle_due, [])
                if not cycle_participants:
                    continue
                cycle_meta = cycle_meta_by_due.get(cycle_due, {})
                cycle_currency = str(cycle_meta.get("currency") or raw_currency).upper()
                cycle_base_currency = str(cycle_meta.get("base_currency") or raw_base_currency).upper()
                cycle_payment_mode = normalize_payment_mode(cycle_meta.get("payment_mode"))
                cycle_payment_label = str(cycle_meta.get("payment_label") or "")
                cycle_payment_details = str(cycle_meta.get("payment_details") or "")
                cycle_payment_link = str(cycle_meta.get("payment_link") or "")
                cycle_comment = str(cycle_meta.get("comment") or "")
                cycle_amount = float(cycle_meta.get("amount") or item["amount"])
                cycle_share_limit = cycle_meta.get("share_limit")
                cycle_offsets = list(cycle_meta.get("offsets") or default_offsets)
                cycle_remind_after_due = bool(cycle_meta.get("remind_after_due"))
                cycle_override_time = normalize_time_string(cycle_meta.get("reminder_time"))
                share_base = calculate_share_base({"share_limit": cycle_share_limit}, cycle_participants)
                share_amount = cycle_amount / share_base
                source_currency = cycle_currency.upper()
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
                        if cycle_override_time:
                            effective_time = cycle_override_time
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
                        for offset in cycle_offsets
                        if cycle_due + timedelta(days=offset) == local_today
                    }
                    if cycle_remind_after_due and local_today > cycle_due:
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

                    ready_offsets: list[int] = []
                    for event_offset in sorted(event_offsets):
                        if register_reminders:
                            if cycle_due not in frozen_due_dates:
                                await db.freeze_cycle_snapshot(int(item["id"]), cycle_due)
                                frozen_due_dates.add(cycle_due)
                            if not await db.register_user_reminder_if_new(
                                int(item["id"]),
                                cycle_due,
                                event_offset,
                                telegram_id,
                            ):
                                continue
                        ready_offsets.append(event_offset)
                    if not ready_offsets:
                        continue

                    currency_cache_key = (telegram_id, cycle_base_currency)
                    target_currency = user_currency_cache.get(currency_cache_key)
                    if target_currency is None:
                        target_currency = await db.get_effective_target_currency(
                            telegram_id,
                            int(item["id"]),
                            subscription_default=cycle_base_currency,
                            default_currency=converter.target_currency,
                        )
                        user_currency_cache[currency_cache_key] = target_currency

                    weight = int(person.get("share_weight") or 1)
                    fixed_amount = parse_fixed_amount(person.get("fixed_amount"))
                    if cycle_payment_mode == PAYMENT_MODE_FIXED:
                        base_amount = fixed_amount if fixed_amount is not None else cycle_amount
                        person_amount_value = base_amount * weight
                    else:
                        person_amount_value = share_amount * weight
                    remaining_amount_value = float(person_amount_value)
                    balance_currency = user_balance_currency_cache.get(telegram_id)
                    current_balance = user_balance_cache.get(telegram_id)
                    if current_balance is None or balance_currency is None:
                        current_balance, balance_currency = await db.get_friend_balance_snapshot_by_telegram(
                            telegram_id,
                            converter.target_currency,
                        )
                        user_balance_cache[telegram_id] = current_balance
                        user_balance_currency_cache[telegram_id] = balance_currency
                    balance_after = current_balance

                    required_balance_amount: Optional[float]
                    if source_currency == balance_currency:
                        required_balance_amount = remaining_amount_value
                    else:
                        required_key = (
                            source_currency,
                            balance_currency,
                            round(remaining_amount_value, 8),
                        )
                        required_balance_amount = balance_conversion_cache.get(required_key)
                        if required_key not in balance_conversion_cache:
                            try:
                                required_balance_amount = await converter.convert_to(
                                    remaining_amount_value,
                                    source_currency,
                                    balance_currency,
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "Balance conversion failed for subscription %s to %s: %s",
                                    item["id"],
                                    balance_currency,
                                    exc,
                                )
                                required_balance_amount = None
                            balance_conversion_cache[required_key] = required_balance_amount

                    spent_balance_amount = 0.0
                    spent_source_amount = 0.0
                    if (
                        current_balance > 0
                        and required_balance_amount is not None
                        and required_balance_amount > 0
                    ):
                        spent_balance_amount, balance_after = await db.consume_friend_balance_by_telegram(
                            telegram_id,
                            required_balance_amount,
                        )
                        if spent_balance_amount > 0:
                            user_balance_cache[telegram_id] = max(balance_after, 0.0)
                            if source_currency == balance_currency:
                                spent_source_amount = spent_balance_amount
                            else:
                                spent_key = (
                                    balance_currency,
                                    source_currency,
                                    round(spent_balance_amount, 8),
                                )
                                spent_source_amount = balance_conversion_cache.get(spent_key) or 0.0
                                if spent_key not in balance_conversion_cache:
                                    try:
                                        spent_source_amount = await converter.convert_to(
                                            spent_balance_amount,
                                            balance_currency,
                                            source_currency,
                                        )
                                    except Exception as exc:  # noqa: BLE001
                                        logger.warning(
                                            "Balance reverse conversion failed for subscription %s to %s: %s",
                                            item["id"],
                                            source_currency,
                                            exc,
                                        )
                                        spent_source_amount = 0.0
                                    balance_conversion_cache[spent_key] = spent_source_amount
                            if spent_source_amount <= 0 and required_balance_amount > 0:
                                spent_ratio = min(spent_balance_amount / required_balance_amount, 1.0)
                                spent_source_amount = remaining_amount_value * spent_ratio
                            remaining_amount_value = max(remaining_amount_value - spent_source_amount, 0.0)
                        else:
                            user_balance_cache[telegram_id] = current_balance
                    else:
                        user_balance_cache[telegram_id] = current_balance

                    is_fully_paid_from_balance = remaining_amount_value <= 0.005
                    if is_fully_paid_from_balance:
                        remaining_amount_value = 0.0

                    converted_value: Optional[float] = None
                    converted_display: Optional[str] = None
                    base_amount_value: Optional[float] = None
                    amount_text = f"{remaining_amount_value:.2f} {cycle_currency}"
                    if not is_fully_paid_from_balance:
                        converted_cache_key = (
                            source_currency,
                            target_currency,
                            round(remaining_amount_value, 8),
                        )
                        converted_base = converted_share_cache.get(converted_cache_key)
                        if converted_cache_key not in converted_share_cache:
                            if source_currency == target_currency:
                                converted_base = None
                            else:
                                try:
                                    converted_base = await converter.convert_to(
                                        remaining_amount_value,
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

                        if converted_base is not None:
                            converted_value = float(converted_base)
                            converted_display = format_converted_amount(
                                converted_base,
                                target_currency,
                                rounding_mode,
                            )
                        base_amount_value = converted_value
                        if base_amount_value is None and source_currency == target_currency:
                            base_amount_value = float(remaining_amount_value)

                    effective_comment = cycle_comment
                    if spent_source_amount > 0 and remaining_amount_value > 0:
                        balance_note = (
                            f"Balance used: {spent_source_amount:.2f} {cycle_currency}. "
                            f"Top up: {remaining_amount_value:.2f} {cycle_currency}."
                        )
                        effective_comment = (
                            f"{balance_note}\n{cycle_comment}" if cycle_comment else balance_note
                        )
                    status_text, due_date_text = _format_status_and_date(cycle_due, local_today)
                    for event_offset in ready_offsets:
                        if is_fully_paid_from_balance:
                            await db.log_payment(int(item["id"]), cycle_due, telegram_id)
                            paid_map.add((due_key, telegram_id))
                            auto_paid_notifications.append(
                                {
                                    "telegram_id": telegram_id,
                                    "person_name": str(person.get("full_name") or f"User {telegram_id}"),
                                    "subscription_name": raw_name,
                                    "status_text": status_text,
                                    "due_date_text": due_date_text,
                                    "due_value": cycle_due.isoformat(),
                                    "tz_name": effective_timezone,
                                    "today": local_today,
                                    "amount_text": f"{person_amount_value:.2f} {cycle_currency}",
                                    "payment_label": cycle_payment_label,
                                    "payment_details": cycle_payment_details,
                                    "payment_link": cycle_payment_link,
                                    "comment": cycle_comment,
                                    "footer": (
                                        "Paid automatically from your balance.\n"
                                        f"Used: {spent_balance_amount:.2f} {balance_currency}\n"
                                        f"Balance left: {balance_after:.2f} {balance_currency}"
                                    ),
                                }
                            )
                            if record_manual_suppressions:
                                await db.register_reminder_suppression(
                                    int(item["id"]),
                                    cycle_due,
                                    telegram_id,
                                    local_today,
                                )
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
                                "due_value": cycle_due.isoformat(),
                                "tz_name": effective_timezone,
                                "today": local_today,
                                "amount_text": amount_text,
                                "converted_text": converted_display,
                                "payment_label": cycle_payment_label,
                                "payment_details": cycle_payment_details,
                                "payment_link": cycle_payment_link,
                                "comment": effective_comment,
                                "share_amount_value": remaining_amount_value,
                                "share_currency": cycle_currency,
                                "converted_value": converted_value,
                                "base_amount_value": base_amount_value,
                                "target_currency": target_currency,
                                "sent_on": local_today,
                            }
                        )
                        queued_events_for_admin[(cycle_due, event_offset)] = {
                            "count": len(cycle_participants),
                            "amount": cycle_amount,
                            "currency": cycle_currency,
                            "payment_label": cycle_payment_label,
                            "comment": cycle_comment,
                        }

            if admin_ids and queued_events_for_admin:
                for (due_date_value, event_offset), details in sorted(queued_events_for_admin.items()):
                    if register_reminders:
                        if not await db.register_reminder_if_new(
                            int(item["id"]),
                            due_date_value,
                            event_offset,
                        ):
                            continue
                    queued_count = int(details.get("count") or 0)
                    safe_currency = escape_html(str(details.get("currency") or raw_currency))
                    total_amount = float(details.get("amount") or item["amount"])
                    admin_comment_value = str(details.get("comment") or "").strip()
                    admin_comment = f"\nComment: {escape_html(admin_comment_value)}" if admin_comment_value else ""
                    context_text = _format_context_text(due_date_value, today, admin_timezone_name)
                    admin_note = (
                        f"🔔 {safe_name}\n"
                        f"{context_text}\n"
                        f"Total: {total_amount:.2f} {safe_currency} | Users: {queued_count}"
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
            if target_telegram_id is None:
                await _close_fully_paid_cycles(
                    db,
                    item,
                    open_cycles,
                    participants_by_cycle,
                )
            continue

        if not admin_ids:
            continue

        for cycle_due in open_cycles:
            cycle_meta = cycle_meta_by_due.get(cycle_due, {})
            settings_snapshot_ready = bool(cycle_meta.get("settings_snapshot_ready"))
            cycle_offsets = list(cycle_meta.get("offsets") or default_offsets)
            cycle_remind_after_due = bool(cycle_meta.get("remind_after_due", default_remind_after_due))
            cycle_currency = str(cycle_meta.get("currency") or raw_currency).upper()
            cycle_amount = float(cycle_meta.get("amount") or item["amount"])
            cycle_comment = str(cycle_meta.get("comment") or raw_comment).strip()
            cycle_override_time = normalize_time_string(cycle_meta.get("reminder_time"))
            if settings_snapshot_ready:
                dispatch_time = cycle_override_time or admin_base_time
            else:
                dispatch_time = cycle_override_time or default_subscription_override_time or admin_base_time
            if enforce_time and current_time < parse_time_string(dispatch_time, admin_base_time):
                continue

            event_offsets = {
                offset
                for offset in cycle_offsets
                if cycle_due + timedelta(days=offset) == today
            }
            if cycle_remind_after_due and today > cycle_due:
                event_offsets.add((today - cycle_due).days)
            for event_offset in sorted(event_offsets):
                if register_reminders:
                    await db.freeze_cycle_snapshot(int(item["id"]), cycle_due)
                    if not await db.register_reminder_if_new(int(item["id"]), cycle_due, event_offset):
                        continue
                safe_currency = escape_html(cycle_currency)
                admin_comment = f"\nComment: {escape_html(cycle_comment)}" if cycle_comment else ""
                context_text = _format_context_text(cycle_due, today, admin_timezone_name)
                admin_note = (
                    f"🔔 {safe_name}\n"
                    f"{context_text}\n"
                    f"Total: {cycle_amount:.2f} {safe_currency}\n"
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

    for notice in auto_paid_notifications:
        telegram_id = int(notice["telegram_id"])
        message_text = _build_reminder_message(
            person_name=str(notice["person_name"]),
            subscription_name=str(notice["subscription_name"]),
            status_text=str(notice["status_text"]),
            due_date_text=str(notice["due_date_text"]),
            amount_text=str(notice["amount_text"]),
            converted_text=None,
            payment_label=str(notice.get("payment_label") or ""),
            payment_details=str(notice.get("payment_details") or ""),
            payment_link=str(notice.get("payment_link") or ""),
            comment=str(notice.get("comment") or ""),
            footer=str(notice["footer"]),
            due_value=str(notice["due_value"]),
            tz_name=str(notice["tz_name"]),
            today=notice["today"],
            # No keyboard goes with an auto-paid notice, so nothing to copy.
            copyable=False,
        )
        sent = await _send_message_with_retry(
            bot,
            telegram_id,
            message_text,
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
            item_details = str(item.get("payment_details") or "")
            message_text = _build_reminder_message(
                person_name=person_name,
                subscription_name=str(item["subscription_name"]),
                status_text=str(item["status_text"]),
                due_date_text=str(item["due_date_text"]),
                amount_text=str(item["amount_text"]),
                converted_text=item.get("converted_text"),
                payment_label=str(item.get("payment_label") or ""),
                payment_details=item_details,
                payment_link=str(item.get("payment_link") or ""),
                comment=str(item.get("comment") or ""),
                footer="Tap “Paid” when the bill is covered.",
                due_value=str(item["due_value"]),
                tz_name=str(item["tz_name"]),
                today=item["today"],
                # The keyboard below carries the copy row for exactly these details.
                copyable=extract_copyable_requisite(item_details) is not None,
            )
            keyboard = build_payment_confirmation_keyboard(
                int(item["subscription_id"]),
                item["due_date"],
                payment_details=item_details,
                payment_link=str(item.get("payment_link") or ""),
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

        # One copy/link row for the whole batch: the first item whose details
        # yield a copyable requisite provides it (identical details are the
        # common case); only the items matching that requisite get the
        # collapsed quote, the rest keep the monospace block.
        batch_details = next(
            (
                details
                for details in (str(item.get("payment_details") or "") for item in items)
                if extract_copyable_requisite(details) is not None
            ),
            "",
        )
        batch_requisite = extract_copyable_requisite(batch_details)
        for item in items:
            item["copyable"] = (
                batch_requisite is not None
                and extract_copyable_requisite(str(item.get("payment_details") or "")) == batch_requisite
            )
        batch_link = next((str(item.get("payment_link") or "") for item in items if item.get("payment_link")), "")
        message_text = _build_batch_reminder_message(
            person_name=person_name,
            items=items,
            total_text=total_text,
            footer="Tap “Paid” when the bill is covered.",
        )
        keyboard = build_batch_payment_confirmation_keyboard(
            items,
            payment_details=batch_details,
            payment_link=batch_link,
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


async def reminder_worker(
    bot: Bot,
    db: Database,
    converter: CurrencyConverter,
    interval_seconds: int = 60,
    rounding_mode: str = "precise",
) -> None:
    logger = logging.getLogger("reminder_worker")
    poll_interval = max(5.0, min(float(interval_seconds), 60.0))
    try:
        while True:
            started = asyncio.get_running_loop().time()
            try:
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
            except Exception:  # noqa: BLE001 - keep the worker alive
                logger.exception("Reminder pass failed; retrying after the next interval")
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(1.0, poll_interval - elapsed))
    except asyncio.CancelledError:  # pragma: no cover - service shutdown
        logger.info("Reminder worker stopped")
        raise
