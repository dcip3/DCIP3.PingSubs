"""A reply-menu tap or a /command abandons a pending text input (MenuStateResetMiddleware)."""

import unittest
from typing import Any
from unittest import mock

from aiogram.fsm.state import State, StatesGroup

import app.handlers.admin as admin_handlers
import app.handlers.members as members_handlers
import app.handlers.topups as topups_handlers
from app.handlers.fallback import USE_THE_MENU_TEXT
from app.infrastructure import MenuStateResetMiddleware, leaves_pending_input
from app.infrastructure import menu_reset
from app.ui.keyboards import LEGACY_MENU_ALIASES, MENU_BUTTON_TEXTS, MENU_LABELS
from app.ui.prompts import CLEANUP_IDS, PROMPT_MESSAGE_ID, WIZARD_CHAT_ID, WIZARD_MESSAGE_ID
from app.ui.states import SettingsForm, SubscriptionForm, TopUpForm
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import CHAT_ID, FakeBot, make_message, make_state

install_keyboard_guard()

PROMPT_ID = 901
TYPO_ID = 902
HINT_ID = 903


class Flow(StatesGroup):
    name = State()


class LeavesPendingInputTests(unittest.TestCase):
    def test_every_menu_label_and_legacy_alias_leaves(self):
        for text in (*MENU_LABELS, *LEGACY_MENU_ALIASES):
            with self.subTest(text=text):
                self.assertIn(text, MENU_BUTTON_TEXTS)
                self.assertTrue(leaves_pending_input(text))

    def test_any_slash_command_leaves(self):
        for text in ("/start", "/help", "/start join_abc", "/whatever"):
            with self.subTest(text=text):
                self.assertTrue(leaves_pending_input(text))

    def test_free_text_and_empty_input_stay(self):
        for text in (None, "", "Netflix", "15.99", "Users", "👥 Users please", " /start"):
            with self.subTest(text=text):
                self.assertFalse(leaves_pending_input(text))


class MiddlewareUnitTests(unittest.IsolatedAsyncioTestCase):
    """The middleware alone, with a recording handler instead of the routers."""

    async def asyncSetUp(self) -> None:
        self.bot = FakeBot()
        self.state = make_state(self.bot)
        self.seen: list[dict[str, Any]] = []

    async def _handler(self, event, data):
        self.seen.append(dict(data))
        return "handled"

    async def _run(self, text: str) -> Any:
        data = {
            "bot": self.bot,
            "state": self.state,
            "raw_state": await self.state.get_state(),
        }
        message = make_message(self.bot, 77, text)
        return await MenuStateResetMiddleware()(self._handler, message, data)

    async def _start_prompt(self) -> None:
        await self.state.set_state(Flow.name)
        await self.state.update_data(
            {PROMPT_MESSAGE_ID: PROMPT_ID, CLEANUP_IDS: [TYPO_ID, HINT_ID], "edit_subscription_id": 5}
        )

    async def test_menu_tap_winds_the_flow_down_then_runs_the_handler(self):
        await self._start_prompt()

        result = await self._run("👥 Users")

        self.assertEqual(result, "handled")
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        self.assertEqual(sorted(self.bot.deleted_ids()), [PROMPT_ID, TYPO_ID, HINT_ID])
        self.assertEqual(len(self.seen), 1)
        self.assertIsNone(self.seen[0]["raw_state"])

    async def test_command_winds_the_flow_down(self):
        await self._start_prompt()

        await self._run("/start")

        self.assertIsNone(await self.state.get_state())
        self.assertIn(PROMPT_ID, self.bot.deleted_ids())
        self.assertIsNone(self.seen[0]["raw_state"])

    async def test_free_text_in_a_state_is_untouched(self):
        await self._start_prompt()

        await self._run("Netflix")

        self.assertEqual(await self.state.get_state(), Flow.name.state)
        self.assertEqual((await self.state.get_data())[PROMPT_MESSAGE_ID], PROMPT_ID)
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.seen[0]["raw_state"], Flow.name.state)

    async def test_menu_tap_without_a_state_does_nothing_extra(self):
        await self._run("💳 Payment")

        self.assertEqual(self.bot.calls, [])
        self.assertIsNone(self.seen[0]["raw_state"])

    async def test_abandoned_wizard_is_retired(self):
        await self.state.set_state(Flow.name)
        await self.state.update_data({WIZARD_MESSAGE_ID: 555, WIZARD_CHAT_ID: CHAT_ID})

        await self._run("📋 Subscriptions")

        self.assertIn(555, self.bot.deleted_ids())
        self.assertIsNone(await self.state.get_state())

    async def test_a_cleanup_failure_is_logged_and_never_blocks_the_tap(self):
        await self._start_prompt()

        with mock.patch.object(menu_reset, "wind_down", mock.AsyncMock(side_effect=RuntimeError("boom"))):
            with self.assertLogs(menu_reset.logger, level="ERROR"):
                result = await self._run("👥 Users")

        self.assertEqual(result, "handled")
        self.assertIsNone(await self.state.get_state())
        self.assertIsNone(self.seen[0]["raw_state"])

    async def test_non_message_events_pass_through(self):
        await self._start_prompt()
        data = {"bot": self.bot, "state": self.state, "raw_state": Flow.name.state}

        await MenuStateResetMiddleware()(self._handler, object(), data)

        self.assertEqual(await self.state.get_state(), Flow.name.state)
        self.assertEqual(self.seen[0]["raw_state"], Flow.name.state)


