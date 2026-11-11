"""Shared UI infrastructure: menu vocabulary, nav helpers, screen/edit helpers, the help screen."""

import asyncio
import unittest

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, ReplyKeyboardMarkup

from app.handlers import admin_router, public_router
from app.handlers.admin import (
    HELP_ROLE_ADMIN,
    HELP_ROLE_GUEST,
    HELP_ROLE_MEMBER,
    handle_public_help,
    help_reply_keyboard,
    help_text,
)
from app.ui import helpers
from app.ui.helpers import (
    PROMPT_OUT_OF_DATE_TEXT,
    STALE_TEXTS,
    answer_callback_once,
    delete_quietly,
    edit_in_place,
    require_edit_subscription_id,
    respond_with_markup,
    safe_edit_text,
    stale_redirect,
)
from app.ui.keyboards import (
    BACK_BUTTON_TEXT,
    CANCEL_BUTTON_TEXT,
    CLOSE_BUTTON_TEXT,
    KEEP_BUTTON_TEXT,
    LEGACY_MENU_ALIASES,
    MENU_BUTTON_TEXTS,
    MENU_HELP,
    MENU_LABELS,
    TO_CARD_TEXT,
    admin_reply_keyboard,
    cancel_button,
    confirm_row,
    dialog_cancel_inline_button,
    guest_reply_keyboard,
    nav_button,
    nav_row,
    pair_rows,
    payment_method_label,
    public_reply_keyboard,
    radio_label,
    toggle_label,
)
from keyboard_guard import install_keyboard_guard
from telegram_fakes import (
    CHAT_ID,
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    FakeBot,
    SendMessage,
    make_callback,
    make_message,
    make_state,
)

install_keyboard_guard()


def _labels(markup: ReplyKeyboardMarkup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.keyboard]


def _dump(button: InlineKeyboardButton) -> dict:
    return button.model_dump(exclude_none=True)


# ------------------------------------------------------------- reply menus


class ReplyMenuTests(unittest.TestCase):
    def test_admin_menu(self):
        markup = admin_reply_keyboard()
        self.assertEqual(
            _labels(markup),
            [["👥 Users", "📋 Subscriptions"], ["💳 Payment", "📊 Reports"], ["⚙️ Settings", "❓ Help"]],
        )

    def test_member_menu(self):
        self.assertEqual(
            _labels(public_reply_keyboard()),
            [["👤 Account", "📋 Subscriptions"], ["📊 Reports", "⚙️ Settings"], ["❓ Help"]],
        )

    def test_guest_menu(self):
        self.assertEqual(_labels(guest_reply_keyboard()), [["❓ Help"]])

    def test_menus_are_persistent_and_resized(self):
        for builder in (admin_reply_keyboard, public_reply_keyboard, guest_reply_keyboard):
            with self.subTest(builder=builder.__name__):
                markup = builder()
                self.assertIs(markup.is_persistent, True)
                self.assertIs(markup.resize_keyboard, True)

    def test_every_menu_label_and_legacy_alias_is_a_menu_text(self):
        shown = {label for builder in (admin_reply_keyboard, public_reply_keyboard, guest_reply_keyboard)
                 for row in _labels(builder()) for label in row}
        self.assertEqual(shown, set(MENU_LABELS))
        self.assertEqual(LEGACY_MENU_ALIASES, {"💳 Payment methods", "📊 Payments report", "➕ Top up", "➕ Add subscription"})
        self.assertEqual(MENU_BUTTON_TEXTS, frozenset(MENU_LABELS) | LEGACY_MENU_ALIASES)
        self.assertIsInstance(MENU_BUTTON_TEXTS, frozenset)


