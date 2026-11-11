"""Reports and cycles (WP8).

* Admin 📊 Reports (reply menu, top-level): the shared report blocks, red
  first, '👥 No participants: iCloud, YouTube' last, and one '🔴 Netflix'
  button per subscription that needs attention (opens the card as a new
  message). No nav row.
* '📊 <b>Reports · Netflix</b>' (sub:report): the subscription's report block,
  [📬 Remind unpaid (2)] while someone is overdue, [◀️ To card].
* '🗂 <b>Open cycles · Netflix</b> · 2' (sub:cycles): one block and one button
  per open cycle, [◀️ To card].
* '🗂 <b>Cycle Thu, 01.10 · Netflix</b>' (cycle:open): status, amount,
  progress, who paid; [♻️ Recreate][🔒 Close cycle] each open a confirmation.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.core.reminders import parse_timezone, tg_due
from app.storage.db import Database
from app.ui.helpers import (
    ADMIN_REPORT_COLLAPSE_MIN_GREEN,
    CYCLE_CLOSED,
    _build_open_cycle_summary,
    _build_subscription_payment_report_text,
    _is_attention_block,
    _resolve_report_cycle_participants,
    assemble_payments_report,
    payments_report_header,
    resolve_report_timezone,
    respond_with_markup,
    send_chunked_responder_text,
    show_subscription_gone,
    stale_redirect,
)
from app.ui.keyboards import (
    admin_report_keyboard,
    cycle_close_confirm_keyboard,
    cycle_progress_words,
    cycle_recreate_confirm_keyboard,
    subscription_cycle_actions_keyboard,
    subscription_open_cycles_keyboard,
    new_subscription_keyboard,
    subscription_report_keyboard,
)
from app.ui.states import Responder
from app.ui.style import BULLET, SEP, expandable, join_blocks, progress_bar, sub_line, title as screen_title, with_banner
from app.ui.text import escape_html, format_days, format_money, plural

Screen = Tuple[str, InlineKeyboardMarkup]

# Empty states are never a dead end: one hint and, where it can be created, one forward button.
REPORTS_EMPTY_LINE = "No subscriptions yet. Start in “📋 Subscriptions”."
REPORT_EMPTY_LINE = "No participants yet."
OPEN_CYCLES_EMPTY_LINE = "No open cycles — everything is settled."
CYCLE_ACTIONS_HINT = (
    "“♻️ Recreate” takes the current people and settings and clears payment marks. "
    "“🔒 Close cycle” marks everyone unpaid as paid."
)
CYCLE_RECREATED_BANNER = "✅ Cycle recreated · payment marks cleared"
CYCLE_CLOSED_BANNER = "⚠️ This cycle is already closed"
# The open cycles screen lists at most this many cycles (oldest first) and buttons.
OPEN_CYCLES_SHOWN = 20
# Names on the cycle screen fold into an expandable quote above five.
NAMES_FOLD_MIN = 6
NAMES_FOLD_BUDGET = 1200
NO_PARTICIPANTS_SHOWN = 20
CLOSE_NAMES_SHOWN = 3


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _name_html(subscription: Dict[str, object]) -> str:
    return escape_html(_clean(subscription.get("name")))


def _parse_due(due_value: str) -> Optional[date]:
    try:
        return datetime.strptime(due_value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


async def _today(db: Database) -> Tuple[str, date]:
    tz_name = await resolve_report_timezone(db)
    return tz_name, datetime.now(parse_timezone(tz_name)).date()


def cycle_status(due: Optional[date], today: date) -> str:
    """``'overdue by 9 days'`` | ``'due today'`` | ``'due tomorrow'`` | ``'due in 14 days'``."""
    if due is None:
        return ""
    days = (due - today).days
    if days < 0:
        return f"overdue by {format_days(-days)}"
    if days == 0:
        return "due today"
    if days == 1:
        return "due tomorrow"
    return f"due in {format_days(days)}"


def progress_line(paid: int, total: int) -> str:
    """``'███░░░░░░░ 1 of 3 paid'`` / ``'██████████ all paid'``; ``'No participants'`` without a bar."""
    if total <= 0:
        return "No participants"
    return f"{progress_bar(paid, total)} {cycle_progress_words(paid, total)}"


# ------------------------------------------------------------------ cycles


async def load_open_cycles(
    db: Database,
    subscription: Dict[str, object],
    only: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Every open cycle (oldest first), or just ``only``, with its amount and who has paid.

    Each item: ``due_value``, ``due``, ``amount``, ``currency``, ``paid``,
    ``total``, ``marks`` (payment marks of the cycle), ``paid_names``,
    ``unpaid_names`` and ``unpaid_ids``. People come from the cycle snapshot,
    or from the live participants while no snapshot exists.
    """
    subscription_id = int(subscription["id"])  # type: ignore[call-overload]
    open_cycles = await db.list_open_cycles(subscription_id)
    if only is not None:
        open_cycles = [due_value for due_value in open_cycles if due_value == only]
    if not open_cycles:
        return []
    state_map = await db.list_cycle_participants_for_due_dates(subscription_id, open_cycles)
    paid_by_due: Dict[str, set[int]] = {}
    for row in await db.list_payments_for_cycles(subscription_id, open_cycles):
        if row.get("paid_by_telegram_id") is not None:
            paid_by_due.setdefault(str(row.get("due_date") or ""), set()).add(int(row["paid_by_telegram_id"]))
    live = await db.list_subscription_participants(subscription_id)
    cycles: List[Dict[str, object]] = []
    for due_value in open_cycles:
        summary = _build_open_cycle_summary(subscription, due_value, date.today(), state_map, live, paid_by_due)
        names_by_id: Dict[int, str] = {}
        for person in _resolve_report_cycle_participants(state_map.get(due_value) or {}, live):
            telegram_id = int(person.get("telegram_id") or 0)
            if telegram_id > 0:
                names_by_id[telegram_id] = _clean(person.get("full_name")) or f"User {telegram_id}"
        paid_ids = set(names_by_id) & paid_by_due.get(due_value, set())
        unpaid_ids = set(names_by_id) - paid_ids

        def by_name(ids: set[int]) -> List[int]:
            return sorted(ids, key=lambda tid: names_by_id[tid].casefold())

        cycles.append(
            {
                "due_value": due_value,
                "due": _parse_due(due_value),
                "amount": summary["amount"],
                "currency": summary["currency"],
                "paid": len(paid_ids),
                "total": len(names_by_id),
                "marks": len(paid_by_due.get(due_value, set())),
                "paid_names": [names_by_id[tid] for tid in by_name(paid_ids)],
                "unpaid_names": [names_by_id[tid] for tid in by_name(unpaid_ids)],
                "unpaid_ids": by_name(unpaid_ids),
            }
        )
    return cycles


