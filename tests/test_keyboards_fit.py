"""Every keyboard the bot sends fits a 390 pt phone and keeps the layout rules.

``assert_fits`` is the one check: no label cut on a phone, the way back
leftmost, no '…' in fixed labels (only in labels cut from user data by
``fit_label``), and at most one coloured call-to-action button. Each work
package appends its builders here with the worst-case fixtures below.

The second half checks the global way-back guard (tests/keyboard_guard.py):
every test module installs it, and it really fails a test whose keyboard has
a way back on the right.
"""

import ast
from pathlib import Path
from typing import Iterable
import unittest

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.ui.keyboard_fit import ELLIPSIS, fit_label, keyboard_problems
from app.ui.keyboards import COMMON_TIMEZONES
import keyboard_guard
from keyboard_guard import install_keyboard_guard, misplaced_back_ok

install_keyboard_guard()

TESTS_DIR = Path(__file__).resolve().parent

# ------------------------------------------------------------ worst-case data

LONG_NAME = "W" * 40
NAME_64 = "Alexandra Konstantinopolskaya-Wellington of Westminster Mews WWW"[:64]
LONG_SUB = "Spotify Family Premium Duo Plus"
LONG_METHOD = "Main corporate RUB card T-Bank"
ALL_TIMEZONES = COMMON_TIMEZONES
LONG_REQUISITES = ("Card 2200 1234 5678 9012, T-Bank, Alexandra Konstantinopolskaya. " * 5)[:300]
USER_TEXTS = (LONG_NAME, NAME_64, LONG_SUB, LONG_METHOD, LONG_REQUISITES, *ALL_TIMEZONES)

STYLED_CTA = ("primary", "success")


def _rows(markup: InlineKeyboardMarkup | ReplyKeyboardMarkup) -> list[list]:
    if isinstance(markup, ReplyKeyboardMarkup):
        return markup.keyboard
    return markup.inline_keyboard


def _cut_from_user_text(label: str, user_texts: Iterable[str]) -> bool:
    """True when the '…' in ``label`` marks where a user-data string was cut."""
    for text in user_texts:
        for end in range(len(text) - 1, 0, -1):
            head = text[:end].rstrip()
            if head and head + ELLIPSIS in label:
                return True
    return False


def keyboard_rule_violations(
    markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None,
    user_texts: Iterable[str] = (),
) -> list[str]:
    """What ``assert_fits`` complains about, as readable lines (empty when fine)."""
    if markup is None:
        return []
    user_texts = tuple(user_texts)
    problems = list(keyboard_problems(markup))
    styled = []
    for index, row in enumerate(_rows(markup)):
        for button in row:
            if ELLIPSIS in button.text and not _cut_from_user_text(button.text, user_texts):
                problems.append(f"row {index}: fixed label {button.text!r} contains '…'")
            if getattr(button, "style", None) in STYLED_CTA:
                styled.append(button.text)
    if len(styled) > 1:
        problems.append(f"more than one coloured button: {styled}")
    return problems


def assert_fits(
    markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None,
    user_texts: Iterable[str] = USER_TEXTS,
) -> None:
    problems = keyboard_rule_violations(markup, user_texts)
    if problems:
        layout = [[button.text for button in row] for row in _rows(markup)]
        raise AssertionError(f"{layout}:\n" + "\n".join(problems))


