from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot, F

from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardMarkup

from app.ui.keyboards import (
    MENU_HELP,
    admin_reply_keyboard,
    build_test_payment_confirmation_keyboard,
    guest_reply_keyboard,
    public_settings_keyboard,
    public_settings_time_keyboard,
    public_settings_timezone_keyboard,
    public_reply_keyboard,
    settings_notifications_keyboard,
    settings_rounding_keyboard,
    settings_time_keyboard,
    settings_timezone_keyboard,
)
from app.ui.helpers import (
    send_public_account_detail,
    send_public_subscription_detail,
    send_public_subscription_payment_report,
    send_public_user_payment_report,
    send_settings_tests_menu,
    send_subscription_list,
    send_test_user_list,
    send_user_subscription_list,
)
from app.ui.states import (
    PublicAccountForm,
    PublicReminderForm,
    PublicSubscriptionCurrencyForm,
    PublicSettingsForm,
    SettingsForm,
    CycleAction,
    SubscriptionAction,
    TestListAction,
    TestSendAction,
)
from app.core.config import Settings
from app.core.reminders import (
    DEFAULT_REMINDER_TIMEZONE,
    normalize_time_string,
    normalize_timezone_name,
    parse_timezone,
)
from app.storage.db import Database
from app.services import (
    CurrencyConverter,
    _build_reminder_message,
    _format_status_and_date,
    _send_message_with_retry,
    format_converted_amount,
)
from app.ui.text import validate_person_name
# The shell (WP1): /start, sign-in links, Help, Close/Cancel and identity errors.
from app.ui.helpers import answer_callback_once, delete_quietly
from app.ui.keyboards import MENU_ACCOUNT, MENU_REPORTS, MENU_SETTINGS, MENU_SUBSCRIPTIONS
from app.ui.prompts import PROMPT_MESSAGE_ID, cancel_prompt, reset_flow_state, wind_down
from app.ui.shell import (
    ADMIN_ACCOUNT_TEXT,
    FIRST_ADMIN_TEXT,
    JOIN_ALREADY_LINKED_TEXT,
    JOIN_FAILED_TEXT,
    JOIN_LINK_BROKEN_TEXT,
    JOIN_LINK_EXPIRED_TEXT,
    MEMBERS_ONLY_ALERT,
    NO_IDENTITY_TEXT,
    STRANGER_START_TEXT,
    admin_start_snapshot,
    admin_start_text,
    join_success_text,
    member_start_snapshot,
    member_start_text,
)
# Member settings (WP12).
from aiogram.types import InlineKeyboardMarkup
from app.ui.helpers import edit_in_place, respond_with_markup
from app.ui.keyboards import PUBLIC_TIME_RESET_CALLBACK, PUBLIC_TIMEZONE_RESET_CALLBACK, use_default_rows
from app.ui.member_settings import (
    SETTINGS_REOPEN_ALERT,
    TIME_NOT_TEXT_HINT,
    TIME_TYPO_HINT,
    TIMEZONE_NOT_TEXT_HINT,
    TIMEZONE_PROMPT,
    TIMEZONE_TYPO_HINT,
    member_settings_snapshot,
    member_settings_text,
    reminder_time_prompt,
    reminder_time_text,
    time_banner,
    time_toast,
    timezone_banner,
    timezone_text,
    timezone_toast,
)
from app.ui.prompts import ask, finish_prompt, note_typo, origin_of
from app.ui.style import toast_from_html, with_banner
from app.ui.text import is_default_word, parse_time_loose, parse_timezone_loose
# Member account, rename and own reports (WP10).
from app.ui.helpers import DEAD_BUTTON, SUBSCRIPTION_GONE
from app.ui.keyboards import (
    CLOSE_CALLBACK,
    PUBLIC_ACCOUNT_RENAME_CALLBACK,
    PUBLIC_ACCOUNT_SHOW_CALLBACK,
    PUBLIC_ACCOUNT_SHOW_NEW_CALLBACK,
)
from app.ui.member_account import (
    ACCOUNT_NEW_ORIGIN,
    ACCOUNT_ORIGIN,
    ACCOUNT_REOPEN_ALERT,
    NOT_SET_UP_ALERT as ACCOUNT_NOT_SET_UP_ALERT,
    NOT_SET_UP_TEXT as ACCOUNT_NOT_SET_UP_TEXT,
    RENAME_NOT_TEXT_HINT,
    SUBSCRIPTIONS_REOPEN_ALERT,
    account_screen,
    not_set_up_text,
    rename_prompt,
    renamed_banner,
)
from app.ui.prompts import ORIGIN_SCREEN
# Member subscriptions (WP9).
from aiogram.fsm.state import State
from aiogram.types import InlineKeyboardButton
from app.ui.guards import guarded, pay_cycle_key
from app.ui.helpers import CYCLE_CLOSED, NO_ACCESS, PROMPT_OUT_OF_DATE_TEXT, SUBSCRIPTION_GONE_BANNER, stale_redirect
from app.ui.keyboards import MEMBER_CYCLE_PAID_ACTION, PUBLIC_CARD_CLOSE_ACTION, USE_DEFAULT_TEXT
from app.ui.member_subscriptions import (
    ALREADY_PAID_TOAST,
    CARD_UPDATED_TOAST,
    CARD_NEW_ORIGIN as MEMBER_CARD_NEW_ORIGIN,
    CARD_ORIGIN as MEMBER_CARD_ORIGIN,
    CURRENCY_NOT_TEXT_HINT,
    CURRENCY_TYPO_HINT,
    currency_prompt as member_currency_prompt,
    currency_screen as member_currency_screen,
    default_shown_in as member_default_shown_in,
    is_reset_word as member_is_reset_word,
    member_access,
    member_card_screen,
    opened_as_new,
    paid_banner,
    reminder_prompt as member_reminder_prompt,
    reminder_screen as member_reminder_screen,
    reminder_times as member_reminder_times,
    shown_in_banner,
    shown_in_toast,
    unpaid_dues,
    viewer_timezone,
)
from app.ui.text import parse_currency
# Admin settings and the test-reminder picker (WP11).
from typing import Any
from app.ui.admin_settings import (
    NOTIFICATIONS_SCREEN_TEXT,
    NOTIFY_KEYS as ADMIN_NOTIFY_KEYS,
    ROUNDING_MODES,
    TIMEZONE_PROMPT as ADMIN_TIMEZONE_PROMPT,
    admin_settings_screen,
    admin_settings_snapshot,
    notification_toast,
    reminder_time_text as admin_reminder_time_text,
    rounding_text as admin_rounding_text,
    rounding_banner,
    rounding_toast,
    shown_page as admin_tests_shown_page,
    test_failed_alert as admin_test_failed_alert,
    test_page_of as admin_test_page_of,
    test_people as admin_test_people,
    test_sent_banner as admin_test_sent_banner,
    time_prompt as admin_time_prompt,
    timezone_text as admin_timezone_text,
)
from app.ui.helpers import USER_GONE

from . import admin_router, public_router

logger = logging.getLogger(__name__)

TEST_REMINDER_CURRENCY_FALLBACKS = ("RUB", "USD", "EUR", "GBP")


def _extract_start_payload(message: Message) -> str | None:
    text = (message.text or "").strip()
    if not text.startswith("/start"):
        return None
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return None
    payload = parts[1].strip()
    return payload or None


# ------------------------------------------------- member settings (WP12)
#
# ⚙️ Settings is a top-level screen (no nav row). Its pickers edit the same
# message and return to it with a toast; the ✏️ prompts edit the screen they
# came from into ⚙️ Settings with a result banner.

_PUBLIC_SETTINGS_PROMPT_STATES = frozenset(
    {PublicSettingsForm.base_time.state, PublicSettingsForm.base_timezone.state}
)
PUBLIC_SETTINGS_ORIGIN = "public_settings"


