"""WP1 shell: /start, sign-in links, Help, Close/Cancel and the identity errors."""

import unittest
from datetime import date, datetime, timedelta, timezone

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup

from app.core.reminders import tg_due
from app.handlers.admin import HELP_ROLE_ADMIN, HELP_ROLE_GUEST, HELP_ROLE_MEMBER, help_text
from app.ui.keyboards import (
    MENU_ACCOUNT,
    MENU_HELP,
    admin_reply_keyboard,
    dialog_cancel_inline_keyboard,
    guest_reply_keyboard,
    public_reply_keyboard,
)
from app.ui.prompts import CLEANUP_IDS, PROMPT_MESSAGE_ID, prompt_markup
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
    AdminStartSnapshot,
    MemberStartSnapshot,
    NextCharge,
    UnpaidBill,
    admin_next_step_line,
    admin_start_snapshot,
    admin_start_text,
    join_success_text,
    member_next_step_line,
    member_share,
    member_start_snapshot,
    member_start_text,
)
from app.ui.style import NBSP
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import USER_ID, DeleteMessage, EditMessageReplyMarkup, EditMessageText, SendMessage
from test_keyboards_fit import AMOUNT, LONG_SUB, NAME_64, assert_fits

install_keyboard_guard()

TODAY = date(2026, 11, 10)
TZ = "Europe/Moscow"
OTHER_ADMIN = 1
MEMBER_ID = USER_ID


def _iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


class Flow(StatesGroup):
    name = State()


# ------------------------------------------------------------------- texts


class FixedTextTests(unittest.TestCase):
    def test_stranger_and_first_admin(self):
        self.assertEqual(
            STRANGER_START_TEXT,
            "👋 <b>PingSubs</b>\n\n"
            "This bot reminds a group about shared subscriptions.\n"
            "Ask the admin for your sign-in link.",
        )
        self.assertEqual(
            FIRST_ADMIN_TEXT,
            "👋 <b>Welcome to PingSubs</b>\n\n"
            "You are the first user, so you are the admin.\n\n"
            "1. “💳 Payment” → add how people pay you\n"
            "2. “👥 Users” → add people\n"
            "3. “📋 Subscriptions” → add a subscription",
        )

    def test_sign_in_link_errors(self):
        self.assertEqual(
            JOIN_LINK_BROKEN_TEXT,
            "⚠️ <b>This sign-in link does not work</b>\n\nIt was used or replaced. Ask the admin for a new one.",
        )
        self.assertEqual(JOIN_LINK_EXPIRED_TEXT, "⌛️ <b>This sign-in link has expired</b>\n\nAsk the admin for a new one.")
        self.assertEqual(
            JOIN_ALREADY_LINKED_TEXT,
            "⚠️ <b>This Telegram account is already linked</b>\n\nAsk the admin if this is a mistake.",
        )
        self.assertEqual(
            JOIN_FAILED_TEXT,
            "⚠️ <b>Could not sign you in</b>\n\nTry the link again or ask the admin for a new one.",
        )

    def test_access_texts(self):
        self.assertEqual(NO_IDENTITY_TEXT, "Could not read your Telegram account. Send /start.")
        self.assertEqual(ADMIN_ACCOUNT_TEXT, "👤 Account is in the member menu. Your menu is below.")
        self.assertEqual(MEMBERS_ONLY_ALERT, "This is for members only.")

    def test_help_does_not_promise_a_top_up_button(self):
        member = help_text(HELP_ROLE_MEMBER)
        self.assertIn("“👤 Account” → “➕ Top up”", member)
        self.assertNotIn("➕ Top up —", member)
        self.assertTrue(help_text(HELP_ROLE_GUEST).endswith("Ask the admin for your sign-in link."))

    def test_join_success(self):
        self.assertEqual(
            join_success_text("Anna  Petrova", ["Netflix", "Spotify"]),
            "✅ <b>Welcome, Anna Petrova</b>\n\nYou are in: Netflix, Spotify.",
        )
        self.assertEqual(
            join_success_text("Anna", []),
            "✅ <b>Welcome, Anna</b>\n\nYou are not in any subscription yet.",
        )
        many = join_success_text("A<b>", [f"S{i}" for i in range(11)])
        self.assertIn("Welcome, A&lt;b&gt;", many)
        self.assertTrue(many.endswith("S7 and 3 more."))