def _cycle_block(cycle: Dict[str, object], tz_name: str, today: date) -> List[str]:
    due_value = str(cycle["due_value"])
    status = cycle_status(cycle.get("due"), today)  # type: ignore[arg-type]
    head = f"📅 <b>{tg_due(due_value, tz_name, 'wD')}</b>" + (f"{SEP}{status}" if status else "")
    return [
        head,
        sub_line(f"Amount: {format_money(cycle.get('amount'), str(cycle.get('currency') or ''), bold=True)}"),  # type: ignore[arg-type]
        sub_line(progress_line(int(cycle.get("paid") or 0), int(cycle.get("total") or 0))),  # type: ignore[call-overload]
    ]


def open_cycles_text(
    subscription: Dict[str, object],
    cycles: Sequence[Dict[str, object]],
    tz_name: str,
    today: date,
) -> str:
    name = _name_html(subscription)
    if not cycles:
        return join_blocks(screen_title("🗂", f"Open cycles{SEP}{name}"), OPEN_CYCLES_EMPTY_LINE)
    shown = list(cycles)[:OPEN_CYCLES_SHOWN]
    blocks: List[object] = [_cycle_block(cycle, tz_name, today) for cycle in shown]
    rest = len(cycles) - len(shown)
    if rest > 0:
        blocks.append(f"…and {plural(rest, 'more cycle')}")
    return join_blocks(screen_title("🗂", f"Open cycles{SEP}{name}", len(cycles)), *blocks)  # type: ignore[arg-type]


