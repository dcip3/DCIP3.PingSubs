"""Global error handler (app/error_handler.py), the fallback router and the bot commands."""

import unittest
from typing import Any, Optional
from unittest import mock

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, Message, Update

import app.handlers.admin as admin_handlers
import app.handlers.members as members_handlers
import main
from app.error_handler import (
    ADMIN_ALERT_TITLE,
    CARD_BUTTON_TEXT,
    USER_ERROR_ALERT,
    USER_ERROR_TEXT,
    AlertThrottle,
    admin_alert_text,
    setup_error_handler,
)
from app.handlers import admin_router, fallback_router, public_router
from app.handlers.fallback import DEAD_BUTTON_TOAST, USE_THE_MENU_TEXT
from app.ui.helpers import USER_GONE, answer_callback_once, edit_in_place, respond_with_markup
from app.ui.keyboards import admin_reply_keyboard, guest_reply_keyboard, public_reply_keyboard
from app.ui.states import MemberAction, SubscriptionForm
from dispatcher_harness import ADMIN_ID, RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import (
    CHAT_ID,
    USER_ID,
    EditMessageText,
    FakeBot,
    SendMessage,
    make_callback,
    make_message,
)

install_keyboard_guard()

ADMINS = (1001, 1002)
FRIEND_ID = 17


class FakeDb:
    def __init__(self, admin_ids=ADMINS, friend: Optional[dict] = None, broken: bool = False) -> None:
        self.admin_ids = list(admin_ids)
        self.friend = friend
        self.broken = broken

    async def list_admin_ids(self) -> list[int]:
        if self.broken:
            raise RuntimeError("database is locked")
        return self.admin_ids

    async def get_friend_by_telegram(self, telegram_id: Optional[int]) -> Optional[dict[str, Any]]:
        if self.broken:
            raise RuntimeError("database is locked")
        return self.friend if self.friend and telegram_id == USER_ID else None


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _keyboard_texts(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.keyboard]


