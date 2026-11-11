"""Layout glyphs (app/ui/style.py), text helpers (app/ui/text.py), plural reminder wording and the double-tap guard."""

from datetime import date, datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery

from app.core.reminders import due_status_html, format_offsets_for_display
from app.ui import guards
from app.ui.style import (
    BULLET,
    NBSP,
    SEP,
    SUBITEM_INDENT,
    bullet,
    expandable,
    join_blocks,
    progress_bar,
    section,
    sub_line,
    title,
    toast_from_html,
    utf16_len,
    visible_len,
    with_banner,
)
from app.ui.text import (
    DEFAULT_WORDS,
    days_left_phrase,
    escape_html,
    format_cycles,
    format_days,
    format_display_name,
    format_money,
    format_payments,
    format_relative_past,
    format_shares,
    format_users,
    is_default_word,
    parse_amount,
    parse_currency,
    parse_date_loose,
    parse_time_loose,
    parse_timezone_loose,
    plural,
    plural_word,
    short_relative,
    utc_offset_label,
    validate_person_name,
)
from keyboard_guard import install_keyboard_guard

install_keyboard_guard()


class StyleTests(unittest.TestCase):
    def test_glyphs(self):
        self.assertEqual(NBSP, " ")
        self.assertEqual(SUBITEM_INDENT, " " * 7)
        self.assertEqual(BULLET, "•")
        self.assertEqual(SEP, " · ")
        self.assertNotIn(" ", SUBITEM_INDENT)

    def test_bullet_and_sub_line(self):
        self.assertEqual(bullet("Anna"), SUBITEM_INDENT + "• Anna")
        self.assertEqual(bullet("Anna", indent=""), "• Anna")
        self.assertEqual(sub_line("Amount: <b>1</b>"), SUBITEM_INDENT + "Amount: <b>1</b>")

    def test_title_drops_empty_facts(self):
        self.assertEqual(title("👥", "Users", 12), "👥 <b>Users</b> · 12")
        self.assertEqual(title("💰", "Pricing · Netflix"), "💰 <b>Pricing · Netflix</b>")
        self.assertEqual(title("🗂", "Open cycles · Netflix", None, "", 0, "2"), "🗂 <b>Open cycles · Netflix</b> · 2")

    def test_section_and_join_blocks(self):
        block = section("📅", "Schedule", ["Next charge: <b>12.11</b>", None, ""])
        self.assertEqual(block, ["📅 <b>Schedule</b>", SUBITEM_INDENT + "Next charge: <b>12.11</b>"])
        self.assertIsNone(section("⚠️", "Unpaid", []))
        self.assertIsNone(section("⚠️", "Unpaid", [None, ""]))
        text = join_blocks("🔔 <b>Reminders</b>", block, None, "", [], section("⚠️", "Unpaid", []))
        self.assertEqual(text, "🔔 <b>Reminders</b>\n\n📅 <b>Schedule</b>\n" + SUBITEM_INDENT + "Next charge: <b>12.11</b>")

    def test_with_banner(self):
        self.assertEqual(with_banner("✅ Saved", "Screen"), "✅ Saved\n\nScreen")
        self.assertEqual(with_banner(None, "Screen"), "Screen")
        self.assertEqual(with_banner("", "Screen"), "Screen")

    def test_toast_from_html(self):
        self.assertEqual(toast_from_html("✅ Amount updated · <b>15.99 USD</b>\n\nScreen"), "✅ Amount updated · 15.99 USD")
        self.assertEqual(toast_from_html("\n<b>Tom &amp; Jerry</b>"), "Tom & Jerry")
        self.assertEqual(len(toast_from_html("x" * 500)), 200)
        self.assertEqual(toast_from_html(None), "")

    def test_progress_bar_clamps(self):
        self.assertEqual(progress_bar(0, 3), "░" * 10)
        self.assertEqual(progress_bar(1, 3), "███" + "░" * 7)
        self.assertEqual(progress_bar(3, 3), "█" * 10)
        self.assertEqual(progress_bar(1, 100), "█" + "░" * 9)  # any progress shows a cell
        self.assertEqual(progress_bar(99, 100), "█" * 9 + "░")  # full only when all paid
        self.assertEqual(progress_bar(5, 3), "█" * 10)
        self.assertEqual(progress_bar(0, 0), "░" * 10)
        self.assertEqual(progress_bar(1, 2, width=4), "██░░")

    def test_utf16_len_counts_surrogate_pairs(self):
        self.assertEqual(utf16_len("abc"), 3)
        self.assertEqual(utf16_len("📋"), 2)
        self.assertEqual(utf16_len("·"), 1)

    def test_expandable_keeps_everything_that_fits(self):
        self.assertEqual(expandable(["a", "b"], 4096, "user"), "<blockquote expandable>a\nb</blockquote>")
        self.assertEqual(expandable([], 4096, "user"), "")

    def test_expandable_folds_the_rest_within_budget(self):
        lines = [f"• Member number {index:02d}" for index in range(30)]
        folded = expandable(lines, 300, "user")
        # The budget is what Telegram counts: visible text, tags not included.
        self.assertLessEqual(visible_len(folded), 300)
        self.assertTrue(folded.startswith("<blockquote expandable>• Member number 00\n"))
        kept = folded.count("• Member number")
        self.assertTrue(folded.endswith(f"\n…and {30 - kept} more users</blockquote>"))
        one_left = expandable(lines[:2], visible_len("• Member number 00\n…and 1 more user"), "user")
        self.assertTrue(one_left.endswith("…and 1 more user</blockquote>"))
        # Not even the first line fits: it is kept cut with '…', never dropped.
        self.assertEqual(
            expandable(["x" * 50] * 3, 60, "entry", "entries"),
            "<blockquote expandable>" + "x" * 39 + "…\n…and 2 more entries</blockquote>",
        )

    def test_expandable_measures_visible_text_and_keeps_one_long_line(self):
        # '&amp;' is one character for Telegram: a 1000-character comment full of
        # ampersands fits a 1200 budget whole.
        comment = escape_html("Family plan: R&D & Q&A. " * 41)
        self.assertEqual(expandable([comment], 1200, "line"), f"<blockquote expandable>{comment}</blockquote>")
        # One line over budget keeps its start (cut before escaping) instead of
        # turning into an empty '…and 1 more line'.
        long_comment = escape_html("R&D " * 500)
        folded = expandable([long_comment], 1200, "line")
        self.assertNotIn("more line", folded)
        self.assertTrue(folded.startswith("<blockquote expandable>R&amp;D R&amp;D"))
        self.assertTrue(folded.endswith("…</blockquote>"))
        self.assertLessEqual(visible_len(folded), 1200)
        self.assertNotIn("&am…", folded)
        # A line wrapped in one tag keeps the tag when it is cut.
        coded = expandable(["<code>" + "1" * 2000 + "</code>"], 100, "line")
        self.assertEqual(coded, "<blockquote expandable><code>" + "1" * 99 + "…</code></blockquote>")


