"""Member subscriptions (WP9): the list, the card and the card's sub-screens.

📋 Subscriptions (member) is a top-level screen: no nav row. The card is the
member's hub for one subscription and returns to the list with [◀️ To list];
opened as a NEW message from a report or push it ends with [✖️ Close] only.
Its sub-screens (💱 Shown in, ⏰ Reminder, 📊 Reports) edit the card's message
and come back to it, keeping whichever nav the card had.

Texts are pure builders over small snapshots (``MemberCard``, list rows) so
tests can pin them without a bot; the ``*_snapshot`` coroutines read the data
with the same unpaid rule as the reports and /start: an open cycle due today
or earlier that this member is in and has not paid.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Optional, Sequence, Tuple

from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

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
    tg_clock,
    tg_due,
)
from app.storage.db import Database
from app.ui.helpers import (
    CARD_COMMENT_FOLD_LEN,
    CARD_FOLD_BUDGET,
    NO_ACCESS,
    SUBSCRIPTION_GONE,
    _build_subscription_payment_report_text,
    _card_participants_block,
    _due_phrase_html,
    _repeats_text,
    answer_callback_once,
    respond_with_markup,
    send_chunked_responder_text,
)
from app.ui.keyboards import (
    CLOSE_CALLBACK,
    MENU_SETTINGS,
    PUBLIC_CARD_CLOSE_ACTION,
    after_due_line,
    build_public_subscription_list_keyboard,
    extract_copyable_requisite,
    is_openable_payment_link,
    payment_method_label,
    public_subscription_currency_keyboard,
    public_subscription_detail_keyboard,
    public_subscription_reminder_time_keyboard,
    public_subscription_report_keyboard,
)
from app.ui.shell import _cycle_participant_ids, _paid_by_due, _parse_due, member_share
from app.ui.states import Responder
from app.ui.style import SEP, expandable, join_blocks, sub_line, title, with_banner
from app.ui.text import days_left_phrase, escape_html, format_money, plural_word

logger = logging.getLogger(__name__)

Screen = Tuple[str, InlineKeyboardMarkup]

LIST_TITLE = title("📋", "Subscriptions")
LIST_HINT = "Tap one to see your share and how to pay."
LIST_EMPTY_TEXT = f"{LIST_TITLE}\n\nYou are not in any subscription yet. The admin adds you."
OVERDUE_TAIL = "overdue"
DUE_TODAY_TAIL = "due today"

# The card lists this many unpaid cycles, then '…and N more cycles'.
UNPAID_LINES_MAX = 5
NO_METHOD_LINE = "Not set yet. Ask the admin how to pay."
CURRENCY_HINT = "Your reminders show your share converted to this currency."
# A remote rate is best-effort: a slow or failed lookup drops the '≈' part.
CONVERSION_TIMEOUT = 3.0

CURRENCY_TYPO_HINT = "⚠️ Need three letters, for example <code>CHF</code>."
CURRENCY_NOT_TEXT_HINT = "⚠️ Send text, for example <code>CHF</code>."
ALREADY_PAID_TOAST = "Already paid"
# A card button that no longer says what it would do just redraws the card.
CARD_UPDATED_TOAST = "Card updated"
# Old typed keywords that still reset a value (the prompt shows [↺ Use default]).
LEGACY_DEFAULT_WORDS = frozenset({"base", "admin"})

# ``origin_screen`` of the card prompts: which card variant to redraw.
CARD_ORIGIN = "member_card"
CARD_NEW_ORIGIN = "member_card_new"


# ===================================================================== texts


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def list_text(total: int, unpaid: int) -> str:
    """``'📋 <b>Subscriptions</b> · 2'``, ``'⚠️ Unpaid: <b>1</b>'`` (only when non-zero), the hint."""
    if total <= 0:
        return LIST_EMPTY_TEXT
    return join_blocks(
        title("📋", "Subscriptions", total),
        f"⚠️ Unpaid: <b>{unpaid}</b>" if unpaid else None,
        LIST_HINT,
    )


@dataclass(frozen=True)
class MemberCard:
    """Everything the member card shows, already resolved for one viewer."""

    subscription_id: int
    name: str
    tz_name: str
    today: date
    currency: str
    repeats: str
    # Open cycles due today or earlier that the viewer has not paid, oldest first.
    unpaid_dues: Tuple[str, ...] = ()
    # The viewer's share per cycle (None: the viewer is not a participant).
    share: Optional[float] = None
    # ``(amount, currency)`` of the share in the viewer's "Shown in" currency, when known.
    converted: Optional[Tuple[float, str]] = None
    # The oldest unpaid cycle when the balance already covered part of the share
    # (the reminder took it): what is left to pay by card, and what the balance paid.
    left_to_pay: Optional[float] = None
    from_balance: Optional[float] = None
    next_charge: Optional[str] = None
    next_paid: bool = False
    reminder_time: str = ""
    reminder_default: bool = True
    shown_in: str = ""
    shown_in_default: bool = True
    # ``'Sber · RUB'`` (plain text), or None without a payment method.
    method: Optional[str] = None
    details: str = ""
    link: str = ""
    comment: str = ""
    # The admin viewer also sees the subscription amount, the participants and the reminder days.
    admin_view: bool = False
    admin_amount: Optional[float] = None
    admin_participants: Tuple[str, ...] = ()
    admin_reminder_days: str = ""

    @property
    def oldest_unpaid(self) -> Optional[str]:
        return self.unpaid_dues[0] if self.unpaid_dues else None


def _unpaid_phrase(due_value: str, today: date) -> str:
    due = _parse_due(due_value)
    if due is None:
        return ""
    days = (due - today).days
    return DUE_TODAY_TAIL if days == 0 else days_left_phrase(days)


def _unpaid_block(card: MemberCard) -> Optional[list[str]]:
    if not card.unpaid_dues:
        return None
    lines = ["⚠️ <b>Unpaid</b>"]
    for due_value in card.unpaid_dues[:UNPAID_LINES_MAX]:
        phrase = _unpaid_phrase(due_value, card.today)
        date_html = f"<b>{tg_due(due_value, card.tz_name, 'wD')}</b>"
        lines.append(sub_line(f"{date_html}{SEP}{phrase}" if phrase else date_html))
    rest = len(card.unpaid_dues) - UNPAID_LINES_MAX
    if rest > 0:
        lines.append(sub_line(f"…and {rest} more {plural_word(rest, 'cycle')}"))
    return lines


def _share_block(card: MemberCard) -> list[str]:
    lines: list[str] = []
    if card.share is not None:
        amount = f"Amount: {format_money(card.share, card.currency, bold=True)}"
        if card.converted is not None:
            amount += f" ({format_money(card.converted[0], card.converted[1], approx=True)})"
        lines.append(amount)
        if card.left_to_pay is not None and card.from_balance:
            # The same split as the reminder push: the full share above, the rest here.
            lines.append(
                f"Left to pay: {format_money(card.left_to_pay, card.currency, bold=True)}"
                f" ({format_money(card.from_balance, card.currency)} from balance)"
            )
    if card.next_charge:
        next_line = f"Next charge: {_due_phrase_html(card.next_charge, card.tz_name, card.today, bold=True)}"
        if card.next_paid:
            next_line += f"{SEP}✅ paid"
        lines.append(next_line)
    lines.append(f"Repeats: {card.repeats}")
    header = "💵 <b>Your share</b>" if card.share is not None else "📅 <b>Schedule</b>"
    return [header, *(sub_line(line) for line in lines)]


def _reminder_block(card: MemberCard) -> list[str]:
    time_html = f"<b>{tg_clock(card.reminder_time, card.tz_name, card.reminder_time)}</b> your time"
    if card.reminder_default:
        time_html += f"{SEP}default"
    shown_in = escape_html(card.shown_in) + (" (default)" if card.shown_in_default else "")
    lines = ["⏰ <b>Reminder</b>", sub_line(time_html), sub_line(f"Shown in: {shown_in}")]
    if card.admin_view and card.admin_reminder_days:
        lines.append(sub_line(card.admin_reminder_days))
    return lines


def _payment_block(card: MemberCard) -> list[str]:
    """Method, requisites (folded when a copy button carries them), the link when it is no button, the comment."""
    lines = ["💳 <b>How to pay</b>"]
    lines.append(sub_line(escape_html(card.method) if card.method else NO_METHOD_LINE))
    detail_lines = [line.strip() for line in card.details.splitlines() if line.strip()]
    if detail_lines:
        coded = [f"<code>{escape_html(line)}</code>" for line in detail_lines]
        if extract_copyable_requisite(card.details) is not None:
            lines.append(expandable(coded, CARD_FOLD_BUDGET, "line"))
        else:
            lines.extend(sub_line(line) for line in coded)
    link = card.link.strip()
    if link and not is_openable_payment_link(link):
        lines.append(sub_line(f"🔗 {escape_html(link)}"))
    comment = _clean(card.comment)
    if comment and comment != " ".join(card.details.split()):
        if len(comment) > CARD_COMMENT_FOLD_LEN:
            lines.append(sub_line("📝 Comment"))
            lines.append(expandable([escape_html(comment)], CARD_FOLD_BUDGET, "line"))
        else:
            lines.append(sub_line(f"📝 {escape_html(comment)}"))
    return lines


def member_card_text(card: MemberCard, *, banner: Optional[str] = None) -> str:
    """'🏷️ <b>Netflix</b>', then Unpaid (first, when any), Your share, Reminder and How to pay.

    The admin viewer also gets the amount on the title line, the
    Participants block and the reminder days.
    """
    facts = []
    if card.admin_view and card.admin_amount is not None:
        facts.append(format_money(card.admin_amount, card.currency, bold=True))
    header = title("🏷️", escape_html(_clean(card.name)), *facts)
    text = join_blocks(
        header,
        _unpaid_block(card),
        _share_block(card),
        list(card.admin_participants) if card.admin_view else None,
        _reminder_block(card),
        _payment_block(card),
    )
    return with_banner(banner, text)


def member_card_markup(card: MemberCard, *, close: bool = False) -> InlineKeyboardMarkup:
    return public_subscription_detail_keyboard(
        card.subscription_id,
        paid_due=card.oldest_unpaid,
        payment_details=card.details,
        payment_link=card.link,
        close=close,
    )


def currency_text(name: str, current: str, default: str, *, has_override: bool) -> str:
    """'🌐 <b>Shown in · Netflix</b>' with the current (and default) currency and what it is for."""
    head = title("🌐", f"Shown in · {escape_html(_clean(name))}")
    if has_override:
        now = f"Now: {escape_html(current)}\nDefault: {escape_html(default)}"
    else:
        now = f"Now: {escape_html(default)} (default)"
    return f"{head}\n\n{now}\n{CURRENCY_HINT}"


def reminder_text(name: str, current: str, default: str, tz_name: str, *, has_override: bool) -> str:
    """'⏰ <b>Reminder time · Netflix</b>' with the time on the reader's clock and where the timezone lives."""
    head = title("⏰", f"Reminder time · {escape_html(_clean(name))}")
    now = f"Now: <b>{tg_clock(current, tz_name, current)}</b> your time"
    if has_override:
        now += f"\nDefault: {escape_html(default)}"
    else:
        now += " (default)"
    zone = f"Your timezone: {escape_html(tz_name)} — change it in “{MENU_SETTINGS}”."
    return f"{head}\n\n{now}\n{zone}"


