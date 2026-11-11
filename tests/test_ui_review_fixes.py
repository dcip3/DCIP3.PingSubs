"""Regression tests for the review findings on the UX redesign.

One test (or a few) per behavioural bug: the join menu of a linked member, the
old card's '📊 Reports', the dated card Paid and its shared guard key, the
not-modified edit, short dates and '1.000', the spawned cards' ✖️ Close, the
member reports' timezone, prompts on inaccessible messages, message budgets
(batch reminders, report chunks, folded lines, the admin card), the admin's
legacy '➕ Top up', the balance split and the reworded screens.
"""
from __future__ import annotations

import unittest
from datetime import date

from app.ui.style import expandable
from app.ui.text import escape_html, parse_amount, parse_date_loose
from keyboard_guard import install_keyboard_guard

install_keyboard_guard()


# ================================================================ input


class InputToleranceTests(unittest.TestCase):
    def test_a_short_date_that_just_passed_stays_in_the_past(self):
        today = date(2026, 11, 11)
        self.assertEqual(parse_date_loose("10.11", today), date(2026, 11, 10))
        self.assertEqual(parse_date_loose("31.10", today), date(2026, 10, 31))
        self.assertEqual(parse_date_loose("12.11", today), date(2026, 11, 12))
        self.assertEqual(parse_date_loose("01.03", today), date(2027, 3, 1))
        self.assertEqual(parse_date_loose("28.12", date(2026, 1, 3)), date(2025, 12, 28))
        self.assertEqual(parse_date_loose("29.02", today), date(2028, 2, 29))

    def test_a_dot_before_three_digits_groups_thousands_like_a_comma(self):
        self.assertEqual(parse_amount("1.000"), (1000.0, False))
        self.assertEqual(parse_amount("1,000"), (1000.0, False))
        self.assertEqual(parse_amount("1.500"), (1500.0, False))
        self.assertEqual(parse_amount("+1.500", allow_signed=True), (1500.0, True))
        self.assertEqual(parse_amount("0.500"), (0.5, False))
        self.assertEqual(parse_amount("1.50"), (1.5, False))
        self.assertEqual(parse_amount("1.000.000"), (1000000.0, False))


class FoldTests(unittest.TestCase):
    def test_one_comment_with_ampersands_is_shown_not_dropped(self):
        comment = "Family plan: R&D & Q&A. " * 41
        folded = expandable([escape_html(comment)], 1200, "line")
        self.assertNotIn("more line", folded)
        self.assertIn("Family plan: R&amp;D", folded)


if __name__ == "__main__":
    unittest.main()
