"""The shell around the sections: /start, sign-in links, and the identity errors.

/start answers with a title plus ONE line that says what to do next for this
user in their current state (principle start-next-step). The lines are built
by pure functions from small snapshots, so the priorities can be tested
without a bot; the ``*_snapshot`` coroutines read the snapshots from the
database with the same rules as the reports (an open cycle due today or
earlier with someone unpaid needs attention).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Optional, Sequence

from app.core.constants import PAYMENT_MODE_FIXED
from app.core.reminders import (
    DEFAULT_REMINDER_TIMEZONE,
    calculate_next_charge_date,
    normalize_monthly_anchor_day,
    normalize_timezone_name,
    parse_timezone,
    tg_due,
)
from app.services import calculate_share_base, normalize_payment_mode, parse_fixed_amount
from app.storage.db import Database
from app.ui.keyboards import MENU_PAYMENT, MENU_REPORTS, MENU_SUBSCRIPTIONS, MENU_USERS
from app.ui.style import SEP, title
from app.ui.text import days_left_phrase, escape_html, format_money, plural_word

# ----------------------------------------------------------------- the texts

START_TITLE = title("👋", "PingSubs")
STRANGER_START_TEXT = (
    f"{START_TITLE}\n\n"
    "This bot reminds a group about shared subscriptions.\n"
    "Ask the admin for your sign-in link."
)
FIRST_ADMIN_TEXT = (
    f"{title('👋', 'Welcome to PingSubs')}\n\n"
    "You are the first user, so you are the admin.\n\n"
    f"1. “{MENU_PAYMENT}” → add how people pay you\n"
    f"2. “{MENU_USERS}” → add people\n"
    f"3. “{MENU_SUBSCRIPTIONS}” → add a subscription"
)
ADMIN_INVITE_NOTE = "Sign-in links are for members; you are already the admin."

JOIN_LINK_BROKEN_TEXT = (
    f"{title('⚠️', 'This sign-in link does not work')}\n\n"
    "It was used or replaced. Ask the admin for a new one."
)
JOIN_LINK_EXPIRED_TEXT = (
    f"{title('⌛️', 'This sign-in link has expired')}\n\n"
    "Ask the admin for a new one."
)
JOIN_ALREADY_LINKED_TEXT = (
    f"{title('⚠️', 'This Telegram account is already linked')}\n\n"
    "Ask the admin if this is a mistake."
)
JOIN_FAILED_TEXT = (
    f"{title('⚠️', 'Could not sign you in')}\n\n"
    "Try the link again or ask the admin for a new one."
)

NOT_IN_ANY_SUBSCRIPTION = "You are not in any subscription yet."
NO_SUBSCRIPTIONS_LINE = f"No subscriptions yet — start in “{MENU_SUBSCRIPTIONS}”."

# One wording for "this update has no readable sender" (messages and alerts).
NO_IDENTITY_TEXT = "Could not read your Telegram account. Send /start."
# An admin tapping a member-only control (the admin's menu has no "👤 Account").
ADMIN_ACCOUNT_TEXT = "👤 Account is in the member menu. Your menu is below."
ADMIN_TOPUP_TEXT = "➕ Top up is in the member menu. Your menu is below."
MEMBERS_ONLY_ALERT = "This is for members only."

# The welcome lists at most this many subscription names, then "and N more".
JOIN_LIST_LIMIT = 8


# ------------------------------------------------------------- the snapshots


@dataclass(frozen=True)
class NextCharge:
    """The next charge worth naming: a subscription, its due day and (for members) the share."""

    name: str
    due_value: str
    amount: Optional[float] = None
    currency: str = ""


@dataclass(frozen=True)
class AdminStartSnapshot:
    today: date
    tz_name: str = DEFAULT_REMINDER_TIMEZONE
    subscription_count: int = 0
    pending_topups: int = 0
    unpaid_subscriptions: int = 0
    next_charge: Optional[NextCharge] = None


@dataclass(frozen=True)
class UnpaidBill:
    name: str
    due_value: str


@dataclass(frozen=True)
class MemberStartSnapshot:
    today: date
    tz_name: str = DEFAULT_REMINDER_TIMEZONE
    subscription_count: int = 0
    # Unpaid bills due today or earlier, oldest first, at most one per subscription.
    unpaid: Sequence[UnpaidBill] = field(default_factory=tuple)
    next_charge: Optional[NextCharge] = None


# ------------------------------------------------------------ pure builders


def _days_until(due_value: str, today: date) -> Optional[int]:
    try:
        return (datetime.strptime(due_value, "%Y-%m-%d").date() - today).days
    except (TypeError, ValueError):
        return None


def _when(due_value: str, tz_name: str, today: date) -> str:
    """``'<tg-time> (in 2 days)'``; the relative part is left out for an unreadable date."""
    days = _days_until(due_value, today)
    date_html = tg_due(due_value, tz_name, "wD")
    return f"{date_html} ({days_left_phrase(days)})" if days is not None else date_html


def admin_next_step_line(snapshot: AdminStartSnapshot) -> str:
    """The one line under the admin's /start title, by priority.

    Top-ups waiting for approval, then subscriptions with unpaid bills, then
    the next charge when everything is paid, then the empty state.
    """
    if snapshot.pending_topups > 0:
        count = snapshot.pending_topups
        verb = "waits" if count == 1 else "wait"
        return f"📥 <b>{count}</b> {plural_word(count, 'top-up')} {verb} for approval — open “{MENU_PAYMENT}”."
    if snapshot.unpaid_subscriptions > 0:
        count = snapshot.unpaid_subscriptions
        verb = "has" if count == 1 else "have"
        return (
            f"⚠️ <b>{count}</b> {plural_word(count, 'subscription')} {verb} unpaid bills"
            f" — open “{MENU_REPORTS}”."
        )
    if snapshot.subscription_count <= 0:
        return NO_SUBSCRIPTIONS_LINE
    charge = snapshot.next_charge
    if charge is None:
        return "✅ All paid."
    return (
        f"✅ All paid. Next charge: {escape_html(charge.name)}{SEP}"
        f"{_when(charge.due_value, snapshot.tz_name, snapshot.today)}."
    )


def admin_start_text(snapshot: AdminStartSnapshot, *, invite_note: bool = False) -> str:
    """``'👋 <b>PingSubs</b>'`` plus the next step; an admin's own sign-in link adds a note."""
    text = f"{START_TITLE}\n\n{admin_next_step_line(snapshot)}"
    if invite_note:
        text += f"\n\n{ADMIN_INVITE_NOTE}"
    return text