class PluralTests(unittest.TestCase):
    def test_plural(self):
        self.assertEqual(plural(1, "day"), "1 day")
        self.assertEqual(plural(2, "day"), "2 days")
        self.assertEqual(plural(0, "user"), "0 users")
        self.assertEqual(plural(3, "entry", "entries"), "3 entries")
        self.assertEqual(plural(1.0, "share"), "1 share")
        self.assertEqual(plural(1.5, "share"), "1.5 shares")
        self.assertEqual(plural_word(1, "user"), "user")
        self.assertEqual(plural_word(7, "user"), "users")

    def test_wrappers(self):
        self.assertEqual(format_days(1), "1 day")
        self.assertEqual(format_days(5), "5 days")
        self.assertEqual(format_users(3), "3 users")
        self.assertEqual(format_shares(1), "1 share")
        self.assertEqual(format_cycles(2), "2 cycles")
        self.assertEqual(format_payments(1), "1 payment")

    def test_reminder_offsets_use_plurals(self):
        self.assertEqual(format_offsets_for_display([2, -1, 0]), "1 day before, on the day, 2 days after")
        self.assertEqual(format_offsets_for_display([-3, 1]), "3 days before, 1 day after")
        self.assertEqual(format_offsets_for_display([]), "none")

    def test_due_status_wording(self):
        today = date(2026, 9, 17)
        self.assertEqual(due_status_html("2026-09-17", "UTC", today), "Due today")
        self.assertTrue(due_status_html("2026-09-18", "UTC", today).endswith(">tomorrow</tg-time>"))
        self.assertTrue(due_status_html("2026-09-20", "UTC", today).endswith(">in 3 days</tg-time>"))
        self.assertTrue(due_status_html("2026-09-16", "UTC", today).endswith(">by 1 day</tg-time>"))
        self.assertTrue(due_status_html("2026-09-12", "UTC", today).startswith("Overdue <tg-time "))
        self.assertTrue(due_status_html("2026-09-12", "UTC", today).endswith(">by 5 days</tg-time>"))
        for value in ("2026-09-18", "2026-09-20", "2026-09-16"):
            self.assertNotIn("(s)", due_status_html(value, "UTC", today))


