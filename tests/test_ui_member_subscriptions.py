"""WP9 member subscriptions: the list, the card, 🌐 Shown in, ⏰ Reminder time, ✅ Paid and the report."""

import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.reminders import tg_clock, tg_due
from app.services import CurrencyConverter
from app.ui import guards
from app.ui.helpers import NO_ACCESS, SUBSCRIPTION_GONE
from app.ui.keyboards import (
    MENU_SUBSCRIPTIONS,
    build_public_subscription_list_keyboard,
    member_subscription_list_label,
    public_subscription_currency_keyboard,
    public_subscription_detail_keyboard,
    public_subscription_reminder_time_keyboard,
    public_subscription_report_keyboard,
)
from app.ui.member_subscriptions import (
    ALREADY_PAID_TOAST,
    CURRENCY_TYPO_HINT,
    LIST_EMPTY_TEXT,
    MemberCard,
    currency_prompt,
    currency_text,
    list_text,
    member_card_text,
    opened_as_new,
    paid_banner,
    reminder_prompt,
    reminder_text,
    report_text,
    shown_in_banner,
    shown_in_toast,
)
from app.ui.prompts import CLEANUP_IDS, ORIGIN_MESSAGE_ID, ORIGIN_SCREEN, PROMPT_MESSAGE_ID
from app.ui.states import PublicReminderForm, PublicSubscriptionCurrencyForm, SubscriptionAction
from app.ui.style import SUBITEM_INDENT
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import USER_ID, EditMessageText, SendMessage, make_message
from test_keyboards_fit import LONG_NAME, LONG_REQUISITES, LONG_SUB, NAME_64, USER_TEXTS, assert_fits

install_keyboard_guard()

NBSP = " "
I = SUBITEM_INDENT  # noqa: E741 - the block indent, as in the screens
OTHER_ADMIN = 1
BOB_ID = 2002
TZ = "UTC"
BIG_ID = 2_000_000_000


