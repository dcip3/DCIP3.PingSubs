"""Label width estimates, row budgets, fit_label and the way-back rule (app/ui/keyboard_fit.py)."""

import unittest

from aiogram.types import (
    DisabledButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.ui.keyboard_fit import (
    INLINE_ROW_BUDGET,
    REPLY_ROW_BUDGET,
    fit_label,
    is_back_button,
    keyboard_overflows,
    keyboard_problems,
    label_units,
    misplaced_back_buttons,
    row_budget,
)
from keyboard_guard import install_keyboard_guard, misplaced_back_ok

install_keyboard_guard()


def _button(text: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data="x")


def _row(*texts: str) -> list[InlineKeyboardButton]:
    return [_button(text) for text in texts]


def _inline(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def _reply(*rows: tuple[str, ...]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=text) for text in row] for row in rows])


class LabelUnitsTests(unittest.TestCase):
    def test_latin_character_table(self):
        self.assertEqual(label_units("a"), 0.85)
        self.assertEqual(label_units("A"), 1.15)
        self.assertEqual(label_units("m"), 1.4)
        self.assertEqual(label_units("W"), 1.4)
        self.assertEqual(label_units("i"), 0.5)
        self.assertEqual(label_units("I"), 0.5)
        self.assertEqual(label_units(" "), 0.5)
        self.assertEqual(label_units("·"), 0.5)
        self.assertEqual(label_units("5"), 0.9)
        self.assertEqual(label_units("1"), 0.5)
        self.assertEqual(label_units("@%"), 2.8)
        self.assertEqual(label_units("\u00a0"), 0.5)  # NBSP from money amounts is a space

    def test_emoji_count_two_and_selectors_count_nothing(self):
        self.assertEqual(label_units("📋"), 2.0)
        self.assertEqual(label_units("◀️"), 2.0)
        self.assertEqual(label_units("⬅️"), 2.0)
        self.assertEqual(label_units("✖️"), 2.0)
        self.assertEqual(label_units("⏰"), 2.0)
        self.assertEqual(label_units("‍️"), 0.0)
        self.assertEqual(label_units("🇬🇧"), 2.0)

    def test_budgets(self):
        self.assertEqual(INLINE_ROW_BUDGET, {1: 27.0, 2: 12.5, 3: 7.5, 4: 5.0})
        self.assertEqual(REPLY_ROW_BUDGET, {1: 30.0, 2: 14.0, 3: 8.5, 4: 6.0})
        self.assertEqual(row_budget(2), 12.5)
        self.assertEqual(row_budget(2, reply=True), 14.0)
        self.assertEqual(row_budget(5), 4.0)
        self.assertEqual(row_budget(8, reply=True), 3.0)


class FitTests(unittest.TestCase):
    def test_reply_menu_labels_fit_two_per_row(self):
        for label in ("📋 Subscriptions", "💳 Payment", "👥 Users", "📊 Reports", "⚙️ Settings", "👤 Account", "❓ Help"):
            with self.subTest(label=label):
                self.assertLessEqual(label_units(label), row_budget(2, reply=True))
        self.assertEqual(keyboard_overflows(_reply(("📋 Subscriptions", "💳 Payment"))), [])

    def test_timezone_labels(self):
        self.assertLessEqual(label_units("✅ Los Angeles"), row_budget(2))
        self.assertGreater(label_units("✅ America/Los_Angeles"), row_budget(2))
        overflows = keyboard_overflows(_inline(_row("✅ America/Los_Angeles", "Europe/Moscow")))
        self.assertEqual([item.text for item in overflows], ["✅ America/Los_Angeles"])
        self.assertIn("needs", str(overflows[0]))

    def test_payment_method_does_not_fit_half_a_row(self):
        self.assertGreater(label_units("💳 Payment method"), row_budget(2))
        self.assertLessEqual(label_units("💳 Payment method"), row_budget(1))
        self.assertLessEqual(label_units("🏦 Method"), row_budget(2))

    def test_none_markup_has_no_problems(self):
        self.assertEqual(keyboard_overflows(None), [])
        self.assertEqual(misplaced_back_buttons(None), [])
        self.assertEqual(keyboard_problems(None), [])


