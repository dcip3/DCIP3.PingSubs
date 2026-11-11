"""WP10 member 👤 Account, the rename prompt and the member's own 📊 Reports."""

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.methods import SendChatAction
from aiogram.types import InlineKeyboardMarkup

from app.ui.helpers import SUBSCRIPTION_GONE
from app.ui.keyboards import (
    MENU_ACCOUNT,
    MENU_REPORTS,
    OWN_REPORT_BUTTONS_MAX,
    PUBLIC_ACCOUNT_SHOW_CALLBACK,
    PUBLIC_ACCOUNT_SHOW_NEW_CALLBACK,
    guest_reply_keyboard,
    member_own_report_keyboard,
    public_account_keyboard,
)
from app.ui.member_account import (
    ACCOUNT_NEW_ORIGIN,
    ACCOUNT_ORIGIN,
    NO_LONGER_IN_SUBSCRIPTION,
    NOT_SET_UP_ALERT,
    NOT_SET_UP_TEXT,
    RENAME_NOT_TEXT_HINT,
    REPORTS_EMPTY_TEXT,
    AccountSnapshot,
    account_text,
    rename_prompt,
    renamed_banner,
    subscriptions_line,
)
from app.ui.prompts import CLEANUP_IDS, ORIGIN_MESSAGE_ID, ORIGIN_SCREEN, PROMPT_MESSAGE_ID
from app.ui.shell import MEMBERS_ONLY_ALERT
from app.ui.states import PublicAccountForm
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import USER_ID, EditMessageText, SendMessage, make_message
from test_keyboards_fit import LONG_NAME, LONG_SUB, NAME_64, USER_TEXTS, assert_fits

install_keyboard_guard()

NBSP = " "
OTHER_ADMIN = 1

FULL = AccountSnapshot(
    full_name="Anna Petrova",
    balance=350.0,
    currency="RUB",
    method="Sber · RUB",
    waiting=((1000.0, "RUB"),),
    subscriptions=("Netflix", "Spotify"),
)
FULL_TEXT = (
    "👤 <b>Anna Petrova</b>\n\n"
    f"💰 Balance: <b>350.00{NBSP}RUB</b>\n"
    "🏦 Payment method: Sber · RUB\n"
    f"⏳ Waiting for approval: <b>1{NBSP}000.00{NBSP}RUB</b>\n"
    "📋 Subscriptions: Netflix, Spotify"
)