def _layout(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _callbacks(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[button.callback_data or "" for button in row] for row in markup.inline_keyboard]


def _paid_callback(subscription_id: int, due: date) -> str:
    return f"cycle:public_paid:{subscription_id}:{due.isoformat()}"


def _sub(action: str, subscription_id: int) -> str:
    return SubscriptionAction(action=action, subscription_id=subscription_id).pack()


# ===================================================================== texts


TODAY = date(2026, 11, 10)
CARD = MemberCard(
    subscription_id=7,
    name="Netflix",
    tz_name=TZ,
    today=TODAY,
    currency="RUB",
    repeats="every month on the 12th",
    unpaid_dues=("2026-10-10",),
    share=250.0,
    converted=(2.75, "USD"),
    next_charge="2026-11-12",
    next_paid=True,
    reminder_time="10:00",
    reminder_default=True,
    shown_in="EUR",
    shown_in_default=True,
    method="Sber · RUB",
    details="2200 1234 5678 9012\nAnna P.",
    link="https://pay.example/x",
    comment="Pay by the 12th",
)


def _expected_card(card: MemberCard = CARD) -> str:
    return (
        "🏷️ <b>Netflix</b>\n\n"
        "⚠️ <b>Unpaid</b>\n"
        f"{I}<b>{tg_due('2026-10-10', TZ, 'wD')}</b> · overdue by 31 days\n\n"
        "💵 <b>Your share</b>\n"
        f"{I}Amount: <b>250.00{NBSP}RUB</b> (≈ 2.75{NBSP}USD)\n"
        f"{I}Next charge: <b>{tg_due('2026-11-12', TZ, 'wD')}</b> ({tg_due('2026-11-12', TZ, 'r', 'in 2 days')}) · ✅ paid\n"
        f"{I}Repeats: every month on the 12th\n\n"
        "⏰ <b>Reminder</b>\n"
        f"{I}<b>{tg_clock('10:00', TZ, '10:00')}</b> your time · default\n"
        f"{I}Shown in: EUR (default)\n\n"
        "💳 <b>How to pay</b>\n"
        f"{I}Sber · RUB\n"
        "<blockquote expandable><code>2200 1234 5678 9012</code>\n<code>Anna P.</code></blockquote>\n"
        f"{I}📝 Pay by the 12th"
    )


class MemberTextTests(unittest.TestCase):
    def test_list_text(self):
        self.assertEqual(
            list_text(2, 1),
            "📋 <b>Subscriptions</b> · 2\n\n⚠️ Unpaid: <b>1</b>\n\nTap one to see your share and how to pay.",
        )
        self.assertEqual(list_text(3, 0), "📋 <b>Subscriptions</b> · 3\n\nTap one to see your share and how to pay.")
        self.assertEqual(list_text(0, 0), LIST_EMPTY_TEXT)
        self.assertEqual(
            LIST_EMPTY_TEXT, "📋 <b>Subscriptions</b>\n\nYou are not in any subscription yet. The admin adds you."
        )

    def test_full_member_card(self):
        self.assertEqual(member_card_text(CARD), _expected_card())
        self.assertNotIn("Default (", member_card_text(CARD))

    def test_banner_sits_on_top(self):
        banner = paid_banner("Netflix", "2026-10-10", TZ)
        self.assertEqual(banner, f"✅ Paid · Netflix <b>{tg_due('2026-10-10', TZ, 'wD')}</b>")
        self.assertEqual(member_card_text(CARD, banner=banner), f"{banner}\n\n{_expected_card()}")

    def test_today_unpaid_and_many_unpaid_cycles(self):
        dues = tuple((TODAY - timedelta(days=30 * n)).isoformat() for n in range(6, -1, -1))
        card = MemberCard(**{**CARD.__dict__, "unpaid_dues": dues})
        text = member_card_text(card)
        self.assertIn(f"{I}<b>{tg_due(dues[0], TZ, 'wD')}</b> · overdue by 180 days\n", text)
        self.assertNotIn(tg_due(dues[-1], TZ, "wD"), text, "only the 5 oldest are listed")
        self.assertIn(f"{I}…and 2 more cycles\n", text)
        today_only = MemberCard(**{**CARD.__dict__, "unpaid_dues": (TODAY.isoformat(),)})
        self.assertIn(f"<b>{tg_due(TODAY.isoformat(), TZ, 'wD')}</b> · due today\n", member_card_text(today_only))

    def test_payment_block_without_copy_button_or_openable_link(self):
        card = MemberCard(
            **{
                **CARD.__dict__,
                "details": "Ask Anna <in person>",
                "link": "pay.example/x",
                "comment": "x" * 600,
                "method": None,
                "converted": None,
                "next_paid": False,
                "unpaid_dues": (),
            }
        )
        text = member_card_text(card)
        self.assertTrue(text.startswith("🏷️ <b>Netflix</b>\n\n💵 <b>Your share</b>\n"), text)
        self.assertIn(f"{I}Amount: <b>250.00{NBSP}RUB</b>\n", text)
        self.assertIn(
            "💳 <b>How to pay</b>\n"
            f"{I}Not set yet. Ask the admin how to pay.\n"
            f"{I}<code>Ask Anna &lt;in person&gt;</code>\n"
            f"{I}🔗 pay.example/x\n"
            f"{I}📝 Comment\n"
            "<blockquote expandable>",
            text,
        )
        self.assertNotIn("✅ paid", text)

    def test_long_requisites_stay_within_the_message_limit(self):
        card = MemberCard(**{**CARD.__dict__, "details": "\n".join([LONG_REQUISITES] * 20)})
        self.assertLess(len(member_card_text(card)), 4096)

    def test_admin_view_adds_amount_participants_and_days(self):
        card = MemberCard(
            **{
                **CARD.__dict__,
                "admin_view": True,
                "admin_amount": 12500.0,
                "admin_participants": ("👥 <b>Participants: 1</b>", f"{I}• Anna — <b>≈ 1.00 RUB</b>"),
                "admin_reminder_days": "1 day before, on the day · After due date: daily until paid",
            }
        )
        text = member_card_text(card)
        self.assertTrue(text.startswith(f"🏷️ <b>Netflix</b> · <b>12{NBSP}500.00{NBSP}RUB</b>\n\n"), text)
        self.assertIn("Repeats: every month on the 12th\n\n👥 <b>Participants: 1</b>\n", text)
        self.assertIn(f"Shown in: EUR (default)\n{I}1 day before, on the day · After due date: daily until paid\n\n", text)

    def test_sub_screen_texts(self):
        self.assertEqual(
            currency_text("Netflix", "EUR", "EUR", has_override=False),
            "🌐 <b>Shown in · Netflix</b>\n\nNow: EUR (default)\n"
            "Your reminders show your share converted to this currency.",
        )
        self.assertEqual(
            currency_text("Net<flix>", "USD", "EUR", has_override=True),
            "🌐 <b>Shown in · Net&lt;flix&gt;</b>\n\nNow: USD\nDefault: EUR\n"
            "Your reminders show your share converted to this currency.",
        )
        self.assertEqual(
            reminder_text("Netflix", "10:00", "10:00", "Europe/Berlin", has_override=False),
            "⏰ <b>Reminder time · Netflix</b>\n\n"
            f"Now: <b>{tg_clock('10:00', 'Europe/Berlin', '10:00')}</b> your time (default)\n"
            "Your timezone: Europe/Berlin — change it in “⚙️ Settings”.",
        )
        self.assertIn(
            f"Now: <b>{tg_clock('12:00', TZ, '12:00')}</b> your time\nDefault: 10:00\n",
            reminder_text("Netflix", "12:00", "10:00", TZ, has_override=True),
        )
        self.assertEqual(
            currency_prompt("Netflix"),
            "✏️ Currency for Netflix reminders? Three letters, for example <code>CHF</code>.",
        )
        self.assertEqual(
            reminder_prompt("Netflix", "Europe/Berlin"),
            "✏️ Reminder time for Netflix? <code>HH:MM</code> in your timezone (Europe/Berlin).",
        )
        self.assertEqual(shown_in_toast("USD"), "Shown in: USD")
        self.assertEqual(shown_in_toast(None), "Shown in: default")
        self.assertEqual(shown_in_banner("CHF"), "✅ Shown in · <b>CHF</b>")
        self.assertEqual(shown_in_banner("EUR", default=True), "✅ Shown in · <b>EUR</b> (default)")
        self.assertEqual(report_text("Netflix", "🟢 <b>Netflix</b>"), "📊 <b>Reports · Netflix</b>\n\n🟢 <b>Netflix</b>")


# ================================================================= keyboards


class MemberKeyboardTests(unittest.TestCase):
    def test_list_rows_keep_the_tail_and_fit(self):
        rows = [
            {"id": BIG_ID, "name": LONG_NAME, "attention": "overdue", "due_value": "2026-11-20"},
            {"id": BIG_ID + 1, "name": NAME_64, "attention": "due today", "due_value": ""},
            {"id": BIG_ID + 2, "name": LONG_SUB, "attention": "", "due_value": "2026-11-20"},
            {"id": 3, "name": "Spotify", "attention": "", "due_value": "2026-11-20"},
            {"id": 4, "name": "iCloud", "attention": "", "due_value": ""},
        ]
        markup = build_public_subscription_list_keyboard(rows)
        assert_fits(markup, (*USER_TEXTS, *(f"⚠️ {name}" for name in (LONG_NAME, NAME_64))))
        labels = [row[0] for row in _layout(markup)]
        self.assertTrue(labels[0].startswith("⚠️ W") and labels[0].endswith("… · overdue"), labels[0])
        self.assertTrue(labels[1].endswith("… · due today"), labels[1])
        self.assertTrue(labels[2].endswith(" · 20.11"), labels[2])
        self.assertEqual(labels[3:], ["Spotify · 20.11", "iCloud"])
        self.assertEqual(_callbacks(markup)[3], ["sub:open_public:3"])
        for row in markup.inline_keyboard:
            self.assertLessEqual(len(row[0].callback_data.encode()), 64)
        self.assertEqual(member_subscription_list_label({"name": "Netflix", "attention": "overdue"}), "⚠️ Netflix · overdue")

    def test_card_keyboard_full_has_six_rows_and_one_primary(self):
        markup = public_subscription_detail_keyboard(
            BIG_ID,
            paid_due="2026-10-10",
            payment_details="2200 1234 5678 9012",
            payment_link="https://pay.example/x",
        )
        layout = _layout(markup)
        self.assertEqual(len(layout), 6)
        self.assertEqual(layout[0], ["✅ Paid · 10.10"])
        self.assertEqual(layout[1][0], "📋 Copy card number ···9012")
        self.assertEqual(layout[2], ["🔗 Open payment link"])
        # One name per concept: "🌐 Shown in" and "⏰ Reminder time", as on the admin screens.
        self.assertEqual(layout[3:], [["⏰ Reminder time"], ["🌐 Shown in", "📊 Reports"], ["◀️ To list"]])
        self.assertEqual(
            _callbacks(markup)[3:],
            [
                [_sub("public_remindertime", BIG_ID)],
                [_sub("public_currency", BIG_ID), _sub("public_report", BIG_ID)],
                [_sub("public_back", BIG_ID)],
            ],
        )
        # The Paid button names its cycle in the callback, so a stale card pays only that one.
        self.assertEqual(_callbacks(markup)[0], [f"cycle:public_paid:{BIG_ID}:2026-10-10"])
        self.assertEqual([b.style for row in markup.inline_keyboard for b in row].count("primary"), 1)
        assert_fits(markup)

    def test_card_keyboard_without_unpaid_and_opened_as_new(self):
        markup = public_subscription_detail_keyboard(7, payment_details=LONG_REQUISITES, close=True)
        layout = _layout(markup)
        self.assertNotIn("✅", " ".join(sum(layout, [])))
        self.assertEqual(layout[-1], ["✖️ Close"])
        self.assertEqual(_callbacks(markup)[-1], ["menu:close"])
        self.assertTrue(opened_as_new(type("M", (), {"reply_markup": markup})()))
        self.assertFalse(opened_as_new(type("M", (), {"reply_markup": public_subscription_detail_keyboard(7)})()))
        # An old card in the chat ([⬅️ Back][✖️ Close]) belongs to the list, not to a push.
        old_card = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="⬅️ Back", callback_data=_sub("public_back", 7)),
                    InlineKeyboardButton(text="✖️ Close", callback_data="menu:close"),
                ]
            ]
        )
        self.assertFalse(opened_as_new(type("M", (), {"reply_markup": old_card})()))
        self.assertFalse(opened_as_new(None))
        assert_fits(markup)

    def test_currency_picker(self):
        markup = public_subscription_currency_keyboard(BIG_ID, "EUR", "EUR", has_override=False)
        self.assertEqual(
            _layout(markup),
            [["✅ Default (EUR)"], ["RUB", "USD"], ["EUR", "TRY"], ["✏️ Other currency"], ["◀️ Back"]],
        )
        self.assertEqual(
            _callbacks(markup),
            [
                [_sub("public_currency_default", BIG_ID)],
                [f"public_sub_currency:{BIG_ID}:{code}" for code in ("RUB", "USD")],
                [f"public_sub_currency:{BIG_ID}:{code}" for code in ("EUR", "TRY")],
                [f"public_sub_currency_other:{BIG_ID}"],
                [_sub("open_public", BIG_ID)],
            ],
        )
        assert_fits(markup)
        # An explicit override is the one marked, even when it equals the default.
        markup = public_subscription_currency_keyboard(BIG_ID, "EUR", "EUR", has_override=True)
        self.assertEqual(_layout(markup)[:3], [["Default (EUR)"], ["RUB", "USD"], ["✅ EUR", "TRY"]])
        markup = public_subscription_currency_keyboard(BIG_ID, "CHF", "EUR", close=True)
        self.assertEqual(_layout(markup)[:3], [["Default (EUR)"], ["RUB", "USD"], ["EUR", "TRY"]])
        self.assertEqual(_callbacks(markup)[-1], [_sub("public_card_close", BIG_ID)])
        assert_fits(markup)
        for row in markup.inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)

    def test_reminder_picker(self):
        markup = public_subscription_reminder_time_keyboard(BIG_ID, "10:00", "10:00", has_override=False)
        self.assertEqual(
            _layout(markup),
            [["✅ Default (10:00)"], ["09:00", "12:00"], ["16:00", "20:00"], ["✏️ Other time"], ["◀️ Back"]],
        )
        self.assertEqual(_callbacks(markup)[0], [_sub("public_remindertime_default", BIG_ID)])
        self.assertEqual(_callbacks(markup)[1], [f"public_sub_remindertime:{BIG_ID}:09:00", f"public_sub_remindertime:{BIG_ID}:12:00"])
        self.assertEqual(_callbacks(markup)[3], [f"public_sub_remindertime_other:{BIG_ID}"])
        self.assertEqual(_callbacks(markup)[4], [_sub("open_public", BIG_ID)])
        assert_fits(markup)
        markup = public_subscription_reminder_time_keyboard(BIG_ID, "12:00", "10:00", close=True)
        self.assertEqual(_layout(markup)[:2], [["Default (10:00)"], ["09:00", "✅ 12:00"]])
        self.assertEqual(_callbacks(markup)[-1], [_sub("public_card_close", BIG_ID)])
        assert_fits(markup)
        for row in markup.inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)

    def test_report_keyboard(self):
        markup = public_subscription_report_keyboard(BIG_ID)
        self.assertEqual(_layout(markup), [["◀️ To card"]])
        self.assertEqual(_callbacks(markup), [[_sub("open_public", BIG_ID)]])
        assert_fits(markup)
        self.assertEqual(_callbacks(public_subscription_report_keyboard(BIG_ID, close=True)), [[_sub("public_card_close", BIG_ID)]])


