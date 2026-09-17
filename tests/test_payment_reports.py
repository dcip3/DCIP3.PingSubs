"""Check the payment report builders: live dates, ordering, header and collapsing."""

from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from app.core.reminders import tg_due
from app.storage.db import Database
from app.ui.helpers import (
    ADMIN_REPORT_COLLAPSE_MIN_GREEN,
    BLOCKQUOTE_CLOSE,
    BLOCKQUOTE_OPEN,
    MEMBER_REPORT_COLLAPSE_MIN_TOTAL,
    _build_report_schedule_line,
    _build_subscription_payment_report_text,
    _build_user_payment_blocks,
    assemble_payments_report,
    payments_report_header,
    split_text_chunks,
)

TZ = "UTC"
TODAY = date(2026, 9, 17)


class ScheduleLineTests(unittest.TestCase):
    def test_future_next_charge_uses_live_date_and_relative_entity(self):
        line = _build_report_schedule_line("2026-09-01", date(2026, 10, 1), TODAY, TZ, extra=" · 1/3 paid")
        self.assertEqual(
            line,
            f"Paid {tg_due('2026-09-01', TZ, 'wd')} → next {tg_due('2026-10-01', TZ, 'wd')} "
            f"({tg_due('2026-10-01', TZ, 'r', 'in 14 d')}) · 1/3 paid",
        )
        self.assertNotIn("<code>", line)

    def test_overdue_next_charge_keeps_server_side_overdue_text(self):
        line = _build_report_schedule_line("2026-08-15", date(2026, 9, 15), TODAY, TZ)
        self.assertTrue(line.endswith("</tg-time> (2 d overdue)"))
        self.assertNotIn('format="r"', line)

    def test_today_and_partial_lines(self):
        self.assertTrue(_build_report_schedule_line(None, TODAY, TODAY, TZ).endswith("</tg-time> (today)"))
        self.assertEqual(_build_report_schedule_line("2026-09-01", None, TODAY, TZ), f"Paid {tg_due('2026-09-01', TZ, 'wd')}")
        self.assertEqual(_build_report_schedule_line(None, None, TODAY, TZ), "")


class AssembleReportTests(unittest.TestCase):
    def test_header_wording(self):
        self.assertEqual(payments_report_header(0), "📊 Reports")
        self.assertEqual(payments_report_header(1), "📊 Reports · <b>1 needs attention</b>")
        self.assertEqual(payments_report_header(2), "📊 Reports · <b>2 need attention</b>")

    def test_red_blocks_come_first_and_all_green_never_collapses(self):
        text = assemble_payments_report(["🟢 <b>A</b>", "🔴 <b>B</b>", "🟢 <b>C</b>"])
        self.assertEqual(text, "📊 Reports · <b>1 needs attention</b>\n\n🔴 <b>B</b>\n\n🟢 <b>A</b>\n\n🟢 <b>C</b>")
        all_green = assemble_payments_report(["🟢 <b>A</b>", "🟢 <b>B</b>", "🟢 <b>C</b>"], collapse_min_green=1)
        self.assertEqual(all_green, "📊 Reports\n\n🟢 <b>A</b>\n\n🟢 <b>B</b>\n\n🟢 <b>C</b>")

    def test_admin_threshold_collapses_three_green_blocks_next_to_a_red_one(self):
        blocks = ["🔴 <b>B</b>", "🟢 <b>A</b>", "🟢 <b>C</b>", "🟢 <b>D</b>"]
        text = assemble_payments_report(blocks, collapse_min_green=ADMIN_REPORT_COLLAPSE_MIN_GREEN)
        self.assertEqual(
            text,
            "📊 Reports · <b>1 needs attention</b>\n\n🔴 <b>B</b>\n\n"
            f"{BLOCKQUOTE_OPEN}🟢 <b>A</b>\n\n🟢 <b>C</b>\n\n🟢 <b>D</b>{BLOCKQUOTE_CLOSE}",
        )
        short = assemble_payments_report(blocks[:3], collapse_min_green=ADMIN_REPORT_COLLAPSE_MIN_GREEN)
        self.assertNotIn(BLOCKQUOTE_OPEN, short)

    def test_member_threshold_collapses_only_beyond_four_subscriptions(self):
        four = ["🔴 <b>B</b>", "🟢 <b>A</b>", "🟢 <b>C</b>", "🟢 <b>D</b>"]
        self.assertNotIn(BLOCKQUOTE_OPEN, assemble_payments_report(four, collapse_min_total=MEMBER_REPORT_COLLAPSE_MIN_TOTAL))
        five = four + ["🟢 <b>E</b>"]
        self.assertIn(BLOCKQUOTE_OPEN, assemble_payments_report(five, collapse_min_total=MEMBER_REPORT_COLLAPSE_MIN_TOTAL))

    def test_long_green_tail_is_split_into_several_whole_quotes(self):
        green = [f"🟢 <b>Sub {index}</b>\n" + "x" * 1500 for index in range(6)]
        text = assemble_payments_report(["🔴 <b>Late</b>"] + green, collapse_min_green=1)
        self.assertEqual(text.count(BLOCKQUOTE_OPEN), 3)
        self.assertEqual(text.count(BLOCKQUOTE_CLOSE), 3)
        chunks = split_text_chunks(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 3900)
            self.assertEqual(chunk.count(BLOCKQUOTE_OPEN), chunk.count(BLOCKQUOTE_CLOSE))
        self.assertEqual("\n\n".join(chunks), text)