def member_next_step_line(snapshot: MemberStartSnapshot) -> str:
    """The one line under the member's /start title.

    An unpaid bill (the oldest first), then the next charge with the member's
    share when everything is paid, then "not in any subscription yet".
    """
    if snapshot.unpaid:
        oldest = snapshot.unpaid[0]
        names = escape_html(oldest.name)
        others = len(snapshot.unpaid) - 1
        if others > 0:
            names += f" and {others} more"
        verb = "is" if others == 0 else "are"
        days = _days_until(oldest.due_value, snapshot.today)
        if days is not None and days >= 0:
            # Every bill in the list is due today or earlier, so the oldest is today's.
            return f"⏳ {names} {verb} due today. Open “{MENU_SUBSCRIPTIONS}”."
        since = tg_due(oldest.due_value, snapshot.tz_name, "wD")
        return f"⚠️ {names} {verb} unpaid since {since}. Open “{MENU_SUBSCRIPTIONS}”."
    if snapshot.subscription_count <= 0:
        return NOT_IN_ANY_SUBSCRIPTION
    charge = snapshot.next_charge
    if charge is None:
        return "✅ All paid."
    amount = ""
    if charge.currency:
        amount = f"{SEP}{format_money(charge.amount, charge.currency, bold=True)}"
    return (
        f"✅ All paid. Next: {escape_html(charge.name)}{amount}"
        f" on {_when(charge.due_value, snapshot.tz_name, snapshot.today)}."
    )


def member_start_text(snapshot: MemberStartSnapshot) -> str:
    return f"{START_TITLE}\n\n{member_next_step_line(snapshot)}"


def join_success_text(full_name: str, subscription_names: Sequence[str]) -> str:
    """``'✅ <b>Welcome, Anna</b>'`` and the subscriptions the new member is in."""
    head = title("✅", f"Welcome, {escape_html(' '.join((full_name or '').split()) or 'friend')}")
    names = [escape_html(name) for name in subscription_names if str(name or "").strip()]
    if not names:
        return f"{head}\n\n{NOT_IN_ANY_SUBSCRIPTION}"
    shown = names[:JOIN_LIST_LIMIT]
    line = ", ".join(shown)
    if len(names) > len(shown):
        line += f" and {len(names) - len(shown)} more"
    return f"{head}\n\nYou are in: {line}."


# ------------------------------------------------------- reading the database


def _cycle_participant_ids(
    cycle_state: Dict[str, Any],
    live_participants: Sequence[Dict[str, Any]],
) -> set[int]:
    """Signed-in participants of a cycle: its snapshot, or the live list before one exists."""
    people = list(cycle_state.get("participants") or [])
    if not people and not cycle_state.get("snapshot_ready"):
        people = list(live_participants)
    ids = set()
    for person in people:
        try:
            telegram_id = int(person.get("telegram_id") or 0)
        except (TypeError, ValueError):
            continue
        if telegram_id > 0:
            ids.add(telegram_id)
    return ids


def _parse_due(value: object) -> Optional[date]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


async def _base_timezone(db: Database) -> str:
    return normalize_timezone_name(
        await db.get_effective_base_timezone(DEFAULT_REMINDER_TIMEZONE),
        DEFAULT_REMINDER_TIMEZONE,
    ) or DEFAULT_REMINDER_TIMEZONE


async def _paid_by_due(db: Database, subscription_id: int, due_values: Sequence[str]) -> Dict[str, set[int]]:
    paid: Dict[str, set[int]] = {}
    for row in await db.list_payments_for_cycles(subscription_id, list(due_values)):
        payer = row.get("paid_by_telegram_id")
        if payer is not None:
            paid.setdefault(str(row.get("due_date") or ""), set()).add(int(payer))
    return paid