# ===================================================================== flows


class _MemberCase(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # No network in tests: a rate lookup fails unless a test says otherwise.
        patcher = mock.patch.object(CurrencyConverter, "convert_to", mock.AsyncMock(side_effect=RuntimeError("offline")))
        self.convert = patcher.start()
        self.addCleanup(patcher.stop)
        await self.db.set_setting("base_timezone", TZ)
        await self.db.set_setting("base_reminder_time", "10:00")  # main() stores it at startup
        await self.make_admin(OTHER_ADMIN)
        self.anna = await self._friend("Anna Petrova", USER_ID)
        self.bob = await self._friend("Bob", BOB_ID)
        self.today = datetime.now(ZoneInfo(TZ)).date()
        self.overdue = self.today - timedelta(days=3)
        self.upcoming = self.today + timedelta(days=5)
        self.netflix = await self.db.create_subscription("Netflix", 30.0, "USD", self.upcoming, 30, None)
        for friend_id in (self.anna, self.bob):
            await self.db.set_participant(self.netflix, friend_id, True)
        await self.db.ensure_cycle(self.netflix, self.overdue)
        await self.db.ensure_cycle(self.netflix, self.upcoming)

    async def _friend(self, name: str, telegram_id: int) -> int:
        friend_id = await self.db.create_friend_invite(name, f"join_{telegram_id}", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(friend_id, telegram_id, "2026-11-01T00:00:00")
        return friend_id

    def _screen(self, message_id: int = 7001, markup: InlineKeyboardMarkup | None = None):
        message = make_message(self.bot, message_id, "screen", from_bot=True)
        if markup is not None:
            message = message.model_copy(update={"reply_markup": markup}).as_(self.bot)
        return message

    def _answers(self) -> list[str | None]:
        return [answer.text for answer in self.bot.answers()]

    def _paid(self, due: date) -> str:
        """[✅ Paid · dd.mm] on the Netflix card: the callback carries the cycle."""
        return _paid_callback(self.netflix, due)

    async def _paid_ids(self, due: date) -> set[int]:
        rows = await self.db.list_payments_for_cycles(self.netflix, [due.isoformat()])
        return {int(row["paid_by_telegram_id"]) for row in rows if row.get("paid_by_telegram_id") is not None}


class ListAndCardFlowTests(_MemberCase):
    async def test_menu_list_then_card_then_back_to_the_list_in_place(self):
        await self.feed(self.message(MENU_SUBSCRIPTIONS))
        sent = self.bot.of(SendMessage)[-1]
        self.assertEqual(
            sent.text,
            "📋 <b>Subscriptions</b> · 1\n\n⚠️ Unpaid: <b>1</b>\n\nTap one to see your share and how to pay.",
        )
        self.assertEqual(_layout(sent.reply_markup), [["⚠️ Netflix · overdue"]])

        await self.feed(self.callback(_sub("open_public", self.netflix), self._screen()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7001)
        self.assertTrue(edit.text.startswith("🏷️ <b>Netflix</b>\n\n⚠️ <b>Unpaid</b>\n"), edit.text)
        self.assertIn(f"{I}Amount: <b>15.00{NBSP}USD</b>\n", edit.text)
        self.assertIn(f"{I}Not set yet. Ask the admin how to pay.", edit.text)
        self.assertEqual(
            _layout(edit.reply_markup),
            [[f"✅ Paid · {self.overdue:%d.%m}"], ["⏰ Reminder time"], ["🌐 Shown in", "📊 Reports"], ["◀️ To list"]],
        )
        self.assertEqual(self._answers(), [None])

        sent_before = len(self.bot.of(SendMessage))
        await self.feed(self.callback(_sub("public_back", self.netflix), self._screen()))
        self.assertEqual(len(self.bot.of(SendMessage)), sent_before, "the list is edited in, not sent twice")
        self.assertTrue(self.bot.of(EditMessageText)[-1].text.startswith("📋 <b>Subscriptions</b> · 1"))

    async def test_left_or_deleted_subscription_redraws_the_list_with_a_toast(self):
        await self.db.set_participant(self.netflix, self.anna, False)
        await self.feed(self.callback(_sub("open_public", self.netflix), self._screen()))
        self.assertEqual(self._answers(), [NO_ACCESS])
        self.assertEqual(self.bot.of(EditMessageText)[-1].text, LIST_EMPTY_TEXT)

        await self.db.delete_subscription(self.netflix)
        for data in (
            _sub("public_currency", self.netflix),
            _sub("public_paid", self.netflix),
            self._paid(self.overdue),
            f"public_sub_currency:{self.netflix}:USD",
            f"public_sub_remindertime_other:{self.netflix}",
        ):
            await self.feed(self.callback(data, self._screen()))
            self.assertEqual(self._answers()[-1], SUBSCRIPTION_GONE)
            self.assertEqual(self.bot.of(EditMessageText)[-1].text, LIST_EMPTY_TEXT)
        self.assertFalse(self.bot.of(SendMessage))

    async def test_opened_as_new_keeps_close_through_its_sub_screens(self):
        report = self._screen(7010)
        await self.feed(self.callback(_sub("public_open_new", self.netflix), report))
        self.assertFalse(self.bot.of(EditMessageText), "the report is left alone")
        card = self.bot.of(SendMessage)[-1]
        self.assertEqual(_layout(card.reply_markup)[-1], ["✖️ Close"])

        card_message = self._screen(7011, card.reply_markup)
        await self.feed(self.callback(_sub("public_currency", self.netflix), card_message))
        picker = self.bot.of(EditMessageText)[-1]
        self.assertEqual(_callbacks(picker.reply_markup)[-1], [_sub("public_card_close", self.netflix)])

        picker_message = self._screen(7011, picker.reply_markup)
        await self.feed(self.callback(f"public_sub_currency:{self.netflix}:EUR", picker_message))
        back_on_card = self.bot.of(EditMessageText)[-1]
        self.assertEqual(_layout(back_on_card.reply_markup)[-1], ["✖️ Close"])
        self.assertEqual(self._answers()[-1], "Shown in: EUR")

        await self.feed(self.callback(_sub("public_card_close", self.netflix), picker_message))
        self.assertEqual(_layout(self.bot.of(EditMessageText)[-1].reply_markup)[-1], ["✖️ Close"])

        # A left subscription on a pushed copy: a toast only, the copy stays.
        await self.db.set_participant(self.netflix, self.anna, False)
        edits = len(self.bot.of(EditMessageText))
        await self.feed(self.callback(_sub("public_currency", self.netflix), card_message))
        self.assertEqual(self._answers()[-1], NO_ACCESS)
        self.assertEqual(len(self.bot.of(EditMessageText)), edits)

    async def test_converted_share_when_a_rate_is_known(self):
        await self.db.set_user_subscription_setting(USER_ID, self.netflix, "target_currency", "EUR")
        await self.feed(self.callback(_sub("open_public", self.netflix), self._screen()))
        text = self.bot.of(EditMessageText)[-1].text
        self.assertIn(f"Amount: <b>15.00{NBSP}USD</b>\n", text, "no rate: the '≈' part is dropped")
        self.assertIn(f"{I}Shown in: EUR\n", text)

        self.convert.side_effect = None
        self.convert.return_value = 1350.0
        await self.feed(self.callback(_sub("open_public", self.netflix), self._screen()))
        self.assertIn(
            f"Amount: <b>15.00{NBSP}USD</b> (≈ 1{NBSP}350.00{NBSP}EUR)\n", self.bot.of(EditMessageText)[-1].text
        )


class PaidFlowTests(_MemberCase):
    async def test_paid_records_the_oldest_cycle_and_redraws_with_a_banner(self):
        await self.feed(self.callback(self._paid(self.overdue), self._screen()))

        self.assertEqual(await self._paid_ids(self.overdue), {USER_ID})
        self.assertEqual(await self._paid_ids(self.upcoming), set())
        edit = self.bot.of(EditMessageText)[-1]
        banner = paid_banner("Netflix", self.overdue.isoformat(), TZ)
        self.assertTrue(edit.text.startswith(f"{banner}\n\n🏷️ <b>Netflix</b>\n\n💵 <b>Your share</b>"), edit.text)
        self.assertEqual(_layout(edit.reply_markup)[0], ["⏰ Reminder time"])
        self.assertEqual(self._answers(), [f"✅ Paid · Netflix {self.overdue:%d.%m.%Y}"])
        admin_notes = [call for call in self.bot.of(SendMessage) if int(call.chat_id) == OTHER_ADMIN]
        self.assertEqual(len(admin_notes), 1)
        # WP15 admin push: the stored name as a tg://user link, not the Telegram profile name.
        self.assertTrue(
            admin_notes[0].text.startswith(f'💸 <b><a href="tg://user?id={USER_ID}">Anna Petrova</a> paid Netflix</b>'),
            admin_notes[0].text,
        )

        await self.feed(self.callback(self._paid(self.overdue), self._screen()))
        self.assertEqual(self._answers()[-1], ALREADY_PAID_TOAST)
        self.assertEqual(await self._paid_ids(self.upcoming), set(), "a second tap never pays ahead")

    async def test_last_payer_closes_the_cycle(self):
        await self.db.log_payment(self.netflix, self.overdue, BOB_ID)
        await self.feed(self.callback(self._paid(self.overdue), self._screen()))
        self.assertNotIn(self.overdue.isoformat(), await self.db.list_open_cycles(self.netflix))

    async def test_double_tap_is_guarded(self):
        # The same key as the reminder's Paid: taps on both pay once.
        key = guards.pay_cycle_key(self.netflix, self.overdue.isoformat(), USER_ID)
        guards._in_progress.add(key)
        try:
            await self.feed(self.callback(self._paid(self.overdue), self._screen()))
        finally:
            guards._in_progress.discard(key)
        self.assertEqual(self._answers(), [guards.ALREADY_RUNNING_TEXT])
        self.assertEqual(await self._paid_ids(self.overdue), set())


class CurrencyFlowTests(_MemberCase):
    async def test_picker_and_preset(self):
        await self.feed(self.callback(_sub("public_currency", self.netflix), self._screen(7002)))
        picker = self.bot.of(EditMessageText)[-1]
        self.assertEqual(picker.text, currency_text("Netflix", "USD", "USD", has_override=False))
        self.assertEqual(_layout(picker.reply_markup)[0], ["✅ Default (USD)"])

        await self.feed(self.callback(f"public_sub_currency:{self.netflix}:RUB", self._screen(7002)))
        self.assertEqual(await self.db.get_user_subscription_setting(USER_ID, self.netflix, "target_currency"), "RUB")
        # A preset gets the same result banner as typed input.
        self.assertTrue(self.bot.of(EditMessageText)[-1].text.startswith("✅ Shown in · <b>RUB</b>\n\n🏷️ <b>Netflix</b>"))
        self.assertEqual(self._answers()[-1], "Shown in: RUB")

        await self.feed(self.callback(_sub("public_currency_default", self.netflix), self._screen(7002)))
        self.assertIsNone(await self.db.get_user_subscription_setting(USER_ID, self.netflix, "target_currency"))
        self.assertEqual(self._answers()[-1], "Shown in: default")

    async def test_other_currency_prompt_typo_and_success(self):
        await self.feed(self.callback(f"public_sub_currency_other:{self.netflix}", self._screen(7002)))
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, currency_prompt("Netflix"))
        self.assertEqual(_layout(prompt.reply_markup), [["↺ Use default"], ["✖️ Cancel"]])
        self.assertEqual(await self.state().get_state(), PublicSubscriptionCurrencyForm.currency.state)
        data = await self.state().get_data()
        self.assertEqual(data[ORIGIN_MESSAGE_ID], 7002)
        prompt_id = data[PROMPT_MESSAGE_ID]

        typo = self.message("euro")
        await self.feed(typo)
        self.assertEqual(self.bot.of(SendMessage)[-1].text, CURRENCY_TYPO_HINT)
        hint_id = (await self.state().get_data())[CLEANUP_IDS][-1]

        answer = self.message(" chf ")
        sent_before = len(self.bot.of(SendMessage))
        await self.feed(answer)
        self.assertEqual(await self.db.get_user_subscription_setting(USER_ID, self.netflix, "target_currency"), "CHF")
        self.assertIsNone(await self.state().get_state())
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7002)
        self.assertTrue(edit.text.startswith("✅ Shown in · <b>CHF</b>\n\n🏷️ <b>Netflix</b>"), edit.text)
        self.assertEqual(len(self.bot.of(SendMessage)), sent_before)
        self.assertEqual(sorted(self.bot.deleted_ids()), sorted([prompt_id, typo.message_id, hint_id, answer.message_id]))

    async def test_use_default_on_the_prompt_and_old_keywords(self):
        await self.db.set_user_subscription_setting(USER_ID, self.netflix, "target_currency", "RUB")
        await self.feed(self.callback(f"public_sub_currency_other:{self.netflix}", self._screen(7002)))
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]
        prompt_message = self._screen(prompt_id)
        await self.feed(self.callback(_sub("public_currency_default", self.netflix), prompt_message))
        self.assertIsNone(await self.db.get_user_subscription_setting(USER_ID, self.netflix, "target_currency"))
        self.assertIsNone(await self.state().get_state())
        self.assertIn(prompt_id, self.bot.deleted_ids())
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7002)
        self.assertTrue(edit.text.startswith("✅ Shown in · <b>USD</b> (default)\n\n🏷️"), edit.text)
        self.assertEqual(self._answers()[-1], "✅ Shown in · USD (default)")

        await self.db.set_user_subscription_setting(USER_ID, self.netflix, "target_currency", "RUB")
        await self.feed(self.callback(f"public_sub_currency_other:{self.netflix}", self._screen(7002)))
        await self.feed(self.message("base"))
        self.assertIsNone(await self.db.get_user_subscription_setting(USER_ID, self.netflix, "target_currency"))

    async def test_a_pick_while_the_prompt_is_open_winds_it_down(self):
        await self.feed(self.callback(f"public_sub_currency_other:{self.netflix}", self._screen(7002)))
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]
        await self.feed(self.callback(f"public_sub_currency:{self.netflix}:EUR", self._screen(7002)))
        self.assertIsNone(await self.state().get_state())
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertEqual(self.bot.of(EditMessageText)[-1].message_id, 7002)

    async def test_out_of_date_prompt(self):
        await self.set_state(PublicSubscriptionCurrencyForm.currency)
        await self.feed(self.message("EUR"))
        self.assertIsNone(await self.state().get_state())
        self.assertIn("This prompt is out of date", self.bot.of(SendMessage)[-1].text)


