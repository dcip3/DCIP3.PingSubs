from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, List, Optional

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram import Bot

from app.core.constants import (
    MONTHLY_PERIOD_SENTINEL,
    PAYMENT_MODE_FIXED,
    PAYMENT_MODE_SPLIT,
)
from app.storage.db import Database
from app.ui.helpers import (
    currency_prompt,
    get_edit_subscription_id,
    payment_mode_prompt,
    period_prompt,
    require_edit_subscription_id,
    send_pricing_settings,
    send_subscription_cycle_actions,
    remember_spawned,
    send_subscription_more,
    send_subscription_open_cycles,
    send_subscription_payment_report,
    send_subscription_detail,
    send_subscription_list,
    share_limit_prompt,
    start_subscription_edit_flow,
)
from app.ui.keyboards import (
    build_due_date_change_keyboard,
    subscription_currency_keyboard,
    subscription_base_currency_keyboard,
)
from app.ui.states import (
    CycleAction,
    DueDateChangeAction,
    Responder,
    SubscriptionAction,
    SubscriptionEditForm,
    SubscriptionForm,
)
from app.core.reminders import (
    calculate_next_charge_date,
    normalize_monthly_anchor_day,
)
from app.ui.text import escape_html

from . import admin_router

# Admin subscription list, creation wizard, card, rename and delete (WP3).
from collections import OrderedDict  # noqa: E402
from datetime import date  # noqa: E402

from aiogram.types import InlineKeyboardMarkup  # noqa: E402

from app.core.reminders import parse_timezone, tg_due  # noqa: E402
from app.ui.guards import guarded  # noqa: E402
from app.ui.helpers import (  # noqa: E402
    DEAD_BUTTON,
    SUBSCRIPTION_GONE,
    SUBSCRIPTION_GONE_BANNER,
    answer_callback_once,
    build_subscription_card,
    delete_quietly,
    edit_in_place,
    resolve_report_timezone,
    respond_with_markup,
    show_card_after_prompt,
    show_subscription_gone,
)
from app.ui.keyboards import (  # noqa: E402
    SUBSCRIPTION_LIST_PAGE_PREFIX,
    cancel_button,
    subscription_delete_confirm_keyboard,
)
from app.ui.prompts import (  # noqa: E402
    advance_wizard,
    cancel_prompt,
    finish_prompt,
    finish_wizard,
    note_typo,
    reset_flow_state,
    start_wizard,
    wind_down,
    wizard_of,
)
from app.ui.style import join_blocks, sub_line, title as screen_title, toast_from_html  # noqa: E402
# Reports and cycles (WP8).
from app.ui.keyboards import CYCLE_FORCE_CLOSE_YES, CYCLE_RECREATE_YES  # noqa: E402
from app.ui.report_screens import (  # noqa: E402
    CYCLE_RECREATED_BANNER,
    cycle_closed_banner,
    send_cycle_confirm,
    show_cycle_closed,
)
# Pricing & schedule (WP4).
from app.ui.helpers import (  # noqa: E402
    build_pricing_screen,
    show_pricing_after_prompt,
    stale_redirect,
    subscription_currency_of,
    subscription_shown_in_of,
)
from app.ui.keyboards import build_period_keyboard  # noqa: E402
from app.ui.prompts import origin_of  # noqa: E402
from app.ui.text import format_payments, plural_word  # noqa: E402
from app.ui.text import (  # noqa: E402
    format_days,
    format_money,
    format_number,
    parse_amount,
    parse_currency,
    parse_date_loose,
    plural,
)


def _response_target(responder: Responder) -> Message:
    return responder.message if isinstance(responder, CallbackQuery) else responder


async def _drop_flow(callback: CallbackQuery, state: FSMContext) -> None:
    """Leaving for another screen: wind the pending prompt or wizard down (never a bare clear)."""
    old_data = await reset_flow_state(state)
    message = callback.message
    bot = callback.bot or (message.bot if message is not None else None)
    if old_data and message is not None and bot is not None:
        await wind_down(bot, message.chat.id, old_data, skip_message_id=message.message_id)


async def _load_subscription(
    callback: CallbackQuery,
    db: Database,
    subscription_id: int,
) -> Optional[Dict[str, object]]:
    """The subscription, or ``None`` after redrawing the list with 'Subscription was deleted'."""
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        await show_subscription_gone(callback, db)
        return None
    return subscription


async def _load_open_cycle(
    callback: CallbackQuery,
    db: Database,
    subscription_id: int,
    due_value: str,
) -> Optional[Dict[str, object]]:
    subscription = await _load_subscription(callback, db, subscription_id)
    if not subscription:
        return None
    open_cycles = await db.list_open_cycles(subscription_id)
    if due_value not in open_cycles:
        # Stale, not an error: redraw the open cycles with 'This cycle is already closed'.
        await show_cycle_closed(callback, db, subscription_id)
        return None
    return subscription


