"""Admin Users section: the users list, the member card and the card's sub-screens.

Every screen is built by a pure-ish ``*_screen`` coroutine that returns
``(text, markup)``, so a handler can show it on a callback (edit in place), as
a new message, or on the origin screen of a finished prompt. The ``send_*``
wrappers cover the common callback/message case.

Navigation: the list is a top-level screen (no nav row); the card returns to
the list ("◀️ To list", same page); every sub-screen returns to the card
("◀️ To card"). A card opened from an admin alert is a new message with
[✖️ Close].
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence, Tuple

from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from app.core.constants import PAYMENT_MODE_FIXED
from app.core.reminders import parse_timezone, tg_time
from app.services import calculate_share_base, normalize_payment_mode, parse_fixed_amount
from app.storage.db import Database
from app.ui.helpers import (
    MEMBER_REPORT_COLLAPSE_MIN_TOTAL,
    USER_GONE,
    _build_user_payment_blocks,
    assemble_payments_report,
    resolve_report_timezone,
    respond_with_markup,
    send_chunked_responder_text,
    spawned_screen_of,
    stale_redirect,
)
from app.ui.keyboards import (
    MEMBERS_PER_PAGE,
    build_members_list_keyboard,
    invite_link_keyboard,
    member_balance_currency_keyboard,
    member_balance_keyboard,
    member_card_nav_keyboard,
    member_delete_confirm_keyboard,
    member_detail_keyboard,
    member_invite_confirm_keyboard,
    member_payment_destination_keyboard,
    member_report_keyboard,
    default_method_value,
    payment_method_choice_banner,
    payment_method_label,
)
from app.ui.states import Responder
from app.ui.style import SEP, expandable, join_blocks, sub_line, title, with_banner
from app.ui.text import days_left_phrase, escape_html, format_days, format_money, format_number, plural

logger = logging.getLogger(__name__)

Screen = Tuple[str, InlineKeyboardMarkup]

INVITE_TTL_DAYS = 7
USERS_TITLE_EMOJI = "👥"
LIST_HINT = "Tap a user to open their card."
EMPTY_LIST_TEXT = "No users yet."
USER_DELETED_BANNER = "⚠️ This user was deleted"
NO_SUBSCRIPTIONS_TEXT = "Not in any subscription yet."
CURRENCY_WARNING = "⚠️ Changing the currency does not convert the amount."
# The card lists this many subscriptions as lines; the rest fold into an expandable quote.
CARD_SUBSCRIPTION_LINES = 5
CARD_FOLD_BUDGET_UTF16 = 2500


# ------------------------------------------------------------------- basics


def full_name_of(friend: Dict[str, object]) -> str:
    return str(friend.get("full_name") or "").strip() or "Unknown"


def first_name_of(friend_or_name: Dict[str, object] | str) -> str:
    """``'Anna'`` from ``'Anna Petrova'`` (plain text, not escaped)."""
    name = full_name_of(friend_or_name) if isinstance(friend_or_name, dict) else str(friend_or_name or "")
    parts = name.split()
    return parts[0] if parts else "Unknown"


def is_pending(friend: Dict[str, object]) -> bool:
    return friend.get("telegram_id") is None


async def default_balance_currency(db: Database) -> str:
    return str(await db.get_setting("target_currency") or "RUB").strip().upper() or "RUB"


def balance_of(friend: Dict[str, object], default_currency: str) -> tuple[float, str]:
    """The balance and its currency; a broken stored currency falls back to the default."""
    try:
        amount = float(friend.get("balance") or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    currency = str(friend.get("balance_currency") or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        currency = default_currency
    return amount, currency


async def friend_balance(db: Database, friend: Dict[str, object]) -> tuple[float, str]:
    return balance_of(friend, await default_balance_currency(db))


# ---------------------------------------------------------------- users list


def sorted_members(friends: Sequence[Dict[str, object]]) -> list[Dict[str, object]]:
    """Unpaid users first (attention-first), then by name; a friend dict may carry ``unpaid``."""
    return sorted(
        friends,
        key=lambda item: (
            not item.get("unpaid"),
            str(item.get("full_name") or "").casefold(),
            int(item.get("id") or 0),
        ),
    )


async def unpaid_telegram_ids(db: Database, today: date) -> set[int]:
    """Telegram ids with an unpaid open cycle due today or earlier, in any subscription.

    The same rule as the member card's '⚠️ unpaid': a cycle's own participant
    snapshot when it has one, else the live participants.
    """
    unpaid: set[int] = set()
    for subscription in await db.list_subscriptions():
        subscription_id = int(subscription["id"])
        due_values = []
        for value in await db.list_open_cycles(subscription_id):
            try:
                if datetime.strptime(value, "%Y-%m-%d").date() <= today:
                    due_values.append(value)
            except ValueError:
                continue
        if not due_values:
            continue
        paid: dict[str, set[int]] = {}
        for row in await db.list_payments_for_cycles(subscription_id, due_values):
            if row.get("paid_by_telegram_id") is not None:
                paid.setdefault(str(row["due_date"]), set()).add(int(row["paid_by_telegram_id"]))
        cycles = await db.list_cycle_participants_for_due_dates(subscription_id, due_values)
        live = {
            int(person["telegram_id"])
            for person in await db.list_subscription_participants(subscription_id)
            if person.get("telegram_id") is not None
        }
        for value in due_values:
            cycle = cycles.get(value) or {}
            if cycle.get("snapshot_ready"):
                ids = {int(p["telegram_id"]) for p in cycle.get("participants") or [] if p.get("telegram_id") is not None}
            else:
                ids = live
            unpaid |= ids - paid.get(value, set())
    return unpaid


async def members_in_list_order(db: Database) -> list[Dict[str, object]]:
    """Every user as the 👥 Users list shows them: each marked ``unpaid``, unpaid first."""
    today = datetime.now(parse_timezone(await resolve_report_timezone(db))).date()
    unpaid = await unpaid_telegram_ids(db, today)
    friends = []
    for friend in await db.list_friends():
        row = dict(friend)
        telegram_id = row.get("telegram_id")
        row["unpaid"] = telegram_id is not None and int(telegram_id) in unpaid  # type: ignore[arg-type]
        friends.append(row)
    return sorted_members(friends)


def page_count(total: int) -> int:
    return max(1, (total + MEMBERS_PER_PAGE - 1) // MEMBERS_PER_PAGE)


def page_of_member(friends: Sequence[Dict[str, object]], friend_id: int) -> Optional[int]:
    """The list page (1-based) that shows ``friend_id``, or None when it is not listed."""
    for index, friend in enumerate(sorted_members(friends)):
        if int(friend.get("id") or 0) == int(friend_id):
            return index // MEMBERS_PER_PAGE + 1
    return None


async def member_list_screen(
    db: Database,
    *,
    page: int = 1,
    focus_friend_id: Optional[int] = None,
    banner: Optional[str] = None,
) -> Screen:
    """'👥 <b>Users</b> · 12', the unpaid and not-signed-in counts when non-zero, the hint; one button per user.

    Users with an unpaid cycle come first and read '⚠️ Anna Petrova · unpaid'.
    """
    friends = await members_in_list_order(db)
    if not friends:
        text = join_blocks(title(USERS_TITLE_EMOJI, "Users"), EMPTY_LIST_TEXT)
        return with_banner(banner, text), build_members_list_keyboard([], page=1, total_pages=1, total_users=0)

    total_pages = page_count(len(friends))
    if focus_friend_id is not None:
        page = page_of_member(friends, focus_friend_id) or page
    page = max(1, min(int(page), total_pages))
    start = (page - 1) * MEMBERS_PER_PAGE
    page_friends = friends[start : start + MEMBERS_PER_PAGE]

    unpaid_count = sum(1 for friend in friends if friend.get("unpaid"))
    pending_count = sum(1 for friend in friends if is_pending(friend))
    attention = [
        f"⚠️ Unpaid: <b>{unpaid_count}</b>" if unpaid_count else None,
        f"⏳ Not signed in yet: <b>{pending_count}</b>" if pending_count else None,
    ]
    text = join_blocks(title(USERS_TITLE_EMOJI, "Users", str(len(friends))), attention, LIST_HINT)
    markup = build_members_list_keyboard(
        page_friends,
        page=page,
        total_pages=total_pages,
        total_users=len(friends),
    )
    return with_banner(banner, text), markup


async def send_member_list(
    target: Responder,
    db: Database,
    *,
    page: int = 1,
    focus_friend_id: Optional[int] = None,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> None:
    text, markup = await member_list_screen(db, page=page, focus_friend_id=focus_friend_id, banner=banner)
    await respond_with_markup(target, text, reply_markup=markup, toast=toast)


# --------------------------------------------------------------- the card


def _parse_utc(value: object) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def link_until_html(moment: datetime, tz_name: Optional[str]) -> str:
    """A live date+time entity with a complete fallback in the configured timezone."""
    zone = parse_timezone(tz_name)
    local = moment.astimezone(zone)
    return tg_time(moment, "wDt", f"{local.strftime('%a, %d.%m.%Y %H:%M')} ({zone.key})")


def invite_status_lines(
    expires_at: object,
    tz_name: Optional[str],
    now: Optional[datetime] = None,
) -> list[str]:
    """'Link works until <tg-time> (in 3 days)', or '⚠️ Link expired'; nothing when unknown."""
    moment = _parse_utc(expires_at)
    if moment is None:
        return []
    current = now if now is not None else datetime.now(timezone.utc)
    if moment <= current:
        return ["⚠️ Link expired"]
    zone = parse_timezone(tz_name)
    days = (moment.astimezone(zone).date() - current.astimezone(zone).date()).days
    return [f"Link works until {link_until_html(moment, tz_name)} ({days_left_phrase(days)})"]


def member_share(subscription: Dict[str, object], participants: Sequence[Dict[str, object]], person: Dict[str, object]) -> float:
    """What this participant pays per cycle, in the subscription currency."""
    weight = int(person.get("share_weight") or 1)
    amount = float(subscription.get("amount") or 0.0)
    if normalize_payment_mode(subscription.get("payment_mode")) == PAYMENT_MODE_FIXED:
        fixed = parse_fixed_amount(person.get("fixed_amount"))
        return (amount if fixed is None else fixed) * weight
    return amount / (calculate_share_base(subscription, list(participants)) or 1) * weight


async def _is_unpaid(db: Database, subscription_id: int, telegram_id: int, today: date) -> bool:
    """True when an open cycle due today or earlier lists this user and has no payment from them."""
    due_values = []
    for value in await db.list_open_cycles(subscription_id):
        try:
            if datetime.strptime(value, "%Y-%m-%d").date() <= today:
                due_values.append(value)
        except ValueError:
            continue
    if not due_values:
        return False
    paid = {
        row["due_date"]
        for row in await db.list_payments_for_cycles(subscription_id, due_values)
        if row.get("paid_by_telegram_id") is not None and int(row["paid_by_telegram_id"]) == telegram_id
    }
    cycles = await db.list_cycle_participants_for_due_dates(subscription_id, due_values)
    for value in due_values:
        if value in paid:
            continue
        cycle = cycles.get(value) or {}
        if cycle.get("snapshot_ready"):
            ids = {int(person.get("telegram_id") or 0) for person in cycle.get("participants") or []}
            if telegram_id not in ids:
                continue
        return True
    return False


async def member_subscriptions(
    db: Database,
    friend: Dict[str, object],
    today: date,
) -> list[tuple[Dict[str, object], float, bool]]:
    """``(subscription, share, unpaid)`` for every subscription the user is in, unpaid first."""
    friend_id = int(friend.get("id") or 0)
    telegram_id = friend.get("telegram_id")
    result = []
    for subscription in await db.list_subscriptions():
        if not int(subscription.get("participant_count") or 0):
            continue
        participants = await db.list_subscription_participants(int(subscription["id"]))
        person = next((item for item in participants if int(item.get("id") or 0) == friend_id), None)
        if person is None:
            continue
        unpaid = False
        if telegram_id is not None:
            unpaid = await _is_unpaid(db, int(subscription["id"]), int(telegram_id), today)
        result.append((subscription, member_share(subscription, participants, person), unpaid))
    result.sort(key=lambda item: not item[2])  # stable: unpaid first, then by next charge
    return result


def subscription_line(subscription: Dict[str, object], share: float, unpaid: bool) -> str:
    line = (
        f"{escape_html(subscription.get('name') or '')}{SEP}"
        f"{format_money(share, str(subscription.get('currency') or '').upper(), bold=True)}"
    )
    return f"{line}{SEP}⚠️ unpaid" if unpaid else line


def subscriptions_block(rows: Sequence[tuple[Dict[str, object], float, bool]]) -> str:
    if not rows:
        return NO_SUBSCRIPTIONS_TEXT
    lines = [subscription_line(*row) for row in rows]
    shown = [sub_line(line) for line in lines[:CARD_SUBSCRIPTION_LINES]]
    block = [f"📋 <b>Subscriptions: {len(rows)}</b>", *shown]
    rest = lines[CARD_SUBSCRIPTION_LINES:]
    if rest:
        block.append(expandable(rest, CARD_FOLD_BUDGET_UTF16, "subscription"))
    return "\n".join(block)


def user_link_html(friend: Dict[str, object]) -> str:
    name = escape_html(full_name_of(friend))
    telegram_id = friend.get("telegram_id")
    if telegram_id is None:
        return name
    return f'<a href="tg://user?id={int(telegram_id)}">{name}</a>'


async def payment_method_line(db: Database, telegram_id: int) -> str:
    assigned = await db.get_user_payment_destination_id(telegram_id)
    effective = await db.get_effective_payment_destination_for_user(telegram_id)
    if not effective:
        return "Payment method: not set"
    default_id = await db.get_default_payment_destination_id()
    is_default_method = default_id is not None and int(effective.get("id") or 0) == int(default_id)
    label = escape_html(payment_method_label(effective.get("title"), effective.get("currency"), default=is_default_method))
    is_assigned = assigned is not None and int(effective.get("id") or 0) == int(assigned)
    return f"Payment method: {label}" + ("" if is_assigned else " (default)")


async def member_card_text(db: Database, friend: Dict[str, object], *, now: Optional[datetime] = None) -> str:
    tz_name = await resolve_report_timezone(db)
    current = now if now is not None else datetime.now(timezone.utc)
    today = current.astimezone(parse_timezone(tz_name)).date()
    amount, currency = await friend_balance(db, friend)
    balance_line = f"Balance: {format_money(amount, currency, bold=True)}"
    header = [f"👤 <b>{user_link_html(friend)}</b>"]
    if is_pending(friend):
        header.append(sub_line("⏳ Not signed in yet"))
        header.extend(sub_line(line) for line in invite_status_lines(friend.get("invite_expires_at"), tz_name, current))
        header.append(sub_line(balance_line))
    else:
        telegram_id = int(friend["telegram_id"])
        header.append(sub_line(f"Telegram ID: <code>{telegram_id}</code>"))
        header.append(sub_line(balance_line))
        header.append(sub_line(await payment_method_line(db, telegram_id)))
    rows = await member_subscriptions(db, friend, today)
    return join_blocks(header, subscriptions_block(rows))


async def member_card_screen(
    db: Database,
    friend_id: int,
    *,
    banner: Optional[str] = None,
    spawned: bool = False,
) -> Optional[Screen]:
    """The member card, or None when the user is gone."""
    friend = await db.get_friend(friend_id)
    if not friend:
        return None
    text = await member_card_text(db, friend)
    markup = member_detail_keyboard(friend_id, is_pending=is_pending(friend), spawned=spawned)
    return with_banner(banner, text), markup


async def show_user_gone(target: Responder, db: Database) -> None:
    """A button or prompt about a deleted user: the list, with a toast or a banner."""
    if isinstance(target, CallbackQuery):
        await stale_redirect(target, USER_GONE, lambda: send_member_list(target, db))
        return
    await send_member_list(target, db, banner=USER_DELETED_BANNER)


async def send_member_detail(
    target: Responder,
    db: Database,
    friend_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> None:
    # The copy opened from an alert (``remember_spawned``) keeps its [✖️ Close].
    screen = await member_card_screen(db, friend_id, banner=banner, spawned=spawned_screen_of(target))
    if screen is None:
        await show_user_gone(target, db)
        return
    text, markup = screen
    await respond_with_markup(target, text, reply_markup=markup, toast=toast)


# ------------------------------------------------------------ sign-in link


def sign_in_link_screen(
    friend: Dict[str, object],
    heading_html: str,
    invite_link: Optional[str],
    expires_at: Optional[datetime],
    tz_name: Optional[str],
) -> Screen:
    """'✅ <b>Anna Petrova added</b>' / '🔗 <b>New sign-in link · Ivan</b>' plus the link.

    Without a link (the bot username is not set) the screen says so and only
    offers the way back to the card.
    """
    friend_id = int(friend["id"])
    if not invite_link:
        text = join_blocks(heading_html, "⚠️ Could not build the link: the bot username is not set.")
        return text, member_card_nav_keyboard(friend_id)
    blocks = [
        heading_html,
        f"Send {escape_html(first_name_of(friend))} this sign-in link:\n{escape_html(invite_link)}",
    ]
    if expires_at is not None:
        blocks.append(f"Works until {link_until_html(expires_at, tz_name)} ({format_days(INVITE_TTL_DAYS)}).")
    return join_blocks(*blocks), invite_link_keyboard(invite_link, friend_id)


def added_heading(friend: Dict[str, object]) -> str:
    return f"✅ <b>{escape_html(full_name_of(friend))} added</b>"


def new_link_heading(friend: Dict[str, object]) -> str:
    return title("🔗", f"New sign-in link{SEP}{escape_html(full_name_of(friend))}")


async def member_invite_confirm_screen(db: Database, friend: Dict[str, object]) -> Screen:
    question = (
        f"⚠️ <b>Make a new sign-in link for {escape_html(first_name_of(friend))}?</b>\n"
        "The old link stops working."
    )
    card = await member_card_text(db, friend)
    return join_blocks(question, card), member_invite_confirm_keyboard(int(friend["id"]))


# ------------------------------------------------------------------ balance


async def member_balance_screen(db: Database, friend_id: int, *, banner: Optional[str] = None) -> Optional[Screen]:
    friend = await db.get_friend(friend_id)
    if not friend:
        return None
    amount, currency = await friend_balance(db, friend)
    text = join_blocks(
        title("💰", f"Balance{SEP}{escape_html(full_name_of(friend))}"),
        format_money(amount, currency, bold=True),
    )
    return with_banner(banner, text), member_balance_keyboard(friend_id)


async def member_balance_currency_screen(db: Database, friend_id: int) -> Optional[Screen]:
    friend = await db.get_friend(friend_id)
    if not friend:
        return None
    _, currency = await friend_balance(db, friend)
    text = join_blocks(
        title("💱", f"Balance currency{SEP}{escape_html(full_name_of(friend))}"),
        f"Now: {escape_html(currency)}\n{CURRENCY_WARNING}",
    )
    return text, member_balance_currency_keyboard(friend_id, currency)


def balance_prompt_text(friend: Dict[str, object], amount: float, currency: str) -> str:
    return (
        f"New balance for {escape_html(first_name_of(friend))}, {escape_html(currency)}? "
        f"Now {format_number(amount)}.\n"
        "Send <code>1200</code>, or <code>+500</code> / <code>-200</code> to change it."
    )


def balance_currency_prompt_text(friend: Dict[str, object]) -> str:
    return (
        f"Currency for {escape_html(first_name_of(friend))}'s balance? "
        "Three letters, for example <code>USD</code>."
    )


# ------------------------------------------------------------------- delete


async def member_delete_confirm_screen(db: Database, friend: Dict[str, object]) -> Screen:
    """'⚠️ <b>Delete Anna Petrova?</b>' plus one consequence line; zero parts are left out."""
    first = escape_html(first_name_of(friend))
    amount, currency = await friend_balance(db, friend)
    tz_name = await resolve_report_timezone(db)
    today = datetime.now(parse_timezone(tz_name)).date()
    subscription_count = len(await member_subscriptions(db, friend, today))
    friend_id = int(friend["id"])  # type: ignore[call-overload]
    waiting = sum(
        1 for request in await db.list_pending_topup_requests() if int(request.get("friend_id") or 0) == friend_id
    )
    parts = [
        f"leaves {plural(subscription_count, 'subscription')}" if subscription_count else "",
        f"loses the balance of {format_money(amount, currency)}" if amount > 0 else "",
        # Their pending top-up requests go with them (they are deleted with the user).
        f"drops {plural(waiting, 'pending top-up')}" if waiting else "",
    ]
    parts = [part for part in parts if part]
    if parts:
        joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        consequence = f"{first} {joined}."
    else:
        consequence = "This cannot be undone."
    text = f"⚠️ <b>Delete {escape_html(full_name_of(friend))}?</b>\n{consequence}"
    return text, member_delete_confirm_keyboard(int(friend["id"]))


# ------------------------------------------------------------------- report


def member_report_title(friend: Dict[str, object], attention_count: int = 0) -> str:
    fact = ""
    if attention_count > 0:
        fact = f"{attention_count} {'needs' if attention_count == 1 else 'need'} attention"
    return title("📊", f"Reports{SEP}{escape_html(full_name_of(friend))}", fact)


async def send_member_report(target: Responder, db: Database, friend_id: int) -> None:
    friend = await db.get_friend(friend_id)
    if not friend:
        await show_user_gone(target, db)
        return
    keyboard = member_report_keyboard(friend_id)
    if is_pending(friend):
        text = join_blocks(member_report_title(friend), f"{escape_html(first_name_of(friend))} has not signed in yet.")
        await respond_with_markup(target, text, reply_markup=keyboard)
        return
    blocks = await _build_user_payment_blocks(db, int(friend["telegram_id"]), await resolve_report_timezone(db))
    if not blocks:
        await respond_with_markup(target, join_blocks(member_report_title(friend), "Nothing to report yet."), reply_markup=keyboard)
        return
    report = assemble_payments_report(blocks, collapse_min_total=MEMBER_REPORT_COLLAPSE_MIN_TOTAL)
    # The shared assembler starts with the generic report header; this view names the user instead.
    body = report.split("\n\n", 1)[1] if "\n\n" in report else report
    attention = sum(1 for block in blocks if block.startswith("🔴"))
    await send_chunked_responder_text(
        target,
        join_blocks(member_report_title(friend, attention), body),
        reply_markup=keyboard,
    )


# ----------------------------------------------------------- payment method


async def member_payment_method_screen(db: Database, friend_id: int) -> Optional[Screen]:
    """The method shown on the card; dead ends keep the title, one line and [◀️ To card]."""
    friend = await db.get_friend(friend_id)
    if not friend:
        return None
    heading = title("🏦", f"Payment method{SEP}{escape_html(full_name_of(friend))}")
    first = escape_html(first_name_of(friend))
    if is_pending(friend):
        return join_blocks(heading, f"{first} has not signed in yet."), member_card_nav_keyboard(friend_id)
    destinations = await db.list_payment_destinations()
    if not destinations:
        return (
            join_blocks(heading, "No payment methods yet. Add one in “💳 Payment”."),
            member_card_nav_keyboard(friend_id),
        )
    telegram_id = int(friend["telegram_id"])
    assigned_id = await db.get_user_payment_destination_id(telegram_id)
    default_id = await db.get_default_payment_destination_id()
    by_id = {int(item["id"]): item for item in destinations}
    if assigned_id is not None and int(assigned_id) in by_id:
        chosen = by_id[int(assigned_id)]
        is_default = default_id is not None and int(assigned_id) == int(default_id)
        now_text = escape_html(payment_method_label(chosen.get("title"), chosen.get("currency"), default=is_default))
        selected_id: Optional[int] = int(assigned_id)
    else:
        default = by_id.get(int(default_id)) if default_id is not None else None
        selected_id = None
        now_text = escape_html(default_method_value(default))
    text = join_blocks(heading, f"Now: {now_text}\nShown on {first}'s card.")
    markup = member_payment_destination_keyboard(friend_id, destinations, selected_id, default_id)
    return text, markup


def payment_method_banner(
    destination: Optional[Dict[str, Any]],
    *,
    default: bool,
    is_default_method: bool = False,
) -> str:
    """The same banner as the subscription picker: '✅ Payment method · <b>Default (⭐ Sber · RUB)</b>'."""
    if default:
        return payment_method_choice_banner(default_method_value(destination))
    if destination is None:
        return payment_method_choice_banner(default_method_value(None))
    label = payment_method_label(destination.get("title"), destination.get("currency"), default=is_default_method)
    return payment_method_choice_banner(label)


def invite_expires_at(now: Optional[datetime] = None) -> datetime:
    current = now if now is not None else datetime.now(timezone.utc)
    return (current + timedelta(days=INVITE_TTL_DAYS)).replace(microsecond=0)


def storage_timestamp(moment: datetime) -> str:
    """The naive-UTC ISO form the friends table stores invite expiries in."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()
