"""WP16: the consistency sweep across every screen package.

* Every callback_data format the bot sent before the redesign (git HEAD at the
  start of the UX work) still reaches a handler other than the catch-all, and
  a tap on it is answered exactly once without an error, even when the object
  it names is gone.
* The app source has none of the retired wording or patterns.
* Every keyboard builder is covered by a fit test; keyboards built outside
  app/ui/keyboards.py are fitted here.
* The behaviour fixed in the sweep: malformed buttons, strangers on an old
  member menu, Back from a settings picker, Send now on an overdue cycle,
  Close kept on spawned copies, the user delete question.
"""
from __future__ import annotations

import ast
import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from app.error_handler import card_button_markup
from app.handlers import admin_router, public_router
from app.infrastructure import SettingsMiddleware
from app.handlers.subscriptions import _wizard_step_view
from app.ui.helpers import DEAD_BUTTON, PROMPT_OUT_OF_DATE_TEXT, resolve_report_timezone
from app.ui.keyboards import CLOSE_CALLBACK, build_participants_keyboard
from app.ui.member_screens import member_delete_confirm_screen
from app.ui.prompts import PROMPT_MESSAGE_ID
from app.ui.states import PublicSettingsForm, SubscriptionAction, SubscriptionEditForm
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import USER_ID, make_message
from test_keyboards_fit import LONG_NAME, NAME_64, assert_fits

install_keyboard_guard()

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"
BIG_ID = 2**31 - 1
NBSP = " "

# Every callback_data shape found in git HEAD before the redesign (CallbackData
# prefixes with each action, and every raw string or prefix), with sample ids.
HEAD_CALLBACKS = (
    # raw strings
    "dialog:cancel",
    "menu:close",
    "public_account:rename",
    "public_settings:menu",
    "public_settings:time",
    "public_settings:time_other",
    "public_settings:time_reset",
    "public_settings:timezone",
    "public_settings:timezone_other",
    "public_settings:timezone_reset",
    "settings:menu",
    "settings:notifications",
    "settings:rounding",
    "settings:tests",
    "settings:time",
    "settings:time_other",
    "settings:timezone",
    "settings:timezone_other",
    "tests:send",
    # raw prefixes
    "create_payment_mode:split",
    "create_payment_mode:fixed",
    "currency:USD",
    "member_balance_currency:5:USD",
    "member_payment_destination:5:3",
    "member_payment_destination_default:5",
    "period:30",
    "period:month",
    "public_settings_time:09:00",
    "public_settings_timezone:Europe/London",
    "public_sub_currency:7:USD",
    "public_sub_currency_other:7",
    "public_sub_remindertime:7:09:00",
    "public_sub_remindertime_other:7",
    "settings_notify:reminders",
    "settings_notify:paid",
    "settings_notify:closed",
    "settings_rounding:precise",
    "settings_rounding:floor",
    "settings_rounding:round",
    "settings_rounding:ceil",
    "settings_time:09:00",
    "settings_timezone:Europe/London",
    "sharelimit:all",
    "sub_base_currency:7:EUR",
    "sub_currency:7:EUR",
    "sub_currency_other:7",
    "sub_payment_destination:7:default",
    "sub_payment_destination:7:3",
    "sub_payment_mode:7:split",
    "sub_payment_mode:7:fixed",
    "sub_remindertime:7:09:00",
    "sub_remindertime_other:7",
    "sub_user_amount:7:5",
    "sub_user_amount_all:7",
    "sub_user_amount_clear:7:5",
    "topup_destination:3",
    # CallbackData classes: prefix, then the fields in their original order
    *(
        f"sub:{action}:7"
        for action in (
            "amount", "back", "basecurrency", "basecurrency_other", "comment", "comment_clear", "confirm_delete",
            "create", "currency", "cycles", "delete", "duedate", "more", "open", "open_public", "participants",
            "participants_manage", "paymentinfo", "paymentmethod", "period", "pricing", "public_back",
            "public_currency", "public_currency_default", "public_remindertime", "public_remindertime_default",
            "public_report", "reminderdays", "reminderloop", "reminders", "reminders_send", "remindertime",
            "remindertime_default", "rename", "report", "share", "useramounts",
        )
    ),
    *(f"cycle:{action}:7:2026-11-01" for action in ("open", "recreate", "force_close")),
    "duedate:move:7:2026-11-01:2026-11-20",
    "duedate:new:7:2026-11-01:2026-11-20",
    "spart:toggle:7:5",
    "spart:weight:7:5",
    "remind:7:2026-11-01",
    "remsend:7:0",
    "remsend:7:501",
    "testsend:501",
    "testlist:page:2",
    "testpaid:0:2026-11-01",
    *(
        f"member:{action}:5"
        for action in (
            "add", "back", "balance", "balance_currency", "balance_currency_other", "balance_set",
            "confirm_delete", "delete", "invite", "open", "page", "payment_destination", "rename", "report",
        )
    ),
    *(
        f"paydest:{action}:3"
        for action in (
            "confirm_delete", "create", "delete", "edit_currency", "edit_details", "edit_link", "edit_title",
            "menu", "open", "set_default",
        )
    ),
    *(f"topup:{action}:9" for action in ("admin_list", "admin_open", "approve", "reject", "start", "submit")),
)


