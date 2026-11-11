"""WP15: reminder pushes, the Paid tap, receipts, admin payment pushes and the test reminder."""

import re
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup

from app.core.reminders import tg_due
from app.handlers.reminders import batch_total_from_text, mark_closed_button, mark_paid_button
from app.services import (
    CurrencyConverter,
    _build_batch_reminder_message,
    _build_reminder_message,
    _run_reminder_pass,
    format_converted_amount,
)
from app.storage.db import Database
from app.ui import guards
from app.ui.helpers import CYCLE_CLOSED, SUBSCRIPTION_GONE
from app.ui.keyboards import (
    balance_receipt_keyboard,
    batch_paid_button_label,
    build_batch_payment_confirmation_keyboard,
    build_payment_confirmation_keyboard,
    build_test_payment_confirmation_keyboard,
    no_participants_push_keyboard,
    subscription_push_keyboard,
)
from app.ui.push_screens import (
    BATCH_HINT,
    PAID_HINT,
    TEST_FOOTER,
    balance_receipt_text,
    batch_receipt_text,
    cycle_closed_text,
    due_phrase,
    no_participants_text,
    paid_receipt_text,
    payment_recorded_text,
    reminders_sent_text,
)
from app.ui.states import SubscriptionAction
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import USER_ID, EditMessageReplyMarkup, EditMessageText, SendMessage, make_message
from test_keyboards_fit import LONG_REQUISITES, LONG_SUB, assert_fits

install_keyboard_guard()

NBSP = "\u00a0"
TZ = "Europe/Moscow"
TODAY = date(2026, 9, 17)
CARD_DETAILS = "Sber card 2202 2032 1234 5678\nAnna K."
PHONE_DETAILS = "SBP +7 999 123-45-67 Tinkoff"
BIG_ID = 2_000_000_000
FIVE_DIGITS = f"12{NBSP}500.00{NBSP}RUB"


def _layout(markup: InlineKeyboardMarkup | None) -> list[list[str]]:
    return [[button.text for button in row] for row in (markup.inline_keyboard if markup else [])]


def _callbacks(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[button.callback_data or "" for button in row] for row in markup.inline_keyboard]


def _item(subscription_id: int, name: str, due: date, details: str = CARD_DETAILS, **extra) -> dict:
    item = {
        "subscription_id": subscription_id,
        "subscription_name": name,
        "due_date": due,
        "due_value": due.isoformat(),
        "tz_name": TZ,
        "today": TODAY,
        "amount_text": f"4.17{NBSP}USD",
        "converted_text": f"380.50{NBSP}RUB",
        "payment_label": "Sber · RUB",
        "payment_details": details,
        "payment_link": "",
    }
    item.update(extra)
    return item


# ===================================================================== texts