def _layout(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _callbacks(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[button.callback_data for button in row] for row in markup.inline_keyboard]


# ------------------------------------------------------------------- texts


class AccountTextTests(unittest.TestCase):
    def test_full_account(self):
        self.assertEqual(account_text(FULL), FULL_TEXT)

    def test_zero_balance_has_no_warning_and_missing_lines_are_left_out(self):
        snapshot = AccountSnapshot(full_name="Anna Petrova", balance=0.0, currency="RUB")
        self.assertEqual(
            account_text(snapshot),
            "👤 <b>Anna Petrova</b>\n\n"
            f"💰 Balance: <b>0.00{NBSP}RUB</b>\n"
            "📋 Subscriptions: none yet",
        )

    def test_banner_sits_on_top(self):
        self.assertEqual(
            account_text(FULL, banner=renamed_banner("Anna  Smirnova")),
            "✅ Renamed · <b>Anna Smirnova</b>\n\n" + FULL_TEXT,
        )

    def test_names_are_escaped(self):
        snapshot = AccountSnapshot(full_name="Anna <K>", balance=1.5, currency="EUR", subscriptions=("A&B",))
        text = account_text(snapshot)
        self.assertTrue(text.startswith("👤 <b>Anna &lt;K&gt;</b>\n\n"), text)
        self.assertTrue(text.endswith("📋 Subscriptions: A&amp;B"), text)

    def test_waiting_in_two_currencies(self):
        snapshot = AccountSnapshot(
            full_name="Anna", balance=0.0, currency="RUB", waiting=((500.0, "RUB"), (10.0, "EUR"))
        )
        self.assertIn(f"⏳ Waiting for approval: <b>500.00{NBSP}RUB + 10.00{NBSP}EUR</b>", account_text(snapshot))

    def test_long_subscription_list_is_capped(self):
        names = [f"Sub {index}" for index in range(11)]
        self.assertEqual(
            subscriptions_line(names),
            "📋 Subscriptions: Sub 0, Sub 1, Sub 2, Sub 3, Sub 4, Sub 5, Sub 6, Sub 7 and 3 more",
        )

    def test_fixed_texts(self):
        self.assertEqual(
            NOT_SET_UP_TEXT,
            "👤 <b>Account</b>\n\nYour profile is not set up yet. Ask the admin for your sign-in link.",
        )
        self.assertEqual(REPORTS_EMPTY_TEXT, "📊 <b>Reports</b>\n\nNothing to report yet.")
        self.assertEqual(
            rename_prompt("Anna <P>"),
            "✏️ Your new display name? Now: Anna &lt;P&gt;.\nThe admin and reminders use this name.",
        )
        self.assertEqual(RENAME_NOT_TEXT_HINT, "⚠️ Send text, for example <code>Anna Petrova</code>.")


# --------------------------------------------------------------- keyboards


class AccountKeyboardTests(unittest.TestCase):
    def test_top_up_first_and_no_nav_row(self):
        markup = public_account_keyboard()
        self.assertEqual(_layout(markup), [["➕ Top up"], ["✏️ Rename"]])
        self.assertEqual(_callbacks(markup), [["topup:start:0"], ["public_account:rename"]])
        self.assertEqual([b.style for row in markup.inline_keyboard for b in row], ["primary", None])
        assert_fits(markup)

    def test_copy_from_a_push_ends_with_close(self):
        markup = public_account_keyboard(close=True)
        self.assertEqual(_layout(markup), [["➕ Top up"], ["✏️ Rename"], ["✖️ Close"]])
        self.assertEqual(_callbacks(markup)[-1], ["menu:close"])
        self.assertIsNone(markup.inline_keyboard[-1][0].style)
        assert_fits(markup)


class OwnReportKeyboardTests(unittest.TestCase):
    def test_one_button_per_red_subscription(self):
        markup = member_own_report_keyboard([{"id": 7, "name": "Netflix"}, {"id": 12, "name": " Spotify  Family "}])
        self.assertEqual(_layout(markup), [["🔴 Netflix"], ["🔴 Spotify Family"]])
        self.assertEqual(_callbacks(markup), [["sub:public_open_new:7"], ["sub:public_open_new:12"]])
        assert_fits(markup)

    def test_worst_case_names_are_cut_and_callbacks_stay_short(self):
        subs = [{"id": 2_000_000_000 + index, "name": name} for index, name in enumerate((LONG_NAME, NAME_64, LONG_SUB))]
        markup = member_own_report_keyboard(subs)
        assert_fits(markup, (*USER_TEXTS, *(f"🔴 {name}" for name in (LONG_NAME, NAME_64, LONG_SUB))))
        self.assertTrue(markup.inline_keyboard[0][0].text.endswith("…"))
        for row in markup.inline_keyboard:
            self.assertLessEqual(len(row[0].callback_data.encode()), 64)

    def test_capped_and_empty(self):
        subs = [{"id": index, "name": f"Sub {index}"} for index in range(12)]
        self.assertEqual(len(member_own_report_keyboard(subs).inline_keyboard), OWN_REPORT_BUTTONS_MAX)
        self.assertIsNone(member_own_report_keyboard([]))


# -------------------------------------------------------------------- flows


class _MemberCase(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin(OTHER_ADMIN)
        self.friend_id = await self.db.create_friend_invite("Anna Petrova", "join_anna", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(self.friend_id, USER_ID, "2026-11-01T00:00:00")

    def _screen(self, message_id: int = 7001, markup: InlineKeyboardMarkup | None = None):
        message = make_message(self.bot, message_id, "account", from_bot=True)
        if markup is not None:
            message = message.model_copy(update={"reply_markup": markup}).as_(self.bot)
        return message

    def _single_answer(self):
        answers = self.bot.answers()
        self.assertEqual(len(answers), 1, [a.text for a in answers])
        return answers[0]


class AccountFlowTests(_MemberCase):
    async def test_reply_menu_sends_the_account(self):
        destination = await self.db.create_payment_destination(title="Sber", currency="RUB", details="2200")
        await self.db.set_default_payment_destination_id(destination)
        await self.db.set_friend_balance(self.friend_id, 350.0)
        request_id = await self.db.create_topup_request(
            friend_id=self.friend_id,
            destination_id=destination,
            amount=1000.0,
            currency="RUB",
            destination_title="Sber",
            destination_details="2200",
        )
        await self.db.mark_topup_request_pending(request_id)
        # A request still being filled in is not waiting for approval.
        await self.db.create_topup_request(
            friend_id=self.friend_id,
            destination_id=destination,
            amount=50.0,
            currency="RUB",
            destination_title="Sber",
            destination_details="2200",
        )
        sub_id = await self.db.create_subscription("Netflix", 10.0, "USD", datetime(2099, 1, 1).date(), 30, None)
        await self.db.set_participant(sub_id, self.friend_id, True)

        await self.feed(self.message(MENU_ACCOUNT))

        call = self.bot.of(SendMessage)[-1]
        self.assertEqual(
            call.text,
            "👤 <b>Anna Petrova</b>\n\n"
            f"💰 Balance: <b>350.00{NBSP}RUB</b>\n"
            # The default method carries ⭐, as everywhere else.
            "🏦 Payment method: ⭐ Sber · RUB\n"
            f"⏳ Waiting for approval: <b>1{NBSP}000.00{NBSP}RUB</b>\n"
            "📋 Subscriptions: Netflix",
        )
        self.assertEqual(_layout(call.reply_markup), [["➕ Top up"], ["✏️ Rename"]])

    async def test_stranger_gets_the_not_set_up_text_and_guest_menu(self):
        await self.db.delete_friend(self.friend_id)
        await self.feed(self.message(MENU_ACCOUNT))
        call = self.bot.of(SendMessage)[-1]
        self.assertEqual(call.text, NOT_SET_UP_TEXT)
        self.assertEqual(call.reply_markup.keyboard, guest_reply_keyboard().keyboard)

    async def test_show_edits_in_place_and_answers_once(self):
        await self.feed(self.callback(PUBLIC_ACCOUNT_SHOW_CALLBACK, self._screen()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7001)
        self.assertTrue(edit.text.startswith("👤 <b>Anna Petrova</b>\n\n"), edit.text)
        self.assertEqual(_layout(edit.reply_markup), [["➕ Top up"], ["✏️ Rename"]])
        self.assertFalse(self.bot.of(SendMessage))
        self.assertIsNone(self._single_answer().text)

    async def test_show_new_sends_a_new_message_with_close(self):
        await self.feed(self.callback(PUBLIC_ACCOUNT_SHOW_NEW_CALLBACK, self._screen(7002)))
        self.assertFalse(self.bot.of(EditMessageText), "the push is left alone")
        call = self.bot.of(SendMessage)[-1]
        self.assertTrue(call.text.startswith("👤 <b>Anna Petrova</b>\n\n"), call.text)
        self.assertEqual(_layout(call.reply_markup), [["➕ Top up"], ["✏️ Rename"], ["✖️ Close"]])
        self.assertIsNone(self._single_answer().text)

    async def test_show_without_profile_alerts(self):
        await self.db.delete_friend(self.friend_id)
        for data in (PUBLIC_ACCOUNT_SHOW_CALLBACK, PUBLIC_ACCOUNT_SHOW_NEW_CALLBACK, "public_account:rename"):
            await self.feed(self.callback(data, self._screen()))
            answer = self.bot.answers()[-1]
            self.assertEqual((answer.text, answer.show_alert), (NOT_SET_UP_ALERT, True))
        self.assertFalse(self.bot.of(EditMessageText))
        self.assertFalse(self.bot.of(SendMessage))

    async def test_admin_gets_members_only_alert(self):
        await self.make_admin(USER_ID)
        for data in (PUBLIC_ACCOUNT_SHOW_CALLBACK, PUBLIC_ACCOUNT_SHOW_NEW_CALLBACK):
            await self.feed(self.callback(data, self._screen()))
            answer = self.bot.answers()[-1]
            self.assertEqual((answer.text, answer.show_alert), (MEMBERS_ONLY_ALERT, True))


class RenameFlowTests(_MemberCase):
    async def test_rename_edits_the_account_with_a_banner(self):
        await self.feed(self.callback("public_account:rename", self._screen()))
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, rename_prompt("Anna Petrova"))
        self.assertEqual(_layout(prompt.reply_markup), [["✖️ Cancel"]])
        self.assertEqual(await self.state().get_state(), PublicAccountForm.full_name.state)
        data = await self.state().get_data()
        self.assertEqual((data[ORIGIN_MESSAGE_ID], data[ORIGIN_SCREEN]), (7001, ACCOUNT_ORIGIN))
        prompt_id = data[PROMPT_MESSAGE_ID]

        typo = self.message("Anna 2")
        await self.feed(typo)
        self.assertEqual(
            self.bot.of(SendMessage)[-1].text,
            "⚠️ Use only letters, spaces and - ' . in a name, for example <code>Anna Petrova</code>.",
        )
        self.assertEqual(await self.state().get_state(), PublicAccountForm.full_name.state)
        hint_id = (await self.state().get_data())[CLEANUP_IDS][-1]

        answer = self.message("  Anna   Smirnova ")
        sent_before = len(self.bot.of(SendMessage))
        await self.feed(answer)

        self.assertEqual((await self.db.get_friend_by_telegram(USER_ID))["full_name"], "Anna Smirnova")
        self.assertIsNone(await self.state().get_state())
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7001)
        self.assertTrue(
            edit.text.startswith("✅ Renamed · <b>Anna Smirnova</b>\n\n👤 <b>Anna Smirnova</b>\n\n"), edit.text
        )
        self.assertEqual(_layout(edit.reply_markup), [["➕ Top up"], ["✏️ Rename"]])
        self.assertEqual(len(self.bot.of(SendMessage)), sent_before, "no 'Name updated.' and no second account")
        self.assertEqual(
            sorted(self.bot.deleted_ids()),
            sorted([prompt_id, typo.message_id, hint_id, answer.message_id]),
        )

    async def test_rename_from_the_push_copy_keeps_close(self):
        await self.feed(self.callback("public_account:rename", self._screen(7003, public_account_keyboard(close=True))))
        self.assertEqual((await self.state().get_data())[ORIGIN_SCREEN], ACCOUNT_NEW_ORIGIN)
        await self.feed(self.message("Anna Smirnova"))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7003)
        self.assertEqual(_layout(edit.reply_markup), [["➕ Top up"], ["✏️ Rename"], ["✖️ Close"]])

    async def test_gone_origin_gets_a_new_account_message(self):
        await self.feed(self.callback("public_account:rename", self._screen()))
        self.bot.fail(EditMessageText, "Bad Request: message to edit not found", message_id=7001)
        await self.feed(self.message("Anna Smirnova"))
        call = self.bot.of(SendMessage)[-1]
        self.assertTrue(call.text.startswith("✅ Renamed · <b>Anna Smirnova</b>\n\n👤 <b>Anna Smirnova</b>"))
        self.assertEqual(_layout(call.reply_markup), [["➕ Top up"], ["✏️ Rename"]])

    async def test_a_second_rename_tap_replaces_the_first_prompt(self):
        await self.feed(self.callback("public_account:rename", self._screen()))
        first_prompt = (await self.state().get_data())[PROMPT_MESSAGE_ID]
        await self.feed(self.callback("public_account:rename", self._screen()))
        self.assertIn(first_prompt, self.bot.deleted_ids())
        self.assertNotEqual((await self.state().get_data())[PROMPT_MESSAGE_ID], first_prompt)

    async def test_menu_tap_abandons_the_rename(self):
        await self.feed(self.callback("public_account:rename", self._screen()))
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]
        await self.feed(self.message(MENU_ACCOUNT))
        self.assertIsNone(await self.state().get_state())
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertEqual((await self.db.get_friend_by_telegram(USER_ID))["full_name"], "Anna Petrova")