class ErrorHandlerTests(unittest.IsolatedAsyncioTestCase):
    """A small dispatcher whose handlers raise on purpose."""

    async def asyncSetUp(self) -> None:
        self.bot = FakeBot()
        self.db = FakeDb()
        self.clock = FakeClock()
        self.exception: BaseException = RuntimeError("boom")
        self.answer_first = False
        self.dp = Dispatcher()
        router = Router()

        @router.message(F.text)
        async def failing_message(message: Message) -> None:
            raise self.exception

        @router.callback_query()
        async def failing_callback(callback: CallbackQuery) -> None:
            if self.answer_first:
                await answer_callback_once(callback, "Saved")
            raise self.exception

        self.dp.include_router(router)
        self.throttle = setup_error_handler(self.dp, self.db, throttle=AlertThrottle(clock=self.clock))

    async def _feed_message(self, text: str = "hello", chat_type: str = "private") -> Any:
        message = make_message(self.bot, 10, text)
        if chat_type != "private":
            message = message.model_copy(update={"chat": message.chat.model_copy(update={"type": chat_type})})
        return await self.dp.feed_update(self.bot, Update(update_id=1, message=message).as_(self.bot))

    async def _feed_callback(self) -> CallbackQuery:
        callback = make_callback(self.bot, make_message(self.bot, 11, "screen", from_bot=True), "x:1")
        await self.dp.feed_update(self.bot, Update(update_id=2, callback_query=callback).as_(self.bot))
        return callback

    def _alerts(self) -> list[SendMessage]:
        return [call for call in self.bot.of(SendMessage) if int(call.chat_id) in ADMINS]

    def _user_messages(self) -> list[SendMessage]:
        return [call for call in self.bot.of(SendMessage) if int(call.chat_id) == CHAT_ID]

    async def test_message_error_tells_the_user_and_alerts_every_admin(self):
        with self.assertLogs("app.error_handler", level="ERROR"):
            await self._feed_message()

        self.assertEqual([call.text for call in self._user_messages()], [USER_ERROR_TEXT])
        alerts = self._alerts()
        self.assertEqual(sorted(int(call.chat_id) for call in alerts), sorted(ADMINS))
        self.assertEqual(
            alerts[0].text,
            "🚨 <b>Bot error</b>\n\n"
            f'User: <a href="tg://user?id={USER_ID}">Anna</a> · <code>{USER_ID}</code>\n'
            "Error: <code>RuntimeError: boom</code>",
        )
        self.assertIsNone(alerts[0].reply_markup)

    async def test_alert_carries_the_card_button_for_a_known_member(self):
        self.db.friend = {"id": FRIEND_ID, "full_name": "Anna"}

        with self.assertLogs("app.error_handler", level="ERROR"):
            await self._feed_message()

        markup = self._alerts()[0].reply_markup
        self.assertEqual(len(markup.inline_keyboard), 1)
        button = markup.inline_keyboard[0][0]
        self.assertEqual(button.text, CARD_BUTTON_TEXT)
        self.assertEqual(button.callback_data, MemberAction(action="open_new", friend_id=FRIEND_ID).pack())
        self.assertEqual(button.callback_data, f"member:open_new:{FRIEND_ID}")
        self.assertIsNone(button.style)

    async def test_alerts_are_throttled_per_kind_with_a_repeat_count(self):
        with self.assertLogs("app.error_handler", level="ERROR"):
            await self._feed_message()
            await self._feed_message()
            await self._feed_message()
        self.assertEqual(len(self._alerts()), len(ADMINS))
        # Every user still hears about every failure.
        self.assertEqual(len(self._user_messages()), 3)

        self.clock.now += 10 * 60
        with self.assertLogs("app.error_handler", level="ERROR"):
            await self._feed_message()

        alerts = self._alerts()
        self.assertEqual(len(alerts), 2 * len(ADMINS))
        self.assertTrue(alerts[-1].text.endswith("\nRepeats in 10 min: 2"))

    async def test_another_kind_of_error_is_not_held_back(self):
        with self.assertLogs("app.error_handler", level="ERROR"):
            await self._feed_message()
            self.exception = ValueError("bad value")
            await self._feed_message()

        self.assertEqual(len(self._alerts()), 2 * len(ADMINS))

    async def test_callback_error_shows_an_alert(self):
        with self.assertLogs("app.error_handler", level="ERROR"):
            await self._feed_callback()

        answers = self.bot.answers()
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0].text, USER_ERROR_ALERT)
        self.assertTrue(answers[0].show_alert)
        self.assertEqual(self._user_messages(), [])
        self.assertEqual(len(self._alerts()), len(ADMINS))

    async def test_callback_answered_before_the_error_gets_a_message(self):
        self.answer_first = True

        with self.assertLogs("app.error_handler", level="ERROR"):
            await self._feed_callback()

        self.assertEqual([answer.text for answer in self.bot.answers()], ["Saved"])
        self.assertEqual([call.text for call in self._user_messages()], [USER_ERROR_TEXT])

    async def test_forbidden_and_retry_after_are_ignored(self):
        method = SendMessage(chat_id=CHAT_ID, text="x")
        for exception in (
            TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user"),
            TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=5),
        ):
            with self.subTest(exception=type(exception).__name__):
                self.bot.calls.clear()
                self.exception = exception
                await self._feed_message()
                await self._feed_callback()
                self.assertEqual(self.bot.of(SendMessage), [])

    async def test_not_modified_and_too_old_only_answer_silently(self):
        method = EditMessageText(text="x", chat_id=CHAT_ID, message_id=11)
        for text in ("Bad Request: message is not modified", "Bad Request: query is too old"):
            with self.subTest(text=text):
                self.bot.calls.clear()
                self.exception = TelegramBadRequest(method=method, message=text)
                await self._feed_message()
                await self._feed_callback()
                self.assertEqual(self.bot.of(SendMessage), [])
                answers = self.bot.answers()
                self.assertEqual(len(answers), 1)
                self.assertIsNone(answers[0].text)

    async def test_a_real_bad_request_is_reported(self):
        method = EditMessageText(text="x", chat_id=CHAT_ID, message_id=11)
        self.exception = TelegramBadRequest(method=method, message="Bad Request: can't parse entities")

        with self.assertLogs("app.error_handler", level="ERROR"):
            await self._feed_message()

        self.assertEqual([call.text for call in self._user_messages()], [USER_ERROR_TEXT])
        self.assertIn("TelegramBadRequest: Telegram server says - Bad Request: can&#x27;t parse entities", self._alerts()[0].text)

    async def test_group_message_errors_alert_admins_but_stay_out_of_the_group(self):
        with self.assertLogs("app.error_handler", level="ERROR"):
            await self._feed_message(chat_type="group")

        self.assertEqual(self._user_messages(), [])
        self.assertEqual(len(self._alerts()), len(ADMINS))

    async def test_a_broken_database_still_tells_the_user(self):
        self.db.broken = True

        with self.assertLogs("app.error_handler", level="ERROR"):
            await self._feed_message()

        self.assertEqual([call.text for call in self._user_messages()], [USER_ERROR_TEXT])
        self.assertEqual(self._alerts(), [])

    async def test_config_admins_get_alerts_when_the_db_list_fails(self):
        dp = Dispatcher()
        router = Router()

        @router.message()
        async def failing(message: Message) -> None:
            raise RuntimeError("boom")

        dp.include_router(router)
        setup_error_handler(dp, FakeDb(broken=True), admin_ids=(1001,))

        with self.assertLogs("app.error_handler", level="ERROR"):
            await dp.feed_update(self.bot, Update(update_id=3, message=make_message(self.bot, 12, "hi")).as_(self.bot))

        self.assertEqual([int(call.chat_id) for call in self._alerts()], [1001])

    async def test_an_undeliverable_alert_does_not_stop_the_others(self):
        self.bot.fail(SendMessage, "Forbidden: bot was blocked by the user", forbidden=True)  # the user message
        self.bot.fail(SendMessage, "Bad Request: chat not found")  # the first admin

        with self.assertLogs("app.error_handler", level="ERROR"):
            await self._feed_message()

        # The user is unreachable and the first admin fails; the second admin is still tried.
        self.assertEqual([int(call.chat_id) for call in self._alerts()], list(ADMINS))


