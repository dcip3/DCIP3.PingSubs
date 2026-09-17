"""Check the Telegram date-time entity helpers used for localized dates."""

from datetime import date, datetime, timezone
import unittest
from zoneinfo import ZoneInfo

from app.core.reminders import due_status_html, tg_clock, tg_due, tg_time


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


class TgClockTests(unittest.TestCase):
    # 2026-09-17 10:00 Moscow (UTC+3) == 07:00 UTC.
    NOW = datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc)

    def test_tg_clock_uses_today_when_the_time_is_still_ahead(self):
        rendered = tg_clock("16:00", "Europe/Moscow", now=self.NOW)
        expected = datetime(2026, 9, 17, 16, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        self.assertEqual(
            rendered,
            f'<tg-time unix="{int(expected.timestamp())}" format="t">16:00 (Europe/Moscow)</tg-time>',
        )

    def test_tg_clock_rolls_to_tomorrow_when_the_time_has_passed(self):
        rendered = tg_clock("09:30", "Europe/Moscow", now=self.NOW)
        expected = datetime(2026, 9, 18, 9, 30, tzinfo=ZoneInfo("Europe/Moscow"))
        self.assertIn(f'unix="{int(expected.timestamp())}"', rendered)
        # The exact current minute has already started, so it also rolls over.
        same_minute = tg_clock("10:00", "Europe/Moscow", now=self.NOW)
        next_day = datetime(2026, 9, 18, 10, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        self.assertIn(f'unix="{int(next_day.timestamp())}"', same_minute)

    def test_tg_clock_treats_naive_now_as_utc_and_accepts_custom_fallback(self):
        naive = datetime(2026, 9, 17, 7, 0)
        self.assertEqual(
            tg_clock("16:00", "Europe/Moscow", now=naive),
            tg_clock("16:00", "Europe/Moscow", now=self.NOW),
        )
        rendered = tg_clock("16:00", "Europe/Moscow", "4 pm <MSK>", now=self.NOW)
        self.assertTrue(rendered.endswith('format="t">4 pm &lt;MSK&gt;</tg-time>'))

    def test_tg_clock_falls_back_to_defaults_for_bad_input(self):
        rendered = tg_clock("nonsense", "Not/AZone", now=self.NOW)
        self.assertTrue(rendered.endswith(">16:00 (Europe/Moscow)</tg-time>"))


if __name__ == "__main__":
    unittest.main()