class MoneyTests(unittest.TestCase):
    def test_nbsp_grouping_and_currency(self):
        self.assertEqual(format_money(1200, "RUB"), "1 200.00 RUB")
        self.assertEqual(format_money(12500.5, "RUB"), "12 500.50 RUB")
        self.assertEqual(format_money(1234567.891, "USD"), "1 234 567.89 USD")
        self.assertEqual(format_money(999.999, "EUR"), "1 000.00 EUR")
        self.assertEqual(format_money(0, "EUR"), "0.00 EUR")
        self.assertEqual(format_money(15.99, "USD"), "15.99 USD")

    def test_approx_bold_negative_and_decimals(self):
        self.assertEqual(format_money(4, "USD", approx=True), "≈ 4.00 USD")
        self.assertEqual(format_money(350, "RUB", bold=True), "<b>350.00 RUB</b>")
        self.assertEqual(format_money(4, "USD", approx=True, bold=True), "≈ <b>4.00 USD</b>")
        self.assertEqual(format_money(-1200, "RUB"), "-1 200.00 RUB")
        self.assertEqual(format_money(-0.001, "RUB"), "0.00 RUB")
        self.assertEqual(format_money(1200.4, "RUB", decimals=0), "1 200 RUB")

    def test_unknown_is_not_zero(self):
        self.assertEqual(format_money(None, "USD"), "? USD")
        self.assertEqual(format_money(float("nan"), "USD"), "? USD")
        self.assertEqual(format_money(float("inf"), "USD", approx=True), "≈ ? USD")


class DaysAndTimeTests(unittest.TestCase):
    NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

    def test_days_left_phrase(self):
        self.assertEqual(days_left_phrase(0), "today")
        self.assertEqual(days_left_phrase(1), "tomorrow")
        self.assertEqual(days_left_phrase(2), "in 2 days")
        self.assertEqual(days_left_phrase(-1), "overdue by 1 day")
        self.assertEqual(days_left_phrase(-5), "overdue by 5 days")

    def test_relative_past(self):
        def ago(**delta):
            return format_relative_past(self.NOW - timedelta(**delta), self.NOW)

        self.assertEqual(ago(seconds=10), "just now")
        self.assertEqual(ago(minutes=5), "just now")
        self.assertEqual(ago(minutes=6), "6 minutes ago")
        self.assertEqual(ago(minutes=59), "59 minutes ago")
        self.assertEqual(ago(hours=1), "1 hour ago")
        self.assertEqual(ago(hours=2), "2 hours ago")
        self.assertEqual(ago(hours=25), "yesterday")
        self.assertEqual(ago(days=3), "3 days ago")
        self.assertEqual(ago(days=7), "7 days ago")
        self.assertEqual(ago(days=8), "02.11.2026")
        self.assertEqual(format_relative_past(self.NOW + timedelta(minutes=3), self.NOW), "just now")

    def test_relative_past_treats_naive_as_utc(self):
        naive = datetime(2026, 11, 10, 10, 0)
        self.assertEqual(format_relative_past(naive, self.NOW), "2 hours ago")

    def test_short_relative(self):
        self.assertEqual(short_relative(self.NOW - timedelta(seconds=20), self.NOW), "1m")
        self.assertEqual(short_relative(self.NOW - timedelta(minutes=5), self.NOW), "5m")
        self.assertEqual(short_relative(self.NOW - timedelta(hours=2, minutes=40), self.NOW), "2h")
        self.assertEqual(short_relative(self.NOW - timedelta(days=3), self.NOW), "3d")
        self.assertEqual(short_relative(self.NOW - timedelta(days=9), self.NOW), "01.11")

    def test_utc_offset_label(self):
        winter = datetime(2026, 1, 15, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 15, tzinfo=timezone.utc)
        self.assertEqual(utc_offset_label("Europe/Moscow", winter), "UTC+3")
        self.assertEqual(utc_offset_label("Asia/Kolkata", winter), "UTC+5:30")
        self.assertEqual(utc_offset_label("America/New_York", winter), "UTC-5")
        self.assertEqual(utc_offset_label("America/New_York", summer), "UTC-4")
        self.assertEqual(utc_offset_label("Europe/London", winter), "UTC")
        self.assertEqual(utc_offset_label("UTC", winter), "UTC")
        self.assertEqual(utc_offset_label("Not/AZone", winter), "UTC")
        self.assertEqual(utc_offset_label(None), "UTC")


