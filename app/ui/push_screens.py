"""Texts of the reminder pushes, the Paid receipts and the admin payment pushes (WP15).

Plain HTML. A push is a title line that states the fact, an empty line, then a
few short lines (principles: push-layout, screen-title-line). Dates are
tg-time entities with a full fallback in the reader's timezone. Money strings
come in already formatted (``format_money`` / ``format_converted_amount``) and
are escaped here like every other value.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from app.core.reminders import tg_due
from app.ui.style import SEP, join_blocks, progress_bar
from app.ui.text import escape_html


def _parse_due(due_value: object) -> Optional[date]:
    try:
        return datetime.strptime(str(due_value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


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
