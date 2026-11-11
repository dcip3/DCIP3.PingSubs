"""Regression tests for the review findings on the UX redesign.

One test (or a few) per behavioural bug: the join menu of a linked member, the
old card's '📊 Reports', the dated card Paid and its shared guard key, the
not-modified edit, short dates and '1.000', the spawned cards' ✖️ Close, the
member reports' timezone, prompts on inaccessible messages, message budgets
(batch reminders, report chunks, folded lines, the admin card), the admin's
legacy '➕ Top up', the balance split and the reworded screens.
"""
from __future__ import annotations

import re
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Chat, InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup

from app.core.reminders import tg_due
from app.handlers.subscriptions import PAST_DATE_HINT
from app.services import CurrencyConverter
from app.ui import helpers
from app.ui.helpers import build_subscription_card, edit_in_place, split_text_chunks
from app.ui.keyboards import CLOSE_CALLBACK, guest_reply_keyboard, public_reply_keyboard
from app.ui.prompts import REOPEN_SCREEN_ALERT
from app.ui.shell import JOIN_ALREADY_LINKED_TEXT
from app.ui.style import expandable, visible_len
from app.ui.text import escape_html, parse_amount, parse_date_loose
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import CHAT_ID, FakeBot, make_callback, make_message

install_keyboard_guard()

OTHER_ADMIN = 9001
TZ = "Europe/Moscow"


def _texts(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _keyboard(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.keyboard]


def _balanced(chunk: str) -> bool:
    """Every opening tag in the chunk is closed in it, in order (no tag cut in half)."""
    if re.search(r"<[^>]*$", chunk) or re.search(r"^[^<]*>", chunk):
        return False
    stack: list[str] = []
    for closing, name in re.findall(r"<(/?)([a-z-]+)[^>]*>", chunk):
        if not closing:
            stack.append(name)
        elif not stack or stack.pop() != name:
            return False
    return not stack


class _Case(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        patcher = mock.patch.object(CurrencyConverter, "convert_to", mock.AsyncMock(side_effect=RuntimeError("offline")))
        patcher.start()
        self.addCleanup(patcher.stop)
        await self.db.set_setting("base_timezone", TZ)
        await self.db.set_setting("base_reminder_time", "10:00")
        self.today = datetime.now(ZoneInfo(TZ)).date()

    async def _friend(self, name: str, telegram_id: int | None, token: str | None = None) -> int:
        friend_id = await self.db.create_friend_invite(name, token or f"join_{telegram_id}", "2099-01-01T00:00:00")
        if telegram_id is not None:
            await self.db.claim_friend_invite(friend_id, telegram_id, "2026-11-01T00:00:00")
        return friend_id

    def _screen(self, message_id: int = 7001, markup: InlineKeyboardMarkup | None = None):
        message = make_message(self.bot, message_id, "screen", from_bot=True)
        if markup is not None:
            message = message.model_copy(update={"reply_markup": markup}).as_(self.bot)
        return message

    def _answers(self) -> list[str | None]:
        return [answer.text for answer in self.bot.answers()]


# ============================================================ the join menu


class JoinMenuTests(_Case):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin(OTHER_ADMIN)
        self.anna = await self._friend("Anna Petrova", None, token="join_tok1")
        self.bob = await self._friend("Bob", None, token="join_tok2")

    async def test_a_linked_member_keeps_the_member_menu_on_any_sign_in_link(self):
        await self.feed(self.message("/start join_tok1"))
        self.assertEqual(self.bot.of(SendMessage)[-1].reply_markup, public_reply_keyboard())

        # Her own link again: claiming it cleared the token. She is greeted as a member.
        await self.feed(self.message("/start join_tok1"))
        again = self.bot.of(SendMessage)[-1]
        self.assertEqual(again.reply_markup, public_reply_keyboard())
        self.assertNotIn("does not work", again.text)

        # Someone else's link: "already linked", still with the member menu.
        await self.feed(self.message("/start join_tok2"))
        other = self.bot.of(SendMessage)[-1]
        self.assertEqual(other.text, JOIN_ALREADY_LINKED_TEXT)
        self.assertEqual(other.reply_markup, public_reply_keyboard())
        self.assertIsNone((await self.db.get_friend(self.bob)).get("telegram_id"))

    async def test_a_stranger_with_a_broken_link_gets_the_guest_menu(self):
        await self.feed(self.message("/start join_nope"))
        sent = self.bot.of(SendMessage)[-1]
        self.assertEqual(sent.reply_markup, guest_reply_keyboard())
        self.assertEqual(_keyboard(sent.reply_markup), [["❓ Help"]])


# =================================================== the old card's Reports


class OldCardReportsTests(_Case):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin()
        self.sub_id = await self.db.create_subscription("Netflix", 10.0, "USD", self.today + timedelta(days=5), 30, None)

    async def test_old_card_reports_button_opens_the_report(self):
        old_card = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💰 Pricing & schedule", callback_data=f"sub:pricing:{self.sub_id}")],
                [InlineKeyboardButton(text="📊 Reports", callback_data=f"sub:more:{self.sub_id}")],
            ]
        )
        await self.feed(self.callback(f"sub:more:{self.sub_id}", self._screen(markup=old_card)))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertTrue(edit.text.startswith("📊 <b>Reports · Netflix</b>"), edit.text)
        self.assertEqual(_texts(edit.reply_markup)[-1], ["◀️ To card"])

    async def test_sub_more_elsewhere_still_redraws_the_card(self):
        old_report_back = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data=f"sub:more:{self.sub_id}")]]
        )
        await self.feed(self.callback(f"sub:more:{self.sub_id}", self._screen(markup=old_report_back)))
        self.assertTrue(self.bot.of(EditMessageText)[-1].text.startswith("🏷️ <b>Netflix</b>"))