def currency_prompt(name: str) -> str:
    return f"✏️ Currency for {escape_html(_clean(name))} reminders? Three letters, for example <code>CHF</code>."


def reminder_prompt(name: str, tz_name: str) -> str:
    return (
        f"✏️ Reminder time for {escape_html(_clean(name))}? "
        f"<code>HH:MM</code> in your timezone ({escape_html(tz_name)})."
    )


def shown_in_toast(code: Optional[str]) -> str:
    """'Shown in: USD', or 'Shown in: default' (``None``)."""
    return f"Shown in: {code or 'default'}"


def shown_in_banner(code: str, *, default: bool = False) -> str:
    """'✅ Shown in · <b>CHF</b>' (' (default)' after the value when it is the default)."""
    return f"✅ Shown in{SEP}<b>{escape_html(code)}</b>" + (" (default)" if default else "")


def paid_banner(name: str, due_value: str, tz_name: str) -> str:
    """'✅ Paid · Netflix <b><tg-time></b>': the cycle the member just paid."""
    return f"✅ Paid{SEP}{escape_html(_clean(name))} <b>{tg_due(due_value, tz_name, 'wD')}</b>"


def report_text(name: str, body: str) -> str:
    """'📊 <b>Reports · Netflix</b>' above the report block of one subscription."""
    return f"{title('📊', f'Reports · {escape_html(_clean(name))}')}\n\n{body}"