async def _public_settings_screen(
    db: Database,
    settings: Settings,
    telegram_id: int,
    *,
    banner: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    snapshot = await member_settings_snapshot(db, settings, telegram_id)
    return with_banner(banner, member_settings_text(snapshot)), public_settings_keyboard()


async def _show_public_settings_menu(
    target: CallbackQuery | Message,
    db: Database,
    settings: Settings,
    telegram_id: int,
    *,
    toast: str | None = None,
    banner: str | None = None,
) -> None:
    """Edit the tapped message into ⚙️ Settings (a typed tap sends it), answering once."""
    text, markup = await _public_settings_screen(db, settings, telegram_id, banner=banner)
    await respond_with_markup(target, text, reply_markup=markup, toast=toast)


async def _show_public_settings_on_origin(
    bot: Bot,
    chat_id: int,
    db: Database,
    settings: Settings,
    telegram_id: int,
    flow_data: dict,
    *,
    banner: str,
) -> None:
    """A prompt finished: the screen it came from becomes ⚙️ Settings with ``banner``.

    When that screen is gone (or unknown, e.g. after a restart), ⚙️ Settings
    is sent as a new message instead.
    """
    text, markup = await _public_settings_screen(db, settings, telegram_id, banner=banner)
    origin = origin_of(flow_data)
    if origin is not None and await edit_in_place(bot, origin[0], origin[1], text, markup):
        return
    await bot.send_message(chat_id, text, reply_markup=markup)


async def _public_settings_picked(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
    *,
    toast: str,
    banner: str,
) -> None:
    """A preset or Default was saved: back to ⚙️ Settings with ``toast``.

    A settings prompt still pending is wound down, so later text is not taken
    as its answer. When the tap was [↺ Use default] on that prompt, the
    prompt goes and the screen it came from shows ⚙️ Settings with ``banner``.
    """
    message = callback.message
    bot = callback.bot
    if bot is not None and message is not None and await state.get_state() in _PUBLIC_SETTINGS_PROMPT_STATES:
        on_prompt = (await state.get_data()).get(PROMPT_MESSAGE_ID) == message.message_id
        old_data = await reset_flow_state(state)
        await wind_down(bot, message.chat.id, old_data)
        if on_prompt:
            await _show_public_settings_on_origin(
                bot, message.chat.id, db, settings, callback.from_user.id, old_data, banner=banner
            )
            await answer_callback_once(callback, toast_from_html(banner))
            return
    # A preset from the picker: ⚙️ Settings with the same result banner as typed input.
    await _show_public_settings_menu(callback, db, settings, callback.from_user.id, toast=toast, banner=banner)


async def _start_reply(message: Message, text: str, markup: ReplyKeyboardMarkup) -> None:
    await message.answer(text, reply_markup=markup)


async def _send_admin_start(message: Message, db: Database, *, invite_note: bool = False) -> None:
    snapshot = await admin_start_snapshot(db)
    await _start_reply(message, admin_start_text(snapshot, invite_note=invite_note), admin_reply_keyboard())


async def _send_member_start(message: Message, db: Database, telegram_id: int) -> None:
    snapshot = await member_start_snapshot(db, telegram_id)
    await _start_reply(message, member_start_text(snapshot), public_reply_keyboard())


def _invite_expired(friend: dict) -> bool:
    raw = str(friend.get("invite_expires_at") or "").strip()
    if not raw:
        return False
    try:
        expires_at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
    return expires_at < datetime.now(timezone.utc).replace(tzinfo=None)


async def _handle_join(message: Message, db: Database, user_id: int, token: str) -> None:
    """``/start join_<token>``: link this Telegram account to the invited user.

    A sender who is already a linked member always keeps the member menu: the
    reduced guest menu is only for people without access.
    """
    guest_menu = guest_reply_keyboard()
    existing_friend = await db.get_friend_by_telegram(user_id)
    invited_friend = await db.get_friend_by_invite_token(token)
    if not invited_friend:
        if existing_friend:
            # Their own link again: claiming it cleared the token, so it is not
            # found any more. They are already in; greet them as a member.
            await _send_member_start(message, db, user_id)
            return
        await _start_reply(message, JOIN_LINK_BROKEN_TEXT, guest_menu)
        return
    invited_id = int(invited_friend["id"])
    if existing_friend and int(existing_friend["id"]) == invited_id:
        # Their own link again: they are already in.
        subs = await db.list_subscriptions_for_user(user_id)
        text = join_success_text(str(existing_friend.get("full_name") or ""), [str(sub["name"]) for sub in subs])
        await _start_reply(message, text, public_reply_keyboard())
        return
    if existing_friend:
        # Someone else's link: this account stays linked as it is.
        await _start_reply(message, JOIN_ALREADY_LINKED_TEXT, public_reply_keyboard())
        return
    if _invite_expired(invited_friend):
        await _start_reply(message, JOIN_LINK_EXPIRED_TEXT, guest_menu)
        return
    if invited_friend.get("telegram_id") is not None:
        await _start_reply(message, JOIN_LINK_BROKEN_TEXT, guest_menu)
        return
    claimed = await db.claim_friend_invite(
        invited_id,
        user_id,
        datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat(),
    )
    if not claimed:
        await _start_reply(message, JOIN_FAILED_TEXT, guest_menu)
        return
    subs = await db.list_subscriptions_for_user(user_id)
    text = join_success_text(str(invited_friend.get("full_name") or ""), [str(sub["name"]) for sub in subs])
    await _start_reply(message, text, public_reply_keyboard())


@public_router.message(Command("start"))
async def handle_start(message: Message, db: Database, state: FSMContext, bot: Bot) -> None:
    """/start: drop any pending input, restore the right menu, and say what to do next."""
    # The menu-reset middleware already did this when a state was set; doing it
    # again is harmless and covers a /start that reached here some other way.
    old_data = await reset_flow_state(state)
    await wind_down(bot, message.chat.id, old_data)

    if not message.from_user:
        await _start_reply(message, STRANGER_START_TEXT, guest_reply_keyboard())
        return

    user_id = message.from_user.id
    start_payload = _extract_start_payload(message)
    is_join = bool(start_payload and start_payload.startswith("join_"))

    if await db.is_admin(user_id):
        await _send_admin_start(message, db, invite_note=is_join)
        return

    if is_join:
        await _handle_join(message, db, user_id, str(start_payload))
        return

    if not await db.has_admins():
        admin_name, _ = validate_person_name(message.from_user.full_name)
        claimed = await db.ensure_first_admin(user_id, admin_name or "Admin")
        if claimed or await db.is_admin(user_id):
            await _start_reply(message, FIRST_ADMIN_TEXT, admin_reply_keyboard())
            return

    if await db.get_friend_by_telegram(user_id):
        await _send_member_start(message, db, user_id)
        return
    await _start_reply(message, STRANGER_START_TEXT, guest_reply_keyboard())


HELP_ROLE_ADMIN = "admin"
HELP_ROLE_MEMBER = "member"
HELP_ROLE_GUEST = "guest"

_HELP_TEXTS = {
    HELP_ROLE_ADMIN: (
        "❓ <b>Help</b>\n\n"
        "👥 Users — people, balances, sign-in links\n"
        "📋 Subscriptions — plans, participants, reminders\n"
        "💳 Payment — payment methods and top-up approvals\n"
        "📊 Reports — who has paid\n"
        "⚙️ Settings — reminder time, timezone, admin notifications\n\n"
        "Start a fresh screen any time with /start."
    ),
    HELP_ROLE_MEMBER: (
        "❓ <b>Help</b>\n\n"
        "👤 Account — balance; “👤 Account” → “➕ Top up” to add money\n"
        "📋 Subscriptions — your share, how to pay, “✅ Paid”\n"
        "📊 Reports — what is paid and what is due\n"
        "⚙️ Settings — your reminder time and timezone\n\n"
        "Tap “✅ Paid” under a reminder once you have paid."
    ),
    HELP_ROLE_GUEST: (
        "❓ <b>Help</b>\n\n"
        "This bot reminds a group about shared subscriptions.\n"
        "Ask the admin for your sign-in link."
    ),
}


def help_text(role: str) -> str:
    """The one help screen for an admin, a linked member or a guest."""
    return _HELP_TEXTS.get(role, _HELP_TEXTS[HELP_ROLE_GUEST])


def help_reply_keyboard(role: str) -> ReplyKeyboardMarkup:
    if role == HELP_ROLE_ADMIN:
        return admin_reply_keyboard()
    if role == HELP_ROLE_MEMBER:
        return public_reply_keyboard()
    return guest_reply_keyboard()


async def _help_role(db: Database, telegram_id: int | None) -> str:
    if telegram_id is None:
        return HELP_ROLE_GUEST
    if await db.is_admin(telegram_id):
        return HELP_ROLE_ADMIN
    if await db.get_friend_by_telegram(telegram_id):
        return HELP_ROLE_MEMBER
    return HELP_ROLE_GUEST


@public_router.message(Command("help"))
@public_router.message(F.text == MENU_HELP)
async def handle_public_help(message: Message, db: Database) -> None:
    role = await _help_role(db, message.from_user.id if message.from_user else None)
    await message.answer(help_text(role), reply_markup=help_reply_keyboard(role))


async def _answer_no_identity(message: Message) -> None:
    """No readable sender: say so, and attach the menu that needs no account."""
    await message.answer(NO_IDENTITY_TEXT, reply_markup=guest_reply_keyboard())


async def _answered_stranger(message: Message, db: Database, emoji: str, section: str) -> bool:
    """A member section tapped from an old menu by an unlinked account.

    Says the profile is not set up and swaps the stale menu for the guest one.
    Returns True when the tap was answered here.
    """
    if await db.get_friend_by_telegram(message.from_user.id if message.from_user else None):
        return False
    await message.answer(not_set_up_text(emoji, section), reply_markup=guest_reply_keyboard())
    return True


# The reply menu. public_router runs first and serves admins and members alike
# (branching on db.is_admin), so the admin_router copies of these labels never
# fire; they are kept only for reading. Never send public_reply_keyboard to an admin.
@public_router.message(F.text == MENU_SUBSCRIPTIONS)
async def handle_public_subscriptions(message: Message, db: Database) -> None:
    if not message.from_user:
        await _answer_no_identity(message)
        return
    if await db.is_admin(message.from_user.id):
        await send_subscription_list(message, db)
        return
    if await _answered_stranger(message, db, "📋", "Subscriptions"):
        return
    await send_user_subscription_list(message, db, message.from_user.id)


@public_router.message(F.text == MENU_ACCOUNT)
async def handle_public_account(message: Message, db: Database) -> None:
    if not message.from_user:
        await _answer_no_identity(message)
        return
    if await db.is_admin(message.from_user.id):
        # An admin with a stale member menu: point at the right menu and send it.
        await message.answer(ADMIN_ACCOUNT_TEXT, reply_markup=admin_reply_keyboard())
        return
    await send_public_account_detail(message, db, message.from_user.id)


@public_router.callback_query(F.data == PUBLIC_ACCOUNT_SHOW_CALLBACK)
async def handle_public_account_show(callback: CallbackQuery, db: Database) -> None:
    """Edit the tapped message back into 👤 Account (the top-up picker's Back)."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    if await db.is_admin(callback.from_user.id):
        await answer_callback_once(callback, MEMBERS_ONLY_ALERT, alert=True)
        return
    await send_public_account_detail(callback, db, callback.from_user.id)


@public_router.callback_query(F.data == PUBLIC_ACCOUNT_SHOW_NEW_CALLBACK)
async def handle_public_account_show_new(callback: CallbackQuery, db: Database) -> None:
    """[👤 Account] under a push: 👤 Account as a NEW message ending in [✖️ Close].

    The push itself is left as it is.
    """
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    if await db.is_admin(callback.from_user.id):
        await answer_callback_once(callback, MEMBERS_ONLY_ALERT, alert=True)
        return
    screen = await account_screen(db, callback.from_user.id, close=True)
    if screen is None:
        await answer_callback_once(callback, ACCOUNT_NOT_SET_UP_ALERT, alert=True)
        return
    text, markup = screen
    chat_id = callback.message.chat.id if callback.message is not None else callback.from_user.id
    await callback.bot.send_message(chat_id, text, reply_markup=markup)
    await answer_callback_once(callback)


@public_router.callback_query(F.data == PUBLIC_ACCOUNT_RENAME_CALLBACK)
async def handle_public_account_rename_start(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    """✏️ Rename: a prompt; the account message it was tapped on is the screen to redraw."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    if await db.is_admin(callback.from_user.id):
        await answer_callback_once(callback, MEMBERS_ONLY_ALERT, alert=True)
        return
    friend = await db.get_friend_by_telegram(callback.from_user.id)
    if not friend:
        await answer_callback_once(callback, ACCOUNT_NOT_SET_UP_ALERT, alert=True)
        return
    if not isinstance(callback.message, Message):
        # Too old to answer under: the account has to be opened again.
        await answer_callback_once(callback, ACCOUNT_REOPEN_ALERT, alert=True)
        return
    await ask(
        callback,
        state,
        PublicAccountForm.full_name,
        rename_prompt(str(friend.get("full_name") or "")),
        origin_screen=ACCOUNT_NEW_ORIGIN if _has_close_button(callback.message) else ACCOUNT_ORIGIN,
    )


def _has_close_button(message: Message) -> bool:
    """True for the 👤 Account copy opened from a push (it ends with [✖️ Close])."""
    markup = message.reply_markup
    if not isinstance(markup, InlineKeyboardMarkup):
        return False
    return any(button.callback_data == CLOSE_CALLBACK for row in markup.inline_keyboard for button in row)


@public_router.message(F.text.in_({MENU_REPORTS, "📊 Payments report"}))
async def handle_public_payments_report(message: Message, db: Database) -> None:
    if not message.from_user:
        await _answer_no_identity(message)
        return
    if await db.is_admin(message.from_user.id):
        await handle_payments_report(message, db)
        return
    if await _answered_stranger(message, db, "📊", "Reports"):
        return
    await send_public_user_payment_report(message, db, message.from_user.id)


# The '🔴 Netflix' buttons under the member's own report (WP10) and under pushes
# open the member card (WP9) as a NEW message ending in [✖️ Close].
@public_router.callback_query(SubscriptionAction.filter(F.action == "public_open_new"))
async def handle_public_subscription_open_new(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    """The member card as a new message; the message the button sits on is left alone."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    viewer_id = callback.from_user.id
    subscription_id = callback_data.subscription_id
    subscription, problem = await member_access(db, subscription_id, viewer_id)
    if subscription is None:
        await answer_callback_once(callback, problem)
        return
    screen = await member_card_screen(db, subscription_id, viewer_id, close=True, converter=converter)
    if screen is None or callback.bot is None:
        await answer_callback_once(callback, SUBSCRIPTION_GONE)
        return
    text, markup = screen
    chat_id = callback.message.chat.id if callback.message is not None else viewer_id
    await callback.bot.send_message(chat_id, text, reply_markup=markup)
    await answer_callback_once(callback)


@public_router.message(F.text == MENU_SETTINGS)
async def handle_public_settings(
    message: Message,
    db: Database,
    settings: Settings,
) -> None:
    if not message.from_user:
        await _answer_no_identity(message)
        return
    if await db.is_admin(message.from_user.id):
        await _show_admin_settings(message, db, settings)
        return
    if await _answered_stranger(message, db, "⚙️", "Settings"):
        return
    await _show_public_settings_menu(message, db, settings, message.from_user.id)


@public_router.callback_query(F.data == "public_settings:menu")
async def handle_public_settings_menu_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    """[◀️ Back] from a picker: ⚙️ Settings again; a ✏️ prompt still open is wound down."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    message = callback.message
    if await state.get_state() is not None and message is not None and callback.bot is not None:
        old_data = await reset_flow_state(state)
        await wind_down(callback.bot, message.chat.id, old_data, skip_message_id=message.message_id)
    if await db.is_admin(callback.from_user.id):
        await _show_admin_settings(callback, db, settings)
        return
    await _show_public_settings_menu(callback, db, settings, callback.from_user.id)


@public_router.callback_query(F.data == "public_settings:time")
async def handle_public_settings_time(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    """⏰ Reminder time: the picker, edited over ⚙️ Settings."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    snapshot = await member_settings_snapshot(db, settings, callback.from_user.id)
    await respond_with_markup(
        callback,
        reminder_time_text(snapshot),
        reply_markup=public_settings_time_keyboard(
            snapshot.time,
            snapshot.default_time,
            has_override=snapshot.has_time_override,
        ),
    )


@public_router.callback_query(F.data == "public_settings:time_other")
async def handle_public_settings_time_other(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    """✏️ Other time: a prompt with [↺ Use default]; the picker is the screen to redraw."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    if not isinstance(callback.message, Message):
        await answer_callback_once(callback, SETTINGS_REOPEN_ALERT, alert=True)
        return
    snapshot = await member_settings_snapshot(db, settings, callback.from_user.id)
    await ask(
        callback,
        state,
        PublicSettingsForm.base_time,
        reminder_time_prompt(snapshot.timezone),
        origin_screen=PUBLIC_SETTINGS_ORIGIN,
        extra_rows=use_default_rows(PUBLIC_TIME_RESET_CALLBACK),
    )


@public_router.callback_query(F.data.startswith("public_settings_time:"))
async def handle_public_settings_time_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    raw_value = callback.data.split(":", 1)[1].strip()
    normalized = normalize_time_string(raw_value)
    if normalized is None:
        await _show_public_settings_menu(callback, db, settings, callback.from_user.id, toast=DEAD_BUTTON)
        return
    await db.set_user_setting(callback.from_user.id, "base_reminder_time", normalized)
    await _public_settings_picked(
        callback, state, db, settings, toast=time_toast(normalized), banner=time_banner(normalized)
    )


@public_router.callback_query(F.data == "public_settings:time_reset")
async def handle_public_settings_time_reset(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    """Default (picker) or [↺ Use default] (prompt): drop the member's own time."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    await db.delete_user_setting(callback.from_user.id, "base_reminder_time")
    snapshot = await member_settings_snapshot(db, settings, callback.from_user.id)
    await _public_settings_picked(
        callback,
        state,
        db,
        settings,
        toast=time_toast(None),
        banner=time_banner(snapshot.time, default=True),
    )


@public_router.callback_query(F.data == "public_settings:timezone")
async def handle_public_settings_timezone(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    """🌍 Timezone: the picker, edited over ⚙️ Settings."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    snapshot = await member_settings_snapshot(db, settings, callback.from_user.id)
    await respond_with_markup(
        callback,
        timezone_text(snapshot),
        reply_markup=public_settings_timezone_keyboard(
            snapshot.timezone,
            snapshot.default_timezone,
            has_override=snapshot.has_timezone_override,
        ),
    )


@public_router.callback_query(F.data == "public_settings:timezone_other")
async def handle_public_settings_timezone_other(callback: CallbackQuery, state: FSMContext) -> None:
    """✏️ Other timezone: a prompt with [↺ Use default]; the picker is the screen to redraw."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    if not isinstance(callback.message, Message):
        await answer_callback_once(callback, SETTINGS_REOPEN_ALERT, alert=True)
        return
    await ask(
        callback,
        state,
        PublicSettingsForm.base_timezone,
        TIMEZONE_PROMPT,
        origin_screen=PUBLIC_SETTINGS_ORIGIN,
        extra_rows=use_default_rows(PUBLIC_TIMEZONE_RESET_CALLBACK),
    )


@public_router.callback_query(F.data.startswith("public_settings_timezone:"))
async def handle_public_settings_timezone_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    raw_value = callback.data.split(":", 1)[1].strip()
    normalized = normalize_timezone_name(raw_value)
    if normalized is None:
        await _show_public_settings_menu(callback, db, settings, callback.from_user.id, toast=DEAD_BUTTON)
        return
    await db.set_user_setting(callback.from_user.id, "timezone", normalized)
    await _public_settings_picked(
        callback, state, db, settings, toast=timezone_toast(normalized), banner=timezone_banner(normalized)
    )


@public_router.callback_query(F.data == "public_settings:timezone_reset")
async def handle_public_settings_timezone_reset(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    """Default (picker) or [↺ Use default] (prompt): drop the member's own timezone."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    await db.delete_user_setting(callback.from_user.id, "timezone")
    snapshot = await member_settings_snapshot(db, settings, callback.from_user.id)
    await _public_settings_picked(
        callback,
        state,
        db,
        settings,
        toast=timezone_toast(None),
        banner=timezone_banner(snapshot.timezone, default=True),
    )


# ------------------------------------------------- member subscriptions (WP9)
#
# 📋 Subscriptions (member) is top-level; the card returns to it with
# [◀️ To list], or ends with [✖️ Close] when opened as a new message. The
# pickers edit the card's message and return to it with a toast; the ✏️
# prompts edit the screen they came from into the card with a banner.

_MEMBER_CARD_PROMPT_STATES = frozenset(
    {PublicReminderForm.reminder_time.state, PublicSubscriptionCurrencyForm.currency.state}
)
MEMBER_CARD_SUBSCRIPTION_ID = "subscription_id"


async def _member_list_redraw(
    target: CallbackQuery | Message,
    db: Database,
    viewer_id: int,
    *,
    banner: str | None = None,
) -> None:
    """The list the member card came from: the admin's own list for an admin, else the member's."""
    if await db.is_admin(viewer_id):
        await send_subscription_list(target, db, banner=SUBSCRIPTION_GONE_BANNER if banner else None)
        return
    await send_user_subscription_list(target, db, viewer_id, banner=banner)


async def _member_gate(callback: CallbackQuery, db: Database, subscription_id: int) -> dict | None:
    """The subscription behind a member-card button, or None after telling the viewer why not.

    Deleted or left: the list is redrawn with the toast ('Subscription was
    deleted' / 'You are no longer in this subscription'). On a copy opened
    from a push or report (it has [✖️ Close]) only the toast is shown.
    """
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return None
    viewer_id = callback.from_user.id
    subscription, problem = await member_access(db, subscription_id, viewer_id)
    if subscription is not None:
        return subscription
    if not isinstance(callback.message, Message) or opened_as_new(callback.message):
        await answer_callback_once(callback, problem)
        return None
    await stale_redirect(callback, problem or NO_ACCESS, lambda: _member_list_redraw(callback, db, viewer_id))
    return None


def _subscription_id_from(data: str | None) -> int | None:
    """``public_sub_currency_other:12`` / ``public_sub_currency:12:USD`` -> 12."""
    parts = (data or "").split(":")
    try:
        return int(parts[1])
    except (IndexError, ValueError):
        return None


async def _show_member_card_on_origin(
    bot: Bot,
    chat_id: int,
    db: Database,
    subscription_id: int,
    viewer_id: int,
    flow_data: dict,
    *,
    banner: str,
    converter: CurrencyConverter | None,
    fallback: Message | None = None,
) -> None:
    """A card prompt finished: the screen it came from becomes the card with ``banner``.

    When that screen is gone (or unknown after a restart) the card is sent as
    a new message; a deleted subscription sends the list instead.
    """
    close = flow_data.get(ORIGIN_SCREEN) == MEMBER_CARD_NEW_ORIGIN
    screen = await member_card_screen(db, subscription_id, viewer_id, banner=banner, close=close, converter=converter)
    if screen is None:
        if fallback is not None:
            await _member_list_redraw(fallback, db, viewer_id, banner=f"⚠️ {SUBSCRIPTION_GONE}")
        return
    text, markup = screen
    origin = origin_of(flow_data)
    if origin is not None and await edit_in_place(bot, origin[0], origin[1], text, markup):
        return
    await bot.send_message(chat_id, text, reply_markup=markup)


async def _member_card_picked(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    subscription_id: int,
    converter: CurrencyConverter | None,
    *,
    toast: str,
    banner: str,
) -> None:
    """A preset or Default was saved: back to the card with ``toast``.

    A card prompt still pending is wound down, so later text is not taken as
    its answer. When the tap was [↺ Use default] on that prompt, the prompt
    goes and the screen it came from becomes the card with ``banner``.
    """
    message = callback.message
    bot = callback.bot
    viewer_id = callback.from_user.id
    if bot is not None and message is not None and await state.get_state() in _MEMBER_CARD_PROMPT_STATES:
        on_prompt = (await state.get_data()).get(PROMPT_MESSAGE_ID) == message.message_id
        old_data = await reset_flow_state(state)
        await wind_down(bot, message.chat.id, old_data)
        if on_prompt:
            await _show_member_card_on_origin(
                bot,
                message.chat.id,
                db,
                subscription_id,
                viewer_id,
                old_data,
                banner=banner,
                converter=converter,
                fallback=message if isinstance(message, Message) else None,
            )
            await answer_callback_once(callback, toast_from_html(banner))
            return
    # A preset from the picker: the card with the same result banner as typed input.
    await send_public_subscription_detail(
        callback,
        db,
        subscription_id,
        banner=banner,
        toast=toast,
        close=opened_as_new(message),
        converter=converter,
    )


async def _ask_for_member_card(
    callback: CallbackQuery,
    state: FSMContext,
    new_state: State,
    text: str,
    subscription_id: int,
    reset_callback: str,
) -> None:
    """A ✏️ prompt with [↺ Use default] above ✖️ Cancel; the tapped picker is the screen to redraw."""
    if not isinstance(callback.message, Message):
        await answer_callback_once(callback, SUBSCRIPTIONS_REOPEN_ALERT, alert=True)
        return
    await ask(
        callback,
        state,
        new_state,
        text,
        origin_screen=MEMBER_CARD_NEW_ORIGIN if opened_as_new(callback.message) else MEMBER_CARD_ORIGIN,
        extra_rows=[[InlineKeyboardButton(text=USE_DEFAULT_TEXT, callback_data=reset_callback)]],
        data={MEMBER_CARD_SUBSCRIPTION_ID: subscription_id},
    )


@public_router.callback_query(SubscriptionAction.filter(F.action == "open_public"))
async def handle_public_subscription_open(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    """The member card, edited over the list (or over its own sub-screen: the way back)."""
    if await _member_gate(callback, db, callback_data.subscription_id) is None:
        return
    await send_public_subscription_detail(callback, db, callback_data.subscription_id, converter=converter)


@public_router.callback_query(SubscriptionAction.filter(F.action == PUBLIC_CARD_CLOSE_ACTION))
async def handle_public_subscription_card_close(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    """The way back to a card opened as a new message: the card again, still ending in [✖️ Close]."""
    if await _member_gate(callback, db, callback_data.subscription_id) is None:
        return
    await send_public_subscription_detail(
        callback, db, callback_data.subscription_id, close=True, converter=converter
    )


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_back"))
async def handle_public_subscription_back(
    callback: CallbackQuery,
    db: Database,
) -> None:
    """[◀️ To list]: edit the card back into the list (an admin gets the admin list)."""
    if not callback.from_user:
        await answer_callback_once(callback, NO_IDENTITY_TEXT, alert=True)
        return
    await _member_list_redraw(callback, db, callback.from_user.id)


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_paid"))
async def handle_public_subscription_paid_legacy(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    """[✅ Paid] on a card sent before the button carried its due date.

    That callback does not say which cycle the label meant, so it records
    nothing: the card is redrawn with the current state (and, when a cycle is
    unpaid, a fresh [✅ Paid · date] that names it).
    """
    subscription_id = callback_data.subscription_id
    if await _member_gate(callback, db, subscription_id) is None:
        return
    await send_public_subscription_detail(
        callback,
        db,
        subscription_id,
        toast=CARD_UPDATED_TOAST,
        close=opened_as_new(callback.message),
        converter=converter,
    )


@public_router.callback_query(CycleAction.filter(F.action == MEMBER_CYCLE_PAID_ACTION))
async def handle_public_subscription_paid(
    callback: CallbackQuery,
    callback_data: CycleAction,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    """[✅ Paid · 10.10]: pay exactly the cycle on the label, redraw the card with '✅ Paid · Netflix <date>'.

    Guarded by the same key as the reminder's ✅ Paid, so taps on both pay
    once. Inside the guard the cycle is checked again: already paid or
    closed meanwhile (an old card, a Paid from the reminder) only redraws
    the card with 'Already paid' / 'This cycle is already closed'.
    """
    from app.handlers.reminders import notify_admins_about_payment, record_cycle_payment

    subscription_id = callback_data.subscription_id
    due_value = callback_data.due_date
    subscription = await _member_gate(callback, db, subscription_id)
    if subscription is None:
        return
    viewer_id = callback.from_user.id
    close = opened_as_new(callback.message)
    payment = None
    async with guarded(callback, pay_cycle_key(subscription_id, due_value, viewer_id)) as acquired:
        if not acquired:
            return
        tz_name = await viewer_timezone(db, viewer_id)
        today = datetime.now(parse_timezone(tz_name)).date()
        participants = await db.list_subscription_participants(subscription_id)
        dues = await unpaid_dues(db, subscription, participants, viewer_id, today)
        if due_value not in dues:
            if due_value not in await db.list_open_cycles(subscription_id):
                toast = CYCLE_CLOSED
            else:
                toast = ALREADY_PAID_TOAST
            await send_public_subscription_detail(
                callback, db, subscription_id, toast=toast, close=close, converter=converter
            )
            return
        payment = await record_cycle_payment(db, subscription, due_value, viewer_id)
        banner = paid_banner(str(subscription.get("name") or ""), due_value, tz_name)
        await send_public_subscription_detail(
            callback,
            db,
            subscription_id,
            banner=banner,
            toast=toast_from_html(banner),
            close=close,
            converter=converter,
        )
    if payment is not None and callback.bot is not None:
        await notify_admins_about_payment(callback.bot, db, payment, viewer_id, callback.from_user.full_name)


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_report"))
async def handle_public_subscription_report(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    """'📊 <b>Reports · Netflix</b>' over the card, with [◀️ To card] (admins may open it too)."""
    if await _member_gate(callback, db, callback_data.subscription_id) is None:
        return
    await send_public_subscription_payment_report(
        callback,
        db,
        callback_data.subscription_id,
        callback.from_user.id,
        close=opened_as_new(callback.message),
    )


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_currency"))
async def handle_public_subscription_currency(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    """🌐 Shown in: the currency picker, edited over the card."""
    subscription = await _member_gate(callback, db, callback_data.subscription_id)
    if subscription is None:
        return
    text, markup = await member_currency_screen(
        db, subscription, callback.from_user.id, close=opened_as_new(callback.message)
    )
    await respond_with_markup(callback, text, reply_markup=markup)


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_currency_default"))
async def handle_public_subscription_currency_default(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    state: FSMContext,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    """✅ Default (picker) or [↺ Use default] (prompt): drop the member's own currency."""
    subscription = await _member_gate(callback, db, callback_data.subscription_id)
    if subscription is None:
        return
    await db.delete_user_subscription_setting(
        callback.from_user.id,
        callback_data.subscription_id,
        "target_currency",
    )
    await _member_card_picked(
        callback,
        state,
        db,
        callback_data.subscription_id,
        converter,
        toast=shown_in_toast(None),
        banner=shown_in_banner(member_default_shown_in(subscription), default=True),
    )


@public_router.callback_query(F.data.startswith("public_sub_currency_other:"))
async def handle_public_subscription_currency_other(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    """✏️ Other currency: a prompt with [↺ Use default]."""
    subscription_id = _subscription_id_from(callback.data)
    if subscription_id is None:
        await answer_callback_once(callback, DEAD_BUTTON)
        return
    subscription = await _member_gate(callback, db, subscription_id)
    if subscription is None:
        return
    await _ask_for_member_card(
        callback,
        state,
        PublicSubscriptionCurrencyForm.currency,
        member_currency_prompt(str(subscription.get("name") or "")),
        subscription_id,
        SubscriptionAction(action="public_currency_default", subscription_id=subscription_id).pack(),
    )


@public_router.callback_query(F.data.startswith("public_sub_currency:"))
async def handle_public_subscription_currency_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    """A currency preset: save it, back to the card with 'Shown in: USD'."""
    parts = (callback.data or "").split(":", 2)
    subscription_id = _subscription_id_from(callback.data)
    currency = parse_currency(parts[2]) if len(parts) == 3 else None
    if subscription_id is None or currency is None:
        await answer_callback_once(callback, DEAD_BUTTON)
        return
    if await _member_gate(callback, db, subscription_id) is None:
        return
    await db.set_user_subscription_setting(callback.from_user.id, subscription_id, "target_currency", currency)
    await _member_card_picked(
        callback,
        state,
        db,
        subscription_id,
        converter,
        toast=shown_in_toast(currency),
        banner=shown_in_banner(currency),
    )


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_remindertime"))
async def handle_public_subscription_reminder_time(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    """⏰ Reminder time: the picker, edited over the card."""
    subscription = await _member_gate(callback, db, callback_data.subscription_id)
    if subscription is None:
        return
    text, markup = await member_reminder_screen(
        db, subscription, callback.from_user.id, close=opened_as_new(callback.message)
    )
    await respond_with_markup(callback, text, reply_markup=markup)


@public_router.callback_query(SubscriptionAction.filter(F.action == "public_remindertime_default"))
async def handle_public_subscription_reminder_time_default(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    state: FSMContext,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    """✅ Default (picker) or [↺ Use default] (prompt): drop the member's own time."""
    subscription = await _member_gate(callback, db, callback_data.subscription_id)
    if subscription is None:
        return
    await db.delete_user_subscription_setting(
        callback.from_user.id,
        callback_data.subscription_id,
        "reminder_time",
    )
    _, default_time, _ = await member_reminder_times(db, subscription, callback.from_user.id)
    await _member_card_picked(
        callback,
        state,
        db,
        callback_data.subscription_id,
        converter,
        toast=time_toast(None),
        banner=time_banner(default_time, default=True),
    )


@public_router.callback_query(F.data.startswith("public_sub_remindertime_other:"))
async def handle_public_subscription_reminder_time_other(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    """✏️ Other time: a prompt with [↺ Use default]."""
    subscription_id = _subscription_id_from(callback.data)
    if subscription_id is None:
        await answer_callback_once(callback, DEAD_BUTTON)
        return
    subscription = await _member_gate(callback, db, subscription_id)
    if subscription is None:
        return
    await _ask_for_member_card(
        callback,
        state,
        PublicReminderForm.reminder_time,
        member_reminder_prompt(
            str(subscription.get("name") or ""), await viewer_timezone(db, callback.from_user.id)
        ),
        subscription_id,
        SubscriptionAction(action="public_remindertime_default", subscription_id=subscription_id).pack(),
    )


@public_router.callback_query(F.data.startswith("public_sub_remindertime:"))
async def handle_public_subscription_reminder_time_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    """A time preset: save it, back to the card with 'Reminder time: 12:00'."""
    parts = (callback.data or "").split(":", 2)
    subscription_id = _subscription_id_from(callback.data)
    normalized = normalize_time_string(parts[2]) if len(parts) == 3 else None
    if subscription_id is None or normalized is None:
        await answer_callback_once(callback, DEAD_BUTTON)
        return
    if await _member_gate(callback, db, subscription_id) is None:
        return
    await db.set_user_subscription_setting(callback.from_user.id, subscription_id, "reminder_time", normalized)
    await _member_card_picked(
        callback,
        state,
        db,
        subscription_id,
        converter,
        toast=time_toast(normalized),
        banner=time_banner(normalized),
    )


async def _member_prompt_subscription(message: Message, state: FSMContext, db: Database) -> dict | None:
    """The subscription a card prompt is about, or None after ending the prompt and saying why."""
    subscription_id = (await state.get_data()).get(MEMBER_CARD_SUBSCRIPTION_ID)
    if not subscription_id:
        await finish_prompt(message, state)
        await message.answer(PROMPT_OUT_OF_DATE_TEXT)
        return None
    subscription, problem = await member_access(db, int(subscription_id), message.from_user.id)
    if subscription is None:
        await finish_prompt(message, state)
        await _member_list_redraw(message, db, message.from_user.id, banner=f"⚠️ {problem}")
        return None
    return subscription


@public_router.message(PublicReminderForm.reminder_time)
async def handle_public_reminder_time_input(
    message: Message,
    state: FSMContext,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    """The typed reminder time ('9', '09.30', '1630'; 'default' resets it).

    A typo keeps the prompt and gets a one-line hint. Success edits the
    screen the prompt came from into the card with a banner, then removes the
    prompt, the hints and the typed messages.
    """
    if not message.from_user:
        await reset_flow_state(state)
        await _answer_no_identity(message)
        return
    if message.text is None:
        await note_typo(message, state, TIME_NOT_TEXT_HINT)
        return
    subscription = await _member_prompt_subscription(message, state, db)
    if subscription is None:
        return
    subscription_id = int(subscription["id"])
    telegram_id = message.from_user.id
    if member_is_reset_word(message.text):
        await db.delete_user_subscription_setting(telegram_id, subscription_id, "reminder_time")
        _, default_time, _ = await member_reminder_times(db, subscription, telegram_id)
        banner = time_banner(default_time, default=True)
    else:
        normalized = parse_time_loose(message.text)
        if normalized is None:
            await note_typo(message, state, TIME_TYPO_HINT)
            return
        await db.set_user_subscription_setting(telegram_id, subscription_id, "reminder_time", normalized)
        banner = time_banner(normalized)
    flow_data = await finish_prompt(message, state)
    await _show_member_card_on_origin(
        message.bot,
        message.chat.id,
        db,
        subscription_id,
        telegram_id,
        flow_data,
        banner=banner,
        converter=converter,
        fallback=message,
    )


@public_router.message(PublicSubscriptionCurrencyForm.currency)
async def handle_public_subscription_currency_input(
    message: Message,
    state: FSMContext,
    db: Database,
    converter: CurrencyConverter,
) -> None:
    """The typed currency ('chf'; 'default' resets it); see the time input."""
    if not message.from_user:
        await reset_flow_state(state)
        await _answer_no_identity(message)
        return
    if message.text is None:
        await note_typo(message, state, CURRENCY_NOT_TEXT_HINT)
        return
    subscription = await _member_prompt_subscription(message, state, db)
    if subscription is None:
        return
    subscription_id = int(subscription["id"])
    telegram_id = message.from_user.id
    if member_is_reset_word(message.text):
        await db.delete_user_subscription_setting(telegram_id, subscription_id, "target_currency")
        banner = shown_in_banner(member_default_shown_in(subscription), default=True)
    else:
        currency = parse_currency(message.text)
        if currency is None:
            await note_typo(message, state, CURRENCY_TYPO_HINT)
            return
        await db.set_user_subscription_setting(telegram_id, subscription_id, "target_currency", currency)
        banner = shown_in_banner(currency)
    flow_data = await finish_prompt(message, state)
    await _show_member_card_on_origin(
        message.bot,
        message.chat.id,
        db,
        subscription_id,
        telegram_id,
        flow_data,
        banner=banner,
        converter=converter,
        fallback=message,
    )


@public_router.message(PublicSettingsForm.base_time)
async def handle_public_settings_time_input(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    """The typed reminder time ('9', '09.30', '1630'; 'default' resets it).

    A typo keeps the prompt and gets a one-line hint. Success edits the
    screen the prompt came from into ⚙️ Settings with a banner, then removes
    the prompt, the hints and the typed messages.
    """
    if not message.from_user:
        await reset_flow_state(state)
        await _answer_no_identity(message)
        return
    if message.text is None:
        await note_typo(message, state, TIME_NOT_TEXT_HINT)
        return
    telegram_id = message.from_user.id
    if is_default_word(message.text):
        await db.delete_user_setting(telegram_id, "base_reminder_time")
        snapshot = await member_settings_snapshot(db, settings, telegram_id)
        banner = time_banner(snapshot.time, default=True)
    else:
        normalized = parse_time_loose(message.text)
        if normalized is None:
            await note_typo(message, state, TIME_TYPO_HINT)
            return
        await db.set_user_setting(telegram_id, "base_reminder_time", normalized)
        banner = time_banner(normalized)
    flow_data = await finish_prompt(message, state)
    await _show_public_settings_on_origin(
        message.bot, message.chat.id, db, settings, telegram_id, flow_data, banner=banner
    )


@public_router.message(PublicSettingsForm.base_timezone)
async def handle_public_settings_timezone_input(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    """The typed timezone ('asia/dubai', 'UTC+3'; 'default' resets it); see the time input."""
    if not message.from_user:
        await reset_flow_state(state)
        await _answer_no_identity(message)
        return
    if message.text is None:
        await note_typo(message, state, TIMEZONE_NOT_TEXT_HINT)
        return
    telegram_id = message.from_user.id
    if is_default_word(message.text):
        await db.delete_user_setting(telegram_id, "timezone")
        snapshot = await member_settings_snapshot(db, settings, telegram_id)
        banner = timezone_banner(snapshot.timezone, default=True)
    else:
        normalized = parse_timezone_loose(message.text)
        if normalized is None:
            await note_typo(message, state, TIMEZONE_TYPO_HINT)
            return
        await db.set_user_setting(telegram_id, "timezone", normalized)
        banner = timezone_banner(normalized)
    flow_data = await finish_prompt(message, state)
    await _show_public_settings_on_origin(
        message.bot, message.chat.id, db, settings, telegram_id, flow_data, banner=banner
    )


@public_router.message(PublicAccountForm.full_name)
async def handle_public_account_rename_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    """The typed display name. A typo keeps the prompt and gets a one-line hint.

    Success edits the account message the prompt came from with the banner
    '✅ Renamed · <b>Name</b>' (a new account message when that one is gone),
    then removes the prompt, the hints and the typed messages.
    """
    if not message.from_user:
        await reset_flow_state(state)
        await _answer_no_identity(message)
        return
    if message.text is None:
        await note_typo(message, state, RENAME_NOT_TEXT_HINT)
        return
    normalized_name, error_message = validate_person_name(message.text)
    if error_message or not normalized_name:
        await note_typo(message, state, error_message or RENAME_NOT_TEXT_HINT)
        return
    telegram_id = message.from_user.id
    friend = await db.get_friend_by_telegram(telegram_id)
    if not friend:
        await finish_prompt(message, state)
        await message.answer(ACCOUNT_NOT_SET_UP_TEXT, reply_markup=guest_reply_keyboard())
        return
    await db.update_friend_name(int(friend["id"]), normalized_name)
    flow_data = await finish_prompt(message, state)
    from_push = flow_data.get(ORIGIN_SCREEN) == ACCOUNT_NEW_ORIGIN
    screen = await account_screen(db, telegram_id, banner=renamed_banner(normalized_name), close=from_push)
    if screen is None:
        return
    text, markup = screen
    origin = origin_of(flow_data)
    if origin is not None and await edit_in_place(message.bot, origin[0], origin[1], text, markup):
        return
    await message.answer(text, reply_markup=markup)


# Close and Cancel are registered on public_router only: it runs first and
# serves admins too, so copies on admin_router would never fire.
@public_router.callback_query(F.data == "menu:close")
async def handle_menu_close(callback: CallbackQuery, state: FSMContext) -> None:
    """✖️ Close: delete this message (too old to delete: strip its buttons), silently.

    Closing the message of a pending prompt also ends that prompt, so the next
    text is not taken as its answer.
    """
    message = callback.message
    bot = callback.bot or (message.bot if message is not None else None)
    if message is not None and bot is not None:
        data = await state.get_data()
        if data.get(PROMPT_MESSAGE_ID) == message.message_id:
            old_data = await reset_flow_state(state)
            await wind_down(bot, message.chat.id, old_data, skip_message_id=message.message_id)
        await delete_quietly(bot, message.chat.id, message.message_id)
    await answer_callback_once(callback)


@public_router.callback_query(F.data == "dialog:cancel")
async def handle_dialog_cancel_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """✖️ Cancel: wind the flow down, delete the prompt, toast 'Cancelled' (works without FSM data)."""
    await cancel_prompt(callback, state)



async def handle_payments_report(message: Message, db: Database) -> None:
    """The admin's 📊 Reports (WP8, app/ui/report_screens.py): a top-level screen, no nav row.

    Reached through handle_public_payments_report ('📊 Reports' and the old
    '📊 Payments report'), which serves admins and members alike.
    """
    from app.ui.report_screens import send_admin_report  # it imports app.ui.helpers

    await send_admin_report(message, db)


# ------------------------------------------------------ admin settings (WP11)
#
# ⚙️ Settings is a top-level screen (no nav row). Its pickers edit the same
# message and return to it with a toast; the ✏️ prompts edit the screen they
# came from into ⚙️ Settings with a result banner. The defaults live in the
# database and in the shared in-memory ``settings`` (the reminder worker and
# the converter read the latter), so a change writes both.

_ADMIN_SETTINGS_PROMPT_STATES = frozenset({SettingsForm.base_time.state, SettingsForm.base_timezone.state})
ADMIN_SETTINGS_ORIGIN = "admin_settings"


async def _show_admin_settings(
    target: CallbackQuery | Message,
    db: Database,
    settings: Settings,
    *,
    toast: str | None = None,
    banner: str | None = None,
) -> None:
    """Edit the tapped message into ⚙️ Settings (a typed tap sends it), answering once."""
    text, markup = await admin_settings_screen(db, settings, banner=banner)
    await respond_with_markup(target, text, reply_markup=markup, toast=toast)


async def _show_admin_settings_on_origin(
    bot: Bot,
    chat_id: int,
    db: Database,
    settings: Settings,
    flow_data: dict,
    *,
    banner: str,
) -> None:
    """A prompt finished: the screen it came from becomes ⚙️ Settings with ``banner``.

    When that screen is gone (or unknown after a restart), it is sent anew.
    """
    text, markup = await admin_settings_screen(db, settings, banner=banner)
    origin = origin_of(flow_data)
    if origin is not None and await edit_in_place(bot, origin[0], origin[1], text, markup):
        return
    await bot.send_message(chat_id, text, reply_markup=markup)


async def _admin_settings_picked(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
    *,
    toast: str,
    banner: str | None = None,
) -> None:
    """A preset was saved: back to ⚙️ Settings with ``banner`` and ``toast``; a pending ✏️ prompt goes."""
    message = callback.message
    if (
        callback.bot is not None
        and message is not None
        and await state.get_state() in _ADMIN_SETTINGS_PROMPT_STATES
    ):
        await wind_down(callback.bot, message.chat.id, await reset_flow_state(state))
    await _show_admin_settings(callback, db, settings, toast=toast, banner=banner)


async def _save_admin_time(db: Database, settings: Settings, value: str) -> None:
    settings.base_reminder_time = value
    await db.set_setting("base_reminder_time", value)


async def _save_admin_timezone(db: Database, settings: Settings, zone: str) -> None:
    settings.base_timezone = zone
    await db.set_setting("base_timezone", zone)


@admin_router.callback_query(F.data == "settings:menu")
async def handle_settings_menu_callback(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    await _show_admin_settings(callback, db, settings)


@admin_router.callback_query(F.data == "settings:time")
async def handle_settings_time(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    """⏰ Reminder time: the picker, edited over ⚙️ Settings."""
    snapshot = await admin_settings_snapshot(db, settings)
    await respond_with_markup(
        callback,
        admin_reminder_time_text(snapshot),
        reply_markup=settings_time_keyboard(snapshot.time),
    )


@admin_router.callback_query(F.data == "settings:time_other")
async def handle_settings_time_other(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    """✏️ Other time: a prompt; the picker is the screen to redraw."""
    if not isinstance(callback.message, Message):
        await answer_callback_once(callback, SETTINGS_REOPEN_ALERT, alert=True)
        return
    snapshot = await admin_settings_snapshot(db, settings)
    await ask(
        callback,
        state,
        SettingsForm.base_time,
        admin_time_prompt(snapshot.timezone),
        origin_screen=ADMIN_SETTINGS_ORIGIN,
    )


@admin_router.callback_query(F.data == "settings:timezone")
async def handle_settings_timezone(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    """🌍 Timezone: city presets, edited over ⚙️ Settings."""
    snapshot = await admin_settings_snapshot(db, settings)
    await respond_with_markup(
        callback,
        admin_timezone_text(snapshot),
        reply_markup=settings_timezone_keyboard(snapshot.timezone),
    )


@admin_router.callback_query(F.data == "settings:timezone_other")
async def handle_settings_timezone_other(callback: CallbackQuery, state: FSMContext) -> None:
    """✏️ Other timezone: a prompt; the picker is the screen to redraw."""
    if not isinstance(callback.message, Message):
        await answer_callback_once(callback, SETTINGS_REOPEN_ALERT, alert=True)
        return
    await ask(
        callback,
        state,
        SettingsForm.base_timezone,
        ADMIN_TIMEZONE_PROMPT,
        origin_screen=ADMIN_SETTINGS_ORIGIN,
    )


@admin_router.callback_query(F.data.startswith("settings_timezone:"))
async def handle_settings_timezone_select(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db: Database,
) -> None:
    raw_value = callback.data.split(":", 1)[1].strip()
    normalized = normalize_timezone_name(raw_value)
    if normalized is None:
        await _show_admin_settings(callback, db, settings, toast=DEAD_BUTTON)
        return
    await _save_admin_timezone(db, settings, normalized)
    await _admin_settings_picked(
        callback, state, db, settings, toast=timezone_toast(normalized), banner=timezone_banner(normalized)
    )


@admin_router.callback_query(F.data.startswith("settings_time:"))
async def handle_settings_time_select(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db: Database,
) -> None:
    raw_value = callback.data.split(":", 1)[1].strip()
    normalized = normalize_time_string(raw_value)
    if normalized is None:
        await _show_admin_settings(callback, db, settings, toast=DEAD_BUTTON)
        return
    await _save_admin_time(db, settings, normalized)
    await _admin_settings_picked(
        callback, state, db, settings, toast=time_toast(normalized), banner=time_banner(normalized)
    )


@admin_router.callback_query(F.data == "settings:rounding")
async def handle_settings_rounding(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    """🔢 Rounding: four radio options, edited over ⚙️ Settings."""
    snapshot = await admin_settings_snapshot(db, settings)
    await respond_with_markup(
        callback,
        admin_rounding_text(snapshot),
        reply_markup=settings_rounding_keyboard(snapshot.rounding),
    )


@admin_router.callback_query(F.data.startswith("settings_rounding:"))
async def handle_settings_rounding_select(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db: Database,
) -> None:
    raw_value = callback.data.split(":", 1)[1].strip().lower()
    if raw_value not in ROUNDING_MODES:
        await _show_admin_settings(callback, db, settings, toast=DEAD_BUTTON)
        return
    settings.currency_rounding = raw_value
    await db.set_setting("currency_rounding", raw_value)
    await _admin_settings_picked(
        callback, state, db, settings, toast=rounding_toast(raw_value), banner=rounding_banner(raw_value)
    )


async def _show_admin_notifications(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    *,
    toast: str | None = None,
) -> None:
    snapshot = await admin_settings_snapshot(db, settings)
    await respond_with_markup(
        callback,
        NOTIFICATIONS_SCREEN_TEXT,
        reply_markup=settings_notifications_keyboard(*snapshot.notifications),
        toast=toast,
    )


@admin_router.callback_query(F.data == "settings:notifications")
async def handle_settings_notifications(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    """🔔 Admin notifications: one toggle per event, each saves at once."""
    await _show_admin_notifications(callback, db, settings)


@admin_router.callback_query(F.data == "settings:tests")
async def handle_settings_tests(callback: CallbackQuery, db: Database) -> None:
    """🧪 Send a test reminder: straight to the person picker (the one-action menu is gone)."""
    await send_settings_tests_menu(callback, db)


@admin_router.callback_query(F.data == "tests:send")
async def handle_test_send_menu(callback: CallbackQuery, db: Database) -> None:
    """The old tests menu's only button (kept for old messages): the same picker."""
    await send_test_user_list(callback, db)


@admin_router.callback_query(TestListAction.filter(F.action == "page"))
async def handle_test_list_page(
    callback: CallbackQuery,
    callback_data: TestListAction,
    db: Database,
) -> None:
    await send_test_user_list(callback, db, page=max(1, callback_data.page))


@admin_router.callback_query(TestSendAction.filter())
async def handle_test_reminder_send(callback: CallbackQuery, **data: Any) -> None:
    """🧪 A test reminder to one person, guarded against double taps.

    The picker stays on the page that was tapped and is redrawn inside the
    guard. All handler data is passed on, so ``_send_test_reminder`` may ask
    for any of it (``settings`` for the rounding, for example).
    """
    callback_data: TestSendAction = data["callback_data"]
    async with guarded(callback, ("testsend", callback_data.telegram_id)) as acquired:
        if not acquired:
            return
        await _send_test_reminder(callback, **data)


async def _send_test_reminder(
    callback: CallbackQuery,
    callback_data: TestSendAction,
    bot: Bot,
    db: Database,
    converter: CurrencyConverter,
    **_unused: Any,
) -> None:
    people = admin_test_people(await db.list_friends())
    person = next(
        (friend for friend in people if int(friend["telegram_id"]) == callback_data.telegram_id),
        None,
    )
    if person is None:
        page = admin_tests_shown_page(callback.message)
        await stale_redirect(callback, USER_GONE, lambda: send_test_user_list(callback, db, page=page))
        return

    full_name = str(person.get("full_name") or "Unknown")
    recipient_timezone = normalize_timezone_name(
        await db.get_effective_user_timezone(int(person["telegram_id"]), DEFAULT_REMINDER_TIMEZONE),
        DEFAULT_REMINDER_TIMEZONE,
    ) or DEFAULT_REMINDER_TIMEZONE
    today = datetime.now(parse_timezone(recipient_timezone)).date()
    default_destination = None
    default_destination_id = await db.get_default_payment_destination_id()
    if default_destination_id is not None:
        default_destination = await db.get_payment_destination(default_destination_id)

    payment_label = "not set"
    payment_details = ""
    payment_link = ""
    amount_currency = "RUB"
    if default_destination:
        amount_currency = str(default_destination.get("currency") or "RUB").upper()
        payment_label = f"{default_destination['title']} · {amount_currency}"
        payment_details = str(default_destination.get("details") or "").strip()
        payment_link = str(default_destination.get("payment_link") or "").strip()

    target_currency = await db.get_effective_target_currency(
        int(person["telegram_id"]),
        0,
        subscription_default=amount_currency,
        default_currency=converter.target_currency,
    )
    test_amount_currency = amount_currency
    if test_amount_currency == target_currency:
        for candidate_currency in TEST_REMINDER_CURRENCY_FALLBACKS:
            if candidate_currency != target_currency:
                test_amount_currency = candidate_currency
                break

    test_amount_value = 100.0
    if test_amount_currency != "RUB":
        try:
            converted_from_rub = await converter.convert_to(100.0, "RUB", test_amount_currency)
        except Exception:  # noqa: BLE001
            converted_from_rub = None
        if converted_from_rub is not None and converted_from_rub > 0:
            test_amount_value = converted_from_rub

    converted_text = None
    if target_currency != test_amount_currency:
        try:
            converted_amount = await converter.convert_to(
                test_amount_value,
                test_amount_currency,
                target_currency,
            )
        except Exception:  # noqa: BLE001
            converted_amount = None
        if converted_amount is not None:
            # The admin rounding setting, like a real reminder (WP15).
            converted_text = format_converted_amount(
                converted_amount,
                target_currency,
                (await db.get_setting("currency_rounding")) or "precise",
            )

    from app.ui.keyboards import extract_copyable_requisite
    from app.ui.push_screens import TEST_FOOTER
    from app.ui.text import format_money

    status_text, due_date_text = _format_status_and_date(today, today)
    # '🧪 <b>Test · Sample subscription · due today</b>': the real reminder builder.
    text = _build_reminder_message(
        person_name=full_name,
        subscription_name="Sample subscription",
        status_text=status_text,
        due_date_text=due_date_text,
        amount_text=format_money(test_amount_value, test_amount_currency),
        converted_text=converted_text,
        payment_label=payment_label,
        payment_details=payment_details,
        payment_link=payment_link,
        comment="Test comment",
        footer=TEST_FOOTER,
        due_value=today.isoformat(),
        tz_name=recipient_timezone,
        today=today,
        copyable=extract_copyable_requisite(payment_details) is not None,
        test=True,
    )
    sent_message_id = await _send_message_with_retry(
        bot,
        int(person["telegram_id"]),
        text,
        reply_markup=build_test_payment_confirmation_keyboard(
            0,
            today,
            payment_details=payment_details,
            payment_link=payment_link,
        ),
        logger=logger,
    )
    if sent_message_id is None:
        await answer_callback_once(callback, admin_test_failed_alert(full_name), alert=True)
        return
    banner = admin_test_sent_banner(full_name)
    await send_test_user_list(
        callback,
        db,
        page=admin_test_page_of(people, callback_data.telegram_id) or 1,
        banner=banner,
        toast=toast_from_html(banner),
    )


@admin_router.callback_query(F.data.startswith("settings_notify:"))
async def handle_settings_notifications_toggle(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    """A toggle saves at once, redraws the same screen and toasts 'Payment recorded: on'."""
    raw_value = callback.data.split(":", 1)[1].strip().lower()
    setting_key = ADMIN_NOTIFY_KEYS.get(raw_value)
    if setting_key is None:
        await _show_admin_notifications(callback, db, settings, toast=DEAD_BUTTON)
        return
    enabled = not await db.get_setting_bool(setting_key, True)
    await db.set_setting(setting_key, "true" if enabled else "false")
    await _show_admin_notifications(callback, db, settings, toast=notification_toast(raw_value, enabled))


@admin_router.message(SettingsForm.base_time)
async def handle_settings_time_input(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db: Database,
) -> None:
    """The typed default time ('9', '09.30', '1630').

    A typo keeps the prompt and gets a one-line hint. Success edits the
    screen the prompt came from into ⚙️ Settings with a banner, then removes
    the prompt, the hints and the typed messages.
    """
    if message.text is None:
        await note_typo(message, state, TIME_NOT_TEXT_HINT)
        return
    normalized = parse_time_loose(message.text)
    if normalized is None:
        await note_typo(message, state, TIME_TYPO_HINT)
        return
    await _save_admin_time(db, settings, normalized)
    flow_data = await finish_prompt(message, state)
    await _show_admin_settings_on_origin(
        message.bot, message.chat.id, db, settings, flow_data, banner=time_banner(normalized)
    )


@admin_router.message(SettingsForm.base_timezone)
async def handle_settings_timezone_input(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db: Database,
) -> None:
    """The typed timezone ('asia/dubai', 'UTC+3'); see the time input."""
    if message.text is None:
        await note_typo(message, state, TIMEZONE_NOT_TEXT_HINT)
        return
    normalized = parse_timezone_loose(message.text)
    if normalized is None:
        await note_typo(message, state, TIMEZONE_TYPO_HINT)
        return
    await _save_admin_timezone(db, settings, normalized)
    flow_data = await finish_prompt(message, state)
    await _show_admin_settings_on_origin(
        message.bot, message.chat.id, db, settings, flow_data, banner=timezone_banner(normalized)
    )
