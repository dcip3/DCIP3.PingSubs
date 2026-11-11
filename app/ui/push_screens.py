"""Texts of the reminder pushes, the Paid receipts and the admin payment pushes (WP15).

Plain HTML. A push is a title line that states the fact, an empty line, then a
few short lines (principles: push-layout, screen-title-line). Dates are
tg-time entities with a full fallback in the reader's timezone. Money strings
come in already formatted (``format_money`` / ``format_converted_amount``) and
are escaped here like every other value.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Mapping, Optional, Sequence

from app.core.reminders import parse_timezone, tg_due, tg_time
from app.ui.keyboards import is_openable_payment_link
from app.ui.style import SEP, cut_visible, expandable, join_blocks, progress_bar, sub_line, visible_len
from app.ui.text import days_left_phrase, escape_html, plural

PAID_HINT = "Tap “✅ Paid” once you have paid."
BATCH_HINT = "Tap the button of each bill you have paid."
TEST_FOOTER = "This is a test. “Paid” records nothing."
TOTAL_UNKNOWN_LINE = "⚠️ Total unknown: a rate is missing"
PAID_TOAST = "✅ Payment recorded"
ALREADY_PAID_TOAST = "Already paid"
TEST_PAID_ALERT = "Test only: nothing recorded."
NO_PARTICIPANTS_TAIL = "Nobody got a reminder."
# A comment longer than this is folded into an expandable quote.
COMMENT_FOLD_LEN = 300
# A comment is never shown longer than this (older comments had no limit).
COMMENT_SHOWN_MAX = 1000
# Requisites shown in a push, folded or in <pre>, stay within this many units.
DETAILS_SHOWN_MAX = 1600
# A link shown as text (it cannot be a button) is cut to this.
LINK_SHOWN_MAX = 300
# A push must stay under Telegram's 4096 (visible text, UTF-16); keep a margin.
PUSH_TEXT_BUDGET = 3900
# In a batch that is over budget, each bill keeps at least this much for its
# comment and its requisites.
BATCH_ITEM_MIN_BUDGET = 120


def _parse_due(due_value: object) -> Optional[date]:
    try:
        return datetime.strptime(str(due_value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def due_phrase(due_value: object, today: date) -> str:
    """``'due today'`` | ``'due tomorrow'`` | ``'due in 3 days'`` | ``'overdue by 3 days'``."""
    due = _parse_due(due_value)
    if due is None:
        return "due"
    phrase = days_left_phrase((due - today).days)
    return phrase if phrase.startswith("overdue") else f"due {phrase}"


def _today_in(tz_name: Optional[str]) -> date:
    return datetime.now(parse_timezone(tz_name)).date()


def _status_fact(status_text: str, due_value: Optional[str], today: Optional[date], tz_name: Optional[str]) -> str:
    if due_value and _parse_due(due_value) is not None:
        return due_phrase(due_value, today or _today_in(tz_name))
    return escape_html((status_text or "").strip()).lower() or "due"


def reminder_title(subscription_name: str, status: str, *, test: bool = False) -> str:
    """``'🔔 <b>Netflix · due tomorrow</b>'``; ``'🧪 <b>Test · Netflix · due today</b>'`` for a test."""
    name = escape_html(subscription_name)
    if test:
        return f"🧪 <b>Test{SEP}{name}{SEP}{status}</b>"
    return f"🔔 <b>{name}{SEP}{status}</b>"


def details_lines(payment_details: str, *, copyable: bool, budget: int = DETAILS_SHOWN_MAX) -> list[str]:
    """Requisites: each line in ``<code>`` inside an expandable quote when a copy button
    carries them (long-press copies a line, as on the member card), else ``<pre>``.

    Both stay within ``budget`` visible units, so long or legacy requisites
    never push the message over Telegram's limit.
    """
    details = (payment_details or "").strip()
    if not details:
        return []
    if copyable:
        coded = [f"<code>{escape_html(line.strip())}</code>" for line in details.splitlines() if line.strip()]
        return [expandable(coded, budget, "line")]
    return [f"<pre>{escape_html(cut_visible(details, max(budget - 2, 20)))}</pre>"]


def link_line(payment_link: str, budget: int = LINK_SHOWN_MAX) -> Optional[str]:
    """The link as text only when it cannot be a button (the button shows it otherwise)."""
    link = (payment_link or "").strip()
    if not link or is_openable_payment_link(link):
        return None
    return f"🔗 Link: {escape_html(cut_visible(link, max(budget, 40)))}"


def comment_line(comment: str, budget: int = COMMENT_SHOWN_MAX) -> Optional[str]:
    """``'📝 Family plan'``; a long comment is folded into an expandable quote.

    A comment is cut to ``budget`` characters (with '…'): older comments had
    no length limit, and a push must stay under Telegram's 4096.
    """
    text = cut_visible((comment or "").strip(), max(budget, 20))
    if not text:
        return None
    if len(text) > COMMENT_FOLD_LEN:
        return f"<blockquote expandable>📝 {escape_html(text)}</blockquote>"
    return f"📝 {escape_html(text)}"


def share_value(amount_text: str, converted_text: Optional[str]) -> str:
    """``'<b>4.17 USD</b> ≈ <b>380.50 RUB</b>'``; the ≈ part only when a rate was known."""
    value = f"<b>{escape_html(amount_text)}</b>"
    if converted_text:
        value += f" ≈ <b>{escape_html(converted_text)}</b>"
    return value


def balance_line(
    used_text: Optional[str],
    left_text: Optional[str],
    converted_text: Optional[str] = None,
) -> Optional[str]:
    """``'💰 From balance: 2.00 USD · left to pay <b>2.17 USD</b>'`` (``≈ <b>…</b>`` when a rate was known)."""
    if not used_text:
        return None
    line = f"💰 From balance: {escape_html(used_text)}"
    if left_text:
        line += f"{SEP}left to pay {share_value(left_text, converted_text)}"
    return line


def _share_and_balance(
    amount_text: str,
    converted_text: Optional[str],
    full_share_text: Optional[str],
    used_text: Optional[str],
    left_text: Optional[str],
) -> tuple[str, Optional[str]]:
    """``(share value, balance line)``: "Your share" is always the full share.

    When the balance covered part of it, ``amount_text`` (and its conversion)
    is what is left to pay; that goes into the balance line, so the push names
    the same share as the member card.
    """
    if used_text and full_share_text:
        return f"<b>{escape_html(full_share_text)}</b>", balance_line(used_text, left_text or amount_text, converted_text)
    return share_value(amount_text, converted_text), balance_line(used_text, left_text)


def reminder_text(
    *,
    subscription_name: str,
    amount_text: str,
    converted_text: Optional[str] = None,
    payment_label: str = "",
    payment_details: str = "",
    payment_link: str = "",
    comment: str = "",
    due_value: Optional[str] = None,
    tz_name: Optional[str] = None,
    today: Optional[date] = None,
    status_text: str = "",
    due_date_text: str = "",
    balance_used_text: Optional[str] = None,
    left_to_pay_text: Optional[str] = None,
    full_share_text: Optional[str] = None,
    copyable: bool = False,
    footer: Optional[str] = PAID_HINT,
    test: bool = False,
) -> str:
    """One bill: '🔔 <b>Netflix · due tomorrow</b>', the date, share, method, details and the hint."""
    status = _status_fact(status_text, due_value, today, tz_name)
    if due_value and _parse_due(due_value) is not None:
        date_html = tg_due(due_value, tz_name, "wD")
    else:
        date_html = escape_html(due_date_text)
    share, balance = _share_and_balance(
        amount_text, converted_text, full_share_text, balance_used_text, left_to_pay_text
    )
    body: list[Optional[str]] = [
        f"📅 {date_html}" if date_html else None,
        f"💵 Your share: {share}",
        f"🏦 {escape_html(payment_label)}" if payment_label else "🏦 Payment method: not set",
        *details_lines(payment_details, copyable=copyable),
        link_line(payment_link),
        comment_line(comment),
        balance,
    ]
    return join_blocks(reminder_title(subscription_name, status, test=test), body, footer)  # type: ignore[arg-type]


def batch_title(count: int, total_text: Optional[str]) -> str:
    """``'🔔 <b>2 bills due</b> · total <b>680.50 RUB</b>'`` (no total when a rate is missing)."""
    head = f"🔔 <b>{plural(count, 'bill')} due</b>"
    if total_text:
        head += f"{SEP}total <b>{escape_html(total_text)}</b>"
    return head


def batch_item_block(
    item: Mapping[str, object],
    *,
    details_shown_above: bool,
    budget: Optional[int] = None,
) -> list[str]:
    """``'<b>Netflix</b> · due tomorrow'`` plus indented share and method lines, then the details.

    ``budget`` (visible units) caps this bill's comment, requisites and link
    when the whole batch would not fit in one message.
    """
    due_value = str(item.get("due_value") or "") or None
    item_today = item.get("today")
    status = _status_fact(
        str(item.get("status_text") or ""),
        due_value,
        item_today if isinstance(item_today, date) else None,
        str(item.get("tz_name") or "") or None,
    )
    details = str(item.get("payment_details") or "").strip()
    label = str(item.get("payment_label") or "").strip()
    pay_to = escape_html(label) if label else "not set"
    if details_shown_above and details:
        pay_to += " (details above)"
    converted = item.get("converted_text")
    used = item.get("balance_used_text")
    left = item.get("left_to_pay_text")
    full = item.get("full_share_text")
    share, balance = _share_and_balance(
        str(item.get("amount_text") or ""),
        str(converted) if converted else None,
        str(full) if full else None,
        str(used) if used else None,
        str(left) if left else None,
    )
    lines: list[Optional[str]] = [
        f"<b>{escape_html(item.get('subscription_name') or '')}</b>{SEP}{status}",
        sub_line(f"Your share: {share}"),
        sub_line(f"Pay to: {pay_to}"),
    ]
    # The caller shrinks ``budget`` until the whole batch fits, so each part may use all of it.
    comment_budget = COMMENT_SHOWN_MAX if budget is None else min(max(budget, 40), COMMENT_SHOWN_MAX)
    details_budget = DETAILS_SHOWN_MAX if budget is None else min(max(budget, 40), DETAILS_SHOWN_MAX)
    link_budget = LINK_SHOWN_MAX if budget is None else min(max(budget // 4, 40), LINK_SHOWN_MAX)
    comment = comment_line(str(item.get("comment") or ""), comment_budget)
    folded_comment = bool(comment) and comment.startswith("<blockquote")  # type: ignore[union-attr]
    if comment and not folded_comment:
        lines.append(sub_line(comment))
    if balance:
        lines.append(sub_line(balance))
    if not details_shown_above:
        lines += details_lines(details, copyable=bool(item.get("copyable")), budget=details_budget)
    lines.append(link_line(str(item.get("payment_link") or ""), link_budget))
    if folded_comment:
        lines.append(comment)
    return [line for line in lines if line]


def _render_batch(
    items: Sequence[Mapping[str, object]],
    head: list[object],
    footer: Optional[str],
    item_budget: Optional[int],
) -> str:
    rendered: set[str] = set()
    blocks: list[object] = list(head)
    for item in items:
        details = str(item.get("payment_details") or "").strip()
        shown_above = bool(details) and details in rendered
        if details:
            rendered.add(details)
        blocks.append(batch_item_block(item, details_shown_above=shown_above, budget=item_budget))
    blocks.append(footer)
    return join_blocks(*blocks)  # type: ignore[arg-type]


def batch_reminder_text(
    items: Sequence[Mapping[str, object]],
    total_text: Optional[str],
    *,
    footer: Optional[str] = BATCH_HINT,
    part: Optional[tuple[int, int]] = None,
) -> str:
    """Several bills in one push; identical requisites are shown once.

    The push stays within ``PUSH_TEXT_BUDGET``: when the full text is too
    long, each bill's comment, requisites and link are cut to an equal share
    of the room. ``part`` ``(n, of)`` marks one of several pushes that
    ``batch_reminder_parts`` split a very long batch into; a part names no
    total (its bills are only some of them).
    """
    if part is None:
        head: list[object] = [batch_title(len(items), total_text)]
        if not total_text:
            head.append(TOTAL_UNKNOWN_LINE)
    else:
        head = [f"{batch_title(len(items), None)}{SEP}part {part[0]} of {part[1]}"]
    text = _render_batch(items, head, footer, None)
    if visible_len(text) <= PUSH_TEXT_BUDGET or not items:
        return text
    # Start from an equal share of the room and shrink until the push fits
    # (or the floor is reached; then ``batch_reminder_parts`` splits it).
    fixed = visible_len(_render_batch(items, head, footer, 0))
    item_budget = max((PUSH_TEXT_BUDGET - fixed) // len(items), BATCH_ITEM_MIN_BUDGET)
    while True:
        text = _render_batch(items, head, footer, item_budget)
        if visible_len(text) <= PUSH_TEXT_BUDGET or item_budget <= BATCH_ITEM_MIN_BUDGET:
            return text
        item_budget = max(int(item_budget * 0.8), BATCH_ITEM_MIN_BUDGET)


def batch_reminder_parts(
    items: Sequence[Mapping[str, object]],
    total_text: Optional[str],
    *,
    footer: Optional[str] = BATCH_HINT,
) -> list[tuple[list[Mapping[str, object]], str]]:
    """``[(bills, text), …]``: one push, or several when one cannot hold every bill.

    Each part carries only its own bills, so the caller gives each its own
    Paid buttons. Bills are kept in order and packed greedily.
    """
    whole = batch_reminder_text(items, total_text, footer=footer)
    if visible_len(whole) <= PUSH_TEXT_BUDGET or len(items) <= 1:
        return [(list(items), whole)]
    groups: list[list[Mapping[str, object]]] = [[]]
    for item in items:
        candidate = [*groups[-1], item]
        if groups[-1] and visible_len(batch_reminder_text(candidate, None, footer=footer, part=(1, 2))) > PUSH_TEXT_BUDGET:
            groups.append([item])
        else:
            groups[-1] = candidate
    if len(groups) == 1:
        return [(list(items), whole)]
    count = len(groups)
    return [
        (group, batch_reminder_text(group, None, footer=footer, part=(index, count)))
        for index, group in enumerate(groups, 1)
    ]


# ------------------------------------------------------------------ receipts


def _clock(moment: datetime, tz_name: Optional[str]) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(parse_timezone(tz_name))
    return tg_time(moment, "t", local.strftime("%H:%M"))


def paid_receipt_text(
    subscription_name: str,
    due_value: str,
    tz_name: Optional[str],
    amount_text: Optional[str],
    recorded_at: datetime,
) -> str:
    """'✅ <b>Paid · Netflix</b>' + '📅 <date> · <b>4.17 USD</b>' + 'Recorded <time>.'"""
    date_line = f"📅 {tg_due(due_value, tz_name, 'wD')}"
    if amount_text:
        date_line += f"{SEP}<b>{escape_html(amount_text)}</b>"
    return join_blocks(
        f"✅ <b>Paid{SEP}{escape_html(subscription_name)}</b>",
        [date_line, f"Recorded {_clock(recorded_at, tz_name)}."],
    )


def batch_receipt_text(total_text: Optional[str], paid_labels: Iterable[str] = ()) -> str:
    """'✅ <b>All paid</b> · 680.50 RUB' and one '✅ Netflix · 17.09' line per bill."""
    head = "✅ <b>All paid</b>"
    if total_text:
        head += f"{SEP}{escape_html(total_text)}"
    lines = [f"✅ {escape_html(label)}" for label in paid_labels if label]
    return join_blocks(head, lines)


def balance_receipt_text(
    subscription_name: str,
    due_value: str,
    tz_name: Optional[str],
    amount_text: str,
    balance_left_text: str,
) -> str:
    """'✅ <b>Netflix paid from your balance</b>' + the date, the share and what is left."""
    return join_blocks(
        f"✅ <b>{escape_html(subscription_name)} paid from your balance</b>",
        [
            f"📅 {tg_due(due_value, tz_name, 'wD')}{SEP}<b>{escape_html(amount_text)}</b>",
            f"Balance left: <b>{escape_html(balance_left_text)}</b>",
        ],
    )


# --------------------------------------------------------------- admin pushes


def person_link(name: str, telegram_id: int) -> str:
    return f'<a href="tg://user?id={int(telegram_id)}">{escape_html(name)}</a>'


def progress_line(paid: int, total: int) -> Optional[str]:
    """``'███░░░░░░░ 1 of 3 paid'``; nothing when the cycle has no participants."""
    if total <= 0:
        return None
    return f"{progress_bar(paid, total)} {paid} of {total} paid"


def cycle_closed_line(next_due: Optional[str], tz_name: Optional[str]) -> str:
    """``'🔒 Cycle closed — everyone paid. Next: <date>.'`` (appended to the payment push)."""
    line = "🔒 Cycle closed — everyone paid."
    if next_due and _parse_due(next_due) is not None:
        line += f" Next: {tg_due(next_due, tz_name, 'wD')}."
    return line


def payment_recorded_text(
    *,
    payer_name: str,
    payer_id: int,
    subscription_name: str,
    due_value: str,
    tz_name: Optional[str],
    amount_text: str,
    converted_text: Optional[str] = None,
    paid: int = 0,
    total: int = 0,
    closed: bool = False,
    next_due: Optional[str] = None,
) -> str:
    """'💸 <b>Ivan Petrov paid Netflix</b>' + the cycle, the amount and the progress bar."""
    amount = f"<b>{escape_html(amount_text)}</b>"
    if converted_text:
        amount += f" (≈ {escape_html(converted_text)})"
    lines: list[Optional[str]] = [
        f"📅 {tg_due(due_value, tz_name, 'wD')}{SEP}{amount}",
        progress_line(paid, total),
        cycle_closed_line(next_due, tz_name) if closed else None,
    ]
    title = f"💸 <b>{person_link(payer_name, payer_id)} paid {escape_html(subscription_name)}</b>"
    return join_blocks(title, lines)  # type: ignore[arg-type]


def cycle_closed_text(
    subscription_name: str,
    total_text: str,
    next_due: Optional[str],
    tz_name: Optional[str],
) -> str:
    """'🔒 <b>Netflix cycle closed</b>' + 'Everyone paid · total …' + 'Next charge: <date>'."""
    lines: list[str] = [f"Everyone paid{SEP}total <b>{escape_html(total_text)}</b>"]
    if next_due and _parse_due(next_due) is not None:
        lines.append(f"Next charge: {tg_due(next_due, tz_name, 'wD')}")
    return join_blocks(f"🔒 <b>{escape_html(subscription_name)} cycle closed</b>", lines)


def reminders_sent_text(
    subscription_name: str,
    due_value: str,
    tz_name: Optional[str],
    today: date,
    total_text: str,
    reminded: int,
    paid: int,
) -> str:
    """'📬 <b>Netflix reminders sent</b>' + 'Due <date> (tomorrow) · <b>12.50 USD</b>' + the counts."""
    due = _parse_due(due_value)
    relative = f" ({days_left_phrase((due - today).days)})" if due is not None else ""
    return join_blocks(
        f"📬 <b>{escape_html(subscription_name)} reminders sent</b>",
        [
            f"Due {tg_due(due_value, tz_name, 'wD')}{relative}{SEP}<b>{escape_html(total_text)}</b>",
            f"Reminded: <b>{int(reminded)}</b>{SEP}paid: {int(paid)}",
        ],
    )


def no_participants_text(
    subscription_name: str,
    due_value: str,
    tz_name: Optional[str],
    total_text: str,
) -> str:
    """'👥 <b>Netflix has no participants</b>' + 'Due <date> · 12.50 USD. Nobody got a reminder.'"""
    return join_blocks(
        f"👥 <b>{escape_html(subscription_name)} has no participants</b>",
        f"Due {tg_due(due_value, tz_name, 'wD')}{SEP}{escape_html(total_text)}. {NO_PARTICIPANTS_TAIL}",
    )