class ParserTests(unittest.TestCase):
    def test_parse_amount(self):
        cases = {
            "149.99": 149.99,
            "149,99": 149.99,
            "1 200": 1200.0,
            "1 200": 1200.0,
            "1,200": 1200.0,
            "1,200.50": 1200.5,
            "1.200,50": 1200.5,
            "1,200,000": 1200000.0,
            "0,125": 0.125,
            " 15 ": 15.0,
            "0": 0.0,
        }
        for text, value in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_amount(text), (value, False))
        for text in ("", "abc", "1.2.3,4", "12e3", "nan", "inf", "+500", "-200", "1,2,3.4.5", "--1", "."):
            with self.subTest(text=text):
                self.assertIsNone(parse_amount(text))

    def test_parse_signed_amount(self):
        self.assertEqual(parse_amount("+500", allow_signed=True), (500.0, True))
        self.assertEqual(parse_amount("-200", allow_signed=True), (-200.0, True))
        self.assertEqual(parse_amount("−200", allow_signed=True), (-200.0, True))
        self.assertEqual(parse_amount("- 1 200,50", allow_signed=True), (-1200.5, True))
        self.assertEqual(parse_amount("300", allow_signed=True), (300.0, False))
        self.assertIsNone(parse_amount("+", allow_signed=True))

    def test_parse_currency(self):
        self.assertEqual(parse_currency("eur"), "EUR")
        self.assertEqual(parse_currency(" Rub "), "RUB")
        for text in ("EU", "EURO", "E1R", "ЕВР", "", None):
            with self.subTest(text=text):
                self.assertIsNone(parse_currency(text))

    def test_parse_time_loose(self):
        cases = {"9": "09:00", "9:5": "09:05", "09.30": "09:30", "0930": "09:30", "930": "09:30",
                 "23:59": "23:59", "0": "00:00", "18 45": "18:45", "7h15": "07:15"}
        for text, value in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_time_loose(text), value)
        for text in ("24:00", "12:60", "abc", "", "12345", "9:", "-1"):
            with self.subTest(text=text):
                self.assertIsNone(parse_time_loose(text))

    def test_parse_date_loose(self):
        today = date(2026, 11, 10)
        self.assertEqual(parse_date_loose("today", today), today)
        self.assertEqual(parse_date_loose("Tomorrow", today), date(2026, 11, 11))
        self.assertEqual(parse_date_loose("05.12.2026", today), date(2026, 12, 5))
        self.assertEqual(parse_date_loose("5.1.2027", today), date(2027, 1, 5))
        self.assertEqual(parse_date_loose("05.01.27", today), date(2027, 1, 5))
        self.assertEqual(parse_date_loose("20.11", today), date(2026, 11, 20))
        self.assertEqual(parse_date_loose("10.11", today), today)
        self.assertEqual(parse_date_loose("01.03", today), date(2027, 3, 1))  # already passed: next year
        self.assertEqual(parse_date_loose("29.02", today), date(2028, 2, 29))
        self.assertEqual(parse_date_loose("05/12/2026", today), date(2026, 12, 5))
        for text in ("31.02.2027", "32.01", "1.13", "abc", "", "2026-12-05"):
            with self.subTest(text=text):
                self.assertIsNone(parse_date_loose(text, today))

    def test_parse_timezone_loose(self):
        cases = {
            "asia/dubai": "Asia/Dubai",
            "Europe/Moscow": "Europe/Moscow",
            "america/los angeles": "America/Los_Angeles",
            "UTC": "UTC",
            "gmt": "UTC",
            "UTC+3": "Etc/GMT-3",
            "utc + 3": "Etc/GMT-3",
            "GMT-5": "Etc/GMT+5",
            "UTC−5": "Etc/GMT+5",
            "UTC+03:00": "Etc/GMT-3",
            "UTC+0": "UTC",
        }
        for text, value in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_timezone_loose(text), value)
        for text in ("Mars/Base", "UTC+15", "GMT-13", "UTC+5:30", "", "hello", "../etc"):
            with self.subTest(text=text):
                self.assertIsNone(parse_timezone_loose(text))

    def test_default_words(self):
        self.assertEqual(DEFAULT_WORDS, {"default", "-", "reset", "none", "clear"})
        for text in ("default", "Default", " - ", "−", "CLEAR", "none", "reset"):
            with self.subTest(text=text):
                self.assertTrue(is_default_word(text))
        for text in ("", None, "def", "10:00"):
            with self.subTest(text=text):
                self.assertFalse(is_default_word(text))