class AdminStartLineTests(unittest.TestCase):
    def _snap(self, **kwargs) -> AdminStartSnapshot:
        base = dict(today=TODAY, tz_name=TZ, subscription_count=2)
        base.update(kwargs)
        return AdminStartSnapshot(**base)

    def test_pending_top_ups_come_first(self):
        snap = self._snap(pending_topups=2, unpaid_subscriptions=3)
        self.assertEqual(admin_next_step_line(snap), "📥 <b>2</b> top-ups wait for approval — open “💳 Payment”.")
        self.assertEqual(
            admin_next_step_line(self._snap(pending_topups=1)),
            "📥 <b>1</b> top-up waits for approval — open “💳 Payment”.",
        )

    def test_unpaid_subscriptions_next(self):
        self.assertEqual(
            admin_next_step_line(self._snap(unpaid_subscriptions=1)),
            "⚠️ <b>1</b> subscription has unpaid bills — open “📊 Reports”.",
        )
        self.assertEqual(
            admin_next_step_line(self._snap(unpaid_subscriptions=4)),
            "⚠️ <b>4</b> subscriptions have unpaid bills — open “📊 Reports”.",
        )

    def test_all_paid_names_the_next_charge(self):
        snap = self._snap(next_charge=NextCharge(name="Net<flix>", due_value=_iso(2)))
        self.assertEqual(
            admin_next_step_line(snap),
            f"✅ All paid. Next charge: Net&lt;flix&gt; · {tg_due(_iso(2), TZ, 'wD')} (in 2 days).",
        )
        today = self._snap(next_charge=NextCharge(name="Netflix", due_value=_iso(0)))
        self.assertTrue(admin_next_step_line(today).endswith("(today)."))
        tomorrow = self._snap(next_charge=NextCharge(name="Netflix", due_value=_iso(1)))
        self.assertTrue(admin_next_step_line(tomorrow).endswith("(tomorrow)."))
        self.assertEqual(admin_next_step_line(self._snap()), "✅ All paid.")

    def test_empty_state(self):
        self.assertEqual(
            admin_next_step_line(self._snap(subscription_count=0)),
            "No subscriptions yet — start in “📋 Subscriptions”.",
        )

    def test_full_text_and_invite_note(self):
        snap = self._snap(subscription_count=0)
        self.assertEqual(admin_start_text(snap), "👋 <b>PingSubs</b>\n\nNo subscriptions yet — start in “📋 Subscriptions”.")
        self.assertEqual(
            admin_start_text(snap, invite_note=True),
            "👋 <b>PingSubs</b>\n\nNo subscriptions yet — start in “📋 Subscriptions”.\n\n"
            "Sign-in links are for members; you are already the admin.",
        )