class FitLabelTests(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(fit_label("Netflix", 2), "Netflix")
        self.assertEqual(fit_label("Netflix", 1, tail=" · 12.11"), "Netflix · 12.11")

    def test_long_text_is_cut_with_an_ellipsis_within_budget(self):
        for buttons, reply in ((1, False), (2, False), (3, False), (4, False), (1, True), (2, True)):
            for text in ("W" * 64, "Alexandra Konstantinopolskaya-Wellington", "i" * 100, "📋" * 30):
                with self.subTest(text=text, buttons=buttons, reply=reply):
                    label = fit_label(text, buttons, reply=reply)
                    self.assertTrue(label.endswith("…"))
                    self.assertLessEqual(label_units(label), row_budget(buttons, reply=reply))
                    self.assertTrue(text.startswith(label[:-1]))

    def test_cut_never_ends_in_a_space_before_the_ellipsis(self):
        label = fit_label("Spotify Family Premium Duo Plus", 2)
        self.assertTrue(label.endswith("…"))
        self.assertFalse(label[:-1].endswith(" "))

    def test_tail_is_kept_whole_and_the_name_is_cut(self):
        label = fit_label("Alexandra Konstantinopolskaya", 1, tail=" · 12\u00a0500 RUB")
        self.assertTrue(label.endswith("… · 12\u00a0500 RUB"))
        self.assertTrue(label.startswith("Alexandra Konstan"))
        self.assertLessEqual(label_units(label), row_budget(1))


class WayBackTests(unittest.TestCase):
    def test_ways_back(self):
        for text in (
            "◀️ Back", "◀️ To card", "◀️ To list", "◀️ To users", "◀️ To reminders", "◀️ To payment",
            "◀️ To pending", "◀️", "⬅️ Back", "✖️ Close", "✖️ Cancel", "❌ Cancel", "Cancel", "Keep",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_back_button(text))

    def test_actions_are_not_ways_back(self):
        for text in (
            "❌ Reject", "✅ Approve", "✅ Paid", "🗑 Delete", "🔄 Refresh", "▶️", "Next ▶️", "Close",
            "✅ Close", "Keep payments", "🔒 Close cycle", "1 / 3",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_back_button(text))

    @misplaced_back_ok
    def test_misplaced_back_buttons_flags_a_way_back_right_of_an_action(self):
        markup = _inline(
            _row("✏️ Rename", "🗑 Delete"),
            _row("🗑 Delete", "✖️ Cancel"),
            _row("🔄 Refresh", "◀️ To list"),
            _row("❌ Reject", "Keep"),
            _row("🗑 Yes, delete", "⬅️ Back"),
        )
        found = misplaced_back_buttons(markup)
        self.assertEqual(
            [(item.row, item.column, item.text) for item in found],
            [(1, 1, "✖️ Cancel"), (2, 1, "◀️ To list"), (3, 1, "Keep"), (4, 1, "⬅️ Back")],
        )
        self.assertIn("the way back goes first", str(found[0]))
        self.assertEqual(keyboard_problems(markup), [str(item) for item in found])

    def test_way_back_on_the_left_passes(self):
        markup = _inline(
            _row("✖️ Cancel", "🗑 Delete"),
            _row("Keep", "❌ Reject"),
            _row("◀️ To list", "🔄 Refresh"),
            _row("⬅️ Back", "✖️ Close"),
            _row("◀️", "▶️"),
            _row("✖️ Close"),
        )
        self.assertEqual(misplaced_back_buttons(markup), [])
        self.assertEqual(keyboard_problems(markup), [])

    def test_disabled_counter_button_is_ignored(self):
        counter = InlineKeyboardButton(text="1 / 3", disabled=DisabledButton())
        disabled_previous = InlineKeyboardButton(text="◀️", disabled=DisabledButton())
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [disabled_previous, counter, _button("▶️")],
                [_button("◀️"), counter, _button("▶️")],
                [counter, _button("◀️ Back")],
            ]
        )
        self.assertEqual(misplaced_back_buttons(markup), [])

    def test_reply_keyboards_are_checked_too(self):
        markup = _reply(("👥 Users", "✖️ Cancel"))
        self.assertEqual([item.text for item in misplaced_back_buttons(markup)], ["✖️ Cancel"])


if __name__ == "__main__":
    unittest.main()
