"""WP8: admin 📊 Reports, the subscription report, open cycles, cycle actions and their confirmations."""

from datetime import date, datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from aiogram.methods import EditMessageText, SendMessage

from app.core.reminders import tg_due
from app.ui.guards import ALREADY_RUNNING_TEXT, guarded
from app.ui.helpers import CYCLE_CLOSED, SUBSCRIPTION_GONE, resolve_report_timezone
from app.ui.keyboards import (
    ADMIN_REPORT_BUTTONS_MAX,
    admin_report_keyboard,
    cycle_close_confirm_keyboard,
    cycle_recreate_confirm_keyboard,
    open_cycle_button_label,
    subscription_cycle_actions_keyboard,
    subscription_open_cycles_keyboard,
    subscription_report_keyboard,
)
from app.ui.report_screens import (
    CYCLE_ACTIONS_HINT,
    CYCLE_RECREATED_BANNER,
    OPEN_CYCLES_EMPTY_LINE,
    OPEN_CYCLES_SHOWN,
    close_confirm_text,
    cycle_closed_banner,
    cycle_status,
    no_participants_line,
    open_cycles_text,
    progress_line,
    recreate_confirm_text,
)
from app.ui.states import CycleAction, SubscriptionAction
from app.ui.style import SUBITEM_INDENT as I
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import make_message
from test_keyboards_fit import LONG_NAME, NAME_64, assert_fits

install_keyboard_guard()

BIG_ID = 2**31 - 1
NBSP = " "
TODAY = date(2026, 11, 11)