class AlertTextAndThrottleTests(unittest.IsolatedAsyncioTestCase):
    def test_alert_text_escapes_names_and_cuts_long_messages(self):
        user = make_message(FakeBot(), 1, "x").from_user.model_copy(update={"first_name": "<Ann & Co>"})

        text = admin_alert_text(user, ValueError("x" * 500), repeats=3)

        self.assertTrue(text.startswith(ADMIN_ALERT_TITLE + "\n\n"))
        self.assertIn('">&lt;Ann &amp; Co&gt;</a>', text)
        self.assertIn("<code>ValueError: " + "x" * (300 - len("ValueError: ")) + "</code>", text)
        self.assertTrue(text.endswith("\nRepeats in 10 min: 3"))

    def test_alert_text_without_a_user(self):
        text = admin_alert_text(None, KeyError("k"))
        self.assertIn("User: unknown", text)
        self.assertNotIn("Repeats", text)

    async def test_throttle_keys_tell_handlers_apart(self):
        bot = FakeBot()
        bot.fail(EditMessageText, "Bad Request: can't parse entities")
        bot.fail(EditMessageText, "Bad Request: can't parse entities")
        callback = make_callback(bot, make_message(bot, 5, "screen", from_bot=True))
        errors = []
        with self.assertLogs("app.ui.helpers", level="WARNING"):
            for call in (
                lambda: respond_with_markup(callback, "<b>broken"),
                lambda: edit_in_place(bot, CHAT_ID, 5, "<b>broken"),
            ):
                # Not assertRaises: it drops the traceback the key is built from.
                try:
                    await call()
                except TelegramBadRequest as error:
                    errors.append(error)
        self.assertEqual(len(errors), 2)
        first, second = (AlertThrottle.key(error) for error in errors)
        self.assertEqual(first[0], "TelegramBadRequest")
        self.assertEqual(first[1], "ui/helpers.py:respond_with_markup")
        self.assertEqual(second[1], "ui/helpers.py:edit_in_place")

        throttle = AlertThrottle(clock=FakeClock())
        self.assertEqual(throttle.admit(errors[0]), 0)
        self.assertEqual(throttle.admit(errors[1]), 0)
        self.assertIsNone(throttle.admit(errors[0]))

    def test_errors_outside_the_app_are_keyed_by_message(self):
        self.assertEqual(AlertThrottle.key(RuntimeError("boom")), ("RuntimeError", "boom"))