def _resolve_cycle_participants(
    cycle_state: Dict[str, object],
    live_participants: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    cycle_participants = list(cycle_state.get("participants") or [])
    snapshot_ready = bool(cycle_state.get("snapshot_ready"))
    if not cycle_participants and not snapshot_ready:
        return live_participants
    return cycle_participants


async def _advance_subscription_after_cycle_close(
    db: Database,
    subscription: Dict[str, object],
    due_value: str,
) -> None:
    if str(subscription.get("next_charge_at") or "") != due_value:
        return
    try:
        cycle_due = datetime.strptime(due_value, "%Y-%m-%d").date()
    except ValueError:
        return
    period_days = int(subscription.get("period_days") or 30)
    monthly_anchor_day = normalize_monthly_anchor_day(subscription.get("monthly_anchor_day"))
    next_due = calculate_next_charge_date(cycle_due, period_days, monthly_anchor_day)
    await db.update_subscription_fields(int(subscription["id"]), next_charge_at=next_due)


def currency_menu_text(subscription: Dict[str, object]) -> str:
    """'💱 <b>Currency · Netflix</b>', the current code and the no-conversion warning."""
    currency = subscription_currency_of(subscription)
    try:
        amount = format_number(float(subscription.get("amount")))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        amount = "?"
    return join_blocks(
        screen_title("💱", f"Currency · {escape_html(subscription.get('name') or '')}"),
        [
            f"Now: {escape_html(currency)}",
            f"⚠️ Changing the currency does not convert the amount ({amount}).",
        ],
    )


def shown_in_menu_text(subscription: Dict[str, object]) -> str:
    """'🌐 <b>Shown in · Netflix</b>', the current code and what it is for."""
    return join_blocks(
        screen_title("🌐", f"Shown in · {escape_html(subscription.get('name') or '')}"),
        [
            f"Now: {escape_html(subscription_shown_in_of(subscription))}",
            "Members see their share converted to this currency in reminders "
            "(they can pick their own on their card).",
        ],
    )


# ------------------------------------------------ Pricing & schedule (WP4)
#
# Every Pricing sub-screen returns to Pricing with a result banner: picker
# presets edit the tapped message, typed prompts edit the screen they were
# opened from (origin 'pricing:<id>') and delete the prompt and the answers.

CYCLE_MOVED_BANNER = "✅ Cycle moved · payments kept"
CYCLE_GONE_TOAST = "This cycle no longer exists"
MOVE_BLOCKED_ALERT = "Could not move the cycle: the new date already has payments."
CURRENCY_OTHER_HINT = "⚠️ Need three letters, for example <code>CHF</code>."
PERIOD_EDIT_HINT = "⚠️ Need a number of days, for example <code>30</code>, or tap “Monthly”."
NEXT_CHARGE_HINT = "⚠️ Need a date, for example <code>15.11.2026</code>, <code>15.11</code> or <code>tomorrow</code>."
MAX_LISTED_DROPPED_CYCLES = 3


def _pricing_origin(subscription_id: int) -> str:
    return f"pricing:{int(subscription_id)}"


def _subscription_period(subscription: Dict[str, object]) -> int:
    try:
        return int(subscription.get("period_days") or 30)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 30


def period_phrase(period: int) -> str:
    """``'monthly'`` or ``'every 30 days'``."""
    return "monthly" if period == MONTHLY_PERIOD_SENTINEL else f"every {format_days(period)}"


def _short_date(due_value: str) -> str:
    """``'2026-10-01'`` -> ``'01.10'`` (the short form used inside a sentence)."""
    try:
        return datetime.strptime(due_value, "%Y-%m-%d").strftime("%d.%m")
    except (TypeError, ValueError):
        return escape_html(due_value)


def _sub_name(subscription: Dict[str, object]) -> str:
    return escape_html(str(subscription.get("name") or ""))


def amount_prompt_text(subscription: Dict[str, object], frozen_cycle_due: Optional[str] = None) -> str:
    """'✏️ New amount for Netflix, USD? Now 15.99.' + example (+ the open-cycle warning)."""
    try:
        now = format_number(float(subscription.get("amount")))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        now = "?"
    currency = escape_html(subscription_currency_of(subscription))
    lines = [f"✏️ New amount for {_sub_name(subscription)}, {currency}? Now {now}.", "Example: <code>149.99</code>"]
    if frozen_cycle_due:
        lines.append(f"⚠️ The open cycle ({_short_date(frozen_cycle_due)}) keeps its old amount.")
    return "\n".join(lines)


def currency_other_prompt_text(subscription: Dict[str, object]) -> str:
    return f"✏️ Currency for {_sub_name(subscription)}? Three letters, for example <code>CHF</code>."


def shown_in_other_prompt_text(subscription: Dict[str, object]) -> str:
    now = escape_html(subscription_shown_in_of(subscription))
    return (
        f"✏️ Show {_sub_name(subscription)} to members in which currency? Now {now}. "
        "Three letters, for example <code>EUR</code>."
    )


def dropped_cycles_warning(dropped: List[str]) -> Optional[str]:
    """'⚠️ The unpaid 01.10 cycle will be dropped.' (open cycles nobody has paid yet)."""
    if not dropped:
        return None
    if len(dropped) > MAX_LISTED_DROPPED_CYCLES:
        return f"⚠️ {len(dropped)} unpaid cycles will be dropped."
    dates = [_short_date(value) for value in dropped]
    listed = dates[0] if len(dates) == 1 else f"{', '.join(dates[:-1])} and {dates[-1]}"
    return f"⚠️ The unpaid {listed} {plural_word(len(dates), 'cycle')} will be dropped."


def due_date_prompt_text(subscription: Dict[str, object], tz_name: str, dropped: List[str]) -> str:
    """'✏️ Next charge date for Netflix? Now <date>.' + accepted forms (+ what gets dropped)."""
    now = tg_due(str(subscription.get("next_charge_at")), tz_name, "wD")
    lines = [
        f"✏️ Next charge date for {_sub_name(subscription)}? Now {now}.",
        "<code>DD.MM.YYYY</code>, <code>DD.MM</code>, <code>today</code> or <code>tomorrow</code>.",
        dropped_cycles_warning(dropped),
    ]
    return "\n".join(line for line in lines if line)


def period_edit_prompt_text(subscription: Dict[str, object]) -> str:
    return f"✏️ How often does {_sub_name(subscription)} repeat? Now {period_phrase(_subscription_period(subscription))}."


def due_date_conflict_text(conflict: Dict[str, object], new_due: str, tz_name: str) -> str:
    """The question when the new date touches a cycle with payments: Move or New cycle."""
    payments = int(conflict.get("payments") or 0)  # type: ignore[arg-type]
    facts = [tg_due(str(conflict["due_date"]), tz_name, "wD")]
    if conflict.get("closed"):
        facts.append("closed")
    facts.append(f"{format_payments(payments)} recorded")
    return join_blocks(
        "📅 <b>Change the next charge?</b>",
        [f"Current cycle: {' · '.join(facts)}", f"New date: {tg_due(new_due, tz_name, 'wD')}"],
        [
            "🔀 Move cycle — same period, payments carry over.",
            "🆕 New cycle — keep the old cycle, ask for payment on the new date.",
        ],
    )


def amount_banner(amount: float, currency: str) -> str:
    return f"✅ Amount · <b>{format_money(amount, currency)}</b>"


def currency_banner(currency: str) -> str:
    return f"✅ Currency · <b>{escape_html(currency)}</b>"


def shown_in_banner(currency: str) -> str:
    return f"✅ Shown in · <b>{escape_html(currency)}</b>"


def next_charge_banner(due_value: str, tz_name: str) -> str:
    return f"✅ Next charge · <b>{tg_due(due_value, tz_name, 'wD')}</b>"


def new_cycle_banner(due_value: str, tz_name: str) -> str:
    return f"✅ New cycle on {tg_due(due_value, tz_name, 'wD')}"


def period_banner(period: int) -> str:
    return f"✅ Period · <b>{period_phrase(period)}</b>"


async def _open_cycle_with_frozen_amount(db: Database, subscription_id: int) -> Optional[str]:
    """The first open cycle whose settings (amount included) are already frozen, if any."""
    open_cycles = await db.list_open_cycles(subscription_id)
    if not open_cycles:
        return None
    states = await db.list_cycle_participants_for_due_dates(subscription_id, open_cycles)
    for due_value in open_cycles:
        if states.get(due_value, {}).get("settings_snapshot_ready"):
            return due_value
    return None


async def _cycles_without_payments(db: Database, subscription_id: int) -> List[str]:
    """Open cycles nobody has paid: a new next-charge date drops them."""
    return [
        due_value
        for due_value in await db.list_open_cycles(subscription_id)
        if await db.count_cycle_payments(subscription_id, due_value) == 0
    ]


async def _pricing_prompt_gone(message: Message, state: FSMContext, db: Database) -> None:
    """A Pricing prompt answered after the subscription was deleted: the list with a banner."""
    await finish_prompt(message, state)
    await send_subscription_list(message, db, banner=SUBSCRIPTION_GONE_BANNER)


# ------------------------------------------------- the creation wizard (one message)
#
# Every step edits the same message: the title with the step, the answers so
# far, then the question and an optional '⚠️' hint for the last typo. Typed
# answers and typos are deleted as the wizard moves on, so it stays the last
# message in the chat. The finish edits the wizard into the new card.

WIZARD_CANCEL_CALLBACK = SubscriptionAction(action="create_cancel", subscription_id=0).pack()
WIZARD_STEPS = 6  # 7 when "Split by shares" asks how many people share it
STALE_STEP_TOAST = "This step is out of date"
SUBSCRIPTION_NAME_MAX_LEN = 64
MAX_PERIOD_DAYS = 3650
MAX_SHARE_LIMIT = 1000

NAME_QUESTION = "✏️ Name? For example <code>Netflix</code>."
AMOUNT_QUESTION = "✏️ Amount? For example <code>149.99</code>."
DATE_QUESTION = "✏️ Next charge date? <code>DD.MM.YYYY</code>, <code>DD.MM</code> or <code>today</code>."

TEXT_ONLY_HINT = "⚠️ Send text, for example <code>Netflix</code>."
NAME_TOO_LONG_HINT = f"⚠️ Keep the name under {SUBSCRIPTION_NAME_MAX_LEN} characters, for example <code>Netflix</code>."
AMOUNT_HINT = "⚠️ Need a number, for example <code>149.99</code>."
AMOUNT_ZERO_HINT = "⚠️ The amount must be more than 0, for example <code>149.99</code>."
CURRENCY_HINT = "⚠️ Need three letters, for example <code>EUR</code>."
DATE_HINT = "⚠️ Need a date, for example <code>15.11.2026</code>, <code>15.11</code> or <code>today</code>."
PAST_DATE_HINT = "⚠️ That date is in the past — send it again to keep it."
PERIOD_HINT = "⚠️ Need a number of days, for example <code>30</code>, or tap a button."
PAYMENT_MODE_HINT = "⚠️ Tap a button, or send <code>split</code> or <code>fixed</code>."
SHARE_LIMIT_HINT = "⚠️ Need a whole number, for example <code>4</code>, or tap “Everyone”."
SUBSCRIPTION_SAVED_TOAST = "Subscription saved"

# Wizards that became a card: a late duplicate tap on their old step buttons
# must not delete the new card as a "stale step".
_FINISHED_WIZARDS: "OrderedDict[tuple[int, int], None]" = OrderedDict()
_FINISHED_WIZARDS_LIMIT = 256

_WIZARD_STATES = {
    1: SubscriptionForm.name,
    2: SubscriptionForm.amount,
    3: SubscriptionForm.currency,
    4: SubscriptionForm.due_date,
    5: SubscriptionForm.period,
    6: SubscriptionForm.payment_mode,
    7: SubscriptionForm.share_limit,
}


def _wizard_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[cancel_button(WIZARD_CANCEL_CALLBACK)]],
    )