async def admin_start_snapshot(db: Database, *, today: Optional[date] = None) -> AdminStartSnapshot:
    tz_name = await _base_timezone(db)
    today = today or datetime.now(parse_timezone(tz_name)).date()
    subscriptions = await db.list_subscriptions()
    pending = len(await db.list_pending_topup_requests())
    unpaid_count = 0
    upcoming: list[tuple[date, str, str]] = []
    for sub in subscriptions:
        subscription_id = int(sub["id"])
        next_due = _parse_due(sub.get("next_charge_at"))
        if next_due is not None and next_due >= today:
            upcoming.append((next_due, str(sub.get("name") or ""), next_due.isoformat()))
        participants = await db.list_subscription_participants(subscription_id)
        if not participants:
            continue
        open_cycles = [due for due in await db.list_open_cycles(subscription_id) if (_parse_due(due) or today) <= today]
        if not open_cycles:
            continue
        paid = await _paid_by_due(db, subscription_id, open_cycles)
        states = await db.list_cycle_participants_for_due_dates(subscription_id, open_cycles)
        if any(
            _cycle_participant_ids(states.get(due) or {}, participants) - paid.get(due, set())
            for due in open_cycles
            if _parse_due(due) is not None
        ):
            unpaid_count += 1
    next_charge = None
    if upcoming:
        _, name, due_value = min(upcoming, key=lambda item: (item[0], item[1].lower()))
        next_charge = NextCharge(name=name, due_value=due_value)
    return AdminStartSnapshot(
        today=today,
        tz_name=tz_name,
        subscription_count=len(subscriptions),
        pending_topups=pending,
        unpaid_subscriptions=unpaid_count,
        next_charge=next_charge,
    )


def member_share(
    subscription: Dict[str, Any],
    participants: Sequence[Dict[str, Any]],
    telegram_id: int,
) -> Optional[float]:
    """What this member pays per cycle, in the subscription currency (None if not a participant)."""
    person = next((p for p in participants if p.get("telegram_id") is not None and int(p["telegram_id"]) == telegram_id), None)
    if person is None:
        return None
    weight = int(person.get("share_weight") or 1)
    amount = float(subscription.get("amount") or 0.0)
    if normalize_payment_mode(subscription.get("payment_mode")) == PAYMENT_MODE_FIXED:
        fixed = parse_fixed_amount(person.get("fixed_amount"))
        return (amount if fixed is None else fixed) * weight
    return amount / calculate_share_base(subscription, list(participants)) * weight


async def member_start_snapshot(
    db: Database,
    telegram_id: int,
    *,
    today: Optional[date] = None,
) -> MemberStartSnapshot:
    base_tz = await _base_timezone(db)
    tz_name = normalize_timezone_name(
        await db.get_effective_user_timezone(telegram_id, base_tz),
        base_tz,
    ) or base_tz
    today = today or datetime.now(parse_timezone(tz_name)).date()
    subscriptions = await db.list_subscriptions_for_user(telegram_id)
    unpaid: list[tuple[date, UnpaidBill]] = []
    upcoming: list[tuple[date, NextCharge]] = []
    for row in subscriptions:
        subscription_id = int(row["id"])
        subscription = await db.get_subscription(subscription_id) or row
        name = str(subscription.get("name") or "")
        participants = await db.list_subscription_participants(subscription_id)
        open_cycles = await db.list_open_cycles(subscription_id)
        next_value = str(subscription.get("next_charge_at") or "")
        paid = await _paid_by_due(db, subscription_id, [*open_cycles, next_value])
        states = await db.list_cycle_participants_for_due_dates(subscription_id, open_cycles)
        overdue = sorted(
            due
            for due in (_parse_due(value) for value in open_cycles)
            if due is not None
            and due <= today
            and telegram_id in _cycle_participant_ids(states.get(due.isoformat()) or {}, participants)
            and telegram_id not in paid.get(due.isoformat(), set())
        )
        if overdue:
            unpaid.append((overdue[0], UnpaidBill(name=name, due_value=overdue[0].isoformat())))
            continue
        next_due = _parse_due(next_value)
        if next_due is not None and telegram_id in paid.get(next_value, set()):
            next_due = calculate_next_charge_date(
                next_due,
                int(subscription.get("period_days") or 30),
                normalize_monthly_anchor_day(subscription.get("monthly_anchor_day")),
            )
        if next_due is None or next_due < today:
            continue
        upcoming.append(
            (
                next_due,
                NextCharge(
                    name=name,
                    due_value=next_due.isoformat(),
                    amount=member_share(subscription, participants, telegram_id),
                    currency=str(subscription.get("currency") or ""),
                ),
            )
        )
    unpaid.sort(key=lambda item: (item[0], item[1].name.lower()))
    upcoming.sort(key=lambda item: (item[0], item[1].name.lower()))
    return MemberStartSnapshot(
        today=today,
        tz_name=tz_name,
        subscription_count=len(subscriptions),
        unpaid=tuple(bill for _, bill in unpaid),
        next_charge=upcoming[0][1] if upcoming else None,
    )