class OwnReportFlowTests(_MemberCase):
    async def _subscription(self, name: str, next_charge, *, paid: bool = False) -> int:
        sub_id = await self.db.create_subscription(name, 30.0, "USD", next_charge, 30, None)
        await self.db.set_participant(sub_id, self.friend_id, True)
        previous = next_charge - timedelta(days=30)
        await self.db.ensure_cycle(sub_id, previous)
        await self.db.close_cycle(sub_id, previous)
        await self.db.ensure_cycle(sub_id, next_charge)
        if paid:
            await self.db.log_payment(sub_id, next_charge, USER_ID)
        return sub_id

    async def test_empty_report(self):
        await self.feed(self.message(MENU_REPORTS))
        call = self.bot.of(SendMessage)[-1]
        self.assertEqual(call.text, REPORTS_EMPTY_TEXT)
        self.assertIsNone(call.reply_markup)

    async def test_red_subscriptions_get_buttons_under_the_report(self):
        today = datetime.now(ZoneInfo("Europe/Moscow")).date()
        late = await self._subscription("Late one", today - timedelta(days=3))
        await self._subscription("Paid one", today + timedelta(days=5), paid=True)

        await self.feed(self.message(MENU_REPORTS))

        self.assertTrue(self.bot.of(SendChatAction), "built under a typing action")
        call = self.bot.of(SendMessage)[-1]
        self.assertIn("🔴 <b>Late one</b>", call.text)
        self.assertIn("🟢 <b>Paid one</b>", call.text)
        self.assertLess(call.text.index("🔴"), call.text.index("🟢"))
        self.assertEqual(_layout(call.reply_markup), [["🔴 Late one"]])
        self.assertEqual(_callbacks(call.reply_markup), [[f"sub:public_open_new:{late}"]])

    async def test_all_green_report_has_no_buttons(self):
        today = datetime.now(ZoneInfo("Europe/Moscow")).date()
        await self._subscription("Paid one", today + timedelta(days=5), paid=True)
        await self.feed(self.message(MENU_REPORTS))
        self.assertIsNone(self.bot.of(SendMessage)[-1].reply_markup)

    async def test_report_button_opens_the_card_as_a_new_message(self):
        today = datetime.now(ZoneInfo("Europe/Moscow")).date()
        late = await self._subscription("Late one", today - timedelta(days=3))
        report = self._screen(7010)
        await self.feed(self.callback(f"sub:public_open_new:{late}", report))
        self.assertFalse(self.bot.of(EditMessageText), "the report is left alone")
        self.assertIn("Late one", self.bot.of(SendMessage)[-1].text)
        self.assertIsNone(self._single_answer().text)

    async def test_report_button_for_a_left_or_deleted_subscription(self):
        today = datetime.now(ZoneInfo("Europe/Moscow")).date()
        late = await self._subscription("Late one", today - timedelta(days=3))
        await self.db.set_participant(late, self.friend_id, False)
        await self.feed(self.callback(f"sub:public_open_new:{late}", self._screen(7011)))
        self.assertEqual(self.bot.answers()[-1].text, NO_LONGER_IN_SUBSCRIPTION)
        await self.db.delete_subscription(late)
        await self.feed(self.callback(f"sub:public_open_new:{late}", self._screen(7012)))
        self.assertEqual(self.bot.answers()[-1].text, SUBSCRIPTION_GONE)
        self.assertFalse(self.bot.of(SendMessage))
        self.assertFalse(self.bot.of(EditMessageText))


if __name__ == "__main__":
    unittest.main()