def _wizard_step_view(step: int) -> tuple[str, InlineKeyboardMarkup]:
    """The question and keyboard of a wizard step (presets reuse the shared prompt builders)."""
    if step == 3:
        return currency_prompt(cancel_callback=WIZARD_CANCEL_CALLBACK)
    if step == 5:
        return period_prompt(
            suggested=MONTHLY_PERIOD_SENTINEL,
            cancel_callback=WIZARD_CANCEL_CALLBACK,
        )
    if step == 6:
        return payment_mode_prompt(cancel_callback=WIZARD_CANCEL_CALLBACK)
    if step == 7:
        return share_limit_prompt(cancel_callback=WIZARD_CANCEL_CALLBACK)
    question = {1: NAME_QUESTION, 2: AMOUNT_QUESTION, 4: DATE_QUESTION}[step]
    return question, _wizard_markup()


def _period_text(period: int) -> str:
    return "every month" if period == MONTHLY_PERIOD_SENTINEL else f"every {format_days(period)}"


def wizard_answer_lines(data: Dict[str, object], tz_name: str) -> list[str]:
    """The answers given so far, as indented 'Label: value' lines."""
    lines: list[str] = []
    if data.get("subscription_name"):
        lines.append(f"Name: {escape_html(data['subscription_name'])}")
    if data.get("amount") is not None:
        amount = float(data["amount"])  # type: ignore[arg-type]
        currency = str(data.get("currency") or "")
        lines.append(f"Amount: {format_money(amount, currency) if currency else format_number(amount)}")
    if data.get("due_date"):
        lines.append(f"Next charge: {tg_due(str(data['due_date']), tz_name, 'wD')}")
    if data.get("period_days") is not None:
        lines.append(f"Repeats: {_period_text(int(data['period_days']))}")  # type: ignore[arg-type]
    if data.get("payment_mode"):
        mode = "split by shares" if data["payment_mode"] == PAYMENT_MODE_SPLIT else "fixed per user"
        lines.append(f"Amounts: {mode}")
    return [sub_line(line) for line in lines]


def wizard_text(data: Dict[str, object], tz_name: str, step: int, hint: Optional[str] = None) -> str:
    """'➕ <b>New subscription</b> · step 2 of 6', the answers, then '✏️ question' (+ '⚠️ hint')."""
    total = 7 if step >= 7 else WIZARD_STEPS
    question, _ = _wizard_step_view(step)
    if hint:
        question = f"{question}\n{hint}"
    return join_blocks(
        screen_title("➕", "New subscription", f"step {step} of {total}"),
        wizard_answer_lines(data, tz_name),
        question,
    )


async def _show_wizard_step(
    bot: Bot,
    state: FSMContext,
    db: Database,
    chat_id: int,
    step: int,
    *,
    answer: Optional[Message] = None,
    hint: Optional[str] = None,
    data: Optional[Dict[str, object]] = None,
) -> None:
    """Edit the wizard into ``step``; the user's answer (or typo) is deleted."""
    update = dict(data or {})
    merged = {**await state.get_data(), **update}
    text = wizard_text(merged, await resolve_report_timezone(db), step, hint)
    _, markup = _wizard_step_view(step)
    wizard_id = await advance_wizard(
        bot,
        state,
        text,
        markup,
        answer=answer,
        new_state=_WIZARD_STATES[step],
        data=update,
    )
    if wizard_id is None:
        # The state outlived the wizard message ids: show the step as a new wizard.
        sent = await bot.send_message(chat_id, text, reply_markup=markup)
        await state.set_state(_WIZARD_STATES[step])
        await state.update_data(**update, wizard_message_id=sent.message_id, wizard_chat_id=chat_id, cleanup_ids=[])
        if answer is not None:
            await delete_quietly(bot, chat_id, answer.message_id)


async def start_subscription_creation(responder: Responder, state: FSMContext) -> None:
    """Step 1 as ONE new message; the previous flow is wound down."""
    question, markup = _wizard_step_view(1)
    text = join_blocks(screen_title("➕", "New subscription", f"step 1 of {WIZARD_STEPS}"), question)
    await start_wizard(responder, state, SubscriptionForm.name, text, markup)


def _creation_data_complete(data: Dict[str, object]) -> bool:
    keys = ("subscription_name", "amount", "currency", "due_date", "period_days")
    return all(data.get(key) is not None for key in keys)


async def _finalize_new_subscription(
    responder: Responder,
    state: FSMContext,
    db: Database,
    share_limit: Optional[int],
) -> None:
    """Create the subscription once (guarded) and edit the wizard into its card."""
    callback = responder if isinstance(responder, CallbackQuery) else None
    message = _response_target(responder)
    bot = responder.bot or message.bot
    async with guarded(callback, ("sub_create", message.chat.id)) as acquired:
        if not acquired:
            return
        current_state = await state.get_state()
        data = await state.get_data()
        finishing = current_state in (SubscriptionForm.payment_mode.state, SubscriptionForm.share_limit.state)
        if not finishing or not _creation_data_complete(data):
            if callback is not None:
                await answer_callback_once(callback, STALE_STEP_TOAST)
            return
        payment_mode = str(data.get("payment_mode") or PAYMENT_MODE_SPLIT).strip().lower()
        if payment_mode not in {PAYMENT_MODE_SPLIT, PAYMENT_MODE_FIXED}:
            payment_mode = PAYMENT_MODE_SPLIT
        name = str(data["subscription_name"])
        sub_id = await db.create_subscription(
            name=name,
            amount=float(data["amount"]),  # type: ignore[arg-type]
            currency=str(data["currency"]),
            due_date=date.fromisoformat(str(data["due_date"])),
            period_days=int(data["period_days"]),  # type: ignore[arg-type]
            share_limit=share_limit,
            payment_mode=payment_mode,
        )
        flow = await finish_wizard(state, answer=None if callback is not None else message, bot=bot)
        finished = wizard_of(flow)
        if finished is not None:
            _FINISHED_WIZARDS[finished] = None
            while len(_FINISHED_WIZARDS) > _FINISHED_WIZARDS_LIMIT:
                _FINISHED_WIZARDS.popitem(last=False)
        card = await build_subscription_card(db, sub_id, banner=f"✅ <b>{escape_html(name)} created</b>")
        if card is None:  # deleted by another admin in the meantime
            await show_subscription_gone(responder, db)
            return
        text, markup = card
        if callback is not None:
            await respond_with_markup(callback, text, reply_markup=markup, toast=SUBSCRIPTION_SAVED_TOAST)
            return
        wizard = wizard_of(flow)
        if wizard is not None and bot is not None and await edit_in_place(bot, wizard[0], wizard[1], text, markup):
            return
        await message.answer(text, reply_markup=markup)