class NameTests(unittest.TestCase):
    def test_kept_helpers(self):
        self.assertEqual(escape_html("<b>&"), "&lt;b&gt;&amp;")
        self.assertEqual(format_display_name("Anna Petrova"), "Anna P.")
        self.assertEqual(format_display_name(None), "Unknown")

    def test_valid_names_are_normalized(self):
        self.assertEqual(validate_person_name("  Anna   Petrova "), ("Anna Petrova", None))
        self.assertEqual(validate_person_name("Jean-Luc O'Neil"), ("Jean-Luc O'Neil", None))

    def test_errors_are_one_hint_with_an_example(self):
        for raw in ("", "Anna 2", "---", "-Anna"):
            with self.subTest(raw=raw):
                name, error = validate_person_name(raw)
                self.assertIsNone(name)
                self.assertTrue(error.startswith("⚠️ "))
                self.assertTrue(error.endswith(", for example <code>Anna Petrova</code>."))
                self.assertNotIn("try again", error.lower())


def _callback() -> AsyncMock:
    callback = AsyncMock()
    callback.answer = AsyncMock()
    return callback


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_tap_is_answered_and_skipped(self):
        first, second = _callback(), _callback()
        key = ("approve", 7)
        async with guards.guarded(first, key) as acquired:
            self.assertTrue(acquired)
            self.assertTrue(guards.is_running(key))
            async with guards.guarded(second, key) as again:
                self.assertFalse(again)
            second.answer.assert_awaited_once_with(guards.ALREADY_RUNNING_TEXT, show_alert=False)
            async with guards.guarded(second, ("approve", 8)) as other:
                self.assertTrue(other)
        first.answer.assert_not_awaited()
        self.assertFalse(guards.is_running(key))

    async def test_key_is_released_on_errors(self):
        key = ("delete", "x")
        with self.assertRaises(RuntimeError):
            async with guards.guarded(_callback(), key) as acquired:
                self.assertTrue(acquired)
                raise RuntimeError("boom")
        async with guards.guarded(_callback(), key) as acquired:
            self.assertTrue(acquired)

    async def test_stale_query_answer_is_ignored(self):
        key = ("paid", 1)
        stale = _callback()
        stale.answer.side_effect = TelegramBadRequest(
            method=AnswerCallbackQuery(callback_query_id="1"), message="query is too old"
        )
        async with guards.guarded(None, key):
            async with guards.guarded(stale, key) as acquired:
                self.assertFalse(acquired)
            async with guards.guarded(None, key) as acquired:
                self.assertFalse(acquired)


if __name__ == "__main__":
    unittest.main()