def _inline(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def _button(text: str, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data="x", style=style)


# ------------------------------------------------------------------ the check


class AssertFitsTests(unittest.TestCase):
    def test_clean_keyboard_passes(self):
        assert_fits(_inline([_button("✖️ Cancel"), _button("🗑 Delete", "danger")], [_button("➕ Top up", "primary")]))
        assert_fits(None)

    def test_fixed_label_with_ellipsis_fails(self):
        problems = keyboard_rule_violations(_inline([_button("✏️ Other time…")]))
        self.assertEqual(problems, ["row 0: fixed label '✏️ Other time…' contains '…'"])

    def test_label_cut_from_user_data_passes(self):
        label = fit_label(NAME_64, 1, tail=" · 12 500 RUB")
        self.assertIn(ELLIPSIS, label)
        assert_fits(_inline([_button(label)]))
        cut_timezone = fit_label("✅ America/Los_Angeles", 2)
        assert_fits(_inline([_button(cut_timezone), _button("UTC")]))

    def test_overflow_fails(self):
        problems = keyboard_rule_violations(_inline([_button("💳 Payment method"), _button("💰 Pricing")]))
        self.assertEqual(len(problems), 1)
        self.assertIn("'💳 Payment method' needs", problems[0])

    @misplaced_back_ok
    def test_way_back_on_the_right_fails(self):
        problems = keyboard_rule_violations(_inline([_button("🗑 Delete", "danger"), _button("✖️ Cancel")]))
        self.assertEqual(len(problems), 1)
        self.assertIn("the way back goes first", problems[0])

    def test_two_coloured_buttons_fail(self):
        markup = _inline([_button("✅ Paid", "success")], [_button("➕ Top up", "primary")])
        self.assertEqual(keyboard_rule_violations(markup), ["more than one coloured button: ['✅ Paid', '➕ Top up']"])
        # The danger verb of a confirmation does not count as a call to action.
        assert_fits(_inline([_button("✖️ Cancel"), _button("🗑 Delete", "danger")], [_button("✅ Paid", "success")]))


# ------------------------------------------------------------ the global guard


def _calls_install_at_module_level(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "install_keyboard_guard":
                return True
    return False


def _run(case: unittest.TestCase) -> unittest.TestResult:
    result = unittest.TestResult()
    case.run(result)
    return result


class KeyboardGuardTests(unittest.TestCase):
    def test_every_test_module_installs_the_guard(self):
        modules = sorted(TESTS_DIR.glob("test_*.py"))
        self.assertIn(Path(__file__).resolve(), [path.resolve() for path in modules])
        missing = [path.name for path in modules if not _calls_install_at_module_level(path)]
        self.assertEqual(missing, [], "call install_keyboard_guard() at module level in these test modules")

    def test_guard_is_installed_in_this_process(self):
        self.assertTrue(keyboard_guard.is_installed())
        before = unittest.TestCase.run
        install_keyboard_guard()  # idempotent
        self.assertIs(unittest.TestCase.run, before)

    def test_way_back_on_the_right_fails_the_test(self):
        class Bad(unittest.TestCase):
            def test_bad(self):
                _inline([_button("🗑 Yes, delete", "danger"), _button("⬅️ Back")])

        result = _run(Bad("test_bad"))
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        message = result.failures[0][1]
        self.assertIn(keyboard_guard.FAILURE_HEADER, message)
        self.assertIn("'⬅️ Back' is button 2", message)

    def test_builder_markups_are_checked(self):
        class BuilderBad(unittest.TestCase):
            def test_bad(self):
                builder = InlineKeyboardBuilder()
                builder.button(text="🗑 Yes, delete", callback_data="sub:confirm_delete:5", style="danger")
                builder.button(text="⬅️ Back", callback_data="sub:more:5")
                builder.as_markup()

        result = _run(BuilderBad("test_bad"))
        self.assertEqual(len(result.failures), 1)
        self.assertIn("'⬅️ Back' is button 2", result.failures[0][1])

    def test_rows_added_after_building_are_checked(self):
        class Later(unittest.TestCase):
            def test_later(self):
                markup = _inline([_button("✖️ Close")])
                markup.inline_keyboard.append([_button("🔄 Refresh"), _button("◀️ To list")])

        result = _run(Later("test_later"))
        self.assertEqual(len(result.failures), 1)
        self.assertIn("'◀️ To list'", result.failures[0][1])

    def test_async_tests_and_builders_are_checked(self):
        class AsyncBad(unittest.IsolatedAsyncioTestCase):
            async def test_bad(self):
                _inline([_button("❌ Reject"), _button("Keep")])

        result = _run(AsyncBad("test_bad"))
        self.assertEqual(len(result.failures), 1)
        self.assertIn("'Keep'", result.failures[0][1])

    def test_opt_out_on_method_and_class(self):
        class MethodOptOut(unittest.TestCase):
            @misplaced_back_ok
            def test_bad(self):
                _inline([_button("🗑 Delete"), _button("✖️ Cancel")])

        @misplaced_back_ok
        class ClassOptOut(unittest.TestCase):
            def test_bad(self):
                _inline([_button("🗑 Delete"), _button("✖️ Cancel")])

        for case in (MethodOptOut("test_bad"), ClassOptOut("test_bad")):
            with self.subTest(case=type(case).__name__):
                result = _run(case)
                self.assertTrue(result.wasSuccessful())

    def test_good_keyboards_and_skips_pass(self):
        class Good(unittest.TestCase):
            def test_good(self):
                _inline([_button("✖️ Cancel"), _button("🗑 Delete")], [_button("◀️ Back")])

            @unittest.skip("skipped on purpose")
            def test_skipped(self):
                _inline([_button("🗑 Delete"), _button("✖️ Cancel")])

        good, skipped = Good("test_good"), Good("test_skipped")
        self.assertTrue(_run(good).wasSuccessful())
        result = _run(skipped)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(skipped._cleanups, [])

    def test_inner_test_keyboards_stay_out_of_the_outer_record(self):
        # The outer test (this one) is not opted out; the bad keyboard built by
        # the inner, opted-out test must not fail it.
        class Inner(unittest.TestCase):
            @misplaced_back_ok
            def test_bad(self):
                _inline([_button("🗑 Delete"), _button("✖️ Cancel")])

        self.assertTrue(_run(Inner("test_bad")).wasSuccessful())


if __name__ == "__main__":
    unittest.main()
