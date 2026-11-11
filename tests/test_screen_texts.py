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
from app.core.reminders import calculate_next_charge_date, tg_clock, tg_due
from app.storage.db import Database
from app.ui.helpers import (
    build_subscription_card,
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
from app.ui.member_screens import send_member_detail, send_member_list
from app.ui.style import SUBITEM_INDENT
from keyboard_guard import install_keyboard_guard

install_keyboard_guard()

TZ = "UTC"
# Money is grouped with NBSP (format_money); either space must still be caught.
MONEY_IN_CODE = re.compile(r"<code>[^<]*\d+\.\d{2}[ \u00a0][A-Z]{3}[^<]*</code>")
DATE_IN_CODE = re.compile(r"<code>\d{2}\.\d{2}(?:\.\d{4})?</code>")
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
        self.assertIsNone(DATE_IN_CODE.search(text), text)
        self.assertIsNone(TOGGLE_IN_CODE.search(text), text)
        self.assertIsNone(ENTITY_IN_CODE.search(text), text)
        self.assertNotIn("<pre>", text)

    async def test_admin_subscription_list_counts_attention_and_lists_items_as_buttons(self):
        message = FakeMessage()
        await send_subscription_list(message, self.db)
        self.assertEqual(
            message.text,
            "📋 <b>Subscriptions</b> · 2\n"
            "\n"
            "⚠️ Unpaid: <b>1</b>\n"
            "👥 No participants: <b>1</b>\n"
            "\n"
            "Tap a subscription to manage it.",
        )
        rows = [[button.text for button in row] for row in message.markups[-1].inline_keyboard]
        # Unpaid first, the amount kept whole; no Back/Close on a top-level screen.
        self.assertEqual(
            rows,
            [["➕ New subscription"], ["⚠️ Netflix · 1 200.00 RUB"], ["👥 Spotify & co · no participants"]],
        )

        await self.db.delete_subscription(self.netflix)
        await self.db.delete_subscription(self.spotify)
        message = FakeMessage()
        await send_subscription_list(message, self.db)
        self.assertEqual(message.text, "📋 <b>Subscriptions</b>\n\nNo subscriptions yet.")

    async def test_member_subscription_list_counts_unpaid_and_lists_items_as_buttons(self):
        message = FakeMessage()
        await send_user_subscription_list(message, self.db, 1001)
        self.assertEqual(
            message.text,
            "📋 <b>Subscriptions</b> · 1\n\n⚠️ Unpaid: <b>1</b>\n\nTap one to see your share and how to pay.",
        )
        rows = [[button.text for button in row] for row in message.markups[-1].inline_keyboard]
        self.assertEqual(rows, [["⚠️ Netflix · overdue"]])

        # Spotify's next charge is in the past (the worker has not run): the list shows the next one ahead.
        await self.db.set_participant(self.spotify, self.anna, True)
        message = FakeMessage()
        await send_user_subscription_list(message, self.db, 1001)
        self.assertTrue(message.text.startswith("📋 <b>Subscriptions</b> · 2\n\n⚠️ Unpaid: <b>1</b>"), message.text)
        following = calculate_next_charge_date(self.overdue, 30, None)
        rows = [[button.text for button in row] for row in message.markups[-1].inline_keyboard]
        self.assertEqual(rows, [["⚠️ Netflix · overdue"], [f"Spotify & co · {following:%d.%m}"]])

        message = FakeMessage()
        await send_user_subscription_list(message, self.db, 9999)
        self.assertEqual(
            message.text,
            "📋 <b>Subscriptions</b>\n\nYou are not in any subscription yet. The admin adds you.",
        )
        self.assertEqual(message.markups, [None])

    async def _admin_card(self, subscription_id: int, comment: str | None = None) -> str:
        if comment is not None:
            await self.db.update_subscription_fields(subscription_id, comment=comment)
        card = await build_subscription_card(self.db, subscription_id)
        self.assertIsNotNone(card)
        text = card[0]
        self._assert_value_style(text)
        self.assertNotIn("<blockquote>", text)  # block layout, not the old plain quotes
        return text

    def _card_next_charge(self, due, distance: str) -> str:
        return f"{SUBITEM_INDENT}Next charge: <b>{tg_due(due.isoformat(), TZ, 'wD')}</b> ({distance})"

    async def test_admin_detail_card_blocks(self):
        upcoming_value = self.upcoming.isoformat()
        overdue_value = self.overdue.isoformat()
        text = await self._admin_card(self.netflix)
        i = SUBITEM_INDENT
        self.assertEqual(
            text,
            "🏷️ <b>Netflix</b> · <b>1 200.00 RUB</b> · every 30 days\n"
            "\n"
            "📅 <b>Schedule</b>\n"
            f"{self._card_next_charge(self.upcoming, tg_due(upcoming_value, TZ, 'r', 'in 14 days'))}\n"
            f"{i}Repeats: every 30 days\n"
            "\n"
            "👥 <b>Participants: 3</b>\n"
            f"{i}Split by shares · 3 shares × <b>≈ 400.00 RUB</b>\n"
            f"{i}• Anna &lt;K&gt; — <b>≈ 400.00 RUB</b>\n"
            f"{i}• Boris — <b>≈ 400.00 RUB</b>\n"
            f"{i}• Dmitry — <b>≈ 400.00 RUB</b>\n"
            "\n"
            "🧾 <b>Current cycle</b>\n"
            f"{i}Due: {tg_due(overdue_value, TZ, 'wD')} (overdue by 2 days)\n"
            f"{i}░░░░░░░░░░ 0 of 3 paid\n"
            f"{i}Unpaid: Anna &lt;., Boris, Dmitry\n"
            f"{i}Open cycles: <b>2</b>\n"
            "\n"
            "🔔 <b>Reminders</b>\n"
            f"{i}<b>10:15</b> in each member's timezone · 1 day before, on the day · After due date: daily until paid\n"
            "\n"
            "💳 <b>Payment</b>\n"
            f"{i}Payment method: ⭐ Card · RUB\n"
            f"{i}<code>2200 1234 5678 9012</code> (more in “💳 Payment”)\n"
            f"{i}🔗 Link: https://pay.example/x\n"
            f"{i}📝 Pay &lt;early&gt;",
        )
        for stale in ("Pricing & schedule", "Payment mode", "Payment:", "Comment:", "Users (", " d overdue", "day(s)"):
            self.assertNotIn(stale, text)

    async def test_admin_card_shown_in_comment_and_next_charge_rules(self):
        await self.db.update_subscription_fields(self.netflix, base_currency="TRY")
        text = await self._admin_card(self.netflix, comment="  2200 1234  5678 9012 \n Ivan I. ")
        self.assertIn(f"\n{SUBITEM_INDENT}Shown to members in: TRY\n", text)
        self.assertNotIn("📝", text)  # the comment repeats the requisites
        self.assertIn(f"<code>2200 1234 5678 9012</code> (more in “💳 Payment”)\n{SUBITEM_INDENT}🔗 Link:", text)

        text = await self._admin_card(self.netflix, comment="2200 1234 – t-bank")
        self.assertTrue(text.endswith(f"\n{SUBITEM_INDENT}📝 2200 1234 – t-bank"), text)

        overdue = await self._admin_card(self.spotify)
        self.assertIn(self._card_next_charge(self.overdue, "overdue by 2 days"), overdue)
        self.assertEqual(overdue.count("<tg-time"), 1)
        self.assertIn(f"👥 <b>Participants</b>\n{SUBITEM_INDENT}No one yet — nobody gets reminders.", overdue)
        self.assertNotIn("Current cycle", overdue)  # no open cycle
        self.assertIn(f"{SUBITEM_INDENT}<b>09:30</b> in each member's timezone · ", overdue)
        self.assertIn(" · After due date: off", overdue)

        await self.db.update_subscription_fields(
            self.spotify, next_charge_at=self.today, period_days=MONTHLY_PERIOD_SENTINEL
        )
        due_today = await self._admin_card(self.spotify)
        self.assertTrue(due_today.startswith("🏷️ <b>Spotify &amp; co</b> · <b>30.00 USD</b> · monthly\n"))
        self.assertIn(self._card_next_charge(self.today, "today"), due_today)
        day = self.today.day
        suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        self.assertIn(f"\n{SUBITEM_INDENT}Repeats: every month on the {day}{suffix}\n", due_today)

    async def test_admin_card_fixed_and_split_lines(self):
        await self.db.set_participant(self.netflix, self.boris, True, share_weight=2)
        split = await self._admin_card(self.netflix)
        i = SUBITEM_INDENT
        self.assertIn(
            f"{i}Split by shares · 4 shares × <b>≈ 300.00 RUB</b>\n"
            f"{i}• Anna &lt;K&gt; — <b>≈ 300.00 RUB</b>\n{i}• Boris ×2 — <b>≈ 600.00 RUB</b>\n"
            f"{i}• Dmitry — <b>≈ 300.00 RUB</b>",
            split,
        )

        await self.db.update_subscription_fields(self.netflix, payment_mode=PAYMENT_MODE_FIXED)
        await self.db.update_participant_fixed_amount(self.netflix, self.anna, 500.0)
        fixed = await self._admin_card(self.netflix)
        self.assertIn(
            f"{i}Fixed per user · total <b>4 100.00 RUB</b>\n"
            f"{i}• Anna &lt;K&gt; — <b>500.00 RUB</b>\n{i}• Boris ×2 — <b>2 400.00 RUB</b>\n"
            f"{i}• Dmitry — <b>1 200.00 RUB</b>",
            fixed,
        )
        self.assertNotIn("≈", fixed)
        self.assertNotIn("Split by shares", fixed)

    async def test_settings_and_info_screens_keep_code_only_for_editable_time_and_requisites(self):
        reminders = await self._render(send_reminder_settings, self.db, self.netflix)
        self._assert_value_style(reminders)
        # WP6: the time is applied in each member's own timezone, so no admin zone and no <code>.
        self.assertEqual(
            reminders,
            "🔔 <b>Reminders · Netflix</b>\n\n"
            "Reminder time: <b>10:15</b> in each member's timezone\n"
            "Days: 1 day before, on the day\n"
            "After due date: daily until paid",
        )

        spotify_reminders = await self._render(send_reminder_settings, self.db, self.spotify)
        self.assertIn("Reminder time: <b>09:30</b> in each member's timezone (default)\n", spotify_reminders)
        self.assertIn("\nAfter due date: off", spotify_reminders)
        self.assertNotIn("<code>", spotify_reminders)

        pricing = await self._render(send_pricing_settings, self.db, self.netflix)
        self._assert_value_style(pricing)
        self.assertTrue(pricing.startswith("💰 <b>Pricing · Netflix</b>\n\nAmount: <b>1 200.00 RUB</b>\n"), pricing)
        self.assertIn("\nShown to members in: RUB\n", pricing)
        self.assertIn(f"\nNext charge: <b>{tg_due(self.upcoming.isoformat(), TZ, 'wD')}</b> (", pricing)
        self.assertNotIn("<code>", pricing)

        participants = await self._render(send_participants_settings, self.db, self.netflix)
        self._assert_value_style(participants)
        self.assertEqual(
            participants,
            "👥 <b>Participants · Netflix</b> · 3\n\nMode: split by shares · 3 shares\n"
            "• Anna &lt;K&gt; — <b>≈ 400.00 RUB</b>\n• Boris — <b>≈ 400.00 RUB</b>\n"
            "• Dmitry — <b>≈ 400.00 RUB</b>\n\nEach person's amount changes with the mode.",
        )

        amounts = await self._render(send_subscription_user_amounts, self.db, self.netflix)
        self._assert_value_style(amounts)
        self.assertIn(
            "Subscription: <b>1 200.00 RUB</b>\nAssigned total: <b>1 200.00 RUB</b>\n\n", amounts
        )

        # WP8: the 'more' submenu is gone (Delete and Reports are on the card); old buttons redraw the card.
        more = await self._render(send_subscription_more, self.db, self.netflix)
        self.assertEqual(more, await self._admin_card(self.netflix))

        payment_info = await self._render(send_subscription_payment_info, self.db, self.netflix)
        self.assertEqual(
            payment_info,
            "💳 <b>Payment · Netflix</b>\n\nPayment method: ⭐ Card · RUB (default)\n"
            "<code>2200 1234 5678 9012</code>\n<code>Ivan I.</code>\n"
            "🔗 Link: https://pay.example/x\n\n📝 Comment: Pay &lt;early&gt;",
        )

    async def test_open_cycle_screens_use_live_dates(self):
        overdue_value = self.overdue.isoformat()
        upcoming_value = self.upcoming.isoformat()
        i = SUBITEM_INDENT
        text = await self._render(send_subscription_open_cycles, self.db, self.netflix)
        self._assert_value_style(text)
        # WP8: one block per cycle with indented lines; the '📦 Snapshot flags' line is gone.
        self.assertEqual(
            text,
            "🗂 <b>Open cycles · Netflix</b> · 2\n\n"
            f"📅 <b>{tg_due(overdue_value, TZ, 'wD')}</b> · overdue by 2 days\n"
            f"{i}Amount: <b>1 200.00 RUB</b>\n"
            f"{i}░░░░░░░░░░ 0 of 3 paid\n\n"
            f"📅 <b>{tg_due(upcoming_value, TZ, 'wD')}</b> · due in 14 days\n"
            f"{i}Amount: <b>1 200.00 RUB</b>\n"
            f"{i}███░░░░░░░ 1 of 3 paid",
        )
        self.assertNotIn("Snapshot", text)

        actions = await self._render(send_subscription_cycle_actions, self.db, self.netflix, overdue_value)
        self._assert_value_style(actions)
        self.assertEqual(
            actions,
            f"🗂 <b>Cycle {tg_due(overdue_value, TZ, 'wD')} · Netflix</b>\n\n"
            "Status: overdue by 2 days\n"
            "Amount: <b>1 200.00 RUB</b>\n"
            "░░░░░░░░░░ 0 of 3 paid\n"
            "⚠️ Unpaid: Anna &lt;K&gt;, Boris, Dmitry\n\n"
            "“♻️ Recreate” takes the current people and settings and clears payment marks. "
            "“🔒 Close cycle” marks everyone unpaid as paid.",
        )
        self.assertNotIn("Snapshot", actions)
        self.assertNotIn("user(s)", actions)

    async def test_member_screens(self):
        detail = await self._render(send_member_detail, self.db, self.anna)
        self._assert_value_style(detail)
        self.assertEqual(
            detail,
            '👤 <b><a href="tg://user?id=1001">Anna &lt;K&gt;</a></b>\n'
            f"{SUBITEM_INDENT}Telegram ID: <code>1001</code>\n"
            f"{SUBITEM_INDENT}Balance: <b>0.00 RUB</b>\n"
            f"{SUBITEM_INDENT}Payment method: ⭐ Card · RUB (default)\n"
            "\n"
            "📋 <b>Subscriptions: 1</b>\n"
            f"{SUBITEM_INDENT}Netflix · <b>400.00 RUB</b> · ⚠️ unpaid",
        )

        pending = await self._render(send_member_detail, self.db, self.pending)
        self._assert_value_style(pending)
        self.assertTrue(pending.startswith(f"👤 <b>Pending P</b>\n{SUBITEM_INDENT}⏳ Not signed in yet\n"), pending)
        self.assertIn(f"{SUBITEM_INDENT}Link works until <tg-time unix=", pending)
        self.assertIn('format="wDt">Thu, 01.01.2099 00:00 (UTC)</tg-time> (in ', pending)
        self.assertNotIn("Telegram ID", pending)
        self.assertTrue(pending.endswith("Not in any subscription yet."), pending)

        account = await self._render(send_public_account_detail, self.db, 1001)
        self._assert_value_style(account)
        self.assertEqual(
            account,
            "👤 <b>Anna &lt;K&gt;</b>\n\n"
            "💰 Balance: <b>0.00 RUB</b>\n"
            "🏦 Payment method: ⭐ Card · RUB\n"
            "📋 Subscriptions: Netflix",
        )
        self.assertNotIn("Telegram ID", account)

        members = await self._render(send_member_list, self.db)
        self.assertEqual(
            members,
            # Attention first: the three people of the overdue Netflix cycle are unpaid.
            "👥 <b>Users</b> · 4\n\n⚠️ Unpaid: <b>3</b>\n⏳ Not signed in yet: <b>1</b>\n\nTap a user to open their card.",
        )
        self.assertNotIn("Page:", members)
        for index in range(7):
            await self._friend(f"Extra {index}", 2000 + index)
        members_page = await self._render(send_member_list, self.db, page=2)
        self.assertNotIn("Page:", members_page)
        tests_page = await self._render(send_test_user_list, self.db, page=2)
        self.assertNotIn("Page:", tests_page)
        self.assertNotIn("<code>", tests_page)

    # The member card's payment block: the requisites fold under the copy button,
    # the openable link is a button only, the comment stays.
    HOW_TO_PAY = (
        "💳 <b>How to pay</b>\n"
        f"{SUBITEM_INDENT}Card · RUB\n"
        "<blockquote expandable><code>2200 1234 5678 9012</code>\n<code>Ivan I.</code></blockquote>\n"
        f"{SUBITEM_INDENT}📝 Pay &lt;early&gt;"
    )

    def _member_next_charge(self, due, distance: str, *, paid: bool = False) -> str:
        line = f"{SUBITEM_INDENT}Next charge: <b>{tg_due(due.isoformat(), TZ, 'wD')}</b> ({distance})"
        return line + (" · ✅ paid" if paid else "")

    def _member_reminder(self, time_value: str, *, default: bool = True, shown_in: str = "RUB (default)") -> str:
        clock = tg_clock(time_value, TZ, time_value)
        return (
            "⏰ <b>Reminder</b>\n"
            f"{SUBITEM_INDENT}<b>{clock}</b> your time" + (" · default" if default else "") + "\n"
            f"{SUBITEM_INDENT}Shown in: {shown_in}"
        )

    async def test_public_subscription_detail(self):
        upcoming_value = self.upcoming.isoformat()
        text = await self._render(send_public_subscription_detail, self.db, self.netflix)
        self._assert_value_style(text)
        # No viewer: no share, so the schedule block; no admin extras.
        self.assertEqual(
            text,
            "🏷️ <b>Netflix</b>\n\n"
            "📅 <b>Schedule</b>\n"
            f"{self._member_next_charge(self.upcoming, tg_due(upcoming_value, TZ, 'r', 'in 14 days'))}\n"
            f"{SUBITEM_INDENT}Repeats: every 30 days\n\n"
            f"{self._member_reminder('10:15')}\n\n"
            f"{self.HOW_TO_PAY}",
        )
        for admin_only in ("💰", "👥", "after due", "1 200.00", "Default ("):
            self.assertNotIn(admin_only, text)

    async def _member_card(self, telegram_id: int, subscription_id: int) -> str:
        message = MemberMessage.for_user(telegram_id)
        await send_public_subscription_detail(message, self.db, subscription_id)
        self.assertEqual(len(MemberMessage.sent), 1)
        text = MemberMessage.sent[0]
        self._assert_value_style(text)
        return text

    async def test_member_card_unpaid_block_and_personal_settings(self):
        overdue_value = self.overdue.isoformat()
        upcoming_value = self.upcoming.isoformat()
        # Anna paid the upcoming cycle ahead: it is shown with '✅ paid'; the overdue one leads.
        text = await self._member_card(1001, self.netflix)
        self.assertEqual(
            text,
            "🏷️ <b>Netflix</b>\n\n"
            "⚠️ <b>Unpaid</b>\n"
            f"{SUBITEM_INDENT}<b>{tg_due(overdue_value, TZ, 'wD')}</b> · overdue by 2 days\n\n"
            "💵 <b>Your share</b>\n"
            f"{SUBITEM_INDENT}Amount: <b>400.00\u00a0RUB</b>\n"
            f"{self._member_next_charge(self.upcoming, tg_due(upcoming_value, TZ, 'r', 'in 14 days'), paid=True)}\n"
            f"{SUBITEM_INDENT}Repeats: every 30 days\n\n"
            f"{self._member_reminder('10:15')}\n\n"
            f"{self.HOW_TO_PAY}",
        )

        await self.db.log_payment(self.netflix, overdue_value, 1001)
        await self.db.set_user_subscription_setting(1001, self.netflix, "reminder_time", "08:00")
        await self.db.set_user_subscription_setting(1001, self.netflix, "target_currency", "usd")
        text = await self._member_card(1001, self.netflix)
        self.assertNotIn("Unpaid", text)
        self.assertTrue(text.startswith("🏷️ <b>Netflix</b>\n\n💵 <b>Your share</b>\n"), text)
        self.assertIn(self._member_reminder("08:00", default=False, shown_in="USD"), text)

    async def test_member_card_admin_view_adds_the_admin_extras(self):
        await self.db.add_admin(1002, "Dmitry")
        upcoming_value = self.upcoming.isoformat()
        text = await self._member_card(1002, self.netflix)
        share = "<b>≈ 400.00\u00a0RUB</b>"
        self.assertEqual(
            text,
            "🏷️ <b>Netflix</b> · <b>1\u00a0200.00\u00a0RUB</b>\n\n"
            "⚠️ <b>Unpaid</b>\n"
            f"{SUBITEM_INDENT}<b>{tg_due(self.overdue.isoformat(), TZ, 'wD')}</b> · overdue by 2 days\n\n"
            "💵 <b>Your share</b>\n"
            f"{SUBITEM_INDENT}Amount: <b>400.00\u00a0RUB</b>\n"
            f"{self._member_next_charge(self.upcoming, tg_due(upcoming_value, TZ, 'r', 'in 14 days'))}\n"
            f"{SUBITEM_INDENT}Repeats: every 30 days\n\n"
            "👥 <b>Participants: 3</b>\n"
            f"{SUBITEM_INDENT}Split by shares · 3 shares × {share}\n"
            f"{SUBITEM_INDENT}• Anna &lt;K&gt; — {share}\n"
            f"{SUBITEM_INDENT}• Boris — {share}\n"
            f"{SUBITEM_INDENT}• Dmitry — {share}\n\n"
            f"{self._member_reminder('10:15')}\n"
            f"{SUBITEM_INDENT}1 day before, on the day · After due date: daily until paid\n\n"
            f"{self.HOW_TO_PAY}",
        )


if __name__ == "__main__":
    unittest.main()