def is_reset_word(text: Optional[str]) -> bool:
    """'default', '-', 'clear', … and the old 'base' / 'admin' keywords."""
    from app.ui.text import is_default_word

    return is_default_word(text) or (text or "").strip().lower() in LEGACY_DEFAULT_WORDS


def opened_as_new(message: object) -> bool:
    """True for the card (or one of its sub-screens) that was opened as a new message with [✖️ Close].

    Old cards and pickers carried [⬅️ Back][✖️ Close]; their Back (to the
    list or the card) says they belong to the list, not to a push.
    """
    markup = getattr(message, "reply_markup", None)
    if not isinstance(markup, InlineKeyboardMarkup):
        return False
    callbacks = [button.callback_data or "" for row in markup.inline_keyboard for button in row]
    if any(data.startswith(f"sub:{PUBLIC_CARD_CLOSE_ACTION}:") for data in callbacks):
        return True
    list_backs = ("sub:public_back:", "sub:open_public:")
    return CLOSE_CALLBACK in callbacks and not any(data.startswith(list_backs) for data in callbacks)


# =================================================================== reading


async def _base_timezone(db: Database) -> str:
    return normalize_timezone_name(
        await db.get_effective_base_timezone(DEFAULT_REMINDER_TIMEZONE),
        DEFAULT_REMINDER_TIMEZONE,
    ) or DEFAULT_REMINDER_TIMEZONE


