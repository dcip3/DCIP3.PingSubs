"""Check the Telegram date-time entity helpers used for localized dates."""

from datetime import date, datetime, timezone
import unittest
from zoneinfo import ZoneInfo

from app.core.reminders import due_status_html, tg_due, tg_time


class TgTimeTests(unittest.TestCase):
    def test_tg_time_escapes_fallback_and_keeps_unix_seconds(self):
        moment = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
        rendered = tg_time(moment, "wD", "Sat <20.09>")
        self.assertEqual(rendered, '<tg-time unix="1789894800" format="wD">Sat &lt;20.09&gt;</tg-time>')

    def test_tg_time_treats_naive_datetimes_as_utc(self):
        naive = datetime(2026, 9, 20, 9, 0)
        aware = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(tg_time(naive, "t", "x"), tg_time(aware, "t", "x"))

    def test_tg_due_anchors_noon_in_subscription_timezone(self):
        rendered = tg_due("2026-09-20", "Europe/Moscow")
        noon = datetime(2026, 9, 20, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        self.assertEqual(rendered, f'<tg-time unix="{int(noon.timestamp())}" format="wD">20.09.2026</tg-time>')

    def test_tg_due_falls_back_to_plain_text_for_bad_dates(self):
        self.assertEqual(tg_due("not-a-date", "Europe/Moscow"), "not-a-date")

    def test_due_status_keeps_the_status_word_outside_the_entity(self):
        today = date(2026, 9, 17)
        future = due_status_html("2026-09-20", "UTC", today)
        self.assertTrue(future.startswith("Due <tg-time "))
        self.assertIn('format="r">in 3 day(s)</tg-time>', future)
        past = due_status_html("2026-09-15", "UTC", today)
        self.assertTrue(past.startswith("Overdue <tg-time "))
        self.assertIn('format="r">by 2 day(s)</tg-time>', past)
        self.assertEqual(due_status_html("2026-09-17", "UTC", today), "Due today")
        self.assertEqual(due_status_html("garbage", "UTC", today), "Unknown date")


if __name__ == "__main__":
    unittest.main()