class ReminderTextTests(unittest.TestCase):
    def test_single_reminder_layout(self):
        text = _build_reminder_message(
            person_name="Anna",
            subscription_name="Netflix",
            amount_text=f"4.17{NBSP}USD",
            converted_text=f"380.50{NBSP}RUB",
            payment_label="Sber · RUB",
            payment_details=CARD_DETAILS,
            payment_link="https://pay.example/x",
            comment="Family plan",
            due_value="2026-09-18",
            tz_name=TZ,
            today=TODAY,
            copyable=True,
            balance_used_text=f"2.00{NBSP}USD",
            left_to_pay_text=f"2.17{NBSP}USD",
        )
        self.assertEqual(
            text,
            "🔔 <b>Netflix · due tomorrow</b>\n\n"
            f"📅 {tg_due('2026-09-18', TZ, 'wD')}\n"
            f"💵 Your share: <b>4.17{NBSP}USD</b> ≈ <b>380.50{NBSP}RUB</b>\n"
            "🏦 Sber · RUB\n"
            "<blockquote expandable><code>Sber card 2202 2032 1234 5678</code>\n<code>Anna K.</code></blockquote>\n"
            "📝 Family plan\n"
            f"💰 From balance: 2.00{NBSP}USD · left to pay <b>2.17{NBSP}USD</b>\n\n"
            "Tap “✅ Paid” once you have paid.",
        )
        self.assertNotIn("Anna,", text, "no greeting line")

    def test_status_in_the_title(self):
        self.assertEqual(due_phrase("2026-09-17", TODAY), "due today")
        self.assertEqual(due_phrase("2026-09-18", TODAY), "due tomorrow")
        self.assertEqual(due_phrase("2026-09-20", TODAY), "due in 3 days")
        self.assertEqual(due_phrase("2026-09-14", TODAY), "overdue by 3 days")
        self.assertEqual(due_phrase("2026-09-16", TODAY), "overdue by 1 day")
        text = _build_reminder_message(
            subscription_name="Netflix", amount_text="1", due_value="2026-09-14", tz_name=TZ, today=TODAY
        )
        self.assertTrue(text.startswith("🔔 <b>Netflix · overdue by 3 days</b>\n\n"), text)

    def test_link_line_only_when_not_a_button(self):
        kwargs = dict(subscription_name="Netflix", amount_text="1", due_value="2026-09-17", tz_name=TZ, today=TODAY)
        self.assertNotIn("🔗", _build_reminder_message(payment_link="https://pay.example/x", **kwargs))
        self.assertIn("🔗 Link: bank app → payments", _build_reminder_message(payment_link="bank app → payments", **kwargs))
        self.assertIn("🏦 Payment method: not set", _build_reminder_message(**kwargs))
        self.assertIn(f"<pre>{PHONE_DETAILS}</pre>", _build_reminder_message(payment_details=PHONE_DETAILS, **kwargs))

    def test_long_comment_is_folded(self):
        text = _build_reminder_message(
            subscription_name="Netflix", amount_text="1", comment="x" * 400, due_value="2026-09-17", tz_name=TZ, today=TODAY
        )
        self.assertIn(f"<blockquote expandable>📝 {'x' * 400}</blockquote>", text)

    def test_test_reminder_title_and_footer(self):
        text = _build_reminder_message(
            subscription_name="Sample subscription",
            amount_text=f"1.10{NBSP}USD",
            due_value="2026-09-17",
            tz_name=TZ,
            today=TODAY,
            footer=TEST_FOOTER,
            test=True,
        )
        self.assertTrue(text.startswith("🧪 <b>Test · Sample subscription · due today</b>\n\n"), text)
        self.assertTrue(text.endswith("\n\nThis is a test. “Paid” records nothing."))

    def test_batch_layout_and_details_once(self):
        items = [
            _item(1, "Netflix", date(2026, 9, 18), copyable=True),
            _item(2, "Spotify", date(2026, 9, 20), CARD_DETAILS + "  ", copyable=True, comment="Duo"),
            _item(3, "YouTube", date(2026, 9, 14), PHONE_DETAILS, balance_used_text="1.00 USD", left_to_pay_text="3.17 USD"),
        ]
        text = _build_batch_reminder_message(items=items, total_text=f"1{NBSP}141.50{NBSP}RUB")
        indent = NBSP * 7
        self.assertEqual(
            text,
            f"🔔 <b>3 bills due</b> · total <b>1{NBSP}141.50{NBSP}RUB</b>\n\n"
            "<b>Netflix</b> · due tomorrow\n"
            f"{indent}Your share: <b>4.17{NBSP}USD</b> ≈ <b>380.50{NBSP}RUB</b>\n"
            f"{indent}Pay to: Sber · RUB\n"
            "<blockquote expandable><code>Sber card 2202 2032 1234 5678</code>\n<code>Anna K.</code></blockquote>\n\n"
            "<b>Spotify</b> · due in 3 days\n"
            f"{indent}Your share: <b>4.17{NBSP}USD</b> ≈ <b>380.50{NBSP}RUB</b>\n"
            f"{indent}Pay to: Sber · RUB (details above)\n"
            f"{indent}📝 Duo\n\n"
            "<b>YouTube</b> · overdue by 3 days\n"
            f"{indent}Your share: <b>4.17{NBSP}USD</b> ≈ <b>380.50{NBSP}RUB</b>\n"
            f"{indent}Pay to: Sber · RUB\n"
            f"{indent}💰 From balance: 1.00 USD · left to pay <b>3.17 USD</b>\n"
            f"<pre>{PHONE_DETAILS}</pre>\n\n"
            "Tap the button of each bill you have paid.",
        )

    def test_batch_without_a_total(self):
        text = _build_batch_reminder_message(items=[_item(1, "A", TODAY), _item(2, "B", TODAY)], total_text=None)
        self.assertTrue(text.startswith("🔔 <b>2 bills due</b>\n\n⚠️ Total unknown: a rate is missing\n\n"), text)
        self.assertTrue(text.endswith(BATCH_HINT))

    def test_converted_amount_uses_the_money_format(self):
        self.assertEqual(format_converted_amount(12500.456, "RUB", "precise"), f"12{NBSP}500.46{NBSP}RUB")
        self.assertEqual(format_converted_amount(12500.456, "RUB", "ceil"), f"12{NBSP}501{NBSP}RUB")
        self.assertEqual(format_converted_amount(12500.456, "RUB", "floor"), f"12{NBSP}500{NBSP}RUB")


