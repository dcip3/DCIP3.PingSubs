"""Member 👤 Account (WP10): the account screen, the rename prompt and own-report bits.

👤 Account is a top-level screen (reply menu): no nav row. Opened as a NEW
message from a push (``public_account:show_new``) it ends with [✖️ Close].
The texts are pure builders over an ``AccountSnapshot`` so tests can pin them
without a database; ``account_snapshot`` reads the data.

This module imports only the keyboards, text and style layers (never
``app.ui.helpers``), so ``helpers`` can build its screens from it.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Dict, Optional, Sequence, Tuple

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.chat_action import ChatActionSender

from app.storage.db import Database
from app.ui.keyboards import payment_method_label, public_account_keyboard
from app.ui.style import title, with_banner
from app.ui.text import NAME_EXAMPLE, escape_html, format_money

ACCOUNT_TITLE = title("👤", "Account")
NOT_SET_UP_LINE = "Your profile is not set up yet. Ask the admin for your sign-in link."
NOT_SET_UP_TEXT = f"{ACCOUNT_TITLE}\n\n{NOT_SET_UP_LINE}"


def not_set_up_text(emoji: str, section: str) -> str:
    """A member section tapped by an unlinked account: the section title and the one line."""
    return f"{title(emoji, section)}\n\n{NOT_SET_UP_LINE}"

# The same message as a callback alert (no reply menu can travel with an alert).
NOT_SET_UP_ALERT = NOT_SET_UP_LINE
RENAME_NOT_TEXT_HINT = f"⚠️ Send text, for example {NAME_EXAMPLE}."
# A button on a message too old to answer under.
ACCOUNT_REOPEN_ALERT = "Open “👤 Account” again."
NO_SUBSCRIPTIONS = "none yet"
# The account lists this many subscription names, then 'and N more'.
SUBSCRIPTION_NAMES_LIMIT = 8

# ``origin_screen`` of the rename prompt: which account variant to redraw.
ACCOUNT_ORIGIN = "account"
ACCOUNT_NEW_ORIGIN = "account_new"

# Own 📊 Reports.
REPORTS_EMPTY_TEXT = f"{title('📊', 'Reports')}\n\nNothing to report yet."
# A '🔴 Netflix' button for a subscription the member has left since.
NO_LONGER_IN_SUBSCRIPTION = "You are no longer in this subscription"
SUBSCRIPTIONS_REOPEN_ALERT = "Open “📋 Subscriptions” again."

Screen = Tuple[str, InlineKeyboardMarkup]


@dataclass(frozen=True)
class AccountSnapshot:
    full_name: str
    balance: float
    currency: str
    # ``'Sber · RUB'`` (plain text), or None when no top-up method applies.
    method: Optional[str] = None
    # Submitted top-ups waiting for the admin, summed per currency: ``((1000.0, 'RUB'),)``.
    waiting: Tuple[Tuple[float, str], ...] = ()
    subscriptions: Tuple[str, ...] = ()


# --------------------------------------------------------------------- texts


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def subscriptions_line(names: Sequence[str]) -> str:
    """``'📋 Subscriptions: Netflix, Spotify'``; long lists end with 'and N more'."""
    cleaned = [escape_html(_clean(name)) for name in names if _clean(name)]
    if not cleaned:
        return f"📋 Subscriptions: {NO_SUBSCRIPTIONS}"
    shown = cleaned[:SUBSCRIPTION_NAMES_LIMIT]
    line = ", ".join(shown)
    if len(cleaned) > len(shown):
        line += f" and {len(cleaned) - len(shown)} more"
    return f"📋 Subscriptions: {line}"


def account_text(snapshot: AccountSnapshot, *, banner: Optional[str] = None) -> str:
    """The account card: the name as title, then balance, method, waiting top-ups, subscriptions.

    A zero balance is shown as it is, with no extra warning; lines that do not
    apply (no method, nothing waiting) are left out.
    """
    lines = [f"💰 Balance: {format_money(snapshot.balance, snapshot.currency, bold=True)}"]
    if snapshot.method:
        lines.append(f"🏦 Payment method: {escape_html(snapshot.method)}")
    if snapshot.waiting:
        amounts = " + ".join(format_money(amount, currency) for amount, currency in snapshot.waiting)
        lines.append(f"⏳ Waiting for approval: <b>{amounts}</b>")
    lines.append(subscriptions_line(snapshot.subscriptions))
    head = title("👤", escape_html(_clean(snapshot.full_name) or "Account"))
    return with_banner(banner, head + "\n\n" + "\n".join(lines))


def rename_prompt(full_name: str) -> str:
    """``'✏️ Your new display name? Now: Anna Petrova.'`` and who uses the name."""
    return (
        f"✏️ Your new display name? Now: {escape_html(_clean(full_name) or 'not set')}.\n"
        "The admin and reminders use this name."
    )


def renamed_banner(full_name: str) -> str:
    return f"✅ Renamed · <b>{escape_html(_clean(full_name))}</b>"


# --------------------------------------------------------------------- reading


def _balance_of(friend: Dict[str, object], default_currency: str) -> tuple[float, str]:
    try:
        amount = float(friend.get("balance") or 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        amount = 0.0
    currency = str(friend.get("balance_currency") or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        currency = default_currency
    return amount, currency


def _waiting_totals(requests: Sequence[Dict[str, object]], friend_id: int) -> Tuple[Tuple[float, str], ...]:
    totals: dict[str, float] = {}
    for request in requests:
        if int(request.get("friend_id") or 0) != friend_id:  # type: ignore[arg-type]
            continue
        try:
            amount = float(request.get("amount") or 0.0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        currency = str(request.get("currency") or "").strip().upper()
        totals[currency] = totals.get(currency, 0.0) + amount
    return tuple((amount, currency) for currency, amount in totals.items())


async def account_snapshot(db: Database, telegram_id: int) -> Optional[AccountSnapshot]:
    """The member's account data, or None when this Telegram account is not linked."""
    friend = await db.get_friend_by_telegram(telegram_id)
    if not friend:
        return None
    default_currency = str(await db.get_setting("target_currency") or "RUB").strip().upper() or "RUB"
    balance, currency = _balance_of(friend, default_currency)
    destination = await db.get_effective_payment_destination_for_user(telegram_id)
    default_id = await db.get_default_payment_destination_id()
    method = (
        payment_method_label(
            destination.get("title"),
            destination.get("currency"),
            default=default_id is not None and int(destination.get("id") or 0) == int(default_id),
        )
        if destination
        else None
    )
    waiting = _waiting_totals(await db.list_pending_topup_requests(), int(friend["id"]))
    subscriptions = tuple(str(sub.get("name") or "") for sub in await db.list_subscriptions_for_user(telegram_id))
    return AccountSnapshot(
        full_name=str(friend.get("full_name") or ""),
        balance=balance,
        currency=currency,
        method=method or None,
        waiting=waiting,
        subscriptions=subscriptions,
    )


async def account_screen(
    db: Database,
    telegram_id: int,
    *,
    banner: Optional[str] = None,
    close: bool = False,
) -> Optional[Screen]:
    """``(text, markup)`` of 👤 Account; ``close`` adds [✖️ Close] for a new message from a push."""
    snapshot = await account_snapshot(db, telegram_id)
    if snapshot is None:
        return None
    return account_text(snapshot, banner=banner), public_account_keyboard(close=close)


# ------------------------------------------------------------------- progress


@asynccontextmanager
async def typing_action(bot: Optional[Bot], chat_id: int) -> AsyncIterator[None]:
    """Show 'typing…' while a heavy reply-menu screen (a report) is built; a no-op without a bot."""
    if bot is None:
        yield
        return
    async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
        yield