async def _stale_wizard_button(callback: CallbackQuery, state: FSMContext) -> None:
    """A wizard-step button from a finished or abandoned flow: say so and remove that message.

    A repeated tap on the running wizard itself (a double tap that already
    moved it on) is only answered.
    """
    message = callback.message
    wizard = wizard_of(await state.get_data())
    here = (message.chat.id, message.message_id) if message is not None else None
    if here is not None and (here == wizard or here in _FINISHED_WIZARDS):
        # A late double tap on the running wizard, or on the card it just became.
        await answer_callback_once(callback)
        return
    if message is not None and callback.bot is not None:
        await delete_quietly(callback.bot, message.chat.id, message.message_id)
    await answer_callback_once(callback, STALE_STEP_TOAST)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "create_cancel"))
async def handle_subscription_create_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """[✖️ Cancel] on the wizard: delete it and what it left behind, toast 'Cancelled'."""
    await cancel_prompt(callback, state)


@admin_router.message(F.text == "➕ Add subscription")
async def subscription_button(message: Message, state: FSMContext) -> None:
    await start_subscription_creation(message, state)


@admin_router.message(SubscriptionForm.name)
async def subscription_form_title(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    chat_id = message.chat.id
    name = " ".join((message.text or "").split())
    if not name:
        await _show_wizard_step(bot, state, db, chat_id, 1, answer=message, hint=TEXT_ONLY_HINT)
        return
    if len(name) > SUBSCRIPTION_NAME_MAX_LEN:
        await _show_wizard_step(bot, state, db, chat_id, 1, answer=message, hint=NAME_TOO_LONG_HINT)
        return
    data = await state.get_data()
    existing = {str(sub.get("name") or "").strip().lower() for sub in await db.list_subscriptions()}
    if name.lower() in existing and data.get("pending_name") != name:
        # Warn once; sending the same name again keeps it.
        hint = f"⚠️ You already have {escape_html(name)}. Send it again to keep it."
        await _show_wizard_step(bot, state, db, chat_id, 1, answer=message, hint=hint, data={"pending_name": name})
        return
    await _show_wizard_step(
        bot, state, db, chat_id, 2, answer=message, data={"subscription_name": name, "pending_name": None}
    )


@admin_router.message(SubscriptionForm.amount)
async def subscription_form_amount(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    parsed = parse_amount(message.text)
    if parsed is None or not math.isfinite(parsed[0]):
        await _show_wizard_step(bot, state, db, message.chat.id, 2, answer=message, hint=AMOUNT_HINT)
        return
    if parsed[0] <= 0:
        await _show_wizard_step(bot, state, db, message.chat.id, 2, answer=message, hint=AMOUNT_ZERO_HINT)
        return
    await _show_wizard_step(bot, state, db, message.chat.id, 3, answer=message, data={"amount": parsed[0]})


@admin_router.message(SubscriptionForm.currency)
async def subscription_form_currency(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    currency = parse_currency(message.text)
    if currency is None:
        await _show_wizard_step(bot, state, db, message.chat.id, 3, answer=message, hint=CURRENCY_HINT)
        return
    await _show_wizard_step(bot, state, db, message.chat.id, 4, answer=message, data={"currency": currency})


@admin_router.callback_query(F.data.startswith("currency:"))
async def handle_currency_quick_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    raw_value = callback.data.split(":", 1)[1].upper()
    current_state = await state.get_state()
    if current_state == SubscriptionForm.currency.state:
        currency = parse_currency(raw_value)
        if currency is None or callback.message is None or callback.bot is None:
            await _stale_wizard_button(callback, state)
            return
        await _show_wizard_step(callback.bot, state, db, callback.message.chat.id, 4, data={"currency": currency})
        await answer_callback_once(callback, f"Currency: {currency}")
        return

    # The subscription currency is edited through 'sub_currency:*'; a 'currency:'
    # button outside the wizard is a stale step.
    await _stale_wizard_button(callback, state)


@admin_router.callback_query(F.data.startswith("sub_base_currency:"))
async def handle_subscription_base_currency_quick_select(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await answer_callback_once(callback, DEAD_BUTTON)
        return
    _, subscription_id_raw, raw_value = parts
    try:
        subscription_id = int(subscription_id_raw)
    except ValueError:
        await answer_callback_once(callback, DEAD_BUTTON)
        return

    currency = parse_currency(raw_value)
    await _drop_flow(callback, state)
    if not await _load_subscription(callback, db, subscription_id):
        return
    if currency is None:
        await send_pricing_settings(callback, db, subscription_id, toast=DEAD_BUTTON)
        return
    await db.update_subscription_fields(subscription_id, base_currency=currency)
    await send_pricing_settings(
        callback, db, subscription_id, banner=shown_in_banner(currency), toast=f"Shown in: {currency}"
    )


@admin_router.message(SubscriptionForm.due_date)
async def subscription_form_due(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    chat_id = message.chat.id
    today = datetime.now(parse_timezone(await resolve_report_timezone(db))).date()
    due = parse_date_loose(message.text, today)
    if due is None:
        await _show_wizard_step(bot, state, db, chat_id, 4, answer=message, hint=DATE_HINT)
        return
    data = await state.get_data()
    if due < today and data.get("pending_due") != due.isoformat():
        # Warn once; sending the same past date again keeps it.
        await _show_wizard_step(
            bot, state, db, chat_id, 4, answer=message, hint=PAST_DATE_HINT, data={"pending_due": due.isoformat()}
        )
        return
    await _show_wizard_step(
        bot, state, db, chat_id, 5, answer=message, data={"due_date": due.isoformat(), "pending_due": None}
    )


def parse_period_input(text: Optional[str]) -> Optional[int]:
    """'monthly' / 'month' -> MONTHLY_PERIOD_SENTINEL; '30', '30 days', '30d' -> 30; else None."""
    raw = " ".join((text or "").strip().lower().split())
    if raw in {"month", "monthly", "every month"}:
        return MONTHLY_PERIOD_SENTINEL
    for suffix in (" days", " day", "days", "day", "d"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)].strip()
            break
    if not raw.isdigit():
        return None
    days = int(raw)
    return days if 0 < days <= MAX_PERIOD_DAYS else None


@admin_router.message(SubscriptionForm.period)
async def subscription_form_period(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    period = parse_period_input(message.text)
    if period is None:
        await _show_wizard_step(bot, state, db, message.chat.id, 5, answer=message, hint=PERIOD_HINT)
        return
    await _show_wizard_step(bot, state, db, message.chat.id, 6, answer=message, data={"period_days": period})


@admin_router.callback_query(F.data.startswith("sharelimit:"))
async def handle_share_limit_quick_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    value = callback.data.split(":", 1)[1].lower()
    if value == "all":
        share_limit: Optional[int] = None
    else:
        await answer_callback_once(callback, DEAD_BUTTON)
        return
    current_state = await state.get_state()
    if current_state == SubscriptionForm.share_limit.state:
        await _finalize_new_subscription(callback, state, db, share_limit)
        return

    if current_state == SubscriptionEditForm.share_limit.state:
        sub_id = await get_edit_subscription_id(state)
        if sub_id is None:
            await _stale_wizard_button(callback, state)
            return
        from .participants import finish_share_count_button  # participants imports this module

        await finish_share_count_button(callback, state, db, sub_id, share_limit)
        return

    await _stale_wizard_button(callback, state)


@admin_router.callback_query(F.data.startswith("period:"))
async def handle_period_quick_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    value = callback.data.split(":", 1)[1]
    if value.lower() == "month":
        days = MONTHLY_PERIOD_SENTINEL
    else:
        try:
            days = int(value)
            if days <= 0:
                raise ValueError
        except ValueError:
            await answer_callback_once(callback, DEAD_BUTTON)
            return

    current_state = await state.get_state()
    if current_state == SubscriptionForm.period.state:
        if callback.message is None or callback.bot is None:
            await _stale_wizard_button(callback, state)
            return
        await _show_wizard_step(callback.bot, state, db, callback.message.chat.id, 6, data={"period_days": days})
        await answer_callback_once(callback, f"Repeats: {_period_text(days)}")
        return

    if current_state == SubscriptionEditForm.period.state:
        sub_id = await get_edit_subscription_id(state)
        if sub_id is None or callback.message is None:
            await _stale_wizard_button(callback, state)
            return
        await _finish_period_edit(callback, state, db, sub_id, days)
        return

    await _stale_wizard_button(callback, state)


@admin_router.callback_query(F.data.startswith("create_payment_mode:"))
async def handle_create_payment_mode_quick_select(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    current_state = await state.get_state()
    if current_state != SubscriptionForm.payment_mode.state:
        await _stale_wizard_button(callback, state)
        return

    mode = callback.data.split(":", 1)[1].strip().lower()
    if mode not in {PAYMENT_MODE_SPLIT, PAYMENT_MODE_FIXED}:
        await answer_callback_once(callback, DEAD_BUTTON)
        return
    await state.update_data(payment_mode=mode)

    if mode == PAYMENT_MODE_FIXED:
        await _finalize_new_subscription(callback, state, db, share_limit=None)
        return

    if callback.message is None or callback.bot is None:
        await _stale_wizard_button(callback, state)
        return
    await _show_wizard_step(callback.bot, state, db, callback.message.chat.id, 7)
    await answer_callback_once(callback, "Payment mode: split")


PAYMENT_MODE_WORDS = {
    "split": PAYMENT_MODE_SPLIT,
    "shares": PAYMENT_MODE_SPLIT,
    "share": PAYMENT_MODE_SPLIT,
    "split by shares": PAYMENT_MODE_SPLIT,
    "fixed": PAYMENT_MODE_FIXED,
    "amount": PAYMENT_MODE_FIXED,
    "fixed per user": PAYMENT_MODE_FIXED,
}


@admin_router.message(SubscriptionForm.payment_mode)
async def subscription_form_payment_mode(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    mode = PAYMENT_MODE_WORDS.get(" ".join((message.text or "").strip().lower().split()))
    if mode is None:
        await _show_wizard_step(bot, state, db, message.chat.id, 6, answer=message, hint=PAYMENT_MODE_HINT)
        return

    await state.update_data(payment_mode=mode)
    if mode == PAYMENT_MODE_FIXED:
        await _finalize_new_subscription(message, state, db, share_limit=None)
        return
    await _show_wizard_step(bot, state, db, message.chat.id, 7, answer=message)


def parse_share_limit_input(text: Optional[str]) -> tuple[bool, Optional[int]]:
    """``(True, None)`` for 'everyone' / 'all', ``(True, 4)`` for '4', ``(False, None)`` otherwise."""
    raw = (text or "").strip().lower()
    if raw in {"everyone", "all"}:
        return True, None
    if raw.isdigit() and 0 < int(raw) <= MAX_SHARE_LIMIT:
        return True, int(raw)
    return False, None


@admin_router.message(SubscriptionForm.share_limit)
async def subscription_form_share_limit(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    valid, share_limit = parse_share_limit_input(message.text)
    if not valid:
        await _show_wizard_step(bot, state, db, message.chat.id, 7, answer=message, hint=SHARE_LIMIT_HINT)
        return
    await _finalize_new_subscription(message, state, db, share_limit)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "open"))
async def handle_subscription_open(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await _drop_flow(callback, state)
    await send_subscription_detail(callback, db, callback_data.subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "open_new"))
async def handle_subscription_open_new(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    """The card as a NEW message from a push, alert or report; the push itself is left alone."""
    card = await build_subscription_card(db, callback_data.subscription_id, close=True)
    if card is None:
        await answer_callback_once(callback, SUBSCRIPTION_GONE)
        return
    text, markup = card
    sent = None
    if callback.message is not None:
        sent = await callback.message.answer(text, reply_markup=markup)
    elif callback.bot is not None:
        sent = await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)
    # Its sub-screens come back through plain sub:open; remembered, it keeps [✖️ Close].
    remember_spawned(sent)
    await answer_callback_once(callback)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "create"))
async def handle_subscription_create(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await start_subscription_creation(callback, state)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "back"))
async def handle_subscription_back(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    """[◀️ To list]: the list page that holds this subscription."""
    await _drop_flow(callback, state)
    await send_subscription_list(callback, db, focus_subscription_id=callback_data.subscription_id)


@admin_router.callback_query(F.data.startswith(SUBSCRIPTION_LIST_PAGE_PREFIX))
async def handle_subscription_list_page(callback: CallbackQuery, db: Database) -> None:
    raw_page = (callback.data or "")[len(SUBSCRIPTION_LIST_PAGE_PREFIX):]
    page = int(raw_page) if raw_page.isdigit() else 1
    await send_subscription_list(callback, db, page=page)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "rename"))
async def handle_subscription_rename_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.rename,
        f"✏️ New name for {escape_html(subscription['name'])}?",
        origin_screen=f"card:{callback_data.subscription_id}",
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "pricing"))
async def handle_subscription_pricing_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    """[💰 Pricing] on the card, [◀️ Back] from its pickers and [✖️ Cancel] on the conflict question."""
    await _drop_flow(callback, state)
    await send_pricing_settings(callback, db, callback_data.subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "amount"))