class MenuResetThroughTheDispatcherTests(RealDispatcherTestCase):
    """The real routers: state handlers registered before the menu handlers no longer swallow taps."""

    async def _prompt_in(self, state: State) -> None:
        await self.set_state(state, **{PROMPT_MESSAGE_ID: PROMPT_ID, CLEANUP_IDS: [TYPO_ID]})

    async def test_users_tap_in_the_subscription_name_prompt_opens_users(self):
        await self.make_admin()
        await self._prompt_in(SubscriptionForm.name)

        with mock.patch.object(members_handlers, "send_member_list", mock.AsyncMock()) as users_menu:
            await self.feed(self.message("👥 Users"))

        users_menu.assert_awaited_once()
        self.assertIsNone(await self.state().get_state())
        self.assertEqual(sorted(self.bot.deleted_ids()), [PROMPT_ID, TYPO_ID])

    async def test_payment_tap_in_the_base_time_prompt_opens_payment(self):
        await self.make_admin()
        await self._prompt_in(SettingsForm.base_time)

        with mock.patch.object(topups_handlers, "_send_payment_destinations_menu", mock.AsyncMock()) as payment:
            await self.feed(self.message("💳 Payment"))

        payment.assert_awaited_once()
        self.assertIsNone(await self.state().get_state())
        self.assertIn(PROMPT_ID, self.bot.deleted_ids())

    async def test_legacy_alias_also_resets(self):
        await self.make_admin()
        await self._prompt_in(SubscriptionForm.amount)

        with mock.patch.object(topups_handlers, "_send_payment_destinations_menu", mock.AsyncMock()) as payment:
            await self.feed(self.message("💳 Payment methods"))

        payment.assert_awaited_once()
        self.assertIsNone(await self.state().get_state())

    async def test_start_clears_the_state(self):
        await self.make_admin()
        await self._prompt_in(SubscriptionForm.name)

        await self.feed(self.message("/start"))

        self.assertIsNone(await self.state().get_state())
        self.assertIn(PROMPT_ID, self.bot.deleted_ids())
        self.assertTrue(self.bot.sent_texts(), "/start answered")

    async def test_member_account_tap_leaves_the_top_up_amount_prompt(self):
        await self._prompt_in(TopUpForm.amount)

        with mock.patch.object(admin_handlers, "send_public_account_detail", mock.AsyncMock()) as account:
            await self.feed(self.message("👤 Account"))

        account.assert_awaited_once()
        self.assertIsNone(await self.state().get_state())

    async def test_free_text_in_a_state_goes_to_the_flow(self):
        await self.make_admin()
        await self._prompt_in(SubscriptionForm.name)

        await self.feed(self.message("Netflix"))

        # The name step took the text (the flow moved on); the menu reset and the catch-all stayed out.
        self.assertNotEqual(await self.state().get_state(), None)
        self.assertNotEqual(await self.state().get_state(), SubscriptionForm.name.state)
        self.assertNotIn(USE_THE_MENU_TEXT, self.bot.sent_texts())

    async def test_group_messages_are_left_alone(self):
        await self.make_admin()

        await self.feed(self.message("👥 Users", chat_type="group"))

        self.assertEqual(self.bot.calls, [])


if __name__ == "__main__":
    unittest.main()
