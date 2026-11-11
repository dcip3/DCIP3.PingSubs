"""Regression tests for the review findings on the UX redesign.

One test (or a few) per behavioural bug: the join menu of a linked member, the
old card's '📊 Reports', the dated card Paid and its shared guard key, the
not-modified edit, short dates and '1.000', the spawned cards' ✖️ Close, the
member reports' timezone, prompts on inaccessible messages, message budgets
(batch reminders, report chunks, folded lines, the admin card), the admin's
legacy '➕ Top up', the balance split and the reworded screens.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup

from app.services import CurrencyConverter
from app.ui.keyboards import guest_reply_keyboard, public_reply_keyboard
from app.ui.shell import JOIN_ALREADY_LINKED_TEXT
from app.ui.style import expandable
from app.ui.text import escape_html, parse_amount, parse_date_loose
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import make_message

install_keyboard_guard()

OTHER_ADMIN = 9001
TZ = "Europe/Moscow"


def _keyboard(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.keyboard]


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


class FoldTests(unittest.TestCase):
    def test_one_comment_with_ampersands_is_shown_not_dropped(self):
        comment = "Family plan: R&D & Q&A. " * 41
        folded = expandable([escape_html(comment)], 1200, "line")
        self.assertNotIn("more line", folded)
        self.assertIn("Family plan: R&amp;D", folded)


if __name__ == "__main__":
    unittest.main()