async def handle_subscription_amount_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    snapshot_due = await _open_cycle_with_frozen_amount(db, callback_data.subscription_id)
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.amount,
        amount_prompt_text(subscription, snapshot_due),
        origin_screen=_pricing_origin(callback_data.subscription_id),
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "currency"))
async def handle_subscription_currency_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    await _drop_flow(callback, state)
    await respond_with_markup(
        callback,
        currency_menu_text(subscription),
        reply_markup=subscription_currency_keyboard(callback_data.subscription_id, subscription_currency_of(subscription)),
    )


@admin_router.callback_query(F.data.startswith("sub_currency:"))
async def handle_subscription_currency_select(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await answer_callback_once(callback, DEAD_BUTTON)
        return
    _, subscription_id_raw, raw_currency = parts
    try:
        subscription_id = int(subscription_id_raw)
    except ValueError:
        await answer_callback_once(callback, DEAD_BUTTON)
        return
    currency = parse_currency(raw_currency)
    await _drop_flow(callback, state)
    if not await _load_subscription(callback, db, subscription_id):
        return
    if currency is None:
        await send_pricing_settings(callback, db, subscription_id, toast=DEAD_BUTTON)
        return
    await db.update_subscription_fields(subscription_id, currency=currency)
    await send_pricing_settings(
        callback, db, subscription_id, banner=currency_banner(currency), toast=f"Currency: {currency}"
    )


@admin_router.callback_query(F.data.startswith("sub_currency_other:"))
async def handle_subscription_currency_other(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    parts = callback.data.split(":", 1)
    if len(parts) != 2:
        await answer_callback_once(callback, DEAD_BUTTON)
        return
    _, subscription_id_raw = parts
    try:
        subscription_id = int(subscription_id_raw)
    except ValueError:
        await answer_callback_once(callback, DEAD_BUTTON)
        return
    subscription = await _load_subscription(callback, db, subscription_id)
    if not subscription:
        return
    await start_subscription_edit_flow(
        callback,
        state,
        subscription_id,
        SubscriptionEditForm.currency,
        currency_other_prompt_text(subscription),
        origin_screen=_pricing_origin(subscription_id),
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "basecurrency"))
async def handle_subscription_base_currency_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    await _drop_flow(callback, state)
    await respond_with_markup(
        callback,
        shown_in_menu_text(subscription),
        reply_markup=subscription_base_currency_keyboard(
            callback_data.subscription_id,
            subscription_shown_in_of(subscription),
        ),
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "basecurrency_other"))
async def handle_subscription_base_currency_other(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.base_currency,
        shown_in_other_prompt_text(subscription),
        origin_screen=_pricing_origin(callback_data.subscription_id),
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "duedate"))
async def handle_subscription_due_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    dropped = await _cycles_without_payments(db, callback_data.subscription_id)
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.due_date,
        due_date_prompt_text(subscription, await resolve_report_timezone(db), dropped),
        origin_screen=_pricing_origin(callback_data.subscription_id),
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "period"))
async def handle_subscription_period_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    subscription = await _load_subscription(callback, db, callback_data.subscription_id)
    if not subscription:
        return
    current_period = _subscription_period(subscription)
    await start_subscription_edit_flow(
        callback,
        state,
        callback_data.subscription_id,
        SubscriptionEditForm.period,
        period_edit_prompt_text(subscription),
        reply_markup=build_period_keyboard(selected=current_period),
        origin_screen=_pricing_origin(callback_data.subscription_id),
    )


