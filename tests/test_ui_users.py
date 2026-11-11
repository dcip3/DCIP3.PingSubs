"""WP2 Users section: list, add user, member card, sign-in link, balance, rename, delete,
member report and the member payment method — texts, keyboards and flows."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest import mock

from aiogram.types import User

from app.ui.guards import guarded
from app.ui.helpers import METHOD_GONE, USER_GONE
from app.ui.keyboards import (
    build_members_list_keyboard,
    invite_link_keyboard,
    member_balance_currency_keyboard,
    member_balance_keyboard,
    member_card_nav_keyboard,
    member_delete_confirm_keyboard,
    member_detail_keyboard,
    member_invite_confirm_keyboard,
    member_payment_destination_keyboard,
    member_report_keyboard,
)
from app.ui.member_screens import (
    invite_status_lines,
    member_balance_currency_screen,
    member_balance_screen,
    member_delete_confirm_screen,
    member_list_screen,
    member_payment_method_screen,
    sign_in_link_screen,
)
from app.ui.prompts import PROMPT_MESSAGE_ID
from app.ui.states import FriendForm, MemberAction, MemberEditForm
from app.ui.style import SUBITEM_INDENT
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import CHAT_ID, EditMessageText, SendMessage, make_message
from test_keyboards_fit import LONG_METHOD, LONG_NAME, NAME_64, USER_TEXTS, assert_fits

install_keyboard_guard()

NBSP = " "
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
BOT_USER = User(id=777, is_bot=True, first_name="PingSubs", username="pingsubs_bot")
NO_USERNAME = User(id=777, is_bot=True, first_name="PingSubs")


def _texts(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


# ----------------------------------------------------------------- keyboards


class UsersKeyboardFitTests(unittest.TestCase):
    """Every Users builder fits a phone in every branch, with worst-case data."""

    NAMES = (LONG_NAME, NAME_64, "Anna Petrova", "Ivan Sidorov", "W" * 64, "Mmmmmmmmmmmmmmmmmmmm")

    def _friends(self, count: int, *, pending_every: int = 2) -> list[dict]:
        names = self.NAMES
        return [
            {
                "id": index + 1,
                "full_name": names[index % len(names)],
                "telegram_id": None if index % pending_every == 0 else 1000 + index,
            }
            for index in range(count)
        ]

    def test_users_list_every_branch(self):
        assert_fits(build_members_list_keyboard([], page=1, total_pages=1, total_users=0))
        friends = self._friends(6)
        for page in (1, 2, 3):
            with self.subTest(page=page):
                markup = build_members_list_keyboard(friends, page=page, total_pages=3, total_users=18)
                assert_fits(markup, USER_TEXTS + self.NAMES)

    def test_users_list_has_no_nav_row_and_one_user_per_row(self):
        markup = build_members_list_keyboard(self._friends(6), page=2, total_pages=3, total_users=18)
        rows = _texts(markup)
        self.assertEqual(rows[0], ["➕ Add user"])
        self.assertTrue(all(len(row) == 1 for row in rows[1:-1]))
        self.assertEqual(rows[-1], ["◀️", "2 / 3", "▶️"])
        self.assertNotIn("menu:close", _callbacks(markup))

    def test_pending_label_keeps_its_tail(self):
        markup = build_members_list_keyboard([{"id": 3, "full_name": LONG_NAME, "telegram_id": None}], total_users=1)
        label = markup.inline_keyboard[1][0].text
        self.assertTrue(label.startswith("⏳ WWW"), label)
        self.assertTrue(label.endswith("… · not signed in"), label)
        short = build_members_list_keyboard([{"id": 4, "full_name": "Ivan Sidorov", "telegram_id": None}], total_users=1)
        self.assertEqual(short.inline_keyboard[1][0].text, "⏳ Ivan Sidorov · not signed in")

    def test_member_card_every_branch(self):
        for pending in (False, True):
            for spawned in (False, True):
                with self.subTest(pending=pending, spawned=spawned):
                    markup = member_detail_keyboard(987654321, is_pending=pending, spawned=spawned)
                    assert_fits(markup)
                    self.assertLessEqual(len(markup.inline_keyboard), 6)

    def test_member_card_layout(self):
        self.assertEqual(
            _texts(member_detail_keyboard(5)),
            [["💰 Balance", "📊 Reports"], ["🏦 Method", "✏️ Rename"], ["🗑 Delete"], ["◀️ To list"]],
        )
        self.assertEqual(
            _texts(member_detail_keyboard(5, is_pending=True)),
            [["💰 Balance", "📊 Reports"], ["✏️ Rename"], ["🔗 New sign-in link"], ["🗑 Delete"], ["◀️ To list"]],
        )
        self.assertEqual(member_detail_keyboard(5).inline_keyboard[-1][0].callback_data, "member:back:5")
        spawned = member_detail_keyboard(5, spawned=True)
        self.assertEqual(spawned.inline_keyboard[-1][0].text, "✖️ Close")
        self.assertEqual(spawned.inline_keyboard[-1][0].callback_data, "menu:close")

    def test_sub_screens_fit_and_return_to_the_card(self):
        for markup in (
            member_balance_keyboard(5),
            member_report_keyboard(5),
            member_card_nav_keyboard(5),
            invite_link_keyboard("https://t.me/pingsubs_bot?start=join_" + "x" * 300, 5),
        ):
            with self.subTest(rows=_texts(markup)):
                assert_fits(markup)
                self.assertEqual(markup.inline_keyboard[-1][0].text, "◀️ To card")
                self.assertEqual(markup.inline_keyboard[-1][0].callback_data, "member:open:5")
        self.assertEqual(_texts(member_balance_keyboard(5)), [["✏️ Set amount", "💱 Currency"], ["◀️ To card"]])

    def test_balance_currency_picker_is_a_radio(self):
        for current in ("RUB", "USD", "EUR", "GBP"):
            with self.subTest(current=current):
                markup = member_balance_currency_keyboard(5, current)
                assert_fits(markup)
                self.assertFalse(any(button.style for row in markup.inline_keyboard for button in row))
        self.assertEqual(
            _texts(member_balance_currency_keyboard(5, "rub")),
            [["✅ RUB", "USD"], ["EUR", "TRY"], ["✏️ Other currency"], ["◀️ Back"]],
        )
        self.assertEqual(
            _callbacks(member_balance_currency_keyboard(5, "RUB"))[:4],
            [
                "member_balance_currency:5:RUB",
                "member_balance_currency:5:USD",
                "member_balance_currency:5:EUR",
                "member_balance_currency:5:TRY",
            ],
        )
        self.assertEqual(member_balance_currency_keyboard(5, "RUB").inline_keyboard[-1][0].callback_data, "member:balance:5")

    def test_confirmations_put_cancel_first(self):
        for markup, verb, yes in (
            (member_delete_confirm_keyboard(5), "🗑 Delete", "member:confirm_delete:5"),
            (member_invite_confirm_keyboard(5), "🔗 New link", "member:invite_yes:5"),
        ):
            with self.subTest(verb=verb):
                assert_fits(markup)
                (row,) = markup.inline_keyboard
                self.assertEqual([button.text for button in row], ["✖️ Cancel", verb])
                self.assertEqual(row[0].callback_data, "member:open:5")
                self.assertIsNone(row[0].style)
                self.assertEqual((row[1].callback_data, row[1].style), (yes, "danger"))

    def test_payment_method_picker_every_branch(self):
        methods = [
            {"id": 1, "title": LONG_METHOD, "currency": "RUB"},
            {"id": 2, "title": "Revolut", "currency": "EUR"},
            {"id": 3, "title": NAME_64, "currency": "USD"},
        ]
        for assigned in (None, 1, 2, 3):
            for default in (None, 1, 2):
                with self.subTest(assigned=assigned, default=default):
                    markup = member_payment_destination_keyboard(5, methods, assigned, default)
                    assert_fits(markup, USER_TEXTS + (f"Default ({LONG_METHOD}", f"⭐ {LONG_METHOD}"))
                    self.assertEqual(markup.inline_keyboard[-1][0].text, "◀️ To card")
        markup = member_payment_destination_keyboard(5, methods[1:2], 2, 2)
        self.assertEqual(_texts(markup), [["Default (⭐ Revolut · EUR)"], ["✅ ⭐ Revolut · EUR"], ["◀️ To card"]])
        markup = member_payment_destination_keyboard(5, methods[1:2], None, 2)
        self.assertEqual(_texts(markup)[0], ["✅ Default (⭐ Revolut · EUR)"])
        long_label = member_payment_destination_keyboard(5, methods[:1], 1, None).inline_keyboard[1][0].text
        self.assertTrue(long_label.startswith("✅ Main") and long_label.endswith("… · RUB"), long_label)

    def test_callback_data_stays_short(self):
        big = 2**31 - 1
        markups = (
            member_detail_keyboard(big, is_pending=True),
            member_balance_currency_keyboard(big, "RUB"),
            member_delete_confirm_keyboard(big),
            member_invite_confirm_keyboard(big),
            member_payment_destination_keyboard(big, [{"id": big, "title": "x", "currency": "RUB"}], None, big),
        )
        for markup in markups:
            for data in _callbacks(markup):
                self.assertLess(len(data.encode()), 64, data)


# --------------------------------------------------------------- pure texts


class SignInLinkTextTests(unittest.TestCase):
    friend = {"id": 5, "full_name": "Anna Petrova", "telegram_id": None}

    def test_added_screen(self):
        expires = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
        text, markup = sign_in_link_screen(
            self.friend, "✅ <b>Anna Petrova added</b>", "https://t.me/bot?start=join_x", expires, "UTC"
        )
        self.assertEqual(
            text,
            "✅ <b>Anna Petrova added</b>\n\n"
            "Send Anna this sign-in link:\nhttps://t.me/bot?start=join_x\n\n"
            f'Works until <tg-time unix="{int(expires.timestamp())}" format="wDt">'
            "Tue, 17.11.2026 12:00 (UTC)</tg-time> (7 days).",
        )
        self.assertEqual(_texts(markup), [["📋 Copy link"], ["◀️ To card"]])

    def test_missing_bot_username(self):
        text, markup = sign_in_link_screen(self.friend, "✅ <b>Anna Petrova added</b>", None, None, "UTC")
        self.assertEqual(
            text, "✅ <b>Anna Petrova added</b>\n\n⚠️ Could not build the link: the bot username is not set."
        )
        self.assertEqual(_texts(markup), [["◀️ To card"]])

    def test_link_status_lines(self):
        in_three = (NOW + timedelta(days=3)).replace(tzinfo=None).isoformat()
        (line,) = invite_status_lines(in_three, "UTC", NOW)
        self.assertTrue(line.startswith("Link works until <tg-time "), line)
        self.assertTrue(line.endswith("</tg-time> (in 3 days)"), line)
        (line,) = invite_status_lines((NOW + timedelta(hours=20)).isoformat(), "UTC", NOW)
        self.assertTrue(line.endswith("(tomorrow)"), line)
        (line,) = invite_status_lines((NOW + timedelta(hours=2)).isoformat(), "UTC", NOW)
        self.assertTrue(line.endswith("(today)"), line)
        self.assertEqual(invite_status_lines((NOW - timedelta(minutes=1)).isoformat(), "UTC", NOW), ["⚠️ Link expired"])
        self.assertEqual(invite_status_lines(None, "UTC", NOW), [])


# ----------------------------------------------------------- db-backed texts


class UsersScreenTextTests(RealDispatcherTestCase):
    async def _linked(self, name: str, telegram_id: int) -> int:
        friend_id = await self.db.create_friend_invite(name, f"t-{telegram_id}", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(friend_id, telegram_id, "2026-01-01T00:00:00")
        return friend_id

    async def test_users_list_texts(self):
        text, markup = await member_list_screen(self.db)
        self.assertEqual(text, "👥 <b>Users</b>\n\nNo users yet.")
        self.assertEqual(markup.inline_keyboard[0][0].style, "primary")

        await self._linked("Anna Petrova", 1001)
        await self.db.create_friend_invite("Ivan Sidorov", "t-x", "2099-01-01T00:00:00")
        text, markup = await member_list_screen(self.db)
        self.assertEqual(text, "👥 <b>Users</b> · 2\n\n⏳ Not signed in yet: <b>1</b>\n\nTap a user to open their card.")
        self.assertEqual(_texts(markup), [["➕ Add user"], ["Anna Petrova"], ["⏳ Ivan Sidorov · not signed in"]])
        self.assertIsNone(markup.inline_keyboard[0][0].style)

    async def test_list_pages_hold_six_and_clamp(self):
        for index in range(13):
            await self._linked(f"User {index:02d}", 3000 + index)
        text, markup = await member_list_screen(self.db, page=9)
        self.assertTrue(text.startswith("👥 <b>Users</b> · 13\n\nTap a user"), text)
        self.assertEqual(_texts(markup)[-1], ["◀️", "3 / 3", "▶️"])
        self.assertEqual(_texts(markup)[1:-1], [["User 12"]])
        assert_fits(markup)

    async def test_balance_and_currency_texts(self):
        friend_id = await self._linked("Anna Petrova", 1001)
        await self.db.set_friend_balance(friend_id, 12500)
        text, _ = await member_balance_screen(self.db, friend_id)
        self.assertEqual(text, f"💰 <b>Balance · Anna Petrova</b>\n\n<b>12{NBSP}500.00{NBSP}RUB</b>")
        text, _ = await member_balance_currency_screen(self.db, friend_id)
        self.assertEqual(
            text,
            "💱 <b>Balance currency · Anna Petrova</b>\n\nNow: RUB\n"
            "⚠️ Changing the currency does not convert the amount.",
        )

    async def test_delete_question_names_what_is_lost(self):
        friend_id = await self._linked("Anna Petrova", 1001)
        friend = await self.db.get_friend(friend_id)
        text, _ = await member_delete_confirm_screen(self.db, friend)
        self.assertEqual(text, "⚠️ <b>Delete Anna Petrova?</b>\nThis cannot be undone.")

        await self.db.set_friend_balance(friend_id, 350)
        today = datetime.now(timezone.utc).date()
        for name in ("Netflix", "Spotify"):
            sub_id = await self.db.create_subscription(name, 10.0, "USD", today + timedelta(days=5), 30, None)
            await self.db.set_participant(sub_id, friend_id, True)
        text, _ = await member_delete_confirm_screen(self.db, await self.db.get_friend(friend_id))
        self.assertEqual(
            text,
            f"⚠️ <b>Delete Anna Petrova?</b>\nAnna leaves 2 subscriptions and loses the balance of 350.00{NBSP}RUB.",
        )

    async def test_payment_method_screen_and_dead_ends(self):
        pending_id = await self.db.create_friend_invite("Ivan Sidorov", "t-p", "2099-01-01T00:00:00")
        text, markup = await member_payment_method_screen(self.db, pending_id)
        self.assertEqual(text, "🏦 <b>Payment method · Ivan Sidorov</b>\n\nIvan has not signed in yet.")
        self.assertEqual(_texts(markup), [["◀️ To card"]])

        friend_id = await self._linked("Anna Petrova", 1001)
        text, _ = await member_payment_method_screen(self.db, friend_id)
        self.assertEqual(
            text, "🏦 <b>Payment method · Anna Petrova</b>\n\nNo payment methods yet. Add one in “💳 Payment”."
        )

        sber = await self.db.create_payment_destination(title="Sber", currency="RUB", details="", payment_link="")
        await self.db.set_default_payment_destination_id(sber)
        text, markup = await member_payment_method_screen(self.db, friend_id)
        self.assertEqual(
            text, "🏦 <b>Payment method · Anna Petrova</b>\n\nNow: Default (⭐ Sber · RUB)\nShown on Anna's card."
        )
        self.assertEqual(_texts(markup), [["✅ Default (⭐ Sber · RUB)"], ["⭐ Sber · RUB"], ["◀️ To card"]])


# ------------------------------------------------------------------- flows


class UsersFlowTests(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin()
        await self.db.set_setting("base_timezone", "UTC")

    async def _linked(self, name: str, telegram_id: int) -> int:
        friend_id = await self.db.create_friend_invite(name, f"t-{telegram_id}", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(friend_id, telegram_id, "2026-01-01T00:00:00")
        return friend_id

    def screen(self, message_id: int):
        return make_message(self.bot, message_id, "screen", from_bot=True)

    def edits(self, message_id: int) -> list[str]:
        return [call.text for call in self.bot.of(EditMessageText) if call.message_id == message_id]

    def last_answer(self):
        answers = self.bot.answers()
        self.assertTrue(answers, "the tap was not answered")
        return answers[-1]

    async def test_users_menu_and_paging(self):
        await self._linked("Anna Petrova", 1001)
        await self.feed(self.message("👥 Users"))
        (sent,) = self.bot.of(SendMessage)
        self.assertEqual(sent.text, "👥 <b>Users</b> · 1\n\nTap a user to open their card.")
        await self.feed(self.callback("member:page:5", self.screen(700)))
        self.assertEqual(len(self.edits(700)), 1)
        self.assertEqual(len(self.bot.answers()), 1)

    async def test_add_user_prompt_typo_and_success_edits_the_list(self):
        await self.feed(self.callback("member:add:0", self.screen(800)))
        (prompt,) = self.bot.of(SendMessage)
        self.assertEqual(prompt.text, "✏️ Name of the new user?\nExample: <code>Anna Petrova</code>")
        self.assertEqual(_texts(prompt.reply_markup), [["✖️ Cancel"]])
        self.assertNotIn("force_reply", prompt.reply_markup.model_dump(exclude_none=True))
        self.assertEqual(await self.state().get_state(), FriendForm.full_name.state)
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]

        typo = self.message("Anna 123")
        await self.feed(typo)
        self.assertTrue(self.bot.sent_texts()[-1].startswith("⚠️ "), self.bot.sent_texts()[-1])
        self.assertIn("<code>Anna Petrova</code>", self.bot.sent_texts()[-1])

        answer = self.message("Anna Petrova")
        with mock.patch.object(self.bot, "get_me", mock.AsyncMock(return_value=BOT_USER)):
            await self.feed(answer)

        (shown,) = self.edits(800)
        self.assertTrue(
            shown.startswith(
                "✅ <b>Anna Petrova added</b>\n\nSend Anna this sign-in link:\nhttps://t.me/pingsubs_bot?start=join_"
            ),
            shown,
        )
        self.assertIn("\n\nWorks until <tg-time unix=", shown)
        self.assertTrue(shown.endswith("(UTC)</tg-time> (7 days)."), shown)
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(_texts(edit.reply_markup), [["📋 Copy link"], ["◀️ To card"]])
        self.assertIsNone(await self.state().get_state())
        deleted = self.bot.deleted_ids()
        for message_id in (prompt_id, typo.message_id, answer.message_id):
            self.assertIn(message_id, deleted)
        (friend,) = await self.db.list_friends()
        self.assertEqual(friend["full_name"], "Anna Petrova")
        self.assertIn(friend["invite_token"], shown)

    async def test_add_user_without_bot_username(self):
        await self.feed(self.callback("member:add:0", self.screen(801)))
        with mock.patch.object(self.bot, "get_me", mock.AsyncMock(return_value=NO_USERNAME)):
            await self.feed(self.message("Ivan Sidorov"))
        self.assertEqual(
            self.edits(801),
            ["✅ <b>Ivan Sidorov added</b>\n\n⚠️ Could not build the link: the bot username is not set."],
        )

    async def test_card_and_stale_card(self):
        friend_id = await self._linked("Anna Petrova", 1001)
        await self.feed(self.callback(f"member:open:{friend_id}", self.screen(810)))
        (card,) = self.edits(810)
        self.assertTrue(card.startswith('👤 <b><a href="tg://user?id=1001">Anna Petrova</a></b>\n'), card)
        self.assertIn(f"{SUBITEM_INDENT}Payment method: not set", card)
        self.assertTrue(card.endswith("\n\nNot in any subscription yet."), card)

        await self.feed(self.callback("member:open:999", self.screen(811)))
        (shown,) = self.edits(811)
        self.assertTrue(shown.startswith("👥 <b>Users</b> · 1"), shown)
        self.assertEqual(self.last_answer().text, USER_GONE)

    async def test_card_from_an_alert_is_a_new_message_with_close(self):
        friend_id = await self._linked("Anna Petrova", 1001)
        await self.feed(self.callback(MemberAction(action="open_new", friend_id=friend_id).pack(), self.screen(812)))
        (sent,) = self.bot.of(SendMessage)
        self.assertEqual(sent.reply_markup.inline_keyboard[-1][0].text, "✖️ Close")
        self.assertEqual(self.edits(812), [])

    async def test_new_sign_in_link_asks_first_then_replaces_the_link(self):
        friend_id = await self.db.create_friend_invite("Ivan Sidorov", "join_old", "2099-01-01T00:00:00")
        await self.feed(self.callback(f"member:invite:{friend_id}", self.screen(820)))
        (question,) = self.edits(820)
        self.assertTrue(
            question.startswith(
                "⚠️ <b>Make a new sign-in link for Ivan?</b>\nThe old link stops working.\n\n👤 <b>Ivan Sidorov</b>\n"
            ),
            question,
        )
        self.assertEqual((await self.db.get_friend(friend_id))["invite_token"], "join_old")

        with mock.patch.object(self.bot, "get_me", mock.AsyncMock(return_value=NO_USERNAME)):
            await self.feed(self.callback(f"member:invite_yes:{friend_id}", self.screen(821)))
        self.assertEqual((self.last_answer().text, self.last_answer().show_alert), ("Bot username is not set", True))
        self.assertEqual((await self.db.get_friend(friend_id))["invite_token"], "join_old")

        with mock.patch.object(self.bot, "get_me", mock.AsyncMock(return_value=BOT_USER)):
            await self.feed(self.callback(f"member:invite_yes:{friend_id}", self.screen(822)))
        (shown,) = self.edits(822)
        token = (await self.db.get_friend(friend_id))["invite_token"]
        self.assertNotEqual(token, "join_old")
        self.assertTrue(
            shown.startswith(
                "🔗 <b>New sign-in link · Ivan Sidorov</b>\n\nSend Ivan this sign-in link:\n"
                f"https://t.me/pingsubs_bot?start={token}\n\nWorks until <tg-time"
            ),
            shown,
        )
        self.assertEqual(self.last_answer().text, "New link ready")

    async def test_new_link_for_a_signed_in_user_redraws_the_card(self):
        friend_id = await self._linked("Anna Petrova", 1001)
        await self.feed(self.callback(f"member:invite:{friend_id}", self.screen(823)))
        self.assertTrue(self.edits(823)[0].startswith("👤 <b><a href="))
        self.assertEqual(self.last_answer().text, "Anna already signed in")

    async def test_set_balance_with_relative_change_and_hints(self):
        friend_id = await self._linked("Anna Petrova", 1001)
        await self.db.set_friend_balance(friend_id, 100)
        await self.feed(self.callback(f"member:balance_set:{friend_id}", self.screen(830)))
        self.assertEqual(
            self.bot.sent_texts()[-1],
            "✏️ New balance for Anna, RUB? Now 100.00.\n"
            "Send <code>1200</code>, or <code>+500</code> / <code>-200</code> to change it.",
        )
        await self.feed(self.message("lots"))
        self.assertEqual(self.bot.sent_texts()[-1], "⚠️ Need a number, for example <code>1200</code> or <code>+500</code>.")
        await self.feed(self.message("-200"))
        self.assertEqual(self.bot.sent_texts()[-1], "⚠️ The balance cannot go below zero.")
        await self.feed(self.message("+1 100"))
        self.assertEqual(
            self.edits(830),
            [
                f"✅ Balance updated · <b>1{NBSP}200.00{NBSP}RUB</b>\n\n"
                f"💰 <b>Balance · Anna Petrova</b>\n\n<b>1{NBSP}200.00{NBSP}RUB</b>"
            ],
        )
        self.assertEqual((await self.db.get_friend(friend_id))["balance"], 1200)
        self.assertIsNone(await self.state().get_state())
        self.assertNotIn("Balance updated.", self.bot.sent_texts())

    async def test_currency_preset_returns_to_the_balance(self):
        friend_id = await self._linked("Anna Petrova", 1001)
        await self.feed(self.callback(f"member_balance_currency:{friend_id}:USD", self.screen(840)))
        (shown,) = self.edits(840)
        self.assertEqual(shown, f"✅ Currency · <b>USD</b>\n\n💰 <b>Balance · Anna Petrova</b>\n\n<b>0.00{NBSP}USD</b>")
        self.assertEqual(self.last_answer().text, "Currency: USD")

    async def test_other_currency_prompt(self):
        friend_id = await self._linked("Anna Petrova", 1001)
        await self.feed(self.callback(f"member:balance_currency_other:{friend_id}", self.screen(841)))
        self.assertEqual(
            self.bot.sent_texts()[-1], "✏️ Currency for Anna's balance? Three letters, for example <code>USD</code>."
        )
        await self.feed(self.message("euro"))
        self.assertEqual(self.bot.sent_texts()[-1], "⚠️ Need three letters, for example <code>USD</code>.")
        await self.feed(self.message("try"))
        self.assertTrue(self.edits(841)[0].startswith("✅ Currency · <b>TRY</b>\n\n💰 <b>Balance · Anna Petrova</b>"))

    async def test_rename_redraws_the_card_with_a_banner(self):
        friend_id = await self._linked("Anna Petrova", 1001)
        await self.feed(self.callback(f"member:rename:{friend_id}", self.screen(850)))
        self.assertEqual(self.bot.sent_texts()[-1], "✏️ New name for Anna Petrova?")
        self.assertEqual(await self.state().get_state(), MemberEditForm.full_name.state)
        await self.feed(self.message("Anna Smirnova"))
        (shown,) = self.edits(850)
        self.assertTrue(shown.startswith("✅ Renamed · <b>Anna Smirnova</b>\n\n👤 <b><a href="), shown)

    async def test_prompt_for_a_deleted_user_shows_the_list(self):
        friend_id = await self._linked("Anna Petrova", 1001)
        await self.feed(self.callback(f"member:balance_set:{friend_id}", self.screen(851)))
        await self.db.delete_friend(friend_id)
        await self.feed(self.message("10"))
        self.assertEqual(self.edits(851), ["⚠️ This user was deleted\n\n👥 <b>Users</b>\n\nNo users yet."])

    async def test_delete_asks_then_keeps_the_page(self):
        ids = [await self._linked(f"User {index:02d}", 3000 + index) for index in range(8)]
        await self.feed(self.callback(f"member:delete:{ids[7]}", self.screen(860)))
        self.assertEqual(self.edits(860), ["⚠️ <b>Delete User 07?</b>\nThis cannot be undone."])
        self.assertTrue(await self.db.get_friend(ids[7]))

        await self.feed(self.callback(f"member:confirm_delete:{ids[7]}", self.screen(861)))
        (shown,) = self.edits(861)
        self.assertTrue(shown.startswith("✅ User 07 deleted\n\n👥 <b>Users</b> · 7"), shown)
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(_texts(edit.reply_markup)[-1], ["◀️", "2 / 2", "▶️"])
        self.assertEqual(self.last_answer().text, "✅ User 07 deleted")
        self.assertIsNone(await self.db.get_friend(ids[7]))

        await self.feed(self.callback(f"member:confirm_delete:{ids[7]}", self.screen(862)))
        self.assertEqual(self.last_answer().text, USER_GONE)

    async def test_delete_double_tap_is_guarded(self):
        friend_id = await self._linked("Anna Petrova", 1001)
        async with guarded(None, ("member_delete", friend_id)):
            await self.feed(self.callback(f"member:confirm_delete:{friend_id}", self.screen(863)))
        self.assertEqual(self.last_answer().text, "⏳ Already running")
        self.assertTrue(await self.db.get_friend(friend_id))

    async def test_member_report_texts(self):
        pending_id = await self.db.create_friend_invite("Ivan Sidorov", "t-p", "2099-01-01T00:00:00")
        await self.feed(self.callback(f"member:report:{pending_id}", self.screen(870)))
        self.assertEqual(self.edits(870), ["📊 <b>Reports · Ivan Sidorov</b>\n\nIvan has not signed in yet."])
        friend_id = await self._linked("Anna Petrova", 1001)
        await self.feed(self.callback(f"member:report:{friend_id}", self.screen(871)))
        self.assertEqual(self.edits(871), ["📊 <b>Reports · Anna Petrova</b>\n\nNothing to report yet."])
        self.assertEqual(_texts(self.bot.of(EditMessageText)[-1].reply_markup), [["◀️ To card"]])

        today = datetime.now(timezone.utc).date()
        sub_id = await self.db.create_subscription("Netflix", 10.0, "USD", today + timedelta(days=5), 30, None)
        await self.db.set_participant(sub_id, friend_id, True)
        await self.feed(self.callback(f"member:report:{friend_id}", self.screen(872)))
        (report,) = self.edits(872)
        self.assertTrue(report.startswith("📊 <b>Reports · Anna Petrova</b>\n\n"), report)
        self.assertNotIn("📊 Reports\n", report)

    async def test_payment_method_pick_returns_to_the_card(self):
        friend_id = await self._linked("Anna Petrova", 1001)
        sber = await self.db.create_payment_destination(title="Sber", currency="RUB", details="", payment_link="")
        revolut = await self.db.create_payment_destination(title="Revolut", currency="EUR", details="", payment_link="")
        await self.db.set_default_payment_destination_id(sber)

        await self.feed(self.callback(f"member:payment_destination:{friend_id}", self.screen(880)))
        self.assertTrue(self.edits(880)[0].startswith("🏦 <b>Payment method · Anna Petrova</b>"))

        await self.feed(self.callback(f"member_payment_destination:{friend_id}:{revolut}", self.screen(881)))
        (card,) = self.edits(881)
        self.assertTrue(card.startswith("✅ Payment method · <b>Revolut · EUR</b>\n\n👤 "), card)
        self.assertIn(f"{SUBITEM_INDENT}Payment method: Revolut · EUR\n", card + "\n")
        self.assertEqual(self.last_answer().text, "✅ Payment method · Revolut · EUR")

        await self.feed(self.callback(f"member_payment_destination_default:{friend_id}", self.screen(882)))
        (card,) = self.edits(882)
        self.assertTrue(card.startswith("✅ Payment method · <b>Default (⭐ Sber · RUB)</b>\n\n"), card)
        self.assertIn("Payment method: ⭐ Sber · RUB (default)", card)

        await self.db.delete_payment_destination(revolut)
        await self.feed(self.callback(f"member_payment_destination:{friend_id}:{revolut}", self.screen(883)))
        self.assertTrue(self.edits(883)[0].startswith("🏦 <b>Payment method · Anna Petrova</b>"))
        self.assertEqual(self.last_answer().text, METHOD_GONE)

    async def test_every_tap_is_answered_once(self):
        friend_id = await self._linked("Anna Petrova", 1001)
        for data in (
            f"member:open:{friend_id}",
            f"member:balance:{friend_id}",
            f"member:balance_currency:{friend_id}",
            f"member:delete:{friend_id}",
            f"member:report:{friend_id}",
            f"member:back:{friend_id}",
        ):
            with self.subTest(data=data):
                before = len(self.bot.answers())
                await self.feed(self.callback(data, self.screen(890)))
                self.assertEqual(len(self.bot.answers()), before + 1)
        self.assertEqual(CHAT_ID, self.bot.of(EditMessageText)[-1].chat_id)


if __name__ == "__main__":
    unittest.main()