class ChunkerTests(unittest.TestCase):
    def test_split_never_cuts_inside_a_blockquote(self):
        head = "H" * 3000
        quote = f"{BLOCKQUOTE_OPEN}A\n\nB\n\n{'C' * 1000}{BLOCKQUOTE_CLOSE}"
        chunks = split_text_chunks(f"{head}\n\n{quote}")
        self.assertEqual(chunks, [head, quote])


class ReportBuilderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._directory.name) / "data" / "test.db")
        await self.db.connect()
        self.today = datetime.now(ZoneInfo(TZ)).date()

    async def asyncTearDown(self):
        await self.db.close()
        self._directory.cleanup()

    async def _friend(self, full_name: str, telegram_id: int) -> int:
        friend_id = await self.db.create_friend_invite(full_name, f"token-{telegram_id}", "2099-01-01")
        self.assertTrue(await self.db.claim_friend_invite(friend_id, telegram_id, "2026-01-01"))
        return friend_id

    async def _subscription(self, name: str, next_charge: date, friend_ids: list[int]) -> int:
        subscription_id = await self.db.create_subscription(name, 30.0, "USD", next_charge, 30, None)
        for friend_id in friend_ids:
            await self.db.set_participant(subscription_id, friend_id, True)
        previous = next_charge - timedelta(days=30)
        await self.db.ensure_cycle(subscription_id, previous)
        await self.db.close_cycle(subscription_id, previous)
        await self.db.ensure_cycle(subscription_id, next_charge)
        return subscription_id

    async def test_admin_overview_matches_target_shape(self):
        anna = await self._friend("Anna Karenina", 1001)
        dmitry = await self._friend("Dmitry Sokolov", 1002)
        boris = await self._friend("Boris Ivanov", 1003)
        overdue = self.today - timedelta(days=2)
        upcoming = self.today + timedelta(days=14)
        netflix = await self._subscription("Netflix Premium", overdue, [anna, dmitry, boris])
        spotify = await self._subscription("Spotify Family", upcoming, [anna, dmitry, boris])
        await self.db.log_payment(netflix, overdue, 1003)
        await self.db.log_payment(spotify, upcoming, 1001)

        blocks = []
        for subscription_id in (spotify, netflix):
            subscription = await self.db.get_subscription(subscription_id)
            participants = await self.db.list_subscription_participants(subscription_id)
            blocks.append(
                await _build_subscription_payment_report_text(
                    self.db, subscription, participants, scope="all", tz_name=TZ
                )
            )
        text = assemble_payments_report(blocks, collapse_min_green=ADMIN_REPORT_COLLAPSE_MIN_GREEN)

        overdue_value = overdue.isoformat()
        upcoming_value = upcoming.isoformat()
        expected = (
            "📊 Reports · <b>1 needs attention</b>\n\n"
            "🔴 <b>Netflix Premium</b>\n"
            f"Paid {tg_due((overdue - timedelta(days=30)).isoformat(), TZ, 'wd')} → "
            f"next {tg_due(overdue_value, TZ, 'wd')} (2 d overdue) · 1/3 paid\n"
            f"⚠️ <b>{tg_due(overdue_value, TZ, 'wd')}</b> (2 d overdue): Anna K., Dmitry S.\n\n"
            "🟢 <b>Spotify Family</b>\n"
            f"Paid {tg_due((upcoming - timedelta(days=30)).isoformat(), TZ, 'wd')} → "
            f"next {tg_due(upcoming_value, TZ, 'wd')} ({tg_due(upcoming_value, TZ, 'r', 'in 14 d')}) · 1/3 paid"
        )
        self.assertEqual(text, expected)
        self.assertNotIn("<code>", text)

    async def test_member_blocks_use_live_dates_and_bold_overdue(self):
        anna = await self._friend("Anna Karenina", 1001)
        overdue = self.today - timedelta(days=3)
        upcoming = self.today + timedelta(days=5)
        await self._subscription("Late one", overdue, [anna])
        paid = await self._subscription("Paid one", upcoming, [anna])
        await self.db.log_payment(paid, upcoming, 1001)

        blocks = await _build_user_payment_blocks(self.db, 1001, TZ)
        self.assertEqual(len(blocks), 2)
        late, fine = blocks
        self.assertTrue(late.startswith("🔴 <b>Late one</b>\n"))
        self.assertIn(f"⚠️ Unpaid: <b>{tg_due(overdue.isoformat(), TZ, 'wd')}</b> (3 d overdue)", late)
        self.assertTrue(fine.startswith("🟢 <b>Paid one</b>\n"))
        self.assertIn(f"({tg_due(upcoming.isoformat(), TZ, 'r', 'in 5 d')}) ✅", fine)
        self.assertNotIn("<code>", late + fine)


if __name__ == "__main__":
    unittest.main()
