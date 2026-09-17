"""Check the screen renderers: one-line lists, plain/bold values and payment block hygiene."""

from datetime import datetime, timedelta
from pathlib import Path
import re
import tempfile
from typing import ClassVar
import unittest
from zoneinfo import ZoneInfo

from aiogram.types import Chat, Message, User

from app.core.constants import MONTHLY_PERIOD_SENTINEL, PAYMENT_MODE_FIXED
from app.core.reminders import calculate_next_charge_date, due_status_html, tg_due
from app.storage.db import Database
from app.ui.helpers import (
    _build_subscription_detail_text,
    _build_user_info_text,
    _payment_info_lines,
    send_member_detail,
    send_member_list,
    send_participants_settings,
    send_pricing_settings,
    send_public_account_detail,
    send_public_subscription_detail,
    send_reminder_settings,
    send_subscription_cycle_actions,
    send_subscription_list,
    send_subscription_more,
    send_subscription_open_cycles,
    send_subscription_payment_info,
    send_subscription_user_amounts,
    send_test_user_list,
    send_user_subscription_list,
)

TZ = "UTC"
MONEY_IN_CODE = re.compile(r"<code>[^<]*\d+\.\d{2} [A-Z]{3}[^<]*</code>")
TOGGLE_IN_CODE = re.compile(r"<code>(?:on|off|enabled|disabled)</code>")
ENTITY_IN_CODE = re.compile(r"<(?:code|pre)>[^<]*<tg-time")


class FakeMessage:
    """Stands in for aiogram's Message: records what a screen renderer sends."""

    from_user = None

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[object] = []

    async def answer(self, text: str, reply_markup=None, **_: object) -> None:
        self.texts.append(text)
        self.markups.append(reply_markup)

    @property
    def text(self) -> str:
        return "\n\n".join(self.texts)


class MemberMessage(Message):
    """A real aiogram Message (so renderers see ``from_user``) that records what it answers."""

    sent: ClassVar[list[str]] = []

    @classmethod
    def for_user(cls, telegram_id: int) -> "MemberMessage":
        cls.sent = []
        return cls(
            message_id=1,
            date=datetime.now(ZoneInfo(TZ)),
            chat=Chat(id=telegram_id, type="private"),
            from_user=User(id=telegram_id, is_bot=False, first_name="Member"),
        )

    async def answer(self, text: str, reply_markup=None, **_: object) -> None:
        type(self).sent.append(text)


def _sections(text: str) -> list[str]:
    """The blockquote bodies of a card, in order; also checks the tags are balanced."""
    bodies = re.findall(r"<blockquote>(.*?)</blockquote>", text, flags=re.S)
    assert text.count("<blockquote") == len(bodies) == text.count("</blockquote>"), text
    return bodies


class PaymentInfoLinesTests(unittest.TestCase):
    def test_single_heading_per_line_code_and_no_empty_comment(self):
        lines = _payment_info_lines(
            "Default (Card (RUB))",
            "2200 1234 5678 9012\n\n  Ivan I. <bank>  \n",
            "https://pay.example/x?a=1&b=2",
            "",
        )
        self.assertEqual(
            lines,
            [
                "💳 Payment: Default (Card (RUB))",
                "<code>2200 1234 5678 9012</code>",
                "<code>Ivan I. &lt;bank&gt;</code>",
                "🔗 Link: https://pay.example/x?a=1&amp;b=2",
            ],
        )
        self.assertEqual(sum(1 for line in lines if line.startswith("💳 Payment")), 1)
        self.assertNotIn("<pre>", "\n".join(lines))

    def test_comment_is_plain_text_when_present(self):
        lines = _payment_info_lines("Card", "", "", "Pay <before> the 5th")
        self.assertEqual(lines, ["💳 Payment: Card", "📝 Comment: Pay &lt;before&gt; the 5th"])

    def test_compact_card_variant_drops_labels_and_a_comment_repeating_the_requisites(self):
        details = "2200 1234 5678 9012\nIvan I."
        lines = _payment_info_lines("Card", details, "", "  2200 1234  5678 9012 \n\n Ivan I.  ", compact=True)
        self.assertEqual(lines, ["💳 Card", "<code>2200 1234 5678 9012</code>", "<code>Ivan I.</code>"])
        self.assertNotIn("📝", "\n".join(lines))

        lines = _payment_info_lines("Card", details, "https://pay.example/x", "Pay <early>", compact=True)
        self.assertEqual(lines[-2:], ["🔗 Link: https://pay.example/x", "📝 Pay &lt;early&gt;"])
        self.assertNotIn("Comment:", "\n".join(lines))
        # Empty requisites never suppress an empty comment into a stray line.
        self.assertEqual(_payment_info_lines("not set", "", "", "", compact=True), ["💳 not set"])