async def build_open_cycles_screen(
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
) -> Optional[Screen]:
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    tz_name, today = await _today(db)
    cycles = await load_open_cycles(db, subscription)
    text = open_cycles_text(subscription, cycles, tz_name, today)
    markup = subscription_open_cycles_keyboard(subscription_id, cycles[:OPEN_CYCLES_SHOWN], today=today)
    return with_banner(banner, text), markup


async def send_open_cycles(
    target: Responder,
    db: Database,
    subscription_id: int,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    screen = await build_open_cycles_screen(db, subscription_id, banner=banner)
    if screen is None:
        await show_subscription_gone(target, db)
        return None
    text, markup = screen
    return await respond_with_markup(target, text, reply_markup=markup, toast=toast)


def _names_lines(label: str, names: Sequence[str]) -> List[str]:
    """``'✅ Paid: Alice Smith'``; above five, the count and the names folded."""
    if not names:
        return []
    escaped = [escape_html(name) for name in names]
    if len(escaped) < NAMES_FOLD_MIN:
        return [f"{label}: {', '.join(escaped)}"]
    return [
        f"{label}: <b>{len(escaped)}</b>",
        expandable([f"{BULLET} {name}" for name in escaped], NAMES_FOLD_BUDGET, "person", "people"),
    ]


def _cycle_title(subscription: Dict[str, object], due_value: str, tz_name: str) -> str:
    return screen_title("🗂", f"Cycle {tg_due(due_value, tz_name, 'wD')}{SEP}{_name_html(subscription)}")


def cycle_actions_text(subscription: Dict[str, object], cycle: Dict[str, object], tz_name: str, today: date) -> str:
    status = cycle_status(cycle.get("due"), today) or "open"  # type: ignore[arg-type]
    lines = [
        f"Status: {status}",
        f"Amount: {format_money(cycle.get('amount'), str(cycle.get('currency') or ''), bold=True)}",  # type: ignore[arg-type]
        progress_line(int(cycle.get("paid") or 0), int(cycle.get("total") or 0)),  # type: ignore[call-overload]
        *_names_lines("✅ Paid", list(cycle.get("paid_names") or [])),  # type: ignore[call-overload]
        *_names_lines("⚠️ Unpaid", list(cycle.get("unpaid_names") or [])),  # type: ignore[call-overload]
    ]
    return join_blocks(_cycle_title(subscription, str(cycle["due_value"]), tz_name), lines, CYCLE_ACTIONS_HINT)


async def _load_cycle(
    db: Database,
    subscription_id: int,
    due_value: str,
) -> Tuple[Optional[Dict[str, object]], Optional[Dict[str, object]]]:
    """``(subscription, cycle)``; the cycle is None when it is closed (or never existed)."""
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None, None
    cycles = await load_open_cycles(db, subscription, only=due_value)
    return subscription, (cycles[0] if cycles else None)


async def show_cycle_closed(target: Responder, db: Database, subscription_id: int) -> None:
    """A cycle that is no longer open: the open cycles list, never a dead end.

    A tap gets the toast 'This cycle is already closed'; a typed message the banner.
    """
    if isinstance(target, CallbackQuery):
        await stale_redirect(target, CYCLE_CLOSED, lambda: send_open_cycles(target, db, subscription_id))
        return
    await send_open_cycles(target, db, subscription_id, banner=CYCLE_CLOSED_BANNER)


async def send_cycle_actions(
    target: Responder,
    db: Database,
    subscription_id: int,
    due_value: str,
    *,
    banner: Optional[str] = None,
    toast: Optional[str] = None,
) -> Optional[Message]:
    subscription, cycle = await _load_cycle(db, subscription_id, due_value)
    if subscription is None:
        await show_subscription_gone(target, db)
        return None
    if cycle is None:
        await show_cycle_closed(target, db, subscription_id)
        return None
    tz_name, today = await _today(db)
    text = with_banner(banner, cycle_actions_text(subscription, cycle, tz_name, today))
    markup = subscription_cycle_actions_keyboard(subscription_id, due_value)
    return await respond_with_markup(target, text, reply_markup=markup, toast=toast)


# ----------------------------------------------------------- confirmations


def recreate_confirm_text(due_value: str, tz_name: str, marks: int, people: int) -> str:
    """'⚠️ <b>Recreate the Thu, 01.10 cycle?</b>' and what happens to its marks and people.

    With nobody in the subscription it says so instead of 'the current 0 people'.
    """
    if people > 0:
        takes = f"the current {plural(people, 'person', 'people')} and settings"
    else:
        takes = "the current settings and will have no participants"
    if marks > 0:
        verb = "is" if marks == 1 else "are"
        consequence = f"Its {plural(marks, 'payment mark')} {verb} cleared; it takes {takes}."
    else:
        consequence = f"It takes {takes}."
    return f"⚠️ <b>Recreate the {tg_due(due_value, tz_name, 'wD')} cycle?</b>\n{consequence}"


def _names_sentence(names: Sequence[str]) -> str:
    """``'Bob'``, ``'Bob and Carol'``, ``'Bob, Carol and Dan'``, ``'Bob, Carol, Dan and 2 more'``."""
    escaped = [escape_html(name) for name in names]
    if len(escaped) > CLOSE_NAMES_SHOWN:
        shown = escaped[:CLOSE_NAMES_SHOWN]
        return f"{', '.join(shown)} and {len(escaped) - CLOSE_NAMES_SHOWN} more"
    if len(escaped) == 1:
        return escaped[0]
    return f"{', '.join(escaped[:-1])} and {escaped[-1]}"


def close_confirm_text(
    due_value: str,
    tz_name: str,
    unpaid_names: Sequence[str],
    *,
    participants: Optional[int] = None,
) -> str:
    """'⚠️ <b>Close the Thu, 01.10 cycle?</b>' and who is marked as paid.

    ``participants`` 0 (nobody is in the cycle at all) gets its own line
    instead of 'Everyone has paid'.
    """
    if not unpaid_names and participants == 0:
        consequence = "Nobody is in this cycle; it is just closed."
    elif not unpaid_names:
        consequence = "Everyone has paid; nobody is marked."
    else:
        verb = "is" if len(unpaid_names) == 1 else "are"
        consequence = f"{_names_sentence(unpaid_names)} {verb} marked as paid."
    return f"⚠️ <b>Close the {tg_due(due_value, tz_name, 'wD')} cycle?</b>\n{consequence}"


def cycle_closed_banner(marked: int) -> str:
    """'✅ Cycle closed · 2 marked as paid' (just '✅ Cycle closed' when nobody was marked)."""
    return f"✅ Cycle closed{SEP}{marked} marked as paid" if marked > 0 else "✅ Cycle closed"


async def send_cycle_confirm(
    target: CallbackQuery,
    db: Database,
    subscription_id: int,
    due_value: str,
    *,
    close: bool,
) -> None:
    """Edit the cycle screen into the Recreate (``close=False``) or Close question."""
    subscription, cycle = await _load_cycle(db, subscription_id, due_value)
    if subscription is None:
        await show_subscription_gone(target, db)
        return
    if cycle is None:
        await show_cycle_closed(target, db, subscription_id)
        return
    tz_name, _ = await _today(db)
    if close:
        text = close_confirm_text(
            due_value,
            tz_name,
            list(cycle.get("unpaid_names") or []),  # type: ignore[call-overload]
            participants=int(cycle.get("total") or 0),  # type: ignore[call-overload]
        )
        markup = cycle_close_confirm_keyboard(subscription_id, due_value)
    else:
        live = await db.list_subscription_participants(subscription_id)
        people = sum(1 for person in live if int(person.get("telegram_id") or 0) > 0)
        text = recreate_confirm_text(due_value, tz_name, int(cycle.get("marks") or 0), people)  # type: ignore[call-overload]
        markup = cycle_recreate_confirm_keyboard(subscription_id, due_value)
    await respond_with_markup(target, text, reply_markup=markup)


# ------------------------------------------------------------------ reports


async def overdue_unpaid_ids(db: Database, subscription: Dict[str, object], today: date) -> set[int]:
    """Signed-in people who have not paid a cycle due today or earlier."""
    ids: set[int] = set()
    for cycle in await load_open_cycles(db, subscription):
        due = cycle.get("due")
        if isinstance(due, date) and due <= today:
            ids.update(int(tid) for tid in cycle.get("unpaid_ids") or [])  # type: ignore[union-attr]
    return ids


async def build_subscription_report(db: Database, subscription_id: int) -> Optional[Screen]:
    """'📊 <b>Reports · Netflix</b>' and its block; None when the subscription is gone."""
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    heading = screen_title("📊", f"Reports{SEP}{_name_html(subscription)}")
    participants = await db.list_subscription_participants(subscription_id)
    if not participants:
        return join_blocks(heading, REPORT_EMPTY_LINE), subscription_report_keyboard(subscription_id, empty=True)
    tz_name, today = await _today(db)
    body = await _build_subscription_payment_report_text(db, subscription, participants, scope="all", tz_name=tz_name)
    unpaid = await overdue_unpaid_ids(db, subscription, today)
    return f"{heading}\n\n{body}", subscription_report_keyboard(subscription_id, len(unpaid))


async def send_subscription_report(target: Responder, db: Database, subscription_id: int) -> None:
    screen = await build_subscription_report(db, subscription_id)
    if screen is None:
        await show_subscription_gone(target, db)
        return
    text, markup = screen
    await send_chunked_responder_text(target, text, reply_markup=markup)


def no_participants_line(names: Sequence[str]) -> str:
    """'👥 No participants: iCloud, YouTube' (long lists end with 'and N more')."""
    cleaned = [escape_html(_clean(name)) for name in names]
    shown = cleaned[:NO_PARTICIPANTS_SHOWN]
    rest = len(cleaned) - len(shown)
    tail = f" and {rest} more" if rest > 0 else ""
    return f"👥 No participants: {', '.join(shown)}{tail}"


async def admin_report(db: Database) -> Tuple[str, List[Dict[str, object]]]:
    """The admin's 📊 Reports text and the subscriptions that need attention (report order)."""
    subscriptions = await db.list_subscriptions()
    if not subscriptions:
        return join_blocks(payments_report_header(0), REPORTS_EMPTY_LINE), []
    tz_name = await resolve_report_timezone(db)
    blocks: List[str] = []
    attention: List[Dict[str, object]] = []
    without_people: List[str] = []
    for subscription in subscriptions:
        participants = await db.list_subscription_participants(subscription["id"])
        if not participants:
            without_people.append(str(subscription.get("name") or ""))
            continue
        block = await _build_subscription_payment_report_text(
            db, subscription, participants, scope="all", tz_name=tz_name
        )
        blocks.append(block)
        if _is_attention_block(block):
            attention.append(subscription)
    if blocks:
        text = assemble_payments_report(blocks, collapse_min_green=ADMIN_REPORT_COLLAPSE_MIN_GREEN)
    else:
        text = payments_report_header(0)
    if without_people:
        text = f"{text}\n\n{no_participants_line(without_people)}"
    return text, attention


async def send_admin_report(message: Message, db: Database) -> None:
    """The admin's 📊 Reports, built under a typing action; '🔴' buttons on the last chunk."""
    from app.ui.member_account import typing_action

    async with typing_action(message.bot, message.chat.id):
        text, attention = await admin_report(db)
        empty = not await db.list_subscriptions()
    markup = new_subscription_keyboard() if empty else admin_report_keyboard(attention)
    await send_chunked_responder_text(message, text, reply_markup=markup)