def _texts(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _callbacks(markup) -> list[list[str | None]]:
    return [[button.callback_data for button in row] for row in markup.inline_keyboard]


def _styled(markup) -> list[tuple[str, str]]:
    return [(button.text, button.style) for row in markup.inline_keyboard for button in row if button.style]


def _cb(action: str, subscription_id: int) -> str:
    return SubscriptionAction(action=action, subscription_id=subscription_id).pack()


def _cycle(action: str, subscription_id: int, due_value: str) -> str:
    return CycleAction(action=action, subscription_id=subscription_id, due_date=due_value).pack()


# ------------------------------------------------------------------ keyboards


class ReportKeyboardTests(unittest.TestCase):
    def test_subscription_report_keyboard(self):
        markup = subscription_report_keyboard(7, 2)
        self.assertEqual(_texts(markup), [["📬 Remind unpaid (2)"], ["◀️ To card"]])
        self.assertEqual(_callbacks(markup), [["sub:reminders_send:7"], ["sub:open:7"]])
        self.assertEqual(_styled(markup), [("📬 Remind unpaid (2)", "primary")])
        self.assertEqual(_texts(subscription_report_keyboard(7)), [["◀️ To card"]])
        self.assertEqual(_styled(subscription_report_keyboard(7)), [])
        for count in (0, 1, 99):
            assert_fits(subscription_report_keyboard(BIG_ID, count))

    def test_open_cycles_keyboard_one_row_per_cycle_and_to_card(self):
        cycles = [
            {"due_value": "2026-10-01", "paid": 1, "total": 3},
            {"due_value": "2026-11-01", "paid": 12, "total": 12},
            {"due_value": "2025-12-31", "paid": 0, "total": 0},
        ]
        markup = subscription_open_cycles_keyboard(7, cycles, today=TODAY)
        self.assertEqual(
            _texts(markup),
            [["📅 01.10 · 1 of 3 paid"], ["📅 01.11 · all paid"], ["📅 31.12.2025 · no participants"], ["◀️ To card"]],
        )
        # cycle:open callbacks are unchanged; the way back is the card (it was 'more').
        self.assertEqual(
            _callbacks(markup),
            [["cycle:open:7:2026-10-01"], ["cycle:open:7:2026-11-01"], ["cycle:open:7:2025-12-31"], ["sub:open:7"]],
        )
        self.assertEqual(_styled(markup), [])
        worst = [{"due_value": "2025-12-31", "paid": 9999, "total": 10000} for _ in range(OPEN_CYCLES_SHOWN)]
        assert_fits(subscription_open_cycles_keyboard(BIG_ID, worst, today=TODAY))
        assert_fits(subscription_open_cycles_keyboard(BIG_ID))
        self.assertEqual(open_cycle_button_label("2026-10-01"), "📅 01.10")

    def test_cycle_actions_keyboard(self):
        markup = subscription_cycle_actions_keyboard(7, "2026-10-01")
        self.assertEqual(_texts(markup), [["♻️ Recreate", "🔒 Close cycle"], ["◀️ To list"]])
        self.assertEqual(
            _callbacks(markup),
            [["cycle:recreate:7:2026-10-01", "cycle:force_close:7:2026-10-01"], ["sub:cycles:7"]],
        )
        self.assertEqual(_styled(markup), [])
        assert_fits(subscription_cycle_actions_keyboard(BIG_ID, "2026-10-01"))

    def test_confirm_keyboards_cancel_left_red_verb_right(self):
        recreate = cycle_recreate_confirm_keyboard(BIG_ID, "2026-10-01")
        self.assertEqual(_texts(recreate), [["✖️ Cancel", "♻️ Recreate"]])
        self.assertEqual(
            _callbacks(recreate),
            [[f"cycle:open:{BIG_ID}:2026-10-01", f"cycle:recreate_yes:{BIG_ID}:2026-10-01"]],
        )
        self.assertEqual(_styled(recreate), [("♻️ Recreate", "danger")])
        close = cycle_close_confirm_keyboard(BIG_ID, "2026-10-01")
        self.assertEqual(_texts(close), [["✖️ Cancel", "🔒 Close cycle"]])
        self.assertEqual(_callbacks(close)[0][1], f"cycle:force_close_yes:{BIG_ID}:2026-10-01")
        self.assertEqual(_styled(close), [("🔒 Close cycle", "danger")])
        for markup in (recreate, close):
            assert_fits(markup)
            for row in _callbacks(markup):
                for data in row:
                    self.assertLess(len(data.encode()), 64, data)

    def test_admin_report_keyboard(self):
        subs = [{"id": BIG_ID, "name": NAME_64}, {"id": 8, "name": " Spotify  Family "}]
        markup = admin_report_keyboard(subs)
        assert_fits(markup)
        texts = _texts(markup)
        self.assertTrue(texts[0][0].startswith("🔴 Alexandra") and texts[0][0].endswith("…"), texts)
        self.assertEqual(texts[1], ["🔴 Spotify Family"])
        self.assertEqual(_callbacks(markup), [[f"sub:open_new:{BIG_ID}"], ["sub:open_new:8"]])
        many = [{"id": index, "name": LONG_NAME} for index in range(20)]
        self.assertEqual(len(admin_report_keyboard(many).inline_keyboard), ADMIN_REPORT_BUTTONS_MAX)
        assert_fits(admin_report_keyboard(many))
        self.assertIsNone(admin_report_keyboard([]))


# ---------------------------------------------------------------------- texts


class ReportTextTests(unittest.TestCase):
    def test_cycle_status_words(self):
        self.assertEqual(cycle_status(TODAY - timedelta(days=9), TODAY), "overdue by 9 days")
        self.assertEqual(cycle_status(TODAY - timedelta(days=1), TODAY), "overdue by 1 day")
        self.assertEqual(cycle_status(TODAY, TODAY), "due today")
        self.assertEqual(cycle_status(TODAY + timedelta(days=1), TODAY), "due tomorrow")
        self.assertEqual(cycle_status(TODAY + timedelta(days=14), TODAY), "due in 14 days")
        self.assertEqual(cycle_status(None, TODAY), "")

    def test_progress_line(self):
        self.assertEqual(progress_line(1, 3), "███░░░░░░░ 1 of 3 paid")
        self.assertEqual(progress_line(3, 3), "██████████ all paid")
        self.assertEqual(progress_line(0, 3), "░░░░░░░░░░ 0 of 3 paid")
        self.assertEqual(progress_line(0, 0), "No participants")

    def test_recreate_question(self):
        due = tg_due("2026-10-01", "UTC", "wD")
        self.assertEqual(
            recreate_confirm_text("2026-10-01", "UTC", 1, 3),
            f"⚠️ <b>Recreate the {due} cycle?</b>\nIts 1 payment mark is cleared; it takes the current 3 people and settings.",
        )
        self.assertEqual(
            recreate_confirm_text("2026-10-01", "UTC", 2, 1),
            f"⚠️ <b>Recreate the {due} cycle?</b>\nIts 2 payment marks are cleared; it takes the current 1 person and settings.",
        )
        self.assertEqual(
            recreate_confirm_text("2026-10-01", "UTC", 0, 3),
            f"⚠️ <b>Recreate the {due} cycle?</b>\nIt takes the current 3 people and settings.",
        )

    def test_close_question_names_who_is_marked(self):
        due = tg_due("2026-10-01", "UTC", "wD")
        head = f"⚠️ <b>Close the {due} cycle?</b>\n"
        self.assertEqual(close_confirm_text("2026-10-01", "UTC", ["Bob", "Carol"]), head + "Bob and Carol are marked as paid.")
        self.assertEqual(close_confirm_text("2026-10-01", "UTC", ["Bob <B>"]), head + "Bob &lt;B&gt; is marked as paid.")
        self.assertEqual(
            close_confirm_text("2026-10-01", "UTC", ["A", "B", "C"]), head + "A, B and C are marked as paid."
        )
        self.assertEqual(
            close_confirm_text("2026-10-01", "UTC", ["A", "B", "C", "D", "E"]),
            head + "A, B, C and 2 more are marked as paid.",
        )
        self.assertEqual(close_confirm_text("2026-10-01", "UTC", []), head + "Everyone has paid; nobody is marked.")

    def test_banners_and_no_participants_line(self):
        self.assertEqual(cycle_closed_banner(2), "✅ Cycle closed · 2 marked as paid")
        self.assertEqual(cycle_closed_banner(0), "✅ Cycle closed")
        self.assertEqual(CYCLE_RECREATED_BANNER, "✅ Cycle recreated · payment marks cleared")
        self.assertEqual(no_participants_line(["iCloud", "You<Tube>"]), "👥 No participants: iCloud, You&lt;Tube&gt;")
        long = no_participants_line([f"Sub {index}" for index in range(25)])
        self.assertTrue(long.endswith(", Sub 19 and 5 more"), long)

    def test_open_cycles_empty_and_budget(self):
        sub = {"id": 7, "name": NAME_64}
        empty = open_cycles_text(sub, [], "UTC", TODAY)
        self.assertEqual(empty, f"🗂 <b>Open cycles · {NAME_64}</b>\n\n{OPEN_CYCLES_EMPTY_LINE}")
        self.assertEqual(OPEN_CYCLES_EMPTY_LINE, "No open cycles — everything is settled.")
        cycles = [
            {
                "due_value": (TODAY - timedelta(days=400 - index)).isoformat(),
                "due": TODAY - timedelta(days=400 - index),
                "amount": 1234567.89,
                "currency": "USD",
                "paid": 9999,
                "total": 10000,
            }
            for index in range(35)
        ]
        text = open_cycles_text(sub, cycles, "Europe/Moscow", TODAY)
        self.assertTrue(text.startswith(f"🗂 <b>Open cycles · {NAME_64}</b> · 35\n\n"), text[:120])
        self.assertTrue(text.endswith("…and 15 more cycles"), text[-60:])
        self.assertEqual(text.count("📅 "), OPEN_CYCLES_SHOWN)
        self.assertLess(len(text), 3900)


# ---------------------------------------------------------------------- flows


class ReportFlowTests(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin()
        self.tz = await resolve_report_timezone(self.db)
        self.today = datetime.now(ZoneInfo(self.tz)).date()
        self.overdue = self.today - timedelta(days=9)
        self.upcoming = self.today + timedelta(days=14)
        self.sub_id = await self.db.create_subscription("Netflix", 15.99, "USD", self.overdue, 30, None)
        self.alice = await self._friend("Alice Smith", 1001)
        self.bob = await self._friend("Bob", 1002)
        self.carol = await self._friend("Carol", 1003)
        await self.db.ensure_cycle(self.sub_id, self.overdue)
        await self.db.log_payment(self.sub_id, self.overdue, 1001)
        self.screen = make_message(self.bot, 800, "screen", from_bot=True)

    async def _friend(self, name: str, telegram_id: int, subscription_id: int | None = None) -> int:
        friend_id = await self.db.create_friend_invite(name, f"token-{telegram_id}", "2099-01-01")
        await self.db.claim_friend_invite(friend_id, telegram_id, "2026-01-01")
        await self.db.set_participant(subscription_id or self.sub_id, friend_id, True)
        return friend_id

    def _edits(self) -> list[EditMessageText]:
        return self.bot.of(EditMessageText)

    def _toasts(self) -> list[str | None]:
        return [answer.text for answer in self.bot.answers()]

    def _due(self, day: date, fmt: str = "wD") -> str:
        return tg_due(day.isoformat(), self.tz, fmt)

    # --------------------------------------------------------- admin report

    async def test_admin_report_lists_red_first_buttons_and_no_participants(self):
        spotify = await self.db.create_subscription("Spotify", 10.0, "USD", self.upcoming, 30, None)
        dave = await self.db.create_friend_invite("Dave", "token-dave", "2099-01-01")
        await self.db.claim_friend_invite(dave, 1004, "2026-01-01")
        await self.db.set_participant(spotify, dave, True)
        await self.db.create_subscription("iCloud", 1.0, "USD", self.upcoming, 30, None)
        await self.db.create_subscription("YouTube", 2.0, "USD", self.upcoming, 30, None)

        await self.feed(self.message("📊 Reports"))
        sent = self.bot.of(SendMessage)
        self.assertEqual(len(sent), 1)
        overdue = self._due(self.overdue, "wd")
        self.assertEqual(
            sent[0].text,
            "📊 <b>Reports</b> · 1 needs attention\n\n"
            "🔴 <b>Netflix</b>\n"
            f"Next charge: {overdue} (overdue by 9 days) · 1 of 3 paid\n"
            f"⚠️ <b>{overdue}</b> · overdue by 9 days: Bob, Carol\n\n"
            "🟢 <b>Spotify</b>\n"
            f"Next charge: {self._due(self.upcoming, 'wd')} ({tg_due(self.upcoming.isoformat(), self.tz, 'r', 'in 14 days')})\n\n"
            "👥 No participants: iCloud, YouTube",
        )
        self.assertEqual(_texts(sent[0].reply_markup), [["🔴 Netflix"]])
        self.assertEqual(_callbacks(sent[0].reply_markup), [[_cb("open_new", self.sub_id)]])

        # The button opens the card as a NEW message with only [✖️ Close]; the report is left alone.
        report = make_message(self.bot, 801, sent[0].text, from_bot=True)
        await self.feed(self.callback(_cb("open_new", self.sub_id), report))
        self.assertFalse(self._edits())
        card = self.bot.of(SendMessage)[-1]
        self.assertTrue(card.text.startswith("🏷️ <b>Netflix</b>"), card.text[:60])
        self.assertEqual(_texts(card.reply_markup)[-1], ["✖️ Close"])

        # The old reply label still works.
        await self.feed(self.message("📊 Payments report"))
        self.assertTrue(self.bot.of(SendMessage)[-1].text.startswith("📊 <b>Reports</b> · 1 needs attention"))

    async def test_admin_report_empty_and_all_without_participants(self):
        await self.db.delete_subscription(self.sub_id)
        await self.feed(self.message("📊 Reports"))
        empty = self.bot.of(SendMessage)[-1]
        # Never a dead end: a hint naming the section and the button that creates one.
        self.assertEqual(empty.text, "📊 <b>Reports</b>\n\nNo subscriptions yet. Start in “📋 Subscriptions”.")
        self.assertEqual(_texts(empty.reply_markup), [["➕ New subscription"]])
        self.assertEqual(empty.reply_markup.inline_keyboard[0][0].callback_data, "sub:create:0")

        await self.db.create_subscription("iCloud", 1.0, "USD", self.upcoming, 30, None)
        await self.feed(self.message("📊 Reports"))
        self.assertEqual(self.bot.of(SendMessage)[-1].text, "📊 <b>Reports</b>\n\n👥 No participants: iCloud")

    # -------------------------------------------------- subscription report

    async def test_subscription_report_offers_remind_unpaid_and_goes_to_the_card(self):
        await self.feed(self.callback(_cb("report", self.sub_id), self.screen))
        edit = self._edits()[-1]
        overdue = self._due(self.overdue, "wd")
        self.assertEqual(
            edit.text,
            "📊 <b>Reports · Netflix</b>\n\n"
            "🔴 <b>Netflix</b>\n"
            f"Next charge: {overdue} (overdue by 9 days) · 1 of 3 paid\n"
            f"⚠️ <b>{overdue}</b> · overdue by 9 days: Bob, Carol",
        )
        self.assertEqual(_texts(edit.reply_markup), [["📬 Remind unpaid (2)"], ["◀️ To card"]])
        self.assertEqual(_callbacks(edit.reply_markup), [[_cb("reminders_send", self.sub_id)], [_cb("open", self.sub_id)]])

        await self.db.log_payment(self.sub_id, self.overdue, 1002)
        await self.db.log_payment(self.sub_id, self.overdue, 1003)
        await self.feed(self.callback(_cb("report", self.sub_id), self.screen))
        self.assertEqual(_texts(self._edits()[-1].reply_markup), [["◀️ To card"]])

    async def test_subscription_report_empty_and_deleted(self):
        empty = await self.db.create_subscription("iCloud", 1.0, "USD", self.upcoming, 30, None)
        await self.feed(self.callback(_cb("report", empty), self.screen))
        self.assertEqual(self._edits()[-1].text, "📊 <b>Reports · iCloud</b>\n\nNo participants yet.")
        self.assertEqual(_texts(self._edits()[-1].reply_markup), [["👥 Add participants"], ["◀️ To card"]])

        await self.db.delete_subscription(empty)
        await self.feed(self.callback(_cb("report", empty), self.screen))
        self.assertTrue(self._edits()[-1].text.startswith("📋 <b>Subscriptions</b>"))
        self.assertEqual(self._toasts()[-1], SUBSCRIPTION_GONE)

    async def test_legacy_more_redraws_the_card(self):
        await self.feed(self.callback(_cb("more", self.sub_id), self.screen))
        edit = self._edits()[-1]
        self.assertTrue(edit.text.startswith("🏷️ <b>Netflix</b>"), edit.text[:60])
        self.assertIn(["📊 Reports", "🗂 Open cycles"], _texts(edit.reply_markup))

    # ------------------------------------------------------------ open cycles

    async def test_open_cycles_and_cycle_screen(self):
        await self.feed(self.callback(_cb("cycles", self.sub_id), self.screen))
        edit = self._edits()[-1]
        self.assertEqual(
            edit.text,
            "🗂 <b>Open cycles · Netflix</b> · 1\n\n"
            f"📅 <b>{self._due(self.overdue)}</b> · overdue by 9 days\n"
            f"{I}Amount: <b>15.99{NBSP}USD</b>\n"
            f"{I}███░░░░░░░ 1 of 3 paid",
        )
        self.assertEqual(
            _texts(edit.reply_markup), [[f"📅 {self.overdue:%d.%m} · 1 of 3 paid"], ["◀️ To card"]]
        )

        await self.feed(self.callback(_cycle("open", self.sub_id, self.overdue.isoformat()), self.screen))
        edit = self._edits()[-1]
        self.assertEqual(
            edit.text,
            f"🗂 <b>Cycle {self._due(self.overdue)} · Netflix</b>\n\n"
            "Status: overdue by 9 days\n"
            f"Amount: <b>15.99{NBSP}USD</b>\n"
            "███░░░░░░░ 1 of 3 paid\n"
            "✅ Paid: Alice Smith\n"
            "⚠️ Unpaid: Bob, Carol\n\n"
            f"{CYCLE_ACTIONS_HINT}",
        )
        self.assertEqual(_texts(edit.reply_markup), [["♻️ Recreate", "🔒 Close cycle"], ["◀️ To list"]])

    async def test_cycle_screen_folds_more_than_five_names(self):
        for index in range(4, 10):
            await self._friend(f"Person {index}", 1000 + index)
        await self.feed(self.callback(_cycle("open", self.sub_id, self.overdue.isoformat()), self.screen))
        text = self._edits()[-1].text
        self.assertIn("⚠️ Unpaid: <b>8</b>\n<blockquote expandable>• Bob\n• Carol\n• Person 4", text)
        self.assertIn("✅ Paid: Alice Smith\n", text)

    async def test_open_cycles_empty_state(self):
        await self.db.close_cycle(self.sub_id, self.overdue)
        await self.feed(self.callback(_cb("cycles", self.sub_id), self.screen))
        edit = self._edits()[-1]
        self.assertEqual(edit.text, f"🗂 <b>Open cycles · Netflix</b>\n\n{OPEN_CYCLES_EMPTY_LINE}")
        self.assertEqual(_texts(edit.reply_markup), [["◀️ To card"]])

    async def test_closed_cycle_tap_redraws_the_open_cycles(self):
        due_value = self.overdue.isoformat()
        await self.db.close_cycle(self.sub_id, self.overdue)
        for action in ("open", "recreate", "force_close", "recreate_yes", "force_close_yes"):
            await self.feed(self.callback(_cycle(action, self.sub_id, due_value), self.screen))
            self.assertEqual(self._toasts()[-1], CYCLE_CLOSED, action)
            self.assertTrue(self._edits()[-1].text.startswith("🗂 <b>Open cycles · Netflix</b>"), action)
        self.assertEqual(len(self.bot.answers()), 5)  # each tap answered exactly once

    # ---------------------------------------------------------- recreate

    async def test_recreate_asks_first_then_clears_payment_marks(self):
        due_value = self.overdue.isoformat()
        await self.feed(self.callback(_cycle("recreate", self.sub_id, due_value), self.screen))
        question = self._edits()[-1]
        self.assertEqual(
            question.text,
            f"⚠️ <b>Recreate the {self._due(self.overdue)} cycle?</b>\n"
            "Its 1 payment mark is cleared; it takes the current 3 people and settings.",
        )
        self.assertEqual(_texts(question.reply_markup), [["✖️ Cancel", "♻️ Recreate"]])
        # Nothing happened yet.
        self.assertEqual(len(await self.db.list_payments_for_cycles(self.sub_id, [due_value])), 1)

        await self.feed(self.callback(_cycle("recreate_yes", self.sub_id, due_value), self.screen))
        self.assertEqual(await self.db.list_payments_for_cycles(self.sub_id, [due_value]), [])
        self.assertIn(due_value, await self.db.list_open_cycles(self.sub_id))
        edit = self._edits()[-1]
        self.assertTrue(
            edit.text.startswith(f"{CYCLE_RECREATED_BANNER}\n\n🗂 <b>Cycle {self._due(self.overdue)} · Netflix</b>\n"),
            edit.text,
        )
        self.assertIn("░░░░░░░░░░ 0 of 3 paid", edit.text)
        self.assertEqual(self._toasts()[-1], "✅ Cycle recreated · payment marks cleared")

    async def test_cancel_returns_to_the_cycle(self):
        due_value = self.overdue.isoformat()
        await self.feed(self.callback(_cycle("force_close", self.sub_id, due_value), self.screen))
        cancel = _callbacks(self._edits()[-1].reply_markup)[0][0]
        self.assertEqual(cancel, _cycle("open", self.sub_id, due_value))
        await self.feed(self.callback(cancel, self.screen))
        self.assertTrue(self._edits()[-1].text.startswith(f"🗂 <b>Cycle {self._due(self.overdue)} · Netflix</b>"))
        self.assertIn(due_value, await self.db.list_open_cycles(self.sub_id))

    # ------------------------------------------------------------- close

    async def test_close_asks_first_then_marks_unpaid_and_lands_on_open_cycles(self):
        due_value = self.overdue.isoformat()
        await self.feed(self.callback(_cycle("force_close", self.sub_id, due_value), self.screen))
        question = self._edits()[-1]
        self.assertEqual(
            question.text,
            f"⚠️ <b>Close the {self._due(self.overdue)} cycle?</b>\nBob and Carol are marked as paid.",
        )
        self.assertEqual(_texts(question.reply_markup), [["✖️ Cancel", "🔒 Close cycle"]])
        self.assertIn(due_value, await self.db.list_open_cycles(self.sub_id))

        await self.feed(self.callback(_cycle("force_close_yes", self.sub_id, due_value), self.screen))
        self.assertNotIn(due_value, await self.db.list_open_cycles(self.sub_id))
        payers = {row["paid_by_telegram_id"] for row in await self.db.list_payments_for_cycles(self.sub_id, [due_value])}
        self.assertEqual(payers, {1001, 1002, 1003})
        edit = self._edits()[-1]
        self.assertEqual(
            edit.text,
            f"✅ Cycle closed · 2 marked as paid\n\n🗂 <b>Open cycles · Netflix</b>\n\n{OPEN_CYCLES_EMPTY_LINE}",
        )
        self.assertEqual(self._toasts()[-1], "✅ Cycle closed · 2 marked as paid")
        subscription = await self.db.get_subscription(self.sub_id)
        self.assertEqual(str(subscription["next_charge_at"]), (self.overdue + timedelta(days=30)).isoformat())

    async def test_cycle_actions_are_guarded(self):
        due_value = self.overdue.isoformat()
        async with guarded(None, ("cycle", (self.sub_id, due_value))):
            await self.feed(self.callback(_cycle("force_close_yes", self.sub_id, due_value), self.screen))
            await self.feed(self.callback(_cycle("recreate_yes", self.sub_id, due_value), self.screen))
        self.assertEqual(self._toasts(), [ALREADY_RUNNING_TEXT, ALREADY_RUNNING_TEXT])
        self.assertIn(due_value, await self.db.list_open_cycles(self.sub_id))
        self.assertEqual(len(await self.db.list_payments_for_cycles(self.sub_id, [due_value])), 1)

    async def test_deleted_subscription_redraws_the_list(self):
        due_value = self.overdue.isoformat()
        await self.db.delete_subscription(self.sub_id)
        for data in (
            _cb("cycles", self.sub_id),
            _cycle("open", self.sub_id, due_value),
            _cycle("recreate", self.sub_id, due_value),
            _cycle("force_close_yes", self.sub_id, due_value),
        ):
            await self.feed(self.callback(data, self.screen))
            self.assertEqual(self._toasts()[-1], SUBSCRIPTION_GONE, data)
            self.assertTrue(self._edits()[-1].text.startswith("📋 <b>Subscriptions</b>"), data)


if __name__ == "__main__":
    unittest.main()