def _handlers():
    for router in (public_router, admin_router):
        yield from router.callback_query.handlers


def _shared_settings():
    for middleware in public_router.callback_query.middleware:
        if isinstance(middleware, SettingsMiddleware):
            return middleware.settings
    raise AssertionError("SettingsMiddleware is not registered")


class LegacyCallbackRoutingTests(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # The admin presets below change the dispatcher's shared Settings; put them back.
        settings = _shared_settings()
        saved = dict(vars(settings))
        self.addCleanup(lambda: [setattr(settings, key, value) for key, value in saved.items()])

    async def test_every_head_callback_reaches_a_real_handler(self):
        self.assertEqual(len(HEAD_CALLBACKS), len(set(HEAD_CALLBACKS)))
        unhandled = []
        for data in HEAD_CALLBACKS:
            self.assertLessEqual(len(data.encode()), 64, data)
            callback = self.callback(data)
            for handler in _handlers():
                matched, _ = await handler.check(callback, raw_state=None)
                if matched:
                    break
            else:
                unhandled.append(data)
        self.assertEqual(unhandled, [])

    async def test_every_head_callback_is_answered_once_with_nothing_behind_it(self):
        # An admin taps every old button on an old message; nothing it names exists any more.
        await self.make_admin()
        for data in HEAD_CALLBACKS:
            with self.subTest(data=data):
                self.bot.calls.clear()
                await self.state().clear()
                await self.feed(self.callback(data))
                self.assertEqual(len(self.bot.answers()), 1, [a.text for a in self.bot.answers()])


# --------------------------------------------------------------- source rules


RETIRED_FRAGMENTS = (
    "⬅️", "day(s)", "user(s)", "share(s)", "notification(s)", "cycle(s)", "payment(s)", "Yes, delete",
    "Menu closed", "Choose a value", "Choose an action", "Session expired", "Invalid action", "Invalid subscription",
    "Unsupported", "Unable to", "not found.", " | ",
)
THROWAWAY = re.compile(r"^\w[\w ]* (updated|saved|set|removed|cleared)\.$")


def _app_sources():
    for path in sorted((ROOT / "app").rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


class SourceRuleTests(unittest.TestCase):
    def test_no_retired_wording_in_ui_strings(self):
        problems = []
        for path, tree in _app_sources():
            docstrings = _docstring_nodes(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in docstrings:
                    continue
                for fragment in RETIRED_FRAGMENTS:
                    if fragment in node.value:
                        problems.append(f"{path.name}:{node.lineno}: {fragment!r} in {node.value[:60]!r}")
                if THROWAWAY.match(node.value):
                    problems.append(f"{path.name}:{node.lineno}: throwaway message {node.value!r}")
        self.assertEqual(problems, [])

    def test_no_bare_state_clear_and_no_direct_callback_answer(self):
        problems = []
        for path, tree in _app_sources():
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                owner = node.func.value
                if not isinstance(owner, ast.Name):
                    continue
                if node.func.attr == "clear" and owner.id == "state" and path.name != "prompts.py":
                    problems.append(f"{path.name}:{node.lineno}: state.clear() outside reset_flow_state")
                if node.func.attr == "answer" and owner.id == "callback" and path.name != "helpers.py":
                    problems.append(f"{path.name}:{node.lineno}: callback.answer() instead of answer_callback_once")
        self.assertEqual(problems, [])

    def test_no_reply_keyboard_reattached_outside_start_help_and_access_replies(self):
        # Edits cannot carry a reply keyboard; only /start, Help, the fallback and
        # the no-access replies send one. Never the admin or member menu elsewhere.
        allowed = {"admin.py", "fallback.py", "topups.py", "helpers.py", "keyboards.py"}
        problems = []
        for path, tree in _app_sources():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in {"admin_reply_keyboard", "public_reply_keyboard"} and path.name not in allowed:
                        problems.append(f"{path.name}:{node.lineno}: {node.func.id}()")
        self.assertEqual(problems, [])


# ---------------------------------------------------------------- fit coverage


class FitCoverageTests(unittest.TestCase):
    def test_every_keyboard_builder_has_a_fit_test(self):
        tree = ast.parse((ROOT / "app" / "ui" / "keyboards.py").read_text(encoding="utf-8"))
        fit_sources = [
            path.read_text(encoding="utf-8")
            for path in TESTS_DIR.glob("test_*.py")
            if "assert_fits(" in path.read_text(encoding="utf-8")
        ]
        missing = []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            returns = ast.unparse(node.returns) if node.returns else ""
            if "Markup" not in returns:
                continue
            if not any(re.search(rf"\b{node.name}\(", source) for source in fit_sources):
                missing.append(node.name)
        self.assertEqual(missing, [])

    def test_keyboards_built_outside_keyboards_py_are_known(self):
        # Each of these is fitted below (or by test_keyboards_fit / test_ui_pushes).
        known = {
            ("error_handler.py", "card_button_markup"),
            ("reminders.py", "grey_button"),
            ("subscriptions.py", "_wizard_markup"),
            ("prompts.py", "prompt_markup"),
        }
        found = set()
        for path, tree in _app_sources():
            if path.name == "keyboards.py":
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = ast.unparse(node)
                    if "InlineKeyboardMarkup(" in body or ".as_markup(" in body:
                        found.add((path.name, node.name))
        self.assertEqual(found, known)

    def test_handler_built_keyboards_fit(self):
        assert_fits(card_button_markup(BIG_ID))
        for step in range(1, 8):
            with self.subTest(step=step):
                _, markup = _wizard_step_view(step)
                assert_fits(markup)
                self.assertEqual([b.text for b in markup.inline_keyboard[-1]], ["✖️ Cancel"])
                self.assertNotIn("force_reply", markup.model_dump(exclude_none=True))

    def test_participants_picker_copy_from_a_push_ends_in_close(self):
        friends = [
            {"id": BIG_ID, "full_name": NAME_64, "is_member": True, "share_weight": 5, "telegram_id": 1},
            {"id": BIG_ID - 1, "full_name": LONG_NAME, "is_member": False, "telegram_id": 2},
        ]
        for close in (False, True):
            markup = build_participants_keyboard(friends, BIG_ID, page=2, total_pages=3, close=close)
            assert_fits(markup)
            last = markup.inline_keyboard[-1]
            self.assertEqual([b.text for b in last], ["✖️ Close"] if close else ["◀️ Back"])
            self.assertEqual(last[0].callback_data == CLOSE_CALLBACK, close)


# --------------------------------------------------------------------- flows


def _layout(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


class SweepFlowTests(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.tz = await resolve_report_timezone(self.db)
        self.today = datetime.now(ZoneInfo(self.tz)).date()

    def screen(self, message_id: int = 900, markup: InlineKeyboardMarkup | None = None):
        message = make_message(self.bot, message_id, "screen", from_bot=True)
        if markup is not None:
            message = message.model_copy(update={"reply_markup": markup}).as_(self.bot)
        return message

    async def _member(self, name: str, telegram_id: int) -> int:
        friend_id = await self.db.create_friend_invite(name, f"t-{telegram_id}", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(friend_id, telegram_id, "2026-01-01T00:00:00")
        return friend_id

    def _answers(self) -> list[tuple[str | None, bool]]:
        return [(a.text, bool(a.show_alert)) for a in self.bot.answers()]

    async def test_stranger_on_an_old_member_menu_gets_the_guest_menu(self):
        for label, title in (
            ("📋 Subscriptions", "📋 <b>Subscriptions</b>"),
            ("📊 Reports", "📊 <b>Reports</b>"),
            ("⚙️ Settings", "⚙️ <b>Settings</b>"),
        ):
            with self.subTest(label=label):
                self.bot.calls.clear()
                await self.feed(self.message(label))
                sent = self.bot.of(SendMessage)[-1]
                self.assertEqual(
                    sent.text, f"{title}\n\nYour profile is not set up yet. Ask the admin for your sign-in link."
                )
                self.assertIsInstance(sent.reply_markup, ReplyKeyboardMarkup)
                self.assertEqual([[b.text for b in row] for row in sent.reply_markup.keyboard], [["❓ Help"]])

    async def test_linked_member_still_gets_settings(self):
        await self._member("Anna Petrova", USER_ID)
        await self.feed(self.message("⚙️ Settings"))
        self.assertTrue(self.bot.of(SendMessage)[-1].text.startswith("⚙️ <b>Settings</b>\n\n⏰ Reminder time"))

    async def test_back_from_a_settings_picker_winds_the_prompt_down(self):
        await self._member("Anna Petrova", USER_ID)
        await self.feed(self.callback("public_settings:time_other", self.screen(901)))
        self.assertEqual(await self.state().get_state(), PublicSettingsForm.base_time.state)
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]
        self.bot.calls.clear()
        await self.feed(self.callback("public_settings:menu", self.screen(901)))
        self.assertIsNone(await self.state().get_state())
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertTrue(self.bot.of(EditMessageText)[-1].text.startswith("⚙️ <b>Settings</b>"))
        self.assertEqual(len(self.bot.answers()), 1)

    async def test_malformed_preset_redraws_pricing_with_one_toast(self):
        await self.make_admin()
        sub_id = await self.db.create_subscription("Netflix", 15.99, "USD", self.today + timedelta(days=5), 30, None)
        for data in (f"sub_currency:{sub_id}:1X", f"sub_base_currency:{sub_id}:EURO"):
            with self.subTest(data=data):
                self.bot.calls.clear()
                await self.feed(self.callback(data, self.screen()))
                self.assertEqual(self._answers(), [(DEAD_BUTTON, False)])
                self.assertTrue(self.bot.of(EditMessageText)[-1].text.startswith("💰 <b>Pricing · Netflix</b>"))
                self.assertEqual((await self.db.get_subscription(sub_id))["currency"], "USD")
        for data in ("sub_currency:x:EUR", "sub_currency_other:x", "period:-3", "sharelimit:9"):
            with self.subTest(data=data):
                self.bot.calls.clear()
                await self.feed(self.callback(data, self.screen()))
                self.assertEqual(self._answers()[-1], (DEAD_BUTTON, False))
                self.assertEqual(len(self.bot.answers()), 1)

    async def test_prompt_out_of_date_does_not_resend_the_reply_menu(self):
        await self.make_admin()
        await self.set_state(SubscriptionEditForm.comment)
        await self.feed(self.message("Pay by the 5th"))
        sent = self.bot.of(SendMessage)[-1]
        self.assertEqual(sent.text, PROMPT_OUT_OF_DATE_TEXT)
        self.assertIsNone(sent.reply_markup)

    async def test_send_now_reaches_an_overdue_cycle_off_the_reminder_days(self):
        await self.make_admin()
        due = self.today - timedelta(days=3)
        sub_id = await self.db.create_subscription("Netflix", 300.0, "RUB", due, 30, None)
        await self.db.update_subscription_fields(sub_id, reminder_offsets="[-1, 0]", remind_after_due=0)
        friend_id = await self._member("Alice Smith", 501)
        await self.db.set_participant(sub_id, friend_id, True)
        await self.db.ensure_cycle(sub_id, due)
        await self.db.set_setting("notify_admin_reminders", "0")
        await self.feed(self.callback(f"remsendok:{sub_id}:0", self.screen(700)))
        self.assertIn(501, [int(call.chat_id) for call in self.bot.of(SendMessage)])
        self.assertTrue(self.bot.of(EditMessageText)[-1].text.startswith("✅ Reminder sent to <b>1</b> person"))

    async def test_toggling_on_the_picker_copy_from_a_push_keeps_close(self):
        await self.make_admin()
        sub_id = await self.db.create_subscription("Netflix", 15.99, "USD", self.today + timedelta(days=5), 30, None)
        friend_id = await self._member("Carol", 503)
        data = SubscriptionAction(action="participants_manage_new", subscription_id=sub_id).pack()
        await self.feed(self.callback(data, self.screen(800)))
        copy = self.bot.of(SendMessage)[-1]
        self.assertEqual(_layout(copy.reply_markup)[-1], ["✖️ Close"])
        self.bot.calls.clear()
        await self.feed(self.callback(f"spart:toggle:{sub_id}:{friend_id}", self.screen(801, copy.reply_markup)))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(_layout(edit.reply_markup)[-1], ["✖️ Close"])
        self.assertEqual(self._answers(), [("Carol added", False)])

    async def test_top_up_from_the_account_copy_keeps_close(self):
        await self._member("Anna Petrova", USER_ID)
        for title in ("Sber", "Tinkoff"):
            await self.db.create_payment_destination(title=title, currency="RUB", details="2200 1234")
        account_copy = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Top up", callback_data="topup:start:0")],
                [InlineKeyboardButton(text="✖️ Close", callback_data=CLOSE_CALLBACK)],
            ]
        )
        await self.feed(self.callback("topup:start:0", self.screen(810, account_copy)))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertTrue(edit.text.startswith("➕ <b>Top up</b>"))
        self.assertEqual(_layout(edit.reply_markup)[-1], ["✖️ Close"])

    async def test_user_delete_question_counts_pending_top_ups(self):
        friend_id = await self._member("Anna Petrova", 1001)
        await self.db.set_friend_balance(friend_id, 350)
        for amount in (100.0, 200.0):
            request_id = await self.db.create_topup_request(
                friend_id=friend_id, destination_id=None, amount=amount, currency="RUB",
                destination_title="Sber", destination_details="2200",
            )
            await self.db.mark_topup_request_pending(request_id)
        text, _ = await member_delete_confirm_screen(self.db, await self.db.get_friend(friend_id))
        self.assertEqual(
            text,
            f"⚠️ <b>Delete Anna Petrova?</b>\nAnna loses the balance of 350.00{NBSP}RUB and drops 2 pending top-ups.",
        )


if __name__ == "__main__":
    unittest.main()