# Reports and cycles (WP8): sub:more (legacy), sub:report, sub:cycles, cycle:*.


@admin_router.callback_query(SubscriptionAction.filter(F.action == "more"))
async def handle_subscription_more_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    """The old 'more' submenu is gone (Delete and Reports are on the card).

    On a card sent before the redesign this button was labelled '📊 Reports',
    so a tap there opens the report (it has [◀️ To card]). Anywhere else (the
    old report's Back, the old submenu) it redraws the card.
    """
    await _drop_flow(callback, state)
    subscription_id = callback_data.subscription_id
    if _tapped_on_old_card(callback, subscription_id):
        await send_subscription_payment_report(callback, db, subscription_id)
        return
    await send_subscription_more(callback, db, subscription_id)


def _tapped_on_old_card(callback: CallbackQuery, subscription_id: int) -> bool:
    """The tapped message is an old card: next to sub:more it still offers sub:pricing."""
    markup = getattr(callback.message, "reply_markup", None)
    if not isinstance(markup, InlineKeyboardMarkup):
        return False
    pricing = SubscriptionAction(action="pricing", subscription_id=subscription_id).pack()
    return any(button.callback_data == pricing for row in markup.inline_keyboard for button in row)


# Payment (sub:paymentinfo), the method picker and its choices: app/handlers/subscription_payment.py (WP7).


@admin_router.callback_query(SubscriptionAction.filter(F.action == "report"))
async def handle_subscription_report_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await _drop_flow(callback, state)
    await send_subscription_payment_report(callback, db, callback_data.subscription_id)