class UserInfoTextTests(unittest.TestCase):
    def test_shape_with_telegram_id(self):
        text = _build_user_info_text(
            {"full_name": "Anna <K>", "telegram_id": 1001, "balance": 150, "balance_currency": "rub"},
            [{"name": "Netflix"}, {"name": "Spotify & co"}],
            include_telegram_id=True,
            title="👤 User Info:",
        )
        self.assertEqual(
            text,
            "👤 User Info:\n"
            "<b>Anna &lt;K&gt;</b>\n"
            "Telegram ID: <code>1001</code>\n"
            "\n"
            "Balance: <b>150.00 RUB</b>\n"
            "\n"
            "Subscriptions:\n"
            "• Netflix\n"
            "• Spotify &amp; co",
        )

    def test_shape_without_telegram_id_and_empty_subscriptions(self):
        text = _build_user_info_text(
            {"full_name": None, "telegram_id": None, "balance": "bad"},
            [],
            include_telegram_id=False,
            title="👤 Account:",
        )
        self.assertEqual(
            text,
            "👤 Account:\n<b>Unknown</b>\n\nBalance: <b>0.00 RUB</b>\n\nSubscriptions:\nNo subscriptions yet.",
        )
        self.assertNotIn("🔴", text)
        self.assertNotIn("🟢", text)


class ScreenTextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._directory.name) / "data" / "test.db")
        await self.db.connect()
        await self.db.set_setting("base_timezone", TZ)
        await self.db.set_setting("base_reminder_time", "09:30")
        self.today = datetime.now(ZoneInfo(TZ)).date()
        self.upcoming = self.today + timedelta(days=14)
        self.overdue = self.today - timedelta(days=2)

        self.anna = await self._friend("Anna <K>", 1001)
        self.dmitry = await self._friend("Dmitry", 1002)
        self.boris = await self._friend("Boris", 1003)
        self.pending = await self.db.create_friend_invite("Pending P", "token-pending", "2099-01-01")

        self.netflix = await self.db.create_subscription("Netflix", 1200.0, "RUB", self.upcoming, 30, None)
        for friend_id in (self.anna, self.dmitry, self.boris):
            await self.db.set_participant(self.netflix, friend_id, True)
        await self.db.update_subscription_fields(
            self.netflix, remind_after_due=1, comment="Pay <early>", reminder_time="10:15"
        )
        await self.db.ensure_cycle(self.netflix, self.overdue)
        await self.db.ensure_cycle(self.netflix, self.upcoming)
        await self.db.log_payment(self.netflix, self.upcoming, 1001)

        self.spotify = await self.db.create_subscription("Spotify & co", 30.0, "USD", self.overdue, 30, None)
        await self.db.update_subscription_fields(self.spotify, remind_after_due=0)

        destination_id = await self.db.create_payment_destination(
            title="Card", currency="RUB", details="2200 1234 5678 9012\nIvan I.", payment_link="https://pay.example/x"
        )
        await self.db.set_default_payment_destination_id(destination_id)

    async def asyncTearDown(self):
        await self.db.close()
        self._directory.cleanup()

    async def _friend(self, full_name: str, telegram_id: int) -> int:
        friend_id = await self.db.create_friend_invite(full_name, f"token-{telegram_id}", "2099-01-01")
        self.assertTrue(await self.db.claim_friend_invite(friend_id, telegram_id, "2026-01-01"))
        return friend_id

    async def _render(self, renderer, *args, **kwargs) -> str:
        message = FakeMessage()
        await renderer(message, *args, **kwargs)
        self.assertTrue(message.texts, "renderer sent nothing")
        return message.text

    def _assert_value_style(self, text: str) -> None:
        self.assertIsNone(MONEY_IN_CODE.search(text), text)
        self.assertIsNone(TOGGLE_IN_CODE.search(text), text)
        self.assertIsNone(ENTITY_IN_CODE.search(text), text)
        self.assertNotIn("<pre>", text)

    async def test_admin_subscription_list_is_one_line_per_subscription(self):
        text = await self._render(send_subscription_list, self.db)
        self.assertEqual(
            text,
            "📋 Subscriptions:\n"
            "Choose a subscription to manage:\n"
            "\n"
            "1. <b>Spotify &amp; co</b> — 30.00 USD · no users\n"
            "2. <b>Netflix</b> — 1200.00 RUB · 3 users",
        )

    async def test_public_subscription_list_uses_live_next_charge(self):
        upcoming_value = self.upcoming.isoformat()
        text = await self._render(send_user_subscription_list, self.db, 1001)
        self.assertEqual(
            text,
            "📋 Subscriptions:\nYour plans:\n\n"
            f"1. <b>Netflix</b> — next charge {tg_due(upcoming_value, TZ, 'wD')} "
            f"({tg_due(upcoming_value, TZ, 'r', 'in 14 d')})",
        )
        await self.db.set_participant(self.spotify, self.anna, True)
        text = await self._render(send_user_subscription_list, self.db, 1001)
        overdue_value = self.overdue.isoformat()
        self.assertIn(f"<b>Spotify &amp; co</b> — next charge {tg_due(overdue_value, TZ, 'wD')}\n", text)
        self.assertNotIn(f"{tg_due(overdue_value, TZ, 'wD')} (", text)

    async def _admin_card(self, subscription_id: int, details: str = "2200 1234", comment: str | None = None) -> str:
        if comment is not None:
            await self.db.update_subscription_fields(subscription_id, comment=comment)
        subscription = await self.db.get_subscription(subscription_id)
        participants = await self.db.list_subscription_participants(subscription_id)
        text = _build_subscription_detail_text(
            subscription, participants, "09:30", TZ, 2, "Default (Card (RUB))", details, "https://pay.example/x"
        )
        self._assert_value_style(text)
        return text

    def _next_charge(self, due, distance: str) -> str:
        return f"📅 Next charge: <b>{tg_due(due.isoformat(), TZ, 'wD')}</b> ({distance})"

    async def test_admin_detail_card_sections(self):
        upcoming_value = self.upcoming.isoformat()
        text = await self._admin_card(self.netflix)
        sections = _sections(text)
        self.assertEqual(len(sections), 4)
        self.assertEqual(
            sections[0],
            "💰 <b>1200.00 RUB</b>\n"
            f"{self._next_charge(self.upcoming, tg_due(upcoming_value, TZ, 'r', 'in 14 d'))}\n"
            "🔁 Every 30 days",
        )
        self.assertEqual(
            sections[1],
            "👥 <b>3 users</b> · split by shares, 3 shares × <b>≈ 400.00 RUB</b>\n"
            "• Anna &lt;K&gt; — <b>≈ 400.00 RUB</b>\n• Boris — <b>≈ 400.00 RUB</b>\n• Dmitry — <b>≈ 400.00 RUB</b>",
        )
        self.assertEqual(sections[2], "🔔 10:15 UTC · 1 day(s) before, same day · after due: <b>on</b>")
        self.assertEqual(
            sections[3],
            "💳 Default (Card (RUB))\n<code>2200 1234</code>\n🔗 Link: https://pay.example/x\n📝 Pay &lt;early&gt;",
        )
        self.assertTrue(text.startswith("<b>🏷️ Netflix</b>\n\n<blockquote>"), text)
        self.assertTrue(text.endswith("</blockquote>\n🗂 Open cycles: <b>2</b>"), text)
        self.assertNotIn("shown in", text)
        for stale in ("Pricing & schedule", "Participants:", "Payment mode", "Payment:", "Comment:", "Users ("):
            self.assertNotIn(stale, text)

    async def test_admin_card_shown_in_comment_and_next_charge_rules(self):
        await self.db.update_subscription_fields(self.netflix, base_currency="TRY")
        text = await self._admin_card(self.netflix, comment="  2200  1234 ")
        self.assertIn("💰 <b>1200.00 RUB</b> · shown in TRY\n", text)
        self.assertNotIn("📝", text)
        self.assertIn("<code>2200 1234</code>\n🔗 Link:", text)

        text = await self._admin_card(self.netflix, comment="2200 1234 – t-bank")
        self.assertIn("\n📝 2200 1234 – t-bank</blockquote>", text)

        overdue = await self._admin_card(self.spotify)
        self.assertEqual(len(_sections(overdue)), 4)
        self.assertIn(self._next_charge(self.overdue, "2 d overdue"), overdue)
        self.assertEqual(overdue.count("<tg-time"), 1)
        self.assertIn("👥 <b>0 users</b> · split by shares\nNo users yet.", overdue)
        self.assertIn("🔔 09:30 UTC · ", overdue)
        self.assertIn(" · after due: <b>off</b>", overdue)

        await self.db.update_subscription_fields(
            self.spotify, next_charge_at=self.today, period_days=MONTHLY_PERIOD_SENTINEL
        )
        due_today = await self._admin_card(self.spotify)
        self.assertIn(self._next_charge(self.today, "today"), due_today)
        self.assertIn("\n🔁 Every month on the same day</blockquote>", due_today)

    async def test_admin_card_fixed_and_split_lines(self):
        await self.db.set_participant(self.netflix, self.boris, True, share_weight=2)
        split = await self._admin_card(self.netflix)
        self.assertIn(
            "👥 <b>3 users</b> · split by shares, 4 shares × <b>≈ 300.00 RUB</b>\n"
            "• Anna &lt;K&gt; — <b>≈ 300.00 RUB</b>\n• Boris ×2 — <b>≈ 600.00 RUB</b>\n• Dmitry — <b>≈ 300.00 RUB</b>",
            split,
        )

        await self.db.update_subscription_fields(self.netflix, payment_mode=PAYMENT_MODE_FIXED)
        await self.db.update_participant_fixed_amount(self.netflix, self.anna, 500.0)
        fixed = await self._admin_card(self.netflix)
        self.assertIn(
            "👥 <b>3 users</b> · fixed per user, total <b>4100.00 RUB</b>\n"
            "• Anna &lt;K&gt; — <b>500.00 RUB</b>\n• Boris ×2 — <b>2400.00 RUB</b>\n• Dmitry — <b>1200.00 RUB</b>",
            fixed,
        )
        self.assertNotIn("≈", fixed)
        self.assertNotIn("share", fixed)

    async def test_settings_and_info_screens_keep_code_only_for_editable_time_and_requisites(self):
        reminders = await self._render(send_reminder_settings, self.db, self.netflix)
        self._assert_value_style(reminders)
        self.assertIn("⏰ Time: <code>10:15</code> (UTC, subscription override)", reminders)
        self.assertIn("📣 Post-due alerts: <b>on</b>", reminders)

        spotify_reminders = await self._render(send_reminder_settings, self.db, self.spotify)
        self.assertIn("⏰ Time: default (<code>09:30</code> UTC)", spotify_reminders)
        self.assertIn("📣 Post-due alerts: <b>off</b>", spotify_reminders)

        pricing = await self._render(send_pricing_settings, self.db, self.netflix)
        self._assert_value_style(pricing)
        self.assertIn("💰 Amount: <b>1200.00 RUB</b>", pricing)
        self.assertIn("🌐 Convert currency: RUB", pricing)
        self.assertIn(f"📅 Next charge: {tg_due(self.upcoming.isoformat(), TZ, 'wD')}", pricing)
        self.assertNotIn("<code>", pricing)

        participants = await self._render(send_participants_settings, self.db, self.netflix)
        self.assertEqual(
            participants,
            "👥 Participants:\n\n💳 Payment mode: Split by shares\n👥 Users: 3\n➗ Shares: 3\n"
            "ℹ️ Mode details: split across 3 user(s)",
        )

        amounts = await self._render(send_subscription_user_amounts, self.db, self.netflix)
        self._assert_value_style(amounts)
        self.assertIn("Subscription: <b>1200.00 RUB</b>\nAssigned total: <b>1200.00 RUB</b>", amounts)

        more = await self._render(send_subscription_more, self.db, self.netflix)
        self.assertEqual(more, "📊 Reports:\n\n🗂 Open cycles: 2")

        payment_info = await self._render(send_subscription_payment_info, self.db, self.netflix)
        self.assertEqual(
            payment_info,
            "💳 Payment: Default (Card (RUB))\n<code>2200 1234 5678 9012</code>\n<code>Ivan I.</code>\n"
            "🔗 Link: https://pay.example/x\n📝 Comment: Pay &lt;early&gt;",
        )

    async def test_open_cycle_screens_use_live_dates(self):
        overdue_value = self.overdue.isoformat()
        upcoming_value = self.upcoming.isoformat()
        text = await self._render(send_subscription_open_cycles, self.db, self.netflix)
        self._assert_value_style(text)
        self.assertIn("Subscription: <b>Netflix</b>", text)
        self.assertIn(
            f"1. <b>{tg_due(overdue_value, TZ, 'wD')}</b> — {due_status_html(overdue_value, TZ, self.today)}\n"
            "💰 Amount: <b>1200.00 RUB</b>\n👥 Users: 3 | ✅ Paid: 0/3\n📦 Snapshot flags: users=0 settings=0",
            text,
        )
        self.assertIn(f"2. <b>{tg_due(upcoming_value, TZ, 'wD')}</b> — Due {tg_due(upcoming_value, TZ, 'r', 'in 14 day(s)')}", text)
        self.assertIn("✅ Paid: 1/3", text)

        actions = await self._render(send_subscription_cycle_actions, self.db, self.netflix, overdue_value)
        self._assert_value_style(actions)
        self.assertIn(f"Cycle: <b>{tg_due(overdue_value, TZ, 'wD')}</b>\nStatus: Overdue ", actions)
        self.assertIn("✅ Force close marks <b>3</b> unpaid user(s)", actions)

    async def test_member_screens(self):
        detail = await self._render(send_member_detail, self.db, self.anna)
        self._assert_value_style(detail)
        self.assertTrue(detail.startswith("👤 User Info:\n<b>Anna &lt;K&gt;</b>\nTelegram ID: <code>1001</code>\n"))
        self.assertIn("Balance: <b>0.00 RUB</b>", detail)
        self.assertIn("• Netflix", detail)
        self.assertTrue(detail.endswith("Payment method: Default (Card · RUB)"))

        pending = await self._render(send_member_detail, self.db, self.pending)
        self.assertIn("Telegram ID: not linked", pending)
        self.assertIn("Status: Awaiting authorization\nInvite expires at (UTC): <code>2099-01-01</code>", pending)

        account = await self._render(send_public_account_detail, self.db, 1001)
        self._assert_value_style(account)
        self.assertIn("Top-up methods: Choose when topping up (default: Card · RUB)", account)
        self.assertNotIn("Telegram ID", account)

        members = await self._render(send_member_list, self.db)
        self.assertEqual(
            members,
            "👥 Users:\nChoose a user to manage:\n1. <b>Anna &lt;K&gt;</b>\n2. <b>Boris</b>\n3. <b>Dmitry</b>\n"
            "4. <b>Pending P</b> (pending)",
        )
        self.assertNotIn("Page:", members)
        for index in range(7):
            await self._friend(f"Extra {index}", 2000 + index)
        members_page = await self._render(send_member_list, self.db, page=2)
        self.assertNotIn("Page:", members_page)
        tests_page = await self._render(send_test_user_list, self.db, page=2)
        self.assertNotIn("Page:", tests_page)
        self.assertNotIn("<code>", tests_page)

    PAYMENT_SECTION = (
        "💳 Default (Card (RUB))\n<code>2200 1234 5678 9012</code>\n<code>Ivan I.</code>\n"
        "🔗 Link: https://pay.example/x\n📝 Pay &lt;early&gt;"
    )

    async def test_public_subscription_detail(self):
        upcoming_value = self.upcoming.isoformat()
        text = await self._render(send_public_subscription_detail, self.db, self.netflix)
        self._assert_value_style(text)
        sections = _sections(text)
        self.assertEqual(len(sections), 3)
        self.assertTrue(text.startswith("<b>🏷️ Netflix</b>\n\n<blockquote>"), text)
        self.assertEqual(
            sections[0],
            "💵 My amount: n/a · reminders in RUB (default)\n"
            f"{self._next_charge(self.upcoming, tg_due(upcoming_value, TZ, 'r', 'in 14 d'))}\n"
            "🔁 Every 30 days",
        )
        self.assertEqual(sections[1], "⏰ <code>10:15</code> (UTC) · default")
        self.assertEqual(sections[2], self.PAYMENT_SECTION)
        for admin_only in ("💰", "👥", "after due", "Overdue", "1200.00"):
            self.assertNotIn(admin_only, text)

    async def _member_card(self, telegram_id: int, subscription_id: int) -> str:
        message = MemberMessage.for_user(telegram_id)
        await send_public_subscription_detail(message, self.db, subscription_id)
        self.assertEqual(len(MemberMessage.sent), 1)
        text = MemberMessage.sent[0]
        self._assert_value_style(text)
        return text

    async def test_member_card_overdue_block_and_personal_settings(self):
        overdue_value = self.overdue.isoformat()
        # Anna paid the upcoming cycle, so her next charge is the one after it.
        following = calculate_next_charge_date(self.upcoming, 30, None)
        text = await self._member_card(1001, self.netflix)
        sections = _sections(text)
        self.assertEqual(len(sections), 4)
        self.assertEqual(
            sections[0],
            "💵 My amount: <b>400.00 RUB</b> · reminders in RUB (default)\n"
            f"{self._next_charge(following, tg_due(following.isoformat(), TZ, 'r', 'in 44 d'))}\n"
            "🔁 Every 30 days",
        )
        self.assertEqual(sections[1], f"⚠️ Overdue\n• <b>{tg_due(overdue_value, TZ, 'wD')}</b>")
        self.assertEqual(sections[2], "⏰ <code>10:15</code> (UTC) · default")
        self.assertEqual(sections[3], self.PAYMENT_SECTION)

        await self.db.log_payment(self.netflix, overdue_value, 1001)
        await self.db.set_user_subscription_setting(1001, self.netflix, "reminder_time", "08:00")
        await self.db.set_user_subscription_setting(1001, self.netflix, "target_currency", "usd")
        text = await self._member_card(1001, self.netflix)
        sections = _sections(text)
        self.assertEqual(len(sections), 3)
        self.assertNotIn("Overdue", text)
        self.assertIn("💵 My amount: <b>400.00 RUB</b> · reminders in USD\n", text)
        self.assertEqual(sections[1], "⏰ <code>08:00</code> (UTC)")

    async def test_member_card_admin_view_folds_admin_extras_into_sections(self):
        await self.db.add_admin(1002, "Dmitry")
        upcoming_value = self.upcoming.isoformat()
        text = await self._member_card(1002, self.netflix)
        sections = _sections(text)
        self.assertEqual(len(sections), 5)
        self.assertEqual(
            sections[0],
            "💰 <b>1200.00 RUB</b>\n"
            "💵 My amount: <b>400.00 RUB</b> · reminders in RUB (default)\n"
            f"{self._next_charge(self.upcoming, tg_due(upcoming_value, TZ, 'r', 'in 14 d'))}\n"
            "🔁 Every 30 days",
        )
        self.assertEqual(
            sections[1],
            "👥 <b>3 users</b> · split by shares, 3 shares × <b>≈ 400.00 RUB</b>\n"
            "• Anna &lt;K&gt; — <b>≈ 400.00 RUB</b>\n• Boris — <b>≈ 400.00 RUB</b>\n• Dmitry — <b>≈ 400.00 RUB</b>",
        )
        self.assertEqual(sections[2], f"⚠️ Overdue\n• <b>{tg_due(self.overdue.isoformat(), TZ, 'wD')}</b>")
        self.assertEqual(
            sections[3], "⏰ <code>10:15</code> (UTC) · default · 1 day(s) before, same day · after due: <b>on</b>"
        )
        self.assertEqual(sections[4], self.PAYMENT_SECTION)


if __name__ == "__main__":
    unittest.main()