class MenuHandlerTests(unittest.TestCase):
    # Plain asyncio.run: the debug loop of IsolatedAsyncioTestCase reports the
    # one-off filter warm-up as a slow callback.
    def _matching_callbacks(self, text: str) -> list[str]:
        return asyncio.run(self._match(text))

    async def _match(self, text: str) -> list[str]:
        bot = FakeBot()
        message = make_message(bot, 1, text)
        found = []
        for router in (public_router, admin_router):
            for handler in router.message.handlers:
                matched, _ = await handler.check(message, bot=bot)
                if matched:
                    found.append(handler.callback.__name__)
        return found

    def test_every_menu_text_has_a_handler(self):
        for text in sorted(MENU_BUTTON_TEXTS):
            with self.subTest(text=text):
                self.assertTrue(self._matching_callbacks(text), f"no handler for {text!r}")

    def test_help_button_and_command_open_the_help_screen(self):
        for text in (MENU_HELP, "/help"):
            with self.subTest(text=text):
                matches = self._matching_callbacks(text)
                self.assertEqual(matches[0], "handle_public_help")


# ----------------------------------------------------------------- nav helpers


class NavHelperTests(unittest.TestCase):
    def test_nav_button_names_the_exit(self):
        self.assertEqual(_dump(nav_button("sub:open:1")), {"text": BACK_BUTTON_TEXT, "callback_data": "sub:open:1"})
        self.assertEqual(_dump(nav_button("sub:open:1", TO_CARD_TEXT))["text"], "◀️ To card")
        self.assertEqual(_dump(nav_button("menu:close")), {"text": CLOSE_BUTTON_TEXT, "callback_data": "menu:close"})
        # menu:close only deletes its message, so it is "Close" whatever the caller asked for.
        self.assertEqual(nav_button("menu:close", TO_CARD_TEXT).text, "✖️ Close")

    def test_nav_row_puts_the_way_back_first(self):
        row = nav_row("sub:list", "◀️ To list", refresh_callback="sub:refresh:1")
        self.assertEqual([_dump(button) for button in row], [
            {"text": "◀️ To list", "callback_data": "sub:list"},
            {"text": "🔄 Refresh", "callback_data": "sub:refresh:1"},
        ])
        self.assertEqual(len(nav_row("sub:list")), 1)

    def test_cancel_is_plain_and_keeps_its_callback(self):
        self.assertEqual(_dump(cancel_button()), {"text": CANCEL_BUTTON_TEXT, "callback_data": "dialog:cancel"})
        self.assertEqual(_dump(dialog_cancel_inline_button()), {"text": "✖️ Cancel", "callback_data": "dialog:cancel"})
        self.assertEqual(_dump(cancel_button("topup:open:3", text=KEEP_BUTTON_TEXT)), {"text": "Keep", "callback_data": "topup:open:3"})

    def test_confirm_row(self):
        row = confirm_row("sub:open:5", "🗑 Delete", "sub:delete_yes:5")
        self.assertEqual([_dump(button) for button in row], [
            {"text": "✖️ Cancel", "callback_data": "sub:open:5"},
            {"text": "🗑 Delete", "callback_data": "sub:delete_yes:5", "style": "danger"},
        ])
        keep = confirm_row("topup:open:3", "❌ Reject", "topup:reject_yes:3", cancel_text=KEEP_BUTTON_TEXT)
        self.assertEqual([button.text for button in keep], ["Keep", "❌ Reject"])

    def test_radio_and_toggle_labels(self):
        self.assertEqual(radio_label("12:00", True), "✅ 12:00")
        self.assertEqual(radio_label("12:00", False), "12:00")
        self.assertEqual(toggle_label("📣 After due date", True), "📣 After due date · 🟢")
        self.assertEqual(toggle_label("📣 After due date", False), "📣 After due date · ⚪")

    def test_payment_method_label(self):
        self.assertEqual(payment_method_label("Sber", "rub"), "Sber · RUB")
        self.assertEqual(payment_method_label(" Sber ", "RUB", default=True), "⭐ Sber · RUB")
        self.assertEqual(payment_method_label("Sber", ""), "Sber")

    def test_pair_rows(self):
        def button(text: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(text=text, callback_data=text[-1])

        rows = pair_rows([button("💰 Pricing"), button("👥 Users"), button("🔀 Move cycle (keep payments)"),
                          button("🔔 Reminders"), button("📊 Reports"), button("🗑 Delete")])
        self.assertEqual([[b.text for b in row] for row in rows], [
            ["💰 Pricing", "👥 Users"],
            ["🔀 Move cycle (keep payments)"],
            ["🔔 Reminders", "📊 Reports"],
            ["🗑 Delete"],
        ])
        self.assertEqual(pair_rows([]), [])


# ------------------------------------------------------- screens and edits


class RespondWithMarkupTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_edits_in_place_and_answers_once_with_the_toast(self):
        bot = FakeBot()
        screen = make_message(bot, 10, "old", from_bot=True)
        callback = make_callback(bot, screen)
        shown = await respond_with_markup(callback, "👥 <b>Users</b>", toast="Updated")
        self.assertIs(shown, screen)
        self.assertEqual([(call.message_id, call.text) for call in bot.of(EditMessageText)], [(10, "👥 <b>Users</b>")])
        self.assertEqual([(a.text, a.show_alert) for a in bot.answers()], [("Updated", False)])
        # A later answer for the same tap does nothing.
        self.assertFalse(await answer_callback_once(callback, "again"))
        self.assertEqual(len(bot.answers()), 1)

    async def test_not_modified_is_fine_and_alerts_are_blocking(self):
        bot = FakeBot()
        bot.fail(EditMessageText, "Bad Request: message is not modified: specified new message content ...")
        callback = make_callback(bot, make_message(bot, 11, "same", from_bot=True))
        await respond_with_markup(callback, "same", toast="Could not send the reminder. Try again.", alert=True)
        self.assertEqual([(a.text, a.show_alert) for a in bot.answers()],
                         [("Could not send the reminder. Try again.", True)])
        self.assertEqual(bot.of(SendMessage), [])

    async def test_uneditable_message_gets_a_new_one(self):
        bot = FakeBot()
        bot.fail(EditMessageText, "Bad Request: message can't be edited")
        callback = make_callback(bot, make_message(bot, 12, "old", from_bot=True))
        shown = await respond_with_markup(callback, "📋 <b>Subscriptions</b>")
        self.assertEqual(bot.sent_texts(), ["📋 <b>Subscriptions</b>"])
        self.assertNotEqual(shown.message_id, 12)
        self.assertEqual(len(bot.answers()), 1)

    async def test_real_bad_requests_are_raised(self):
        bot = FakeBot()
        bot.fail(EditMessageText, "Bad Request: can't parse entities")
        callback = make_callback(bot, make_message(bot, 13, "old", from_bot=True))
        with self.assertRaises(TelegramBadRequest):
            await respond_with_markup(callback, "<b>broken")

    async def test_a_second_answer_after_a_pre_answer_is_tolerated(self):
        bot = FakeBot()
        callback = make_callback(bot, make_message(bot, 14, "old", from_bot=True))
        await callback.answer()  # an old call site answering by itself
        bot.fail(AnswerCallbackQuery, "Bad Request: query is too old and response timeout expired or query ID is invalid")
        await respond_with_markup(callback, "new")
        self.assertEqual(len(bot.answers()), 2)

    async def test_message_target_answers_with_any_markup(self):
        bot = FakeBot()
        typed = make_message(bot, 15, "❓ Help")
        shown = await respond_with_markup(typed, "❓ <b>Help</b>", reply_markup=admin_reply_keyboard())
        self.assertEqual(bot.sent_texts(), ["❓ <b>Help</b>"])
        self.assertIsNotNone(shown)

    async def test_long_toasts_are_cut_to_telegram_limit(self):
        bot = FakeBot()
        callback = make_callback(bot, make_message(bot, 16, "x", from_bot=True))
        await answer_callback_once(callback, "x" * 300)
        self.assertEqual(len(bot.answers()[0].text), 200)


class EditHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_in_place(self):
        bot = FakeBot()
        self.assertTrue(await edit_in_place(bot, CHAT_ID, 20, "screen"))
        bot.fail(EditMessageText, "Bad Request: message is not modified")
        # The screen already shows the text: that is success, not "gone".
        self.assertTrue(await edit_in_place(bot, CHAT_ID, 20, "screen"))
        bot.fail(EditMessageText, "Bad Request: message to edit not found")
        self.assertFalse(await edit_in_place(bot, CHAT_ID, 21, "screen"))
        bot.fail(EditMessageText, "Bad Request: message is too long")
        with self.assertRaises(TelegramBadRequest), self.assertLogs("app.ui.helpers", "WARNING"):
            await edit_in_place(bot, CHAT_ID, 20, "x" * 5000)

    async def test_safe_edit_text_falls_back_to_a_new_message(self):
        bot = FakeBot()
        message = make_message(bot, 22, "old", from_bot=True)
        self.assertIs(await safe_edit_text(message, "new"), message)
        bot.fail(EditMessageText, "Bad Request: there is no text in the message to edit")
        fresh = await safe_edit_text(message, "new")
        self.assertNotEqual(fresh.message_id, 22)
        self.assertEqual(bot.sent_texts(), ["new"])

    async def test_delete_quietly(self):
        bot = FakeBot()
        bot.fail(DeleteMessage, "Bad Request: message can't be deleted for everyone", message_id=31)
        bot.fail(DeleteMessage, "Forbidden: bot was blocked by the user", message_id=32, forbidden=True)
        bot.fail(DeleteMessage, "Bad Request: message to delete not found", message_id=33)
        await delete_quietly(bot, CHAT_ID, 30, None, 31, 30, 32, 33, 0)
        self.assertEqual(bot.deleted_ids(), [30, 31, 32, 33])
        self.assertEqual([call.message_id for call in bot.of(EditMessageReplyMarkup)], [31])

    async def test_delete_quietly_survives_a_failed_fallback(self):
        bot = FakeBot()
        bot.fail(DeleteMessage, "Bad Request: message can't be deleted")
        bot.fail(EditMessageReplyMarkup, "Bad Request: message can't be edited")
        await delete_quietly(bot, CHAT_ID, 40)


class StaleRedirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_redraw_through_respond_with_markup_carries_the_toast(self):
        bot = FakeBot()
        callback = make_callback(bot, make_message(bot, 50, "card", from_bot=True))
        await stale_redirect(callback, STALE_TEXTS.SUBSCRIPTION_GONE,
                             lambda: respond_with_markup(callback, "📋 <b>Subscriptions</b>"))
        self.assertEqual([a.text for a in bot.answers()], ["Subscription was deleted"])
        self.assertEqual(bot.of(EditMessageText)[0].text, "📋 <b>Subscriptions</b>")

    async def test_redraw_that_does_not_answer_is_answered_after(self):
        bot = FakeBot()
        callback = make_callback(bot, make_message(bot, 51, "card", from_bot=True))

        async def redraw() -> None:
            await callback.message.edit_text("👥 <b>Users</b>")

        await stale_redirect(callback, STALE_TEXTS.USER_GONE, redraw())
        self.assertEqual([a.text for a in bot.answers()], ["User was deleted"])

    def test_stale_texts(self):
        self.assertEqual(
            (STALE_TEXTS.SUBSCRIPTION_GONE, STALE_TEXTS.USER_GONE, STALE_TEXTS.METHOD_GONE,
             STALE_TEXTS.REQUEST_HANDLED, STALE_TEXTS.CYCLE_CLOSED, STALE_TEXTS.NO_ACCESS),
            ("Subscription was deleted", "User was deleted", "Payment method was deleted",
             "Request already handled", "This cycle is already closed", "You are no longer in this subscription"),
        )
        self.assertIs(helpers.SUBSCRIPTION_GONE, STALE_TEXTS.SUBSCRIPTION_GONE)


class OutOfDatePromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_subscription_id_winds_the_prompt_down(self):
        bot = FakeBot()
        state = make_state(bot)
        await state.update_data(prompt_message_id=60, cleanup_ids=[61])
        result = await require_edit_subscription_id(make_message(bot, 62, "15.99"), state)
        self.assertIsNone(result)
        self.assertEqual(bot.sent_texts(), [PROMPT_OUT_OF_DATE_TEXT])
        self.assertEqual(PROMPT_OUT_OF_DATE_TEXT, "⌛️ This prompt is out of date. Open “📋 Subscriptions” again.")
        self.assertEqual(bot.deleted_ids(), [60, 61])
        self.assertEqual(await state.get_data(), {})

    async def test_present_subscription_id_passes(self):
        bot = FakeBot()
        state = make_state(bot)
        await state.update_data(edit_subscription_id="7")
        self.assertEqual(await require_edit_subscription_id(make_message(bot, 63, "x"), state), 7)
        self.assertEqual(bot.calls, [])


# ------------------------------------------------------------------- help


class _HelpDb:
    def __init__(self, admins=(), members=()):
        self.admins, self.members = set(admins), set(members)

    async def is_admin(self, telegram_id):
        return telegram_id in self.admins

    async def get_friend_by_telegram(self, telegram_id):
        return {"id": 1} if telegram_id in self.members else None


class HelpScreenTests(unittest.IsolatedAsyncioTestCase):
    def test_help_texts(self):
        self.assertEqual(
            help_text(HELP_ROLE_ADMIN),
            "❓ <b>Help</b>\n\n"
            "👥 Users — people, balances, sign-in links\n"
            "📋 Subscriptions — plans, participants, reminders\n"
            "💳 Payment — payment methods and top-up approvals\n"
            "📊 Reports — who has paid\n"
            "⚙️ Settings — reminder time, timezone, admin notifications\n\n"
            "Start a fresh screen any time with /start.",
        )
        member = help_text(HELP_ROLE_MEMBER)
        self.assertTrue(member.startswith("❓ <b>Help</b>\n\n👤 Account — balance; “👤 Account” → “➕ Top up” to add money\n"))
        self.assertTrue(member.endswith("\n\nTap “✅ Paid” under a reminder once you have paid."))
        self.assertEqual(
            help_text(HELP_ROLE_GUEST),
            "❓ <b>Help</b>\n\nThis bot reminds a group about shared subscriptions.\nAsk the admin for your sign-in link.",
        )

    def test_help_lines_follow_the_menu_order(self):
        for role, builder in ((HELP_ROLE_ADMIN, admin_reply_keyboard), (HELP_ROLE_MEMBER, public_reply_keyboard)):
            with self.subTest(role=role):
                emojis = [label.split()[0] for row in _labels(builder()) for label in row if label != MENU_HELP]
                lines = help_text(role).split("\n\n")[1].split("\n")
                self.assertEqual([line.split()[0] for line in lines], emojis)

    async def test_each_role_gets_its_screen_and_menu(self):
        cases = (
            (_HelpDb(admins={4242}), HELP_ROLE_ADMIN),
            (_HelpDb(members={4242}), HELP_ROLE_MEMBER),
            (_HelpDb(), HELP_ROLE_GUEST),
        )
        for db, role in cases:
            with self.subTest(role=role):
                bot = FakeBot()
                await handle_public_help(make_message(bot, 70, MENU_HELP), db)
                sent = bot.of(SendMessage)
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0].text, help_text(role))
                self.assertEqual(_labels(sent[0].reply_markup), _labels(help_reply_keyboard(role)))


if __name__ == "__main__":
    unittest.main()