# ======================================================= not-modified edits


class NotModifiedTests(_Case):
    async def test_edit_in_place_reports_not_modified_as_shown(self):
        bot = FakeBot()
        bot.fail(EditMessageText, "Bad Request: message is not modified: specified new message content")
        self.assertTrue(await edit_in_place(bot, CHAT_ID, 20, "same"))

    async def test_saving_the_same_amount_twice_sends_no_second_pricing(self):
        await self.make_admin()
        sub_id = await self.db.create_subscription("Netflix", 10.0, "USD", self.today + timedelta(days=5), 30, None)
        screen = make_message(self.bot, 600, "pricing", from_bot=True)
        await self.feed(self.callback(f"sub:amount:{sub_id}", screen))
        sent_before = len(self.bot.of(SendMessage))
        # The origin already shows exactly this text and banner.
        self.bot.fail(EditMessageText, "Bad Request: message is not modified", message_id=600)
        await self.feed(self.message("10"))
        self.assertEqual(len(self.bot.of(SendMessage)), sent_before, "no duplicate Pricing message")
        self.assertIsNone(await self.state().get_state())


# ================================================================ input


class InputToleranceTests(unittest.TestCase):
    def test_a_short_date_that_just_passed_stays_in_the_past(self):
        today = date(2026, 11, 11)
        self.assertEqual(parse_date_loose("10.11", today), date(2026, 11, 10))
        self.assertEqual(parse_date_loose("31.10", today), date(2026, 10, 31))
        self.assertEqual(parse_date_loose("12.11", today), date(2026, 11, 12))
        self.assertEqual(parse_date_loose("01.03", today), date(2027, 3, 1))
        self.assertEqual(parse_date_loose("28.12", date(2026, 1, 3)), date(2025, 12, 28))
        self.assertEqual(parse_date_loose("29.02", today), date(2028, 2, 29))

    def test_a_dot_before_three_digits_groups_thousands_like_a_comma(self):
        self.assertEqual(parse_amount("1.000"), (1000.0, False))
        self.assertEqual(parse_amount("1,000"), (1000.0, False))
        self.assertEqual(parse_amount("1.500"), (1500.0, False))
        self.assertEqual(parse_amount("+1.500", allow_signed=True), (1500.0, True))
        self.assertEqual(parse_amount("0.500"), (0.5, False))
        self.assertEqual(parse_amount("1.50"), (1.5, False))
        self.assertEqual(parse_amount("1.000.000"), (1000000.0, False))


class PastDueDateTests(_Case):
    async def test_next_charge_warns_once_about_a_past_date(self):
        await self.make_admin()
        due = self.today + timedelta(days=5)
        sub_id = await self.db.create_subscription("Netflix", 10.0, "USD", due, 30, None)
        screen = make_message(self.bot, 600, "pricing", from_bot=True)
        await self.feed(self.callback(f"sub:duedate:{sub_id}", screen))
        yesterday = self.today - timedelta(days=1)
        await self.feed(self.message(yesterday.strftime("%d.%m")))
        self.assertEqual(self.bot.sent_texts()[-1], PAST_DATE_HINT)
        self.assertEqual((await self.db.get_subscription(sub_id))["next_charge_at"], due.isoformat())
        # The same date again keeps it: yesterday, never a year ahead.
        await self.feed(self.message(yesterday.strftime("%d.%m")))
        stored = str((await self.db.get_subscription(sub_id))["next_charge_at"])
        self.assertLessEqual(stored, self.today.isoformat())
        self.assertIsNone(await self.state().get_state())


# ============================================================ spawned cards