class ReminderFlowTests(_MemberCase):
    async def test_picker_preset_and_default(self):
        await self.feed(self.callback(_sub("public_remindertime", self.netflix), self._screen(7003)))
        picker = self.bot.of(EditMessageText)[-1]
        self.assertEqual(picker.text, reminder_text("Netflix", "10:00", "10:00", TZ, has_override=False))
        self.assertEqual(_layout(picker.reply_markup)[0], ["✅ Default (10:00)"])

        await self.feed(self.callback(f"public_sub_remindertime:{self.netflix}:12:00", self._screen(7003)))
        self.assertEqual(await self.db.get_user_subscription_setting(USER_ID, self.netflix, "reminder_time"), "12:00")
        self.assertEqual(self._answers()[-1], "Reminder time: 12:00")
        self.assertEqual(self.bot.of(EditMessageText)[-1].message_id, 7003)

        await self.feed(self.callback(_sub("public_remindertime_default", self.netflix), self._screen(7003)))
        self.assertIsNone(await self.db.get_user_subscription_setting(USER_ID, self.netflix, "reminder_time"))
        self.assertEqual(self._answers()[-1], "Reminder time: default")

    async def test_other_time_prompt(self):
        await self.feed(self.callback(f"public_sub_remindertime_other:{self.netflix}", self._screen(7003)))
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, reminder_prompt("Netflix", TZ))
        self.assertEqual(_layout(prompt.reply_markup), [["↺ Use default"], ["✖️ Cancel"]])
        self.assertEqual(await self.state().get_state(), PublicReminderForm.reminder_time.state)
        self.assertEqual((await self.state().get_data())[ORIGIN_SCREEN], "member_card")

        await self.feed(self.message("25:99"))
        self.assertEqual(self.bot.of(SendMessage)[-1].text, "⚠️ Need a time like <code>16:00</code>.")
        await self.feed(self.message("9"))
        self.assertEqual(await self.db.get_user_subscription_setting(USER_ID, self.netflix, "reminder_time"), "09:00")
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7003)
        self.assertTrue(edit.text.startswith("✅ Reminder time · <b>09:00</b>\n\n🏷️ <b>Netflix</b>"), edit.text)

        await self.feed(self.callback(f"public_sub_remindertime_other:{self.netflix}", self._screen(7003)))
        await self.feed(self.message("default"))
        self.assertIsNone(await self.db.get_user_subscription_setting(USER_ID, self.netflix, "reminder_time"))
        self.assertTrue(
            self.bot.of(EditMessageText)[-1].text.startswith("✅ Reminder time · <b>10:00</b> (default)\n\n")
        )

    async def test_prompt_from_a_pushed_copy_redraws_the_copy_with_close(self):
        copy_markup = public_subscription_reminder_time_keyboard(self.netflix, "10:00", "10:00", close=True)
        await self.feed(self.callback(f"public_sub_remindertime_other:{self.netflix}", self._screen(7004, copy_markup)))
        self.assertEqual((await self.state().get_data())[ORIGIN_SCREEN], "member_card_new")
        await self.feed(self.message("16:30"))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7004)
        self.assertEqual(_layout(edit.reply_markup)[-1], ["✖️ Close"])

    async def test_gone_origin_gets_a_new_card(self):
        await self.feed(self.callback(f"public_sub_remindertime_other:{self.netflix}", self._screen(7003)))
        self.bot.fail(EditMessageText, "Bad Request: message to edit not found", message_id=7003)
        await self.feed(self.message("16:30"))
        self.assertTrue(self.bot.of(SendMessage)[-1].text.startswith("✅ Reminder time · <b>16:30</b>\n\n🏷️"))