class ReceiptAndAdminTextTests(unittest.TestCase):
    def test_paid_receipt(self):
        moment = datetime(2026, 9, 17, 9, 5, tzinfo=timezone.utc)
        self.assertEqual(
            paid_receipt_text("Netflix", "2026-09-18", TZ, f"4.17{NBSP}USD", moment),
            "✅ <b>Paid · Netflix</b>\n\n"
            f"📅 {tg_due('2026-09-18', TZ, 'wD')} · <b>4.17{NBSP}USD</b>\n"
            f'Recorded <tg-time unix="{int(moment.timestamp())}" format="t">12:05</tg-time>.',
        )

    def test_batch_receipt(self):
        self.assertEqual(
            batch_receipt_text(f"680.50{NBSP}RUB", ["Netflix · 17.09", "Spotify · 20.09"]),
            f"✅ <b>All paid</b> · 680.50{NBSP}RUB\n\n✅ Netflix · 17.09\n✅ Spotify · 20.09",
        )
        self.assertEqual(batch_receipt_text(None), "✅ <b>All paid</b>")
        self.assertEqual(batch_total_from_text(f"🔔 2 bills due · total 680.50{NBSP}RUB\n\nNetflix"), f"680.50{NBSP}RUB")
        self.assertIsNone(batch_total_from_text("🔔 2 bills due\n\n⚠️ Total unknown: a rate is missing"))

    def test_balance_receipt(self):
        self.assertEqual(
            balance_receipt_text("Netflix", "2026-09-17", TZ, f"380.00{NBSP}RUB", f"120.00{NBSP}RUB"),
            "✅ <b>Netflix paid from your balance</b>\n\n"
            f"📅 {tg_due('2026-09-17', TZ, 'wD')} · <b>380.00{NBSP}RUB</b>\n"
            f"Balance left: <b>120.00{NBSP}RUB</b>",
        )

    def test_payment_recorded_push(self):
        text = payment_recorded_text(
            payer_name="Ivan <Petrov>",
            payer_id=42,
            subscription_name="Netflix",
            due_value="2026-09-17",
            tz_name=TZ,
            amount_text=f"4.17{NBSP}USD",
            converted_text=f"380.50{NBSP}RUB",
            paid=2,
            total=3,
        )
        self.assertEqual(
            text,
            '💸 <b><a href="tg://user?id=42">Ivan &lt;Petrov&gt;</a> paid Netflix</b>\n\n'
            f"📅 {tg_due('2026-09-17', TZ, 'wD')} · <b>4.17{NBSP}USD</b> (≈ 380.50{NBSP}RUB)\n"
            "███████░░░ 2 of 3 paid",
        )
        closed = payment_recorded_text(
            payer_name="Ivan",
            payer_id=42,
            subscription_name="Netflix",
            due_value="2026-09-17",
            tz_name=TZ,
            amount_text="1",
            paid=3,
            total=3,
            closed=True,
            next_due="2026-10-17",
        )
        self.assertTrue(
            closed.endswith(
                f"██████████ 3 of 3 paid\n🔒 Cycle closed — everyone paid. Next: {tg_due('2026-10-17', TZ, 'wD')}."
            ),
            closed,
        )

    def test_cycle_closed_push(self):
        self.assertEqual(
            cycle_closed_text("Netflix", f"12.50{NBSP}USD", "2026-10-17", TZ),
            "🔒 <b>Netflix cycle closed</b>\n\n"
            f"Everyone paid · total <b>12.50{NBSP}USD</b>\n"
            f"Next charge: {tg_due('2026-10-17', TZ, 'wD')}",
        )

    def test_reminders_sent_push(self):
        self.assertEqual(
            reminders_sent_text("Netflix", "2026-09-18", TZ, TODAY, f"12.50{NBSP}USD", 2, 1),
            "📬 <b>Netflix reminders sent</b>\n\n"
            f"Due {tg_due('2026-09-18', TZ, 'wD')} (tomorrow) · <b>12.50{NBSP}USD</b>\n"
            "Reminded: <b>2</b> · paid: 1",
        )

    def test_no_participants_push(self):
        self.assertEqual(
            no_participants_text("Netflix", "2026-09-17", TZ, f"12.50{NBSP}USD"),
            "👥 <b>Netflix has no participants</b>\n\n"
            f"Due {tg_due('2026-09-17', TZ, 'wD')} · 12.50{NBSP}USD. Nobody got a reminder.",
        )