async def viewer_timezone(db: Database, telegram_id: Optional[int]) -> str:
    base = await _base_timezone(db)
    if telegram_id is None:
        return base
    return normalize_timezone_name(await db.get_effective_user_timezone(telegram_id, base), base) or base


async def is_participant(db: Database, subscription_id: int, telegram_id: Optional[int]) -> bool:
    if telegram_id is None:
        return False
    return any(int(sub["id"]) == int(subscription_id) for sub in await db.list_subscriptions_for_user(telegram_id))


async def member_access(
    db: Database,
    subscription_id: int,
    telegram_id: Optional[int],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(subscription, None)`` when the viewer may open it (a participant, or an admin).

    Otherwise ``(None, toast)``: 'Subscription was deleted' or 'You are no
    longer in this subscription'.
    """
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None, SUBSCRIPTION_GONE
    if await is_participant(db, subscription_id, telegram_id):
        return subscription, None
    if telegram_id is not None and await db.is_admin(telegram_id):
        return subscription, None
    return None, NO_ACCESS


async def unpaid_dues(
    db: Database,
    subscription: Dict[str, Any],
    participants: Sequence[Dict[str, Any]],
    telegram_id: int,
    today: date,
) -> list[str]:
    """Open cycles due today or earlier that ``telegram_id`` is in and has not paid, oldest first."""
    subscription_id = int(subscription["id"])
    open_cycles = [due for due in await db.list_open_cycles(subscription_id) if (_parse_due(due) or today) <= today]
    open_cycles = [due for due in open_cycles if _parse_due(due) is not None]
    if not open_cycles:
        return []
    paid = await _paid_by_due(db, subscription_id, open_cycles)
    states = await db.list_cycle_participants_for_due_dates(subscription_id, open_cycles)
    return sorted(
        due
        for due in open_cycles
        if telegram_id in _cycle_participant_ids(states.get(due) or {}, participants)
        and telegram_id not in paid.get(due, set())
    )


def _upcoming_charge(subscription: Dict[str, Any], today: date) -> Optional[date]:
    """The subscription's next charge, moved past dates the worker has not caught up with yet."""
    due = _parse_due(subscription.get("next_charge_at"))
    if due is None:
        return None
    period = int(subscription.get("period_days") or 30)
    anchor = normalize_monthly_anchor_day(subscription.get("monthly_anchor_day"))
    for _ in range(400):
        if due >= today:
            break
        due = calculate_next_charge_date(due, period, anchor)
    return due


async def list_rows(db: Database, telegram_id: int) -> list[Dict[str, Any]]:
    """The member's subscriptions as list rows, unpaid first (oldest bill first), then by name."""
    tz_name = await viewer_timezone(db, telegram_id)
    today = datetime.now(parse_timezone(tz_name)).date()
    rows: list[Dict[str, Any]] = []
    for row in await db.list_subscriptions_for_user(telegram_id):
        subscription = await db.get_subscription(int(row["id"])) or row
        participants = await db.list_subscription_participants(int(row["id"]))
        dues = await unpaid_dues(db, subscription, participants, telegram_id, today)
        upcoming = _upcoming_charge(subscription, today)
        attention = ""
        if dues:
            attention = DUE_TODAY_TAIL if _parse_due(dues[0]) == today else OVERDUE_TAIL
        rows.append(
            {
                "id": int(row["id"]),
                "name": str(subscription.get("name") or ""),
                "attention": attention,
                "oldest_unpaid": dues[0] if dues else "",
                "due_value": upcoming.isoformat() if upcoming else "",
            }
        )
    rows.sort(
        key=lambda item: (
            0 if item["attention"] else 1,
            item["oldest_unpaid"] or "9999",
            item["name"].lower(),
        )
    )
    return rows


async def _converted_share(
    converter: Any,
    amount: Optional[float],
    source: str,
    target: str,
) -> Optional[Tuple[float, str]]:
    """The share in the viewer's "Shown in" currency, or None (same currency, no rate, slow rate)."""
    if converter is None or amount is None or not source or not target or source == target:
        return None
    try:
        value = await asyncio.wait_for(converter.convert_to(amount, source, target), CONVERSION_TIMEOUT)
    except Exception as error:  # noqa: BLE001 - a missing rate only drops the '≈' part
        logger.debug("No %s→%s rate for the member card: %s", source, target, error)
        return None
    try:
        return float(value), target
    except (TypeError, ValueError):
        return None


async def reminder_times(
    db: Database,
    subscription: Dict[str, Any],
    telegram_id: Optional[int],
) -> Tuple[str, str, bool]:
    """``(effective, default, has_override)`` of the viewer's reminder time for one subscription.

    The default is the subscription's own time, else the member's reminder
    time (else the admin's base time): the order the reminder worker uses.
    """
    admin_base = parse_time_string(await db.get_effective_base_reminder_time()).strftime("%H:%M")
    member_base = admin_base
    override = None
    if telegram_id is not None:
        member_base = parse_time_string(
            await db.get_effective_user_base_reminder_time(telegram_id, admin_base),
            admin_base,
        ).strftime("%H:%M")
        override = normalize_time_string(
            await db.get_user_subscription_setting(telegram_id, int(subscription["id"]), "reminder_time")
        )
    default = normalize_time_string(subscription.get("reminder_time")) or member_base
    return override or default, default, bool(override)


def default_shown_in(subscription: Dict[str, Any]) -> str:
    return str(subscription.get("base_currency") or subscription.get("currency") or "RUB").strip().upper()


async def shown_in(
    db: Database,
    subscription: Dict[str, Any],
    telegram_id: Optional[int],
) -> Tuple[str, str, bool]:
    """``(effective, default, has_override)`` of the currency the viewer's reminders are shown in."""
    default = default_shown_in(subscription)
    override = ""
    if telegram_id is not None:
        raw = await db.get_user_subscription_setting(telegram_id, int(subscription["id"]), "target_currency")
        override = str(raw or "").strip().upper()
        if len(override) != 3 or not override.isalpha():
            override = ""
    return override or default, default, bool(override)


async def _balance_cover(
    db: Database,
    subscription_id: int,
    dues: Sequence[str],
    telegram_id: Optional[int],
    share: Optional[float],
    currency: str,
) -> Tuple[Optional[float], Optional[float]]:
    """``(left to pay, paid from balance)`` for the oldest unpaid cycle, or ``(None, None)``.

    The reminder spends the balance when it is sent and stores the share left
    to pay; the card shows the same split instead of asking for the full share.
    """
    if not dues or telegram_id is None or share is None:
        return None, None
    stored = await db.get_reminder_share_left(subscription_id, dues[0], telegram_id)
    if not stored or str(stored.get("share_currency") or "").strip().upper() != currency:
        return None, None
    try:
        left = float(stored["share_amount"])
    except (TypeError, ValueError):
        return None, None
    if left < 0 or left >= share - 0.005:
        return None, None
    return round(left, 2), round(share - left, 2)


async def member_card_snapshot(
    db: Database,
    subscription_id: int,
    telegram_id: Optional[int],
    *,
    converter: Any = None,
) -> Optional[MemberCard]:
    """The card for ``telegram_id`` (a member, or an admin viewing as a member); None when deleted."""
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    participants = await db.list_subscription_participants(subscription_id)
    tz_name = await viewer_timezone(db, telegram_id)
    today = datetime.now(parse_timezone(tz_name)).date()
    currency = str(subscription.get("currency") or "").strip().upper()
    admin_view = telegram_id is not None and await db.is_admin(telegram_id)

    share = member_share(subscription, participants, telegram_id) if telegram_id is not None else None
    dues: list[str] = []
    next_paid = False
    upcoming = _upcoming_charge(subscription, today)
    if telegram_id is not None and share is not None:
        dues = await unpaid_dues(db, subscription, participants, telegram_id, today)
        if upcoming is not None:
            paid = await _paid_by_due(db, subscription_id, [upcoming.isoformat()])
            next_paid = telegram_id in paid.get(upcoming.isoformat(), set())

    left_to_pay, from_balance = await _balance_cover(db, subscription_id, dues, telegram_id, share, currency)
    effective_time, _, time_override = await reminder_times(db, subscription, telegram_id)
    effective_currency, _, currency_override = await shown_in(db, subscription, telegram_id)
    converted = await _converted_share(converter, share, currency, effective_currency)

    destination = await db.get_effective_subscription_payment_destination(subscription_id)
    method = payment_method_label(destination.get("title"), destination.get("currency")) if destination else None

    admin_participants: Tuple[str, ...] = ()
    admin_days = ""
    if admin_view:
        admin_participants = tuple(_card_participants_block(subscription, participants))
        offsets = format_offsets_for_display(parse_offsets(subscription.get("reminder_offsets")))
        admin_days = f"{escape_html(offsets)}{SEP}{after_due_line(bool(subscription.get('remind_after_due')))}"

    amount: Optional[float]
    try:
        amount = float(subscription.get("amount"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        amount = None
    return MemberCard(
        subscription_id=subscription_id,
        name=str(subscription.get("name") or ""),
        tz_name=tz_name,
        today=today,
        currency=currency,
        repeats=_repeats_text(subscription),
        unpaid_dues=tuple(dues),
        share=share,
        converted=converted,
        left_to_pay=left_to_pay,
        from_balance=from_balance,
        next_charge=upcoming.isoformat() if upcoming else None,
        next_paid=next_paid,
        reminder_time=effective_time,
        reminder_default=not time_override,
        shown_in=effective_currency,
        shown_in_default=not currency_override,
        method=method or None,
        details=str(destination.get("details") or "").strip() if destination else "",
        link=str(destination.get("payment_link") or "").strip() if destination else "",
        comment=str(subscription.get("comment") or "").strip(),
        admin_view=admin_view,
        admin_amount=amount,
        admin_participants=admin_participants,
        admin_reminder_days=admin_days,
    )


async def member_card_screen(
    db: Database,
    subscription_id: int,
    telegram_id: Optional[int],
    *,
    banner: Optional[str] = None,
    close: bool = False,
    converter: Any = None,
) -> Optional[Screen]:
    """``(text, markup)`` of the member card, or None when the subscription is gone."""
    card = await member_card_snapshot(db, subscription_id, telegram_id, converter=converter)
    if card is None:
        return None
    return member_card_text(card, banner=banner), member_card_markup(card, close=close)


# ================================================================== screens


def _viewer_of(target: Responder) -> Optional[int]:
    user = target.from_user
    return user.id if user is not None else None


async def send_member_list(
    target: Responder,
    db: Database,
    telegram_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    """The member's 📋 Subscriptions: a typed tap sends it, a callback edits the tapped message."""
    rows = await list_rows(db, telegram_id)
    text = list_text(len(rows), sum(1 for row in rows if row["attention"]))
    markup = build_public_subscription_list_keyboard(rows) if rows else None
    return await respond_with_markup(target, with_banner(banner, text), reply_markup=markup, toast=toast)


async def send_member_card(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
    close: bool = False,
    converter: Any = None,
) -> Optional[Message]:
    """The card for the target's sender: edit on a tap, a new message for a typed message.

    A deleted subscription: a tap gets the toast 'Subscription was deleted'
    and the message stays; a typed message gets the member list.
    """
    viewer = _viewer_of(target)
    screen = await member_card_screen(db, subscription_id, viewer, banner=banner, close=close, converter=converter)
    if screen is None:
        if isinstance(target, CallbackQuery):
            await answer_callback_once(target, SUBSCRIPTION_GONE)
            return None
        if viewer is None:
            return await target.answer(SUBSCRIPTION_GONE)
        return await send_member_list(target, db, viewer, banner=f"⚠️ {SUBSCRIPTION_GONE}")
    text, markup = screen
    return await respond_with_markup(target, text, reply_markup=markup, toast=toast)


async def send_member_report(
    target: Responder,
    db: Database,
    subscription_id: int,
    telegram_id: int,
    *,
    close: bool = False,
) -> None:
    """'📊 <b>Reports · Netflix</b>' for the viewer, with [◀️ To card].

    A participant sees their own payments; an admin who is not in the
    subscription sees everyone's (the admin bypass, as on the card).
    """
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        if isinstance(target, CallbackQuery):
            await answer_callback_once(target, SUBSCRIPTION_GONE)
        return
    participants = await db.list_subscription_participants(subscription_id)
    in_it = any(int(p.get("telegram_id") or 0) == telegram_id for p in participants)
    body = await _build_subscription_payment_report_text(
        db,
        subscription,
        participants,
        scope="user" if in_it else "all",
        user_id=telegram_id if in_it else None,
        # The viewer's timezone, as on the card: "due today" and Paid agree.
        tz_name=await viewer_timezone(db, telegram_id),
    )
    await send_chunked_responder_text(
        target,
        report_text(str(subscription.get("name") or ""), body),
        reply_markup=public_subscription_report_keyboard(subscription_id, close=close),
    )


async def currency_screen(
    db: Database,
    subscription: Dict[str, Any],
    telegram_id: int,
    *,
    close: bool = False,
) -> Screen:
    current, default, has_override = await shown_in(db, subscription, telegram_id)
    return (
        currency_text(str(subscription.get("name") or ""), current, default, has_override=has_override),
        public_subscription_currency_keyboard(
            int(subscription["id"]), current, default, has_override=has_override, close=close
        ),
    )


async def reminder_screen(
    db: Database,
    subscription: Dict[str, Any],
    telegram_id: int,
    *,
    close: bool = False,
) -> Screen:
    current, default, has_override = await reminder_times(db, subscription, telegram_id)
    tz_name = await viewer_timezone(db, telegram_id)
    return (
        reminder_text(str(subscription.get("name") or ""), current, default, tz_name, has_override=has_override),
        public_subscription_reminder_time_keyboard(
            int(subscription["id"]), current, default, has_override=has_override, close=close
        ),
    )