class MemberStartLineTests(unittest.TestCase):
    def _snap(self, **kwargs) -> MemberStartSnapshot:
        base = dict(today=TODAY, tz_name=TZ, subscription_count=2)
        base.update(kwargs)
        return MemberStartSnapshot(**base)

    def test_unpaid_since(self):
        snap = self._snap(unpaid=(UnpaidBill("Netflix", _iso(-3)),))
        self.assertEqual(
            member_next_step_line(snap),
            f"⚠️ Netflix is unpaid since {tg_due(_iso(-3), TZ, 'wD')}. Open “📋 Subscriptions”.",
        )
        several = self._snap(unpaid=(UnpaidBill("Netflix", _iso(-3)), UnpaidBill("Spotify", _iso(-1))))
        self.assertEqual(
            member_next_step_line(several),
            f"⚠️ Netflix and 1 more are unpaid since {tg_due(_iso(-3), TZ, 'wD')}. Open “📋 Subscriptions”.",
        )

    def test_due_today(self):
        snap = self._snap(unpaid=(UnpaidBill("Netflix", _iso(0)),))
        self.assertEqual(member_next_step_line(snap), "⏳ Netflix is due today. Open “📋 Subscriptions”.")

    def test_all_paid_with_share(self):
        charge = NextCharge(name="Netflix", due_value=_iso(2), amount=250.0, currency="RUB")
        self.assertEqual(
            member_start_text(self._snap(next_charge=charge)),
            "👋 <b>PingSubs</b>\n\n"
            f"✅ All paid. Next: Netflix · <b>250.00{NBSP}RUB</b> on {tg_due(_iso(2), TZ, 'wD')} (in 2 days).",
        )
        big = NextCharge(name="Netflix", due_value=_iso(5), amount=AMOUNT, currency="RUB")
        self.assertIn(f"<b>12{NBSP}500.00{NBSP}RUB</b>", member_next_step_line(self._snap(next_charge=big)))
        unknown = NextCharge(name="Netflix", due_value=_iso(5), amount=None, currency="RUB")
        self.assertIn(f"<b>?{NBSP}RUB</b>", member_next_step_line(self._snap(next_charge=unknown)))

    def test_not_in_any_subscription(self):
        self.assertEqual(
            member_next_step_line(self._snap(subscription_count=0)),
            "You are not in any subscription yet.",
        )
        self.assertEqual(member_next_step_line(self._snap()), "✅ All paid.")


class MemberShareTests(unittest.TestCase):
    def test_split_and_fixed(self):
        people = [{"telegram_id": 7, "share_weight": 2}, {"telegram_id": 8, "share_weight": 1}]
        sub = {"amount": 300.0, "payment_mode": "split", "share_limit": None}
        self.assertAlmostEqual(member_share(sub, people, 7), 200.0)
        self.assertAlmostEqual(member_share({**sub, "share_limit": 6}, people, 8), 50.0)
        fixed = {"amount": 99.0, "payment_mode": "fixed"}
        self.assertAlmostEqual(member_share(fixed, [{"telegram_id": 7, "fixed_amount": 10}], 7), 10.0)
        self.assertAlmostEqual(member_share(fixed, [{"telegram_id": 7, "fixed_amount": None}], 7), 99.0)
        self.assertIsNone(member_share(sub, people, 9))


# ---------------------------------------------------------------- keyboards


class ShellKeyboardFitTests(unittest.TestCase):
    def test_menus_and_prompt_cancel_fit(self):
        for markup in (
            admin_reply_keyboard(),
            public_reply_keyboard(),
            guest_reply_keyboard(),
            dialog_cancel_inline_keyboard(),
            dialog_cancel_inline_keyboard(),
            prompt_markup(),
        ):
            with self.subTest(markup=markup):
                assert_fits(markup)

    def test_menu_layouts(self):
        def labels(markup):
            return [[button.text for button in row] for row in markup.keyboard]

        self.assertEqual(
            labels(admin_reply_keyboard()),
            [["👥 Users", "📋 Subscriptions"], ["💳 Payment", "📊 Reports"], ["⚙️ Settings", "❓ Help"]],
        )
        self.assertEqual(
            labels(public_reply_keyboard()),
            [["👤 Account", "📋 Subscriptions"], ["📊 Reports", "⚙️ Settings"], ["❓ Help"]],
        )
        self.assertEqual(labels(guest_reply_keyboard()), [["❓ Help"]])


# ------------------------------------------------------- snapshots from the DB