# ================================================================= keyboards


class PushKeyboardTests(unittest.TestCase):
    def test_single_and_test_keyboards_fit(self):
        for markup in (
            build_payment_confirmation_keyboard(BIG_ID, "2026-09-17", LONG_REQUISITES, "https://pay.example/x"),
            build_test_payment_confirmation_keyboard(0, "2026-09-17", LONG_REQUISITES, "https://pay.example/x"),
        ):
            assert_fits(markup)
            self.assertEqual(markup.inline_keyboard[-1][0].text, "✅ Paid")
            self.assertEqual(markup.inline_keyboard[-1][0].style, "success")

    def test_batch_labels_with_short_dates_and_long_names(self):
        items = [
            _item(BIG_ID, LONG_SUB, date(2026, 9, 17), LONG_REQUISITES, amount_text=FIVE_DIGITS),
            _item(BIG_ID - 1, "Netflix", date(2026, 9, 20), LONG_REQUISITES, amount_text=FIVE_DIGITS),
        ]
        markup = build_batch_payment_confirmation_keyboard(items, payment_details=LONG_REQUISITES, payment_link="https://x.example")
        assert_fits(markup)
        labels = [row[0].text for row in markup.inline_keyboard[2:]]
        self.assertTrue(labels[0].startswith("✅ Spotify Family") and labels[0].endswith("… · 17.09"), labels[0])
        self.assertEqual(labels[1], "✅ Netflix · 20.09")
        self.assertEqual(
            [row[0].callback_data for row in markup.inline_keyboard[2:]],
            [f"remind:{BIG_ID}:2026-09-17", f"remind:{BIG_ID - 1}:2026-09-20"],
        )
        self.assertTrue(all(row[0].style is None for row in markup.inline_keyboard[2:]), "one styled button at most")
        lone = build_batch_payment_confirmation_keyboard(items[:1])
        self.assertEqual(lone.inline_keyboard[0][0].style, "success")

        greyed, remaining = mark_paid_button(markup, f"remind:{BIG_ID}:2026-09-17")
        assert_fits(greyed)
        self.assertEqual(remaining, 1)
        self.assertTrue(greyed.inline_keyboard[2][0].text.startswith("☑️ Paid · Spotify"), greyed.inline_keyboard[2][0].text)
        self.assertTrue(greyed.inline_keyboard[2][0].text.endswith(" · 17.09"))
        closed, _ = mark_closed_button(markup, f"remind:{BIG_ID - 1}:2026-09-20")
        assert_fits(closed)
        self.assertEqual(closed.inline_keyboard[3][0].text, "🔒 Closed · Netflix · 20.09")

    def test_single_closed_grey(self):
        markup = build_payment_confirmation_keyboard(7, "2026-09-17")
        closed, remaining = mark_closed_button(markup, "remind:7:2026-09-17")
        self.assertEqual(_layout(closed), [["🔒 Cycle closed"]])
        self.assertEqual(remaining, 0)

    def test_batch_label_helper(self):
        self.assertEqual(batch_paid_button_label({"subscription_name": "Netflix"}, "2026-09-17"), "✅ Netflix · 17.09")

    def test_admin_push_keyboards(self):
        markup = subscription_push_keyboard(BIG_ID)
        self.assertEqual(_layout(markup), [["📋 Open subscription"]])
        self.assertEqual(_callbacks(markup), [[SubscriptionAction(action="open_new", subscription_id=BIG_ID).pack()]])
        assert_fits(markup)
        markup = no_participants_push_keyboard(BIG_ID)
        self.assertEqual(_layout(markup), [["👥 Add participants"]])
        self.assertEqual(_callbacks(markup), [[f"sub:participants_manage_new:{BIG_ID}"]])
        self.assertLessEqual(len(markup.inline_keyboard[0][0].callback_data.encode()), 64)
        assert_fits(markup)
        markup = balance_receipt_keyboard()
        self.assertEqual(_layout(markup), [["➕ Top up"]])
        self.assertEqual(_callbacks(markup), [["topup:start_new:0"]])
        assert_fits(markup)