class SpawnedCardTests(_Case):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin()

    def _spawned_id(self) -> int:
        chat_id, message_id = list(helpers._SPAWNED_SCREENS)[-1]
        self.assertEqual(chat_id, CHAT_ID)
        return message_id

    async def test_admin_card_from_a_push_keeps_close_after_a_sub_screen(self):
        sub_id = await self.db.create_subscription("Netflix", 10.0, "USD", self.today + timedelta(days=5), 30, None)
        await self.feed(self.callback(f"sub:open_new:{sub_id}", self._screen(900)))
        card = self.bot.of(SendMessage)[-1]
        self.assertEqual(card.reply_markup.inline_keyboard[-1][0].callback_data, CLOSE_CALLBACK)
        copy_id = self._spawned_id()

        await self.feed(self.callback(f"sub:pricing:{sub_id}", self._screen(copy_id)))
        await self.feed(self.callback(f"sub:open:{sub_id}", self._screen(copy_id)))
        redrawn = self.bot.of(EditMessageText)[-1]
        self.assertEqual(redrawn.message_id, copy_id)
        self.assertEqual(_texts(redrawn.reply_markup)[-1], ["✖️ Close"])

        # A typed value redraws the same copy, still with Close.
        await self.feed(self.callback(f"sub:amount:{sub_id}", self._screen(copy_id)))
        await self.feed(self.message("12"))
        self.assertEqual(self.bot.of(EditMessageText)[-1].message_id, copy_id)
        await self.feed(self.callback(f"sub:open:{sub_id}", self._screen(copy_id)))
        self.assertEqual(_texts(self.bot.of(EditMessageText)[-1].reply_markup)[-1], ["✖️ Close"])

        # A card in the list flow still goes back to the list.
        await self.feed(self.callback(f"sub:open:{sub_id}", self._screen(4321)))
        self.assertEqual(_texts(self.bot.of(EditMessageText)[-1].reply_markup)[-1], ["◀️ To list"])

    async def test_member_card_from_an_alert_keeps_close_after_balance(self):
        anna = await self._friend("Anna Petrova", 5001)
        await self.feed(self.callback(f"member:open_new:{anna}", self._screen(900)))
        copy_id = self._spawned_id()
        await self.feed(self.callback(f"member:balance:{anna}", self._screen(copy_id)))
        await self.feed(self.callback(f"member:open:{anna}", self._screen(copy_id)))
        redrawn = self.bot.of(EditMessageText)[-1]
        self.assertEqual(redrawn.message_id, copy_id)
        self.assertEqual(_texts(redrawn.reply_markup)[-1], ["✖️ Close"])


# ================================================= inaccessible messages


class InaccessiblePromptTests(_Case):
    async def test_a_prompt_on_an_old_message_gets_a_calm_alert(self):
        await self.make_admin()
        old = InaccessibleMessage(chat=Chat(id=CHAT_ID, type="private"), message_id=5, date=0)
        sub_id = await self.db.create_subscription("Netflix", 10.0, "USD", self.today + timedelta(days=5), 30, None)
        for data in ("member:add:0", f"sub:rename:{sub_id}", f"sub:amount:{sub_id}"):
            with self.subTest(data=data):
                self.bot.calls.clear()
                await self.feed(make_callback(self.bot, old, data))  # the harness fails on the error handler
                answers = self.bot.answers()
                self.assertEqual([(a.text, a.show_alert) for a in answers], [(REOPEN_SCREEN_ALERT, True)])
                self.assertIsNone(await self.state().get_state())
                self.assertFalse(self.bot.of(SendMessage))


class ReportChunkTests(unittest.TestCase):
    def test_an_oversized_report_block_is_split_between_lines_never_inside_a_tag(self):
        lines = []
        for week in range(16):
            due = (date(2026, 7, 6) + timedelta(days=7 * week)).isoformat()
            names = ", ".join(f"Person {n:02d} Longname" for n in range(20))
            lines.append(f"⚠️ <b>{tg_due(due, TZ, 'wd')}</b> · overdue by {week + 1} days: {names}")
        block = "🔴 <b>Netflix</b>\n" + "\n".join(lines)
        self.assertGreater(len(block), 3900)
        chunks = split_text_chunks(f"📊 <b>Reports</b>\n\n{block}")
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 3900)
            self.assertTrue(_balanced(chunk), chunk[:120])
        self.assertEqual(sum(chunk.count("overdue by") for chunk in chunks), 16)


class FoldTests(unittest.TestCase):
    def test_one_comment_with_ampersands_is_shown_not_dropped(self):
        comment = "Family plan: R&D & Q&A. " * 41
        folded = expandable([escape_html(comment)], 1200, "line")
        self.assertNotIn("more line", folded)
        self.assertIn("Family plan: R&amp;D", folded)


class AdminCardBudgetTests(_Case):
    async def test_worst_case_admin_card_stays_under_the_limit(self):
        await self.make_admin()
        sub_id = await self.db.create_subscription("Netflix", 400.0, "RUB", self.today + timedelta(days=5), 30, None)
        for index in range(40):
            friend_id = await self._friend(f"Participant Number {index:02d} Longsurname", 6000 + index)
            await self.db.set_participant(sub_id, friend_id, True)
        await self.db.ensure_cycle(sub_id, self.today - timedelta(days=2))
        destination_id = await self.db.create_payment_destination(
            title="Sber", currency="RUB", details="4" * 1500, payment_link="https://pay.example/" + "l" * 480
        )
        await self.db.set_default_payment_destination_id(destination_id)
        await self.db.update_subscription_fields(sub_id, comment="Comment & notes " * 62)
        text, _ = await build_subscription_card(self.db, sub_id)
        self.assertLessEqual(visible_len(text), 4096)
        self.assertIn("(more in “💳 Payment”)", text)


if __name__ == "__main__":
    unittest.main()