class SnapshotTests(RealDispatcherTestCase):
    async def _member(self, name: str, telegram_id: int) -> int:
        friend_id = await self.db.create_friend_invite(name, f"join_{telegram_id}", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(friend_id, telegram_id, "2026-11-01T00:00:00")
        return friend_id

    async def test_admin_snapshot_counts_unpaid_and_pending(self):
        anna = await self._member("Anna Petrova", 501)
        bob = await self._member("Bob", 502)
        overdue = await self.db.create_subscription("Netflix", 300.0, "RUB", TODAY - timedelta(days=3), 30, None)
        paid = await self.db.create_subscription("Spotify", 100.0, "RUB", TODAY + timedelta(days=4), 30, None)
        for sub in (overdue, paid):
            await self.db.set_participant(sub, anna, True)
            await self.db.set_participant(sub, bob, True)
        await self.db.ensure_cycle(overdue, TODAY - timedelta(days=3))
        await self.db.log_payment(overdue, TODAY - timedelta(days=3), 501)
        snap = await admin_start_snapshot(self.db, today=TODAY)
        self.assertEqual((snap.subscription_count, snap.unpaid_subscriptions, snap.pending_topups), (2, 1, 0))
        self.assertEqual(snap.next_charge, NextCharge(name="Spotify", due_value=_iso(4)))

        await self.db.log_payment(overdue, TODAY - timedelta(days=3), 502)
        request_id = await self.db.create_topup_request(
            friend_id=anna, destination_id=None, amount=500.0, currency="RUB",
            destination_title="Sber", destination_details="",
        )
        await self.db.mark_topup_request_pending(request_id)
        snap = await admin_start_snapshot(self.db, today=TODAY)
        self.assertEqual((snap.unpaid_subscriptions, snap.pending_topups), (0, 1))
        self.assertTrue(admin_next_step_line(snap).startswith("📥 <b>1</b> top-up waits"))

    async def test_member_snapshot_unpaid_then_next_with_share(self):
        anna = await self._member("Anna", MEMBER_ID)
        bob = await self._member("Bob", 502)
        netflix = await self.db.create_subscription("Netflix", 300.0, "RUB", TODAY - timedelta(days=2), 30, None)
        spotify = await self.db.create_subscription(LONG_SUB, 90.0, "RUB", TODAY + timedelta(days=3), 30, None)
        await self.db.set_participant(netflix, anna, True, share_weight=2)
        await self.db.set_participant(netflix, bob, True)
        await self.db.set_participant(spotify, anna, True)
        await self.db.set_participant(spotify, bob, True)
        await self.db.ensure_cycle(netflix, TODAY - timedelta(days=2))

        snap = await member_start_snapshot(self.db, MEMBER_ID, today=TODAY)
        self.assertEqual(snap.subscription_count, 2)
        self.assertEqual(list(snap.unpaid), [UnpaidBill("Netflix", _iso(-2))])

        await self.db.log_payment(netflix, TODAY - timedelta(days=2), MEMBER_ID)
        snap = await member_start_snapshot(self.db, MEMBER_ID, today=TODAY)
        self.assertEqual(snap.unpaid, ())
        self.assertEqual(snap.next_charge, NextCharge(LONG_SUB, _iso(3), 45.0, "RUB"))

        # Paid ahead for the next charge: the one after it is named instead.
        await self.db.log_payment(spotify, TODAY + timedelta(days=3), MEMBER_ID)
        snap = await member_start_snapshot(self.db, MEMBER_ID, today=TODAY)
        self.assertEqual(snap.next_charge.due_value, _iso(28))
        self.assertEqual(snap.next_charge.name, "Netflix")
        self.assertAlmostEqual(snap.next_charge.amount, 200.0)


# ------------------------------------------------------------- the handlers


class StartHandlerTests(RealDispatcherTestCase):
    def _last_send(self) -> SendMessage:
        sent = self.bot.of(SendMessage)
        self.assertTrue(sent, "nothing was sent")
        return sent[-1]

    def _assert_menu(self, call: SendMessage, expected: ReplyKeyboardMarkup) -> None:
        self.assertIsInstance(call.reply_markup, ReplyKeyboardMarkup)
        self.assertEqual(call.reply_markup.keyboard, expected.keyboard)
        self.assertTrue(call.reply_markup.is_persistent)

    async def test_first_user_becomes_admin(self):
        await self.feed(self.message("/start"))
        call = self._last_send()
        self.assertEqual(call.text, FIRST_ADMIN_TEXT)
        self._assert_menu(call, admin_reply_keyboard())
        self.assertTrue(await self.db.is_admin(USER_ID))

    async def test_returning_admin_gets_the_next_step(self):
        await self.make_admin()
        await self.feed(self.message("/start"))
        call = self._last_send()
        self.assertEqual(call.text, "👋 <b>PingSubs</b>\n\nNo subscriptions yet — start in “📋 Subscriptions”.")
        self._assert_menu(call, admin_reply_keyboard())

    async def test_stranger_gets_the_guest_menu(self):
        await self.make_admin(OTHER_ADMIN)
        await self.feed(self.message("/start"))
        call = self._last_send()
        self.assertEqual(call.text, STRANGER_START_TEXT)
        self._assert_menu(call, guest_reply_keyboard())

    async def test_linked_member_gets_member_menu(self):
        await self.make_admin(OTHER_ADMIN)
        friend_id = await self.db.create_friend_invite("Anna", "join_x", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(friend_id, USER_ID, "2026-11-01T00:00:00")
        await self.feed(self.message("/start"))
        call = self._last_send()
        self.assertEqual(call.text, "👋 <b>PingSubs</b>\n\nYou are not in any subscription yet.")
        self._assert_menu(call, public_reply_keyboard())

    async def test_start_winds_a_pending_prompt_down(self):
        await self.make_admin()
        await self.set_state(Flow.name, **{PROMPT_MESSAGE_ID: 901, CLEANUP_IDS: [902]})
        await self.feed(self.message("/start"))
        self.assertIsNone(await self.state().get_state())
        self.assertEqual(sorted(self.bot.deleted_ids()), [901, 902])

    async def test_join_success(self):
        await self.make_admin(OTHER_ADMIN)
        friend_id = await self.db.create_friend_invite(NAME_64, "join_ok", "2099-01-01T00:00:00")
        sub = await self.db.create_subscription("Netflix", 300.0, "RUB", TODAY, 30, None)
        await self.db.set_participant(sub, friend_id, True)
        await self.feed(self.message("/start join_ok"))
        call = self._last_send()
        self.assertEqual(call.text, f"✅ <b>Welcome, {NAME_64}</b>\n\nYou are in: Netflix.")
        self._assert_menu(call, public_reply_keyboard())
        self.assertEqual(int((await self.db.get_friend(friend_id))["telegram_id"]), USER_ID)

    async def test_join_errors_get_the_guest_menu(self):
        await self.make_admin(OTHER_ADMIN)
        past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None).isoformat()
        await self.db.create_friend_invite("Old", "join_old", past)
        cases = (("/start join_nope", JOIN_LINK_BROKEN_TEXT), ("/start join_old", JOIN_LINK_EXPIRED_TEXT))
        for text, expected in cases:
            with self.subTest(text=text):
                await self.feed(self.message(text))
                call = self._last_send()
                self.assertEqual(call.text, expected)
                self._assert_menu(call, guest_reply_keyboard())

    async def test_join_when_already_linked_elsewhere(self):
        await self.make_admin(OTHER_ADMIN)
        mine = await self.db.create_friend_invite("Me", "join_me", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(mine, USER_ID, "2026-11-01T00:00:00")
        await self.db.create_friend_invite("Other", "join_other", "2099-01-01T00:00:00")
        await self.feed(self.message("/start join_other"))
        self.assertEqual(self._last_send().text, JOIN_ALREADY_LINKED_TEXT)

    async def test_admin_opening_a_sign_in_link(self):
        await self.make_admin()
        await self.db.create_friend_invite("Anna", "join_a", "2099-01-01T00:00:00")
        await self.feed(self.message("/start join_a"))
        call = self._last_send()
        self.assertTrue(call.text.endswith("\n\nSign-in links are for members; you are already the admin."))
        self._assert_menu(call, admin_reply_keyboard())


class MenuHandlerTests(RealDispatcherTestCase):
    async def test_help_button_matches_help_command(self):
        await self.make_admin()
        await self.feed(self.message(MENU_HELP))
        await self.feed(self.message("/help"))
        sent = self.bot.of(SendMessage)
        self.assertEqual([call.text for call in sent], [help_text(HELP_ROLE_ADMIN)] * 2)

    async def test_admin_tapping_account(self):
        await self.make_admin()
        await self.feed(self.message(MENU_ACCOUNT))
        call = self.bot.of(SendMessage)[-1]
        self.assertEqual(call.text, ADMIN_ACCOUNT_TEXT)
        self.assertEqual(call.reply_markup.keyboard, admin_reply_keyboard().keyboard)

    async def test_admin_rename_account_gets_members_only_alert(self):
        await self.make_admin()
        await self.feed(self.callback("public_account:rename"))
        answer = self.bot.answers()[-1]
        self.assertEqual((answer.text, answer.show_alert), (MEMBERS_ONLY_ALERT, True))


class CloseCancelTests(RealDispatcherTestCase):
    async def test_close_deletes_silently(self):
        callback = self.callback("menu:close")
        await self.feed(callback)
        self.assertEqual(self.bot.deleted_ids(), [callback.message.message_id])
        self.assertEqual(len(self.bot.answers()), 1)
        self.assertIsNone(self.bot.answers()[0].text)

    async def test_close_too_old_strips_buttons_and_keeps_text(self):
        callback = self.callback("menu:close")
        self.bot.fail(DeleteMessage, "Bad Request: message can't be deleted for everyone")
        await self.feed(callback)
        stripped = self.bot.of(EditMessageReplyMarkup)
        self.assertEqual([call.message_id for call in stripped], [callback.message.message_id])
        self.assertEqual(self.bot.of(EditMessageText), [])

    async def test_close_on_a_prompt_ends_the_prompt(self):
        callback = self.callback("menu:close")
        await self.set_state(Flow.name, **{PROMPT_MESSAGE_ID: callback.message.message_id, CLEANUP_IDS: [77]})
        await self.feed(callback)
        self.assertIsNone(await self.state().get_state())
        self.assertEqual(sorted(self.bot.deleted_ids()), sorted([77, callback.message.message_id]))

    async def test_close_elsewhere_keeps_a_pending_prompt(self):
        await self.set_state(Flow.name, **{PROMPT_MESSAGE_ID: 1})
        await self.feed(self.callback("menu:close"))
        self.assertIsNotNone(await self.state().get_state())

    async def test_cancel_toasts_and_works_without_fsm_data(self):
        callback = self.callback("dialog:cancel")
        await self.feed(callback)
        self.assertEqual(self.bot.deleted_ids(), [callback.message.message_id])
        self.assertEqual([answer.text for answer in self.bot.answers()], ["Cancelled"])

    async def test_cancel_winds_down_typos(self):
        callback = self.callback("dialog:cancel")
        await self.set_state(Flow.name, **{PROMPT_MESSAGE_ID: callback.message.message_id, CLEANUP_IDS: [55, 56]})
        await self.feed(callback)
        self.assertIsNone(await self.state().get_state())
        self.assertEqual(sorted(self.bot.deleted_ids()), sorted([55, 56, callback.message.message_id]))


if __name__ == "__main__":
    unittest.main()