class FallbackRouterTests(RealDispatcherTestCase):
    async def test_fallback_router_is_included_last(self):
        self.assertEqual(self.dp.sub_routers, [public_router, admin_router, fallback_router])

    async def test_unknown_button_gets_a_toast_not_a_spinner(self):
        await self.feed(self.callback("nope:1"))

        answers = self.bot.answers()
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0].text, DEAD_BUTTON_TOAST)
        self.assertFalse(answers[0].show_alert)
        self.assertEqual(self.bot.of(SendMessage), [])

    async def test_non_admin_tapping_an_admin_button_gets_the_toast(self):
        await self.feed(self.callback("member:open:1"))

        self.assertEqual([answer.text for answer in self.bot.answers()], [DEAD_BUTTON_TOAST])

    async def test_handled_buttons_never_reach_the_fallback(self):
        await self.feed(self.callback("menu:close"))
        await self.feed(self.callback("dialog:cancel"))

        self.assertNotIn(DEAD_BUTTON_TOAST, [answer.text for answer in self.bot.answers()])
        self.assertEqual(len(self.bot.answers()), 2)

    async def test_free_text_from_a_guest_points_at_the_menu(self):
        await self.feed(self.message("hello?"))

        sent = self.bot.of(SendMessage)
        self.assertEqual([call.text for call in sent], [USE_THE_MENU_TEXT])
        self.assertEqual(_keyboard_texts(sent[0].reply_markup), _keyboard_texts(guest_reply_keyboard()))

    async def test_a_stale_admin_menu_tap_from_a_guest_gets_the_guest_menu(self):
        await self.feed(self.message("👥 Users"))

        sent = self.bot.of(SendMessage)
        self.assertEqual([call.text for call in sent], [USE_THE_MENU_TEXT])
        self.assertEqual(_keyboard_texts(sent[0].reply_markup), _keyboard_texts(guest_reply_keyboard()))

    async def test_free_text_from_an_admin_gets_the_admin_menu(self):
        await self.make_admin()

        await self.feed(self.message("what now"))

        sent = self.bot.of(SendMessage)
        self.assertEqual([call.text for call in sent], [USE_THE_MENU_TEXT])
        self.assertEqual(_keyboard_texts(sent[0].reply_markup), _keyboard_texts(admin_reply_keyboard()))

    async def test_free_text_from_a_member_gets_the_member_menu(self):
        friend_id = await self.db.create_friend_invite("Anna", "join_x", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(friend_id, USER_ID, "2026-11-10T10:00:00")

        await self.feed(self.message("hi"))

        sent = self.bot.of(SendMessage)
        self.assertEqual(_keyboard_texts(sent[0].reply_markup), _keyboard_texts(public_reply_keyboard()))

    async def test_unknown_command_points_at_the_menu(self):
        await self.feed(self.message("/settings"))

        self.assertEqual(self.bot.sent_texts(), [USE_THE_MENU_TEXT])

    async def test_text_in_a_state_is_never_answered_by_the_fallback(self):
        # A guest left in an admin-only state: no handler takes the text, and the catch-all stays out.
        await self.set_state(SubscriptionForm.name)

        await self.feed(self.message("Netflix"))

        self.assertEqual(self.bot.of(SendMessage), [])

    async def test_menu_handlers_are_not_swallowed(self):
        await self.make_admin()

        with mock.patch.object(admin_handlers, "send_subscription_list", mock.AsyncMock()) as subscriptions:
            await self.feed(self.message("📋 Subscriptions"))

        subscriptions.assert_awaited_once()
        self.assertNotIn(USE_THE_MENU_TEXT, self.bot.sent_texts())

    async def test_help_and_start_are_not_swallowed(self):
        await self.feed(self.message("/help"))
        await self.feed(self.message("/start"))

        texts = self.bot.sent_texts()
        self.assertEqual(len(texts), 2)
        self.assertNotIn(USE_THE_MENU_TEXT, texts)

    async def test_non_text_messages_without_a_state_are_ignored(self):
        await self.feed(self.message(""))

        self.assertEqual(self.bot.calls, [])

    async def test_group_text_is_ignored(self):
        await self.feed(self.message("hello", chat_type="group"))

        self.assertEqual(self.bot.calls, [])


class CardButtonTests(RealDispatcherTestCase):
    async def test_card_button_opens_the_member_card_as_a_new_message(self):
        await self.make_admin()
        friend_id = await self.db.create_friend_invite("Ivan Petrov", "join_y", "2099-01-01T00:00:00")
        alert = make_message(self.bot, 321, "🚨 Bot error", from_bot=True)

        await self.feed(self.callback(MemberAction(action="open_new", friend_id=friend_id).pack(), alert))

        sent = self.bot.of(SendMessage)
        self.assertEqual(len(sent), 1)
        self.assertIn("Ivan Petrov", sent[0].text)
        self.assertEqual(self.bot.of(EditMessageText), [], "the alert stays as it is")
        self.assertEqual(len(self.bot.answers()), 1)
        self.assertNotEqual(self.bot.answers()[0].text, DEAD_BUTTON_TOAST)

    async def test_card_button_for_a_deleted_user_toasts(self):
        await self.make_admin()

        await self.feed(self.callback(MemberAction(action="open_new", friend_id=999).pack()))

        self.assertEqual([answer.text for answer in self.bot.answers()], [USER_GONE])
        self.assertEqual(self.bot.of(SendMessage), [])

    async def test_a_failing_handler_is_reported_through_the_real_dispatcher(self):
        await self.make_admin()

        with mock.patch.object(members_handlers, "send_member_list", mock.AsyncMock(side_effect=RuntimeError("db gone"))):
            with self.assertLogs("app.error_handler", level="ERROR"):
                await self.feed(self.message("👥 Users"), allow_errors=True)

        texts = self.bot.sent_texts()
        self.assertIn(USER_ERROR_TEXT, texts)
        alerts = [text for text in texts if text.startswith(ADMIN_ALERT_TITLE)]
        self.assertEqual(len(alerts), 1)
        self.assertIn("RuntimeError: db gone", alerts[0])
        self.assertIn(f"tg://user?id={ADMIN_ID}", alerts[0])


class BotCommandsTests(unittest.TestCase):
    def test_commands_are_main_menu_and_help(self):
        self.assertEqual(
            [(command.command, command.description) for command in main.BOT_COMMANDS],
            [("start", "Main menu"), ("help", "Help")],
        )


if __name__ == "__main__":
    unittest.main()