# ===================================================================== flows


OTHER_ADMIN = 1
BOB_ID = 2002


class _PushCase(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        patcher = mock.patch.object(CurrencyConverter, "convert_to", mock.AsyncMock(side_effect=RuntimeError("offline")))
        patcher.start()
        self.addCleanup(patcher.stop)
        await self.db.set_setting("base_timezone", "UTC")
        await self.make_admin(OTHER_ADMIN)
        self.anna = await self._friend("Anna Petrova", USER_ID)
        self.bob = await self._friend("Bob", BOB_ID)
        self.today = datetime.now(ZoneInfo("UTC")).date()
        self.due = self.today
        self.netflix = await self.db.create_subscription("Netflix", 30.0, "USD", self.due, 30, None)
        for friend_id in (self.anna, self.bob):
            await self.db.set_participant(self.netflix, friend_id, True)
        await self.db.ensure_cycle(self.netflix, self.due)

    async def _friend(self, name: str, telegram_id: int) -> int:
        friend_id = await self.db.create_friend_invite(name, f"join_{telegram_id}", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(friend_id, telegram_id, "2026-11-01T00:00:00")
        return friend_id

    def _push(self, markup: InlineKeyboardMarkup, message_id: int = 8001, text: str = "push"):
        message = make_message(self.bot, message_id, text, from_bot=True)
        return message.model_copy(update={"reply_markup": markup}).as_(self.bot)

    def _answers(self):
        return [(answer.text, bool(answer.show_alert)) for answer in self.bot.answers()]

    async def _paid_ids(self, subscription_id: int, due: date) -> set[int]:
        rows = await self.db.list_payments_for_cycles(subscription_id, [due.isoformat()])
        return {int(row["paid_by_telegram_id"]) for row in rows if row.get("paid_by_telegram_id") is not None}


class PaidTapTests(_PushCase):
    async def test_single_paid_becomes_a_receipt_and_admins_get_a_push(self):
        due = self.due.isoformat()
        markup = build_payment_confirmation_keyboard(self.netflix, due, CARD_DETAILS)
        await self.db.record_reminder_message(
            subscription_id=self.netflix,
            due_date=due,
            telegram_id=USER_ID,
            message_id=8001,
            share_amount=15.0,
            share_currency="USD",
            converted_amount=1350.0,
            converted_currency="RUB",
            converted_display=f"1{NBSP}350.00{NBSP}RUB",
        )
        await self.feed(self.callback(f"remind:{self.netflix}:{due}", self._push(markup)))

        self.assertEqual(await self._paid_ids(self.netflix, self.due), {USER_ID})
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 8001)
        self.assertIsNone(edit.reply_markup, "the receipt has no buttons")
        self.assertTrue(
            edit.text.startswith(f"✅ <b>Paid · Netflix</b>\n\n📅 {tg_due(due, 'UTC', 'wD')} · <b>15.00{NBSP}USD</b>\nRecorded "),
            edit.text,
        )
        self.assertRegex(edit.text, r'Recorded <tg-time unix="\d+" format="t">\d\d:\d\d</tg-time>\.$')
        self.assertEqual(self._answers(), [("✅ Payment recorded", False)])

        admin_pushes = [call for call in self.bot.of(SendMessage) if int(call.chat_id) == OTHER_ADMIN]
        self.assertEqual(len(admin_pushes), 1)
        self.assertEqual(
            admin_pushes[0].text,
            f'💸 <b><a href="tg://user?id={USER_ID}">Anna Petrova</a> paid Netflix</b>\n\n'
            f"📅 {tg_due(due, 'UTC', 'wD')} · <b>15.00{NBSP}USD</b> (≈ 1{NBSP}350.00{NBSP}RUB)\n"
            "█████░░░░░ 1 of 2 paid",
        )
        self.assertEqual(_layout(admin_pushes[0].reply_markup), [["📋 Open subscription"]])

    async def test_last_payer_closes_the_cycle_and_the_push_says_so(self):
        await self.db.log_payment(self.netflix, self.due, BOB_ID)
        due = self.due.isoformat()
        await self.feed(self.callback(f"remind:{self.netflix}:{due}", self._push(build_payment_confirmation_keyboard(self.netflix, due))))
        self.assertNotIn(due, await self.db.list_open_cycles(self.netflix))
        push = [call for call in self.bot.of(SendMessage) if int(call.chat_id) == OTHER_ADMIN][0]
        next_due = (self.due + timedelta(days=30)).isoformat()
        self.assertTrue(
            push.text.endswith(f"██████████ 2 of 2 paid\n🔒 Cycle closed — everyone paid. Next: {tg_due(next_due, 'UTC', 'wD')}."),
            push.text,
        )
        # The computed share (no stored reminder): 30 USD split by two.
        self.assertIn(f"<b>15.00{NBSP}USD</b>\n", push.text)

    async def test_only_the_closed_toggle_sends_the_closed_push(self):
        await self.db.set_setting("notify_admin_paid", "0")
        await self.db.log_payment(self.netflix, self.due, BOB_ID)
        due = self.due.isoformat()
        await self.feed(self.callback(f"remind:{self.netflix}:{due}", self._push(build_payment_confirmation_keyboard(self.netflix, due))))
        push = [call for call in self.bot.of(SendMessage) if int(call.chat_id) == OTHER_ADMIN]
        self.assertEqual(len(push), 1)
        next_due = (self.due + timedelta(days=30)).isoformat()
        self.assertEqual(
            push[0].text,
            "🔒 <b>Netflix cycle closed</b>\n\n"
            f"Everyone paid · total <b>30.00{NBSP}USD</b>\n"
            f"Next charge: {tg_due(next_due, 'UTC', 'wD')}",
        )

    async def test_batch_greys_each_bill_then_becomes_a_receipt(self):
        spotify = await self.db.create_subscription("Spotify", 10.0, "USD", self.due, 30, None)
        await self.db.set_participant(spotify, self.anna, True)
        await self.db.ensure_cycle(spotify, self.due)
        due = self.due.isoformat()
        items = [_item(self.netflix, "Netflix", self.due), _item(spotify, "Spotify", self.due)]
        markup = build_batch_payment_confirmation_keyboard(items, payment_details=CARD_DETAILS)
        header = f"🔔 2 bills due · total 25.00{NBSP}USD"

        await self.feed(self.callback(f"remind:{self.netflix}:{due}", self._push(markup, text=header)))
        grey = self.bot.of(EditMessageReplyMarkup)[-1]
        short = self.due.strftime("%d.%m")
        self.assertEqual(
            _layout(grey.reply_markup),
            [["📋 Copy card number ···5678"], [f"☑️ Paid · Netflix · {short}"], [f"✅ Spotify · {short}"]],
        )
        self.assertFalse(self.bot.of(EditMessageText))

        await self.feed(self.callback(f"remind:{spotify}:{due}", self._push(grey.reply_markup, text=header)))
        receipt = self.bot.of(EditMessageText)[-1]
        self.assertEqual(
            receipt.text,
            f"✅ <b>All paid</b> · 25.00{NBSP}USD\n\n✅ Netflix · {short}\n✅ Spotify · {short}",
        )
        self.assertIsNone(receipt.reply_markup)
        self.assertEqual(await self._paid_ids(spotify, self.due), {USER_ID})

    async def test_closed_cycle_greys_the_button(self):
        due = self.due.isoformat()
        await self.db.close_cycle(self.netflix, due)
        await self.feed(self.callback(f"remind:{self.netflix}:{due}", self._push(build_payment_confirmation_keyboard(self.netflix, due))))
        self.assertEqual(_layout(self.bot.of(EditMessageReplyMarkup)[-1].reply_markup), [["🔒 Cycle closed"]])
        self.assertEqual(self._answers(), [(CYCLE_CLOSED, False)])
        self.assertEqual(await self._paid_ids(self.netflix, self.due), set())

    async def test_already_paid_closes_the_screen_without_a_second_push(self):
        due = self.due.isoformat()
        await self.db.log_payment(self.netflix, self.due, USER_ID)
        await self.feed(self.callback(f"remind:{self.netflix}:{due}", self._push(build_payment_confirmation_keyboard(self.netflix, due))))
        self.assertTrue(self.bot.of(EditMessageText)[-1].text.startswith("✅ <b>Paid · Netflix</b>"))
        self.assertEqual(self._answers(), [("Already paid", False)])
        self.assertFalse([call for call in self.bot.of(SendMessage) if int(call.chat_id) == OTHER_ADMIN])

    async def test_double_tap_is_guarded(self):
        due = self.due.isoformat()
        # One key per payment: the same as the member card's ✅ Paid.
        key = guards.pay_cycle_key(self.netflix, due, USER_ID)
        guards._in_progress.add(key)
        try:
            await self.feed(self.callback(f"remind:{self.netflix}:{due}", self._push(build_payment_confirmation_keyboard(self.netflix, due))))
        finally:
            guards._in_progress.discard(key)
        self.assertEqual(self._answers(), [(guards.ALREADY_RUNNING_TEXT, False)])
        self.assertEqual(await self._paid_ids(self.netflix, self.due), set())

    async def test_stranger_gets_no_access(self):
        await self.db.set_participant(self.netflix, self.anna, False)
        due = self.due.isoformat()
        await self.feed(self.callback(f"remind:{self.netflix}:{due}", self._push(build_payment_confirmation_keyboard(self.netflix, due))))
        self.assertEqual(self._answers(), [("You are no longer in this subscription", False)])

    async def test_test_paid_records_nothing_and_greys(self):
        markup = build_test_payment_confirmation_keyboard(0, "2026-09-17", CARD_DETAILS)
        await self.feed(self.callback("testpaid:0:2026-09-17", self._push(markup)))
        self.assertEqual(
            _layout(self.bot.of(EditMessageReplyMarkup)[-1].reply_markup),
            [["📋 Copy card number ···5678"], ["☑️ Paid (test)"]],
        )
        self.assertEqual(self._answers(), [("Test only: nothing recorded.", True)])


class ParticipantsFromPushTests(_PushCase):
    async def test_add_participants_opens_the_picker_as_a_new_message(self):
        await self.make_admin(USER_ID)
        data = SubscriptionAction(action="participants_manage_new", subscription_id=self.netflix).pack()
        await self.feed(self.callback(data, self._push(no_participants_push_keyboard(self.netflix))))
        self.assertFalse(self.bot.of(EditMessageText), "the push is left alone")
        sent = self.bot.of(SendMessage)[-1]
        self.assertTrue(sent.text.startswith("👥 <b>Choose people · Netflix</b>"), sent.text)
        self.assertEqual(_layout(sent.reply_markup)[-1], ["✖️ Close"])
        self.assertEqual(_callbacks(sent.reply_markup)[-1], ["menu:close"])

    async def test_deleted_subscription(self):
        await self.make_admin(USER_ID)
        await self.db.delete_subscription(self.netflix)
        data = SubscriptionAction(action="participants_manage_new", subscription_id=self.netflix).pack()
        await self.feed(self.callback(data, self._push(no_participants_push_keyboard(self.netflix))))
        self.assertEqual(self._answers(), [(SUBSCRIPTION_GONE, False)])


# ========================================================== the reminder pass


class ReminderPassTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.directory.name) / "test.db")
        await self.db.connect()
        self.converter = CurrencyConverter("RUB")
        self.converter.convert_to = AsyncMock(side_effect=AssertionError("Unexpected conversion"))
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)))
        self.friend_id = await self.db.create_friend_invite("Anna", "invite", "2030-01-01")
        await self.db.claim_friend_invite(self.friend_id, 123, "2026-09-17")

    async def asyncTearDown(self):
        await self.converter.close()
        await self.db.close()
        self.directory.cleanup()

    async def run_pass(self, day: date = TODAY, register: bool = False):
        return await _run_reminder_pass(
            self.bot,
            self.db,
            self.converter,
            subscription_id=None,
            now=datetime(day.year, day.month, day.day, 16, tzinfo=timezone.utc),
            rounding_mode="precise",
            enforce_time=False,
            register_reminders=register,
        )

    async def plan(self, *, with_anna: bool = True) -> int:
        subscription_id = await self.db.create_subscription("Plan", 100, "RUB", TODAY, 30, 2)
        if with_anna:
            await self.db.set_participant(subscription_id, self.friend_id, True)
        return subscription_id

    def sent_to(self, chat_id: int) -> list:
        return [call for call in self.bot.send_message.await_args_list if call.args[0] == chat_id]

    async def test_admins_get_reminders_sent_with_counts(self):
        await self.db.add_admin(9001, "Boss")
        subscription_id = await self.plan()
        await self.run_pass()
        notes = self.sent_to(9001)
        self.assertEqual(len(notes), 1)
        self.assertEqual(
            notes[0].args[1],
            reminders_sent_text("Plan", "2026-09-17", TZ, TODAY, f"100.00{NBSP}RUB", 1, 0),
        )
        self.assertIn("(today)", notes[0].args[1])
        self.assertNotIn("|", notes[0].args[1])
        self.assertEqual(_callbacks(notes[0].kwargs["reply_markup"]), [[f"sub:open_new:{subscription_id}"]])
        member = self.sent_to(123)[0]
        self.assertTrue(member.args[1].endswith(PAID_HINT))

    async def test_paid_from_balance_is_a_receipt_with_top_up_when_short(self):
        await self.plan()
        await self.db.set_friend_balance(self.friend_id, 70)
        await self.run_pass()
        receipts = self.sent_to(123)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            receipts[0].args[1],
            balance_receipt_text("Plan", "2026-09-17", TZ, f"50.00{NBSP}RUB", f"20.00{NBSP}RUB"),
        )
        self.assertEqual(_layout(receipts[0].kwargs["reply_markup"]), [["➕ Top up"]])

    async def test_no_top_up_when_the_balance_covers_the_next_share(self):
        await self.plan()
        await self.db.set_friend_balance(self.friend_id, 200)
        await self.run_pass()
        receipt = self.sent_to(123)[0]
        self.assertIn(f"Balance left: <b>150.00{NBSP}RUB</b>", receipt.args[1])
        self.assertIsNone(receipt.kwargs["reply_markup"])

    async def test_partial_balance_is_its_own_line(self):
        await self.db.update_subscription_fields(await self.plan(), comment="Family plan")
        await self.db.set_friend_balance(self.friend_id, 20)
        await self.run_pass()
        text = self.sent_to(123)[0].args[1]
        # "Your share" is the full share, as on the member card; the rest is in the balance line.
        self.assertIn("💵 Your share: <b>50.00\u00a0RUB</b>", text)
        self.assertIn("📝 Family plan\n💰 From balance: 20.00\u00a0RUB · left to pay <b>30.00\u00a0RUB</b>", text)

    async def test_no_participants_alert_once_per_cycle(self):
        await self.db.add_admin(9001, "Boss")
        subscription_id = await self.plan(with_anna=False)
        await self.run_pass(TODAY - timedelta(days=1), register=True)
        await self.run_pass(TODAY, register=True)
        notes = self.sent_to(9001)
        self.assertEqual(len(notes), 1, [call.args[1] for call in notes])
        self.assertEqual(notes[0].args[1], no_participants_text("Plan", "2026-09-17", TZ, f"100.00{NBSP}RUB"))
        self.assertEqual(_callbacks(notes[0].kwargs["reply_markup"]), [[f"sub:participants_manage_new:{subscription_id}"]])

    async def test_cycle_closed_by_the_pass_tells_the_admins(self):
        await self.db.add_admin(123, "Anna")  # the only participant is an admin...
        await self.db.set_setting("notify_admin_reminders", "0")  # ...so the pass marks her paid
        await self.plan()
        await self.run_pass()
        notes = self.sent_to(123)
        self.assertEqual(len(notes), 1, [call.args[1] for call in notes])
        self.assertEqual(
            notes[0].args[1],
            cycle_closed_text("Plan", f"100.00{NBSP}RUB", "2026-10-17", TZ),
        )


class GuardMetaTests(unittest.TestCase):
    def test_receipt_patterns_have_no_trailing_period_in_toasts(self):
        for toast in ("✅ Payment recorded", "Already paid", CYCLE_CLOSED):
            self.assertLessEqual(len(toast), 200)
        self.assertIsNone(re.search(r"\.$", CYCLE_CLOSED))


if __name__ == "__main__":
    unittest.main()
