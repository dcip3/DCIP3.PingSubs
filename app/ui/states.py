from __future__ import annotations

from typing import Union

from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message


class FriendForm(StatesGroup):
    telegram_id = State()
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


class SubscriptionForm(StatesGroup):
    name = State()
    amount = State()
    currency = State()
    due_date = State()
    period = State()
    share_limit = State()


class SubscriptionEditForm(StatesGroup):
    rename = State()
    amount = State()
    currency = State()
    base_currency = State()
    due_date = State()
    period = State()
    share_limit = State()
    reminder_time = State()
    reminder_offsets = State()
    comment = State()


class MemberEditForm(StatesGroup):
    full_name = State()


class SubscriptionAction(CallbackData, prefix="sub"):
    action: str
    subscription_id: int


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


class TestSendAction(CallbackData, prefix="testsend"):
    subscription_id: int
    telegram_id: int


class TestPaidAction(CallbackData, prefix="testpaid"):
    subscription_id: int
    due_date: str


class MemberAction(CallbackData, prefix="member"):
    action: str
    friend_id: int


Responder = Union[Message, CallbackQuery]
