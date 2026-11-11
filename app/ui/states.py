from __future__ import annotations

from typing import Union

from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message


class FriendForm(StatesGroup):
    full_name = State()


class SettingsForm(StatesGroup):
    base_time = State()
    base_timezone = State()


class PublicSettingsForm(StatesGroup):
    base_time = State()
    base_timezone = State()


class PublicReminderForm(StatesGroup):
    reminder_time = State()


class PublicSubscriptionCurrencyForm(StatesGroup):
    currency = State()


class PublicAccountForm(StatesGroup):
    full_name = State()


class SubscriptionForm(StatesGroup):
    name = State()
    amount = State()
    currency = State()
    due_date = State()
    period = State()
    payment_mode = State()
    share_limit = State()


class SubscriptionEditForm(StatesGroup):
    rename = State()
    amount = State()
    currency = State()
    base_currency = State()
    user_amount = State()
    user_amount_all = State()
    due_date = State()
    period = State()
    share_limit = State()
    reminder_time = State()
    reminder_offsets = State()
    comment = State()


class MemberEditForm(StatesGroup):
    full_name = State()
    balance = State()
    balance_currency = State()


class PaymentDestinationForm(StatesGroup):
    title = State()
    currency = State()
    details = State()
    payment_link = State()


class PaymentDestinationEditForm(StatesGroup):
    title = State()
    currency = State()
    details = State()
    payment_link = State()


class TopUpForm(StatesGroup):
    amount = State()


class SubscriptionAction(CallbackData, prefix="sub"):
    action: str
    subscription_id: int


class CycleAction(CallbackData, prefix="cycle"):
    action: str
    subscription_id: int
    due_date: str


class DueDateChangeAction(CallbackData, prefix="duedate"):
    action: str
    subscription_id: int
    old_due: str
    new_due: str


class ParticipantAction(CallbackData, prefix="spart"):
    action: str
    subscription_id: int
    friend_id: int


class ReminderAction(CallbackData, prefix="remind"):
    subscription_id: int
    due_date: str


class ReminderSendAction(CallbackData, prefix="remsend"):
    subscription_id: int
    telegram_id: int


class ReminderSendConfirmAction(CallbackData, prefix="remsendok"):
    """The [📬 Send] of the send-now confirmation; same fields as ReminderSendAction (0 = every unpaid)."""

    subscription_id: int
    telegram_id: int


class TestSendAction(CallbackData, prefix="testsend"):
    telegram_id: int


class TestListAction(CallbackData, prefix="testlist"):
    action: str
    page: int


class TestPaidAction(CallbackData, prefix="testpaid"):
    subscription_id: int
    due_date: str


class MemberAction(CallbackData, prefix="member"):
    """Users section buttons. ``friend_id`` is the page number for ``page``.

    Actions: page, back, add, open, open_new (the card as a NEW message with
    [✖️ Close], from admin alerts), report, invite (now asks first),
    invite_yes (makes the new sign-in link), balance, balance_set,
    balance_currency, balance_currency_other, rename, payment_destination,
    delete (asks first), confirm_delete.
    """

    action: str
    friend_id: int


class PaymentDestinationAction(CallbackData, prefix="paydest"):
    """Admin 💳 Payment buttons (handlers in app/handlers/payment_methods.py).

    Actions: menu, open, create, edit_title, edit_currency, edit_details,
    edit_link, clear_link (the link prompt's [🗑 Clear link]), set_default,
    delete (asks first), confirm_delete. The wizard's quick buttons use the
    raw callbacks 'paydest_wiz_cur:<CODE>' and 'paydest_wiz_skip'.
    """

    action: str
    destination_id: int


class TopUpAction(CallbackData, prefix="topup"):
    """Top-up buttons (handlers in app/handlers/topups.py); ``request_id`` is 0 when unused.

    Member: start (the picker on 👤 Account), start_new (the picker as a NEW
    message, from pushes), submit (✅ I paid), cancel (drops the unsent
    request screen). The picker's methods use the raw 'topup_destination:<id>'.
    Admin: admin_list (📥 Pending top-ups), admin_open (a review from the list),
    review (the push redrawn after Keep), approve, reject (now asks first),
    reject_yes (rejects).
    """

    action: str
    request_id: int


Responder = Union[Message, CallbackQuery]