class ReportFlowTests(_MemberCase):
    async def test_member_report_with_to_card(self):
        await self.feed(self.callback(_sub("public_report", self.netflix), self._screen(7005)))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertTrue(edit.text.startswith("📊 <b>Reports · Netflix</b>\n\n🔴 <b>Netflix</b>"), edit.text)
        self.assertEqual(_layout(edit.reply_markup), [["◀️ To card"]])
        self.assertEqual(_callbacks(edit.reply_markup), [[_sub("open_public", self.netflix)]])

    async def test_admin_outside_the_subscription_may_open_it(self):
        await self.db.set_participant(self.netflix, self.anna, False)
        await self.make_admin(USER_ID)
        await self.feed(self.callback(_sub("public_report", self.netflix), self._screen(7005)))
        self.assertTrue(self.bot.of(EditMessageText)[-1].text.startswith("📊 <b>Reports · Netflix</b>\n\n"))
        self.assertNotIn(NO_ACCESS, self._answers())


class PushPaidStillWorksTests(_MemberCase):
    async def test_reminder_paid_uses_the_shared_service(self):
        await self.feed(self.callback(f"remind:{self.netflix}:{self.overdue.isoformat()}", self._screen(7006)))
        self.assertEqual(await self._paid_ids(self.overdue), {USER_ID})
        self.assertEqual(self._answers(), ["✅ Payment recorded"])
        self.assertTrue(
            any(call.text.startswith("💸 <b>") and "paid Netflix</b>" in call.text for call in self.bot.of(SendMessage))
        )


if __name__ == "__main__":
    unittest.main()
