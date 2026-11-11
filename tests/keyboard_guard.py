"""Check the way-back rule on every inline keyboard any test builds.

The project runs ``python -m unittest discover -s tests`` (CI too), so there is
no pytest conftest. Instead every ``tests/test_*.py`` module calls
``install_keyboard_guard()`` at import time (``test_keyboards_fit`` fails when
one does not). Installing patches, once per process:

* ``InlineKeyboardMarkup.__init__`` to record every markup built while a test
  runs (builders, handlers, services, scheduler pushes);
* ``unittest.TestCase.run`` to start a fresh record per test and to add a last
  cleanup that fails the test when a recorded markup has a way back
  (``◀️ …``, ``⬅️ …``, ``✖️ Close``, ``✖️ Cancel``, ``Keep``) to the right of
  an action. The check runs after tearDown, so rows added to a markup later
  count too.

Tests that build bad keyboards on purpose opt out with ``@misplaced_back_ok``
on the test method or on the whole TestCase class.
"""
from __future__ import annotations

import unittest
from typing import TypeVar

from aiogram.types import InlineKeyboardMarkup

from app.ui.keyboard_fit import misplaced_back_buttons

OPT_OUT_ATTR = "__misplaced_back_ok__"
_GUARD_FLAG = "__keyboard_guard__"
FAILURE_HEADER = "Way back not on the left:"

_T = TypeVar("_T")

# One record per running test; a stack so a test that runs another TestCase
# (the guard's own self-test) keeps the two records apart.
_records: list[list[InlineKeyboardMarkup]] = []


def misplaced_back_ok(target: _T) -> _T:
    """Opt a test method or a TestCase class out of the way-back check."""
    setattr(target, OPT_OUT_ATTR, True)
    return target


def is_installed() -> bool:
    return bool(getattr(unittest.TestCase.run, _GUARD_FLAG, False))


def _opted_out(test: unittest.TestCase) -> bool:
    method = getattr(test, getattr(test, "_testMethodName", ""), None)
    return bool(getattr(method, OPT_OUT_ATTR, False) or getattr(type(test), OPT_OUT_ATTR, False))


def keyboard_rule_problems(markups: list[InlineKeyboardMarkup]) -> list[str]:
    problems = [
        f"{[[button.text for button in row] for row in markup.inline_keyboard]}: {problem}"
        for markup in markups
        for problem in misplaced_back_buttons(markup)
    ]
    return list(dict.fromkeys(problems))


def install_keyboard_guard() -> None:
    """Patch markup construction and ``TestCase.run`` (idempotent)."""
    if is_installed():
        return

    original_init = InlineKeyboardMarkup.__init__
    original_run = unittest.TestCase.run

    def recording_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if _records:
            _records[-1].append(self)

    def guarded_run(self, result=None):
        built: list[InlineKeyboardMarkup] = []

        def check_keyboards() -> None:
            if _opted_out(self):
                return
            problems = keyboard_rule_problems(built)
            if problems:
                raise self.failureException(FAILURE_HEADER + "\n" + "\n".join(problems))

        # Registered first, so it runs last: after tearDown and the test's own cleanups.
        self.addCleanup(check_keyboards)
        _records.append(built)
        try:
            return original_run(self, result)
        finally:
            # By identity: two empty records compare equal.
            del _records[next(i for i, record in enumerate(_records) if record is built)]
            # A skipped test returns before cleanups run; do not leave ours behind.
            self._cleanups = [entry for entry in self._cleanups if entry[0] is not check_keyboards]

    setattr(guarded_run, _GUARD_FLAG, True)
    InlineKeyboardMarkup.__init__ = recording_init
    unittest.TestCase.run = guarded_run