@admin_router.callback_query(SubscriptionAction.filter(F.action == "cycles"))
async def handle_subscription_cycles_callback(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    await _drop_flow(callback, state)
    await send_subscription_open_cycles(callback, db, callback_data.subscription_id)


@admin_router.callback_query(CycleAction.filter(F.action == "open"))
async def handle_cycle_open_callback(
    callback: CallbackQuery,
    callback_data: CycleAction,
    db: Database,
    state: FSMContext,
) -> None:
    """The cycle screen; a closed cycle redraws the open cycles with 'This cycle is already closed'."""
    await _drop_flow(callback, state)
    await send_subscription_cycle_actions(
        callback,
        db,
        callback_data.subscription_id,
        callback_data.due_date,
    )


@admin_router.callback_query(CycleAction.filter(F.action == "recreate"))
async def handle_cycle_recreate_callback(
    callback: CallbackQuery,
    callback_data: CycleAction,
    db: Database,
    state: FSMContext,
) -> None:
    """♻️ Recreate (also on old messages) asks first: '⚠️ <b>Recreate the … cycle?</b>'."""
    await _drop_flow(callback, state)
    await send_cycle_confirm(callback, db, callback_data.subscription_id, callback_data.due_date, close=False)


@admin_router.callback_query(CycleAction.filter(F.action == "force_close"))
async def handle_cycle_force_close_callback(
    callback: CallbackQuery,
    callback_data: CycleAction,
    db: Database,
    state: FSMContext,
) -> None:
    """🔒 Close cycle (the old '✅ Force close' too) asks first: '⚠️ <b>Close the … cycle?</b>'."""
    await _drop_flow(callback, state)
    await send_cycle_confirm(callback, db, callback_data.subscription_id, callback_data.due_date, close=True)


@admin_router.callback_query(CycleAction.filter(F.action == CYCLE_RECREATE_YES))
async def handle_cycle_recreate_yes(
    callback: CallbackQuery,
    callback_data: CycleAction,
    db: Database,
) -> None:
    """Recreate the cycle from the current people and settings, then redraw it with a banner."""
    subscription_id, due_value = callback_data.subscription_id, callback_data.due_date
    async with guarded(callback, ("cycle", (subscription_id, due_value))) as acquired:
        if not acquired:
            return
        subscription = await _load_open_cycle(callback, db, subscription_id, due_value)
        if not subscription:
            return
        await db.reset_cycle(subscription_id, due_value)
        await db.ensure_cycle(subscription_id, due_value)
        await send_subscription_cycle_actions(
            callback,
            db,
            subscription_id,
            due_value,
            banner=CYCLE_RECREATED_BANNER,
            toast=toast_from_html(CYCLE_RECREATED_BANNER),
        )


async def force_close_cycle(db: Database, subscription: Dict[str, object], due_value: str) -> int:
    """Mark everyone unpaid in the cycle as paid, close it and move the next charge on.

    Returns how many people were marked as paid.
    """
    subscription_id = int(subscription["id"])  # type: ignore[call-overload]
    await db.freeze_cycle_snapshot(subscription_id, due_value)
    cycle_state = (await db.list_cycle_participants_for_due_dates(subscription_id, [due_value])).get(due_value) or {}
    live_participants = await db.list_subscription_participants(subscription_id)
    participant_ids = sorted(
        {
            int(person.get("telegram_id") or 0)
            for person in _resolve_cycle_participants(cycle_state, live_participants)
            if int(person.get("telegram_id") or 0) > 0
        }
    )
    paid_ids = {
        int(row["paid_by_telegram_id"])
        for row in await db.list_payments_for_cycles(subscription_id, [due_value])
        if row.get("paid_by_telegram_id") is not None
    }
    added_count = 0
    for telegram_id in participant_ids:
        if telegram_id in paid_ids:
            continue
        await db.log_payment(subscription_id, due_value, telegram_id)
        added_count += 1
    await db.close_cycle(subscription_id, due_value)
    await _advance_subscription_after_cycle_close(db, subscription, due_value)
    return added_count


@admin_router.callback_query(CycleAction.filter(F.action == CYCLE_FORCE_CLOSE_YES))
async def handle_cycle_force_close_yes(
    callback: CallbackQuery,
    callback_data: CycleAction,
    db: Database,
) -> None:
    """Close the cycle (everyone unpaid marked as paid), then the open cycles with a banner."""
    subscription_id, due_value = callback_data.subscription_id, callback_data.due_date
    async with guarded(callback, ("cycle", (subscription_id, due_value))) as acquired:
        if not acquired:
            return
        subscription = await _load_open_cycle(callback, db, subscription_id, due_value)
        if not subscription:
            return
        marked = await force_close_cycle(db, subscription, due_value)
        banner = cycle_closed_banner(marked)
        await send_subscription_open_cycles(
            callback,
            db,
            subscription_id,
            banner=banner,
            toast=toast_from_html(banner),
        )


# The comment prompt (sub:comment, sub:comment_clear) lives in app/handlers/subscription_payment.py (WP7).


def subscription_delete_question(name: str, open_cycles: int, participants: int) -> str:
    """'⚠️ <b>Delete Netflix?</b>' and one consequence line (``name`` is plain text)."""
    if open_cycles:
        what = f"Its {plural(open_cycles, 'open cycle')} and payment history are deleted"
    else:
        what = "Its payment history is deleted"
    tail = "; members stop getting reminders." if participants else "."
    return f"⚠️ <b>Delete {escape_html(name)}?</b>\n{what}{tail}"


@admin_router.callback_query(SubscriptionAction.filter(F.action == "delete"))
async def handle_subscription_delete_prompt(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
    state: FSMContext,
) -> None:
    """Edit the card into the question above the card: [✖️ Cancel][🗑 Delete]."""
    await _drop_flow(callback, state)
    subscription_id = callback_data.subscription_id
    subscription = await _load_subscription(callback, db, subscription_id)
    if not subscription:
        return
    card = await build_subscription_card(db, subscription_id)
    if card is None:
        await show_subscription_gone(callback, db)
        return
    question = subscription_delete_question(
        str(subscription["name"]),
        len(await db.list_open_cycles(subscription_id)),
        len(await db.list_subscription_participants(subscription_id)),
    )
    await respond_with_markup(
        callback,
        f"{question}\n\n{card[0]}",
        reply_markup=subscription_delete_confirm_keyboard(subscription_id),
    )


@admin_router.callback_query(SubscriptionAction.filter(F.action == "confirm_delete"))
async def handle_subscription_delete_confirm(
    callback: CallbackQuery,
    callback_data: SubscriptionAction,
    db: Database,
) -> None:
    """Delete once (guarded; another admin may have deleted it already), then the list with a banner."""
    subscription_id = callback_data.subscription_id
    async with guarded(callback, ("sub_delete", subscription_id)) as acquired:
        if not acquired:
            return
        subscription = await db.get_subscription(subscription_id)
        if not subscription:
            await show_subscription_gone(callback, db)
            return
        await db.delete_subscription(subscription_id)
        banner = f"✅ {escape_html(subscription['name'])} deleted"
        await send_subscription_list(callback, db, banner=banner, toast=toast_from_html(banner))


RENAME_HINT = "⚠️ Send text, for example <code>Netflix Premium</code>."
RENAME_TOO_LONG_HINT = (
    f"⚠️ Keep the name under {SUBSCRIPTION_NAME_MAX_LEN} characters, for example <code>Netflix Premium</code>."
)


@admin_router.message(SubscriptionEditForm.rename)
async def edit_subscription_rename(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    new_name = " ".join((message.text or "").split())
    if not new_name:
        await note_typo(message, state, RENAME_HINT)
        return
    if len(new_name) > SUBSCRIPTION_NAME_MAX_LEN:
        await note_typo(message, state, RENAME_TOO_LONG_HINT)
        return

    if not await db.get_subscription(sub_id):
        await finish_prompt(message, state)
        await send_subscription_list(message, db, banner=SUBSCRIPTION_GONE_BANNER)
        return
    await db.update_subscription_fields(sub_id, name=new_name)
    flow_data = await finish_prompt(message, state)
    await show_card_after_prompt(
        message, db, sub_id, flow_data, banner=f"✅ Renamed · <b>{escape_html(new_name)}</b>"
    )


@admin_router.message(SubscriptionEditForm.amount)
async def edit_subscription_amount(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    parsed = parse_amount(message.text)
    if parsed is None or not math.isfinite(parsed[0]):
        await note_typo(message, state, AMOUNT_HINT)
        return
    if parsed[0] <= 0:
        await note_typo(message, state, AMOUNT_ZERO_HINT)
        return

    subscription = await db.get_subscription(sub_id)
    if not subscription:
        await _pricing_prompt_gone(message, state, db)
        return
    await db.update_subscription_fields(sub_id, amount=parsed[0])
    flow_data = await finish_prompt(message, state)
    banner = amount_banner(parsed[0], subscription_currency_of(subscription))
    await show_pricing_after_prompt(message, db, sub_id, flow_data, banner=banner)


@admin_router.message(SubscriptionEditForm.currency)
async def edit_subscription_currency(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    currency = parse_currency(message.text)
    if currency is None:
        await note_typo(message, state, CURRENCY_OTHER_HINT)
        return

    if not await db.get_subscription(sub_id):
        await _pricing_prompt_gone(message, state, db)
        return
    await db.update_subscription_fields(sub_id, currency=currency)
    flow_data = await finish_prompt(message, state)
    await show_pricing_after_prompt(message, db, sub_id, flow_data, banner=currency_banner(currency))


@admin_router.message(SubscriptionEditForm.base_currency)
async def edit_subscription_base_currency(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    currency = parse_currency(message.text)
    if currency is None:
        await note_typo(message, state, CURRENCY_HINT)
        return

    if not await db.get_subscription(sub_id):
        await _pricing_prompt_gone(message, state, db)
        return
    await db.update_subscription_fields(sub_id, base_currency=currency)
    flow_data = await finish_prompt(message, state)
    await show_pricing_after_prompt(message, db, sub_id, flow_data, banner=shown_in_banner(currency))


async def _apply_due_date_change(db: Database, subscription: Dict[str, object], due_date) -> None:
    sub_id = int(subscription["id"])
    await db.discard_unpaid_open_cycles(sub_id, keep_due=due_date.isoformat())
    update_fields: Dict[str, object] = {"next_charge_at": due_date}
    if int(subscription.get("period_days") or 30) == MONTHLY_PERIOD_SENTINEL:
        update_fields["monthly_anchor_day"] = due_date.day
    await db.update_subscription_fields(sub_id, **update_fields)


async def _find_due_date_conflict_cycle(
    db: Database,
    subscription: Dict[str, object],
    due_date,
) -> Optional[Dict[str, object]]:
    """Return the cycle whose payments would be affected by moving the charge date.

    Two cases matter:
    - an open cycle that already has recorded payments (a reschedule must keep them);
    - the latest closed (fully paid) cycle whose billing period still covers the
      new date — asking everyone to pay again within the same period would be a
      double charge.
    """
    sub_id = int(subscription["id"])
    new_value = due_date.isoformat()

    open_cycles = await db.list_open_cycles(sub_id)
    paid_open: List[str] = []
    for due_value in open_cycles:
        if due_value == new_value:
            continue
        if await db.count_cycle_payments(sub_id, due_value) > 0:
            paid_open.append(due_value)
    if paid_open:
        old_next = str(subscription.get("next_charge_at") or "")
        candidate = old_next if old_next in paid_open else paid_open[0]
        return {
            "due_date": candidate,
            "closed": False,
            "payments": await db.count_cycle_payments(sub_id, candidate),
        }

    latest_closed = await db.get_latest_closed_cycle(sub_id)
    if latest_closed:
        closed_due_value = str(latest_closed["due_date"])
        try:
            closed_due = datetime.strptime(closed_due_value, "%Y-%m-%d").date()
        except ValueError:
            return None
        period_days = int(subscription.get("period_days") or 30)
        monthly_anchor_day = normalize_monthly_anchor_day(subscription.get("monthly_anchor_day"))
        period_end = calculate_next_charge_date(closed_due, period_days, monthly_anchor_day)
        if closed_due < due_date < period_end:
            return {
                "due_date": closed_due_value,
                "closed": True,
                "payments": await db.count_cycle_payments(sub_id, closed_due_value),
            }
    return None


@admin_router.message(SubscriptionEditForm.due_date)
async def edit_subscription_due_date(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    tz_name = await resolve_report_timezone(db)
    today = datetime.now(parse_timezone(tz_name)).date()
    due_date = parse_date_loose(message.text, today)
    if due_date is None:
        await note_typo(message, state, NEXT_CHARGE_HINT)
        return
    if due_date < today and (await state.get_data()).get("pending_due") != due_date.isoformat():
        # Warn once, as the creation wizard does; sending the same past date again keeps it.
        await note_typo(message, state, PAST_DATE_HINT)
        await state.update_data(pending_due=due_date.isoformat())
        return

    subscription = await db.get_subscription(sub_id)
    if not subscription:
        await _pricing_prompt_gone(message, state, db)
        return

    conflict = await _find_due_date_conflict_cycle(db, subscription, due_date)
    flow_data = await finish_prompt(message, state)
    if conflict:
        # Ask on the Pricing screen the prompt came from; both answers redraw Pricing.
        text = due_date_conflict_text(conflict, due_date.isoformat(), tz_name)
        markup = build_due_date_change_keyboard(sub_id, str(conflict["due_date"]), due_date.isoformat())
        origin = origin_of(flow_data)
        if origin is None or message.bot is None or not await edit_in_place(message.bot, *origin, text, markup):
            await message.answer(text, reply_markup=markup)
        return

    await _apply_due_date_change(db, subscription, due_date)
    await show_pricing_after_prompt(
        message, db, sub_id, flow_data, banner=next_charge_banner(due_date.isoformat(), tz_name)
    )


def _parse_iso_date(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


@admin_router.callback_query(DueDateChangeAction.filter(F.action == "move"))
async def handle_due_date_move(
    callback: CallbackQuery,
    callback_data: DueDateChangeAction,
    db: Database,
    state: FSMContext,
) -> None:
    """[🔀 Move cycle]: the cycle and its payments move to the new date; back to Pricing."""
    await _drop_flow(callback, state)
    sub_id = callback_data.subscription_id
    async with guarded(callback, ("duedate", sub_id)) as acquired:
        if not acquired:
            return
        subscription = await _load_subscription(callback, db, sub_id)
        if not subscription:
            return
        new_date = _parse_iso_date(callback_data.new_due)
        if new_date is None:
            await send_pricing_settings(callback, db, sub_id, toast=DEAD_BUTTON)
            return
        cycle = await db.get_cycle(sub_id, callback_data.old_due)
        if not cycle:
            await stale_redirect(callback, CYCLE_GONE_TOAST, lambda: send_pricing_settings(callback, db, sub_id))
            return
        if not await db.move_cycle(sub_id, callback_data.old_due, callback_data.new_due):
            await answer_callback_once(callback, MOVE_BLOCKED_ALERT, alert=True)
            return
        await db.discard_unpaid_open_cycles(sub_id, keep_due=callback_data.new_due)

        period_days = _subscription_period(subscription)
        update_fields: Dict[str, object] = {}
        if period_days == MONTHLY_PERIOD_SENTINEL:
            update_fields["monthly_anchor_day"] = new_date.day
        if cycle.get("closed_at"):
            monthly_anchor_day = new_date.day if period_days == MONTHLY_PERIOD_SENTINEL else None
            update_fields["next_charge_at"] = calculate_next_charge_date(new_date, period_days, monthly_anchor_day)
        else:
            update_fields["next_charge_at"] = new_date
        await db.update_subscription_fields(sub_id, **update_fields)
        await send_pricing_settings(
            callback, db, sub_id, banner=CYCLE_MOVED_BANNER, toast=toast_from_html(CYCLE_MOVED_BANNER)
        )


@admin_router.callback_query(DueDateChangeAction.filter(F.action == "new"))
async def handle_due_date_new_cycle(
    callback: CallbackQuery,
    callback_data: DueDateChangeAction,
    db: Database,
    state: FSMContext,
) -> None:
    """[🆕 New cycle]: the old cycle stays, payment is asked on the new date; back to Pricing."""
    await _drop_flow(callback, state)
    sub_id = callback_data.subscription_id
    async with guarded(callback, ("duedate", sub_id)) as acquired:
        if not acquired:
            return
        subscription = await _load_subscription(callback, db, sub_id)
        if not subscription:
            return
        new_date = _parse_iso_date(callback_data.new_due)
        if new_date is None:
            await send_pricing_settings(callback, db, sub_id, toast=DEAD_BUTTON)
            return
        await _apply_due_date_change(db, subscription, new_date)
        banner = new_cycle_banner(new_date.isoformat(), await resolve_report_timezone(db))
        await send_pricing_settings(callback, db, sub_id, banner=banner, toast=toast_from_html(banner))


async def _apply_period(db: Database, subscription: Dict[str, object], period: int) -> None:
    """Save the period; Monthly keeps the anchor day (or takes it from the next charge)."""
    monthly_anchor_day: Optional[int] = None
    if period == MONTHLY_PERIOD_SENTINEL:
        monthly_anchor_day = normalize_monthly_anchor_day(subscription.get("monthly_anchor_day"))
        if monthly_anchor_day is None:
            next_charge = _parse_iso_date(str(subscription.get("next_charge_at")))
            monthly_anchor_day = next_charge.day if next_charge else None
    await db.update_subscription_fields(
        int(subscription["id"]),  # type: ignore[arg-type]
        period_days=period,
        monthly_anchor_day=monthly_anchor_day,
    )


async def _finish_period_edit(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    sub_id: int,
    period: int,
) -> None:
    """A period preset tapped on the prompt: save, remove the prompt, redraw Pricing with a banner."""
    message = callback.message
    data = await state.get_data()
    if message is None or data.get("prompt_message_id") not in (None, message.message_id):
        # A preset on an older prompt than the one that is running.
        await _stale_wizard_button(callback, state)
        return
    subscription = await db.get_subscription(sub_id)
    if not subscription:
        await _drop_flow(callback, state)
        await show_subscription_gone(callback, db)
        return
    await _apply_period(db, subscription, period)
    flow_data = await finish_prompt(message, state, bot=callback.bot)  # type: ignore[arg-type]
    banner = period_banner(period)
    screen = await build_pricing_screen(db, sub_id, banner=banner)
    if screen is not None:
        text, markup = screen
        origin = origin_of(flow_data)
        bot = callback.bot
        if origin is None or bot is None or not await edit_in_place(bot, *origin, text, markup):
            await message.answer(text, reply_markup=markup)
    await answer_callback_once(callback, f"Period: {period_phrase(period)}")


@admin_router.message(SubscriptionEditForm.period)
async def edit_subscription_period(message: Message, state: FSMContext, db: Database) -> None:
    sub_id = await require_edit_subscription_id(message, state)
    if sub_id is None:
        return

    period = parse_period_input(message.text)
    if period is None:
        await note_typo(message, state, PERIOD_EDIT_HINT)
        return

    subscription = await db.get_subscription(sub_id)
    if not subscription:
        await _pricing_prompt_gone(message, state, db)
        return
    await _apply_period(db, subscription, period)
    flow_data = await finish_prompt(message, state)
    await show_pricing_after_prompt(message, db, sub_id, flow_data, banner=period_banner(period))


# The typed comment is handled in app/handlers/subscription_payment.py (WP7).
