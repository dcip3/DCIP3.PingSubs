"""WP7: a subscription's 💳 Payment, its 🏦 Payment method picker and the comment prompt."""

import unittest
from datetime import date

from aiogram.methods import EditMessageText, SendMessage

from app.ui.helpers import METHOD_GONE, SUBSCRIPTION_GONE
from app.ui.keyboards import (
    comment_edit_keyboard,
    comment_quick_rows,
    subscription_payment_destination_keyboard,
    subscription_payment_info_keyboard,
)
from app.ui.prompts import CANCELLED_TOAST
from app.ui.states import SubscriptionAction, SubscriptionEditForm
from app.ui.style import SEP
from app.ui.subscription_payment_screens import (
    COMMENT_TEXT_HINT,
    COMMENT_TOO_LONG_HINT,
    PaymentChoice,
    comment_lines,
    comment_prompt_text,
    method_banner,
    method_toast,
    payment_info_text,
    payment_method_text,
)
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import make_message
from test_keyboards_fit import LONG_METHOD, LONG_REQUISITES, NAME_64, assert_fits

install_keyboard_guard()

BIG_ID = 2**31 - 1

SBER = {"id": 1, "title": "Sber", "currency": "RUB", "details": "2200 1234 5678 9012\nIvan I.", "payment_link": ""}
REVOLUT = {"id": 2, "title": "Revolut", "currency": "eur", "details": "", "payment_link": "https://pay.example/r"}


def _texts(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _callbacks(markup) -> list[list[str | None]]:
    return [[button.callback_data for button in row] for row in markup.inline_keyboard]


def _styled(markup) -> list[tuple[str, str]]:
    return [(button.text, button.style) for row in markup.inline_keyboard for button in row if button.style]


def _cb(action: str, subscription_id: int) -> str:
    return SubscriptionAction(action=action, subscription_id=subscription_id).pack()


def _sub(**fields) -> dict:
    sub = {"id": 7, "name": "Netflix", "comment": "Pay by the 5th", "payment_destination_id": None}
    sub.update(fields)
    return sub


# ------------------------------------------------------------------ keyboards


class PaymentKeyboardTests(unittest.TestCase):
    def test_payment_keyboard_rows_and_unchanged_callbacks(self):
        markup = subscription_payment_info_keyboard(7)
        self.assertEqual(_texts(markup), [["🏦 Method", "📝 Comment"], ["◀️ To card"]])
        self.assertEqual(_callbacks(markup), [["sub:paymentmethod:7", "sub:comment:7"], ["sub:open:7"]])
        self.assertEqual(_styled(markup), [])
        assert_fits(subscription_payment_info_keyboard(BIG_ID))

    def test_method_picker_marks_default_and_selection_with_text(self):
        markup = subscription_payment_destination_keyboard(7, [SBER, REVOLUT], None, 1)
        self.assertEqual(
            _texts(markup),
            [["✅ Default (⭐ Sber · RUB)"], ["⭐ Sber · RUB"], ["Revolut · EUR"], ["◀️ To payment"]],
        )
        self.assertEqual(
            _callbacks(markup),
            [
                ["sub_payment_destination:7:default"],
                ["sub_payment_destination:7:1"],
                ["sub_payment_destination:7:2"],
                ["sub:paymentinfo:7"],
            ],
        )
        self.assertEqual(_styled(markup), [])
        explicit = subscription_payment_destination_keyboard(7, [SBER, REVOLUT], 2, 1)
        self.assertEqual(_texts(explicit)[:3], [["Default (⭐ Sber · RUB)"], ["⭐ Sber · RUB"], ["✅ Revolut · EUR"]])
        # Choosing the default method itself is its own choice, not "Default".
        self.assertEqual(_texts(subscription_payment_destination_keyboard(7, [SBER], 1, 1))[:2], [["Default (⭐ Sber · RUB)"], ["✅ ⭐ Sber · RUB"]])

    def test_a_deleted_own_method_falls_back_to_default(self):
        markup = subscription_payment_destination_keyboard(7, [SBER, REVOLUT], 99, None)
        self.assertEqual(_texts(markup)[0], ["✅ Default (not set)"])
        self.assertEqual(_texts(markup)[1:3], [["Sber · RUB"], ["Revolut · EUR"]])

    def test_no_methods_leaves_only_the_way_back(self):
        markup = subscription_payment_destination_keyboard(7, [], None, None)
        self.assertEqual(_texts(markup), [["◀️ To payment"]])
        self.assertEqual(_callbacks(markup), [["sub:paymentinfo:7"]])

    def test_method_picker_fits_with_worst_case_titles(self):
        methods = [
            {"id": BIG_ID - n, "title": title, "currency": "RUB"}
            for n, title in enumerate((LONG_METHOD, NAME_64, "W" * 40, "Sber"))
        ]
        for selected, default in ((None, BIG_ID), (BIG_ID - 1, BIG_ID), (BIG_ID - 2, None)):
            with self.subTest(selected=selected, default=default):
                markup = subscription_payment_destination_keyboard(BIG_ID, methods, selected, default)
                assert_fits(markup, user_texts=[m["title"] for m in methods])
                for row in markup.inline_keyboard[1:-1]:
                    self.assertTrue(row[0].text.endswith(f"{SEP}RUB"), row[0].text)
                for row in markup.inline_keyboard:
                    self.assertLess(len(row[0].callback_data.encode()), 64)
        cut = subscription_payment_destination_keyboard(7, methods, None, BIG_ID)
        # Default names the starred method with its currency; only the title is cut.
        self.assertTrue(cut.inline_keyboard[0][0].text.startswith("✅ Default (⭐ Main corp"))
        self.assertTrue(cut.inline_keyboard[0][0].text.endswith(f"…{SEP}RUB)"), cut.inline_keyboard[0][0].text)

    def test_comment_keyboard_has_no_close(self):
        with_comment = comment_edit_keyboard(BIG_ID, True)
        self.assertEqual(_texts(with_comment), [["🗑 Clear comment"], ["✖️ Cancel"]])
        self.assertEqual(_callbacks(with_comment), [[f"sub:comment_clear:{BIG_ID}"], ["dialog:cancel"]])
        self.assertNotIn("force_reply", with_comment.model_dump(exclude_none=True))
        self.assertEqual(_texts(comment_edit_keyboard(7, False)), [["✖️ Cancel"]])
        self.assertNotIn("force_reply", comment_edit_keyboard(7, False).model_dump(exclude_none=True))
        self.assertEqual(comment_quick_rows(7, False), [])
        for has_comment in (True, False):
            assert_fits(comment_edit_keyboard(BIG_ID, has_comment))


# ---------------------------------------------------------------------- texts


class PaymentTextTests(unittest.TestCase):
    def test_payment_follows_the_default_method(self):
        choice = PaymentChoice(dict(SBER), True, 1, None)
        self.assertEqual(
            payment_info_text(_sub(), choice),
            "💳 <b>Payment · Netflix</b>\n\n"
            "Payment method: ⭐ Sber · RUB (default)\n<code>2200 1234 5678 9012</code>\n<code>Ivan I.</code>\n\n"
            "📝 Comment: Pay by the 5th",
        )

    def test_payment_with_own_method_link_and_no_comment(self):
        choice = PaymentChoice(dict(REVOLUT), False, 1, 2)
        self.assertEqual(
            payment_info_text(_sub(name="A<B>", comment=""), choice),
            "💳 <b>Payment · A&lt;B&gt;</b>\n\nPayment method: Revolut · EUR\n🔗 Link: https://pay.example/r\n\n📝 Comment: none",
        )

    def test_payment_without_any_method(self):
        self.assertEqual(
            payment_info_text(_sub(comment=""), PaymentChoice(None, True, None, None)),
            "💳 <b>Payment · Netflix</b>\n\nPayment method: not set — members see no payment details.\n\n📝 Comment: none",
        )

    def test_long_requisites_and_comments_fold(self):
        details = "\n".join(f"line {n} <x>" for n in range(6))
        choice = PaymentChoice({**SBER, "details": details}, True, 1, None)
        text = payment_info_text(_sub(comment="x" * 3000), choice)
        self.assertIn(
            "<blockquote expandable><code>line 0 &lt;x&gt;</code>\n<code>line 1 &lt;x&gt;</code>", text
        )
        self.assertIn("\n\n📝 Comment\n<blockquote expandable>", text)
        self.assertLess(len(text.encode("utf-16-le")) // 2, 4096)
        # Four lines stay plain <code> lines.
        four = payment_info_text(_sub(), PaymentChoice({**SBER, "details": "a\nb\nc\nd"}, True, 1, None))
        self.assertNotIn("blockquote", four)
        self.assertEqual(comment_lines("y" * 500), [f"📝 Comment: {'y' * 500}"])
        self.assertEqual(comment_lines("y" * 501)[0], "📝 Comment")
        # Worst-case requisites keep the screen inside Telegram's limit.
        worst = PaymentChoice({**SBER, "details": "\n".join([LONG_REQUISITES] * 8), "payment_link": "h" * 500}, True, 1, None)
        self.assertLess(len(payment_info_text(_sub(name=NAME_64, comment="z " * 2000), worst).encode("utf-16-le")) // 2, 4096)

    def test_method_screen_texts(self):
        self.assertEqual(
            payment_method_text(_sub(), PaymentChoice(dict(SBER), True, 1, None), has_methods=True),
            "🏦 <b>Payment method · Netflix</b>\n\nNow: Default (⭐ Sber · RUB)",
        )
        self.assertEqual(
            payment_method_text(_sub(), PaymentChoice(dict(REVOLUT), False, 1, 2), has_methods=True),
            "🏦 <b>Payment method · Netflix</b>\n\nNow: Revolut · EUR",
        )
        self.assertEqual(
            payment_method_text(_sub(), PaymentChoice(None, True, None, None), has_methods=True),
            "🏦 <b>Payment method · Netflix</b>\n\nNow: Default (not set) — members see no payment details.",
        )
        self.assertEqual(
            payment_method_text(_sub(), PaymentChoice(None, True, None, None), has_methods=False),
            "🏦 <b>Payment method · Netflix</b>\n\nNo payment methods yet. Add one in “💳 Payment”.",
        )

    def test_banner_and_toast(self):
        own = PaymentChoice(dict(REVOLUT), False, 1, 2)
        self.assertEqual(method_banner(own), "✅ Payment method · <b>Revolut · EUR</b>")
        self.assertEqual(method_toast(own), "✅ Payment method · Revolut · EUR")
        default = PaymentChoice(dict(SBER), True, 1, None)
        self.assertEqual(method_banner(default), "✅ Payment method · <b>Default (⭐ Sber · RUB)</b>")
        self.assertEqual(method_toast(default), "✅ Payment method · Default (⭐ Sber · RUB)")

    def test_comment_prompt(self):
        self.assertEqual(
            comment_prompt_text(_sub()),
            "✏️ Comment for Netflix reminders? Now: “Pay by the 5th”.\nSend the new text.",
        )
        self.assertEqual(
            comment_prompt_text(_sub(comment="")),
            "✏️ Comment for Netflix reminders? Now: none.\nSend the new text.",
        )
        long = comment_prompt_text(_sub(comment="Pay <early>\nplease " + "x" * 200))
        self.assertNotIn("<code>", long)
        self.assertIn("Now: “Pay &lt;early&gt; please xxx", long)
        self.assertIn("…”.\nSend the new text.", long)
        self.assertLess(len(long), 200)


# ----------------------------------------------------------------------- flows


class PaymentFlowTests(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin()
        self.sub_id = await self.db.create_subscription("Netflix", 300.0, "RUB", date(2026, 12, 1), 30, None)
        await self.db.update_subscription_fields(self.sub_id, comment="Pay by the 5th")
        self.sber = await self.db.create_payment_destination(
            title="Sber", currency="RUB", details="2200 1234 5678 9012\nIvan I."
        )
        self.revolut = await self.db.create_payment_destination(
            title="Revolut", currency="EUR", details="", payment_link="https://pay.example/r"
        )
        await self.db.set_default_payment_destination_id(self.sber)
        self.screen = make_message(self.bot, 700, "payment", from_bot=True)

    def _edits(self) -> list[EditMessageText]:
        return self.bot.of(EditMessageText)

    def _toasts(self) -> list[str | None]:
        return [answer.text for answer in self.bot.answers()]

    async def _sub(self) -> dict:
        return await self.db.get_subscription(self.sub_id)

    def _assert_payment_edit(self, banner: str | None = None, message_id: int = 700) -> EditMessageText:
        edit = self._edits()[-1]
        self.assertEqual(edit.message_id, message_id)
        head = f"{banner}\n\n" if banner else ""
        self.assertTrue(edit.text.startswith(f"{head}💳 <b>Payment · Netflix</b>\n\n"), edit.text)
        self.assertEqual(_texts(edit.reply_markup), [["🏦 Method", "📝 Comment"], ["◀️ To card"]])
        return edit

    async def _open_comment_prompt(self) -> int:
        await self.feed(self.callback(_cb("comment", self.sub_id), self.screen))
        return (await self.state().get_data())["prompt_message_id"]

    # ---------------------------------------------------------------- Payment

    async def test_payment_screen(self):
        await self.feed(self.callback(_cb("paymentinfo", self.sub_id), self.screen))
        edit = self._assert_payment_edit()
        self.assertEqual(
            edit.text,
            "💳 <b>Payment · Netflix</b>\n\n"
            "Payment method: ⭐ Sber · RUB (default)\n<code>2200 1234 5678 9012</code>\n<code>Ivan I.</code>\n\n"
            "📝 Comment: Pay by the 5th",
        )
        self.assertEqual(self._toasts(), [None])

    async def test_a_deleted_subscription_redraws_the_list(self):
        await self.db.delete_subscription(self.sub_id)
        for data in (
            _cb("paymentinfo", self.sub_id),
            _cb("paymentmethod", self.sub_id),
            f"sub_payment_destination:{self.sub_id}:{self.revolut}",
            _cb("comment", self.sub_id),
            _cb("comment_clear", self.sub_id),
        ):
            with self.subTest(data=data):
                await self.feed(self.callback(data, self.screen))
                self.assertEqual(self._toasts()[-1], SUBSCRIPTION_GONE)
                self.assertTrue(self._edits()[-1].text.startswith("📋 <b>Subscriptions</b>"))
        self.assertIsNone(await self.state().get_state())

    # --------------------------------------------------------- Payment method

    async def test_method_picker_choices_return_to_payment(self):
        await self.feed(self.callback(_cb("paymentmethod", self.sub_id), self.screen))
        menu = self._edits()[-1]
        self.assertEqual(menu.text, "🏦 <b>Payment method · Netflix</b>\n\nNow: Default (⭐ Sber · RUB)")
        self.assertEqual(
            _texts(menu.reply_markup),
            [["✅ Default (⭐ Sber · RUB)"], ["Revolut · EUR"], ["⭐ Sber · RUB"], ["◀️ To payment"]],  # by title
        )

        await self.feed(self.callback(f"sub_payment_destination:{self.sub_id}:{self.revolut}", self.screen))
        self.assertEqual((await self._sub())["payment_destination_id"], self.revolut)
        edit = self._assert_payment_edit("✅ Payment method · <b>Revolut · EUR</b>")
        self.assertIn("\nPayment method: Revolut · EUR\n🔗 Link: https://pay.example/r\n", edit.text)
        self.assertEqual(self._toasts()[-1], "✅ Payment method · Revolut · EUR")

        await self.feed(self.callback(_cb("paymentmethod", self.sub_id), self.screen))
        self.assertEqual(_texts(self._edits()[-1].reply_markup)[:2], [["Default (⭐ Sber · RUB)"], ["✅ Revolut · EUR"]])

        await self.feed(self.callback(f"sub_payment_destination:{self.sub_id}:default", self.screen))
        self.assertIsNone((await self._sub())["payment_destination_id"])
        self._assert_payment_edit("✅ Payment method · <b>Default (⭐ Sber · RUB)</b>")
        self.assertEqual(self._toasts()[-1], "✅ Payment method · Default (⭐ Sber · RUB)")
        self.assertNotIn("Payment method updated.", [t for t in self._toasts() if t])

    async def test_a_deleted_method_redraws_the_picker(self):
        await self.db.delete_payment_destination(self.revolut)
        await self.feed(self.callback(f"sub_payment_destination:{self.sub_id}:{self.revolut}", self.screen))
        self.assertEqual(self._toasts()[-1], METHOD_GONE)
        self.assertTrue(self._edits()[-1].text.startswith("🏦 <b>Payment method · Netflix</b>"))
        self.assertIsNone((await self._sub())["payment_destination_id"])

    async def test_a_broken_choice_redraws_payment_with_a_toast(self):
        await self.feed(self.callback(f"sub_payment_destination:{self.sub_id}:abc", self.screen))
        answer = self.bot.answers()[-1]
        self.assertEqual((answer.text, answer.show_alert), ("This button no longer works", False))
        self.assertEqual(len(self.bot.answers()), 1)
        self.assertTrue(self._edits()[-1].text.startswith("💳 <b>Payment · Netflix</b>"))
        self.bot.calls.clear()
        await self.feed(self.callback("sub_payment_destination:x:abc", self.screen))
        self.assertEqual([(a.text, a.show_alert) for a in self.bot.answers()], [("This button no longer works", False)])
        self.assertEqual(self._edits(), [])

    async def test_no_methods_configured(self):
        await self.db.delete_payment_destination(self.sber)
        await self.db.delete_payment_destination(self.revolut)
        await self.feed(self.callback(_cb("paymentmethod", self.sub_id), self.screen))
        menu = self._edits()[-1]
        self.assertEqual(
            menu.text, "🏦 <b>Payment method · Netflix</b>\n\nNo payment methods yet. Add one in “💳 Payment”."
        )
        self.assertEqual(_texts(menu.reply_markup), [["◀️ To payment"]])
        await self.feed(self.callback(_cb("paymentinfo", self.sub_id), self.screen))
        self.assertIn("\nPayment method: not set — members see no payment details.\n", self._edits()[-1].text)

    # ---------------------------------------------------------------- Comment

    async def test_comment_prompt_and_typed_success_edit_payment(self):
        prompt_id = await self._open_comment_prompt()
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, "✏️ Comment for Netflix reminders? Now: “Pay by the 5th”.\nSend the new text.")
        self.assertEqual(_texts(prompt.reply_markup), [["🗑 Clear comment"], ["✖️ Cancel"]])
        self.assertNotIn("force_reply", prompt.reply_markup.model_dump(exclude_none=True))
        data = await self.state().get_data()
        self.assertEqual(data["origin_screen"], f"payment:{self.sub_id}")
        self.assertEqual(await self.state().get_state(), SubscriptionEditForm.comment.state)

        too_long = self.message("x" * 1001)
        await self.feed(too_long)
        self.assertEqual(self.bot.sent_texts()[-1], COMMENT_TOO_LONG_HINT)
        answer = self.message("Pay by the 10th <please>")
        await self.feed(answer)
        self.assertEqual((await self._sub())["comment"], "Pay by the 10th <please>")
        edit = self._assert_payment_edit("✅ Comment saved")
        self.assertTrue(edit.text.endswith("\n\n📝 Comment: Pay by the 10th &lt;please&gt;"), edit.text)
        for message_id in (prompt_id, too_long.message_id, answer.message_id):
            self.assertIn(message_id, self.bot.deleted_ids())
        self.assertIsNone(await self.state().get_state())
        self.assertNotIn("Comment updated.", self.bot.sent_texts())

    async def test_without_a_comment_there_is_no_clear_button(self):
        await self.db.update_subscription_fields(self.sub_id, comment="")
        await self._open_comment_prompt()
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, "✏️ Comment for Netflix reminders? Now: none.\nSend the new text.")
        self.assertEqual(_texts(prompt.reply_markup), [["✖️ Cancel"]])

    async def test_typed_clear_words_clear_the_comment(self):
        for word in ("clear", "None", "-"):
            with self.subTest(word=word):
                await self.db.update_subscription_fields(self.sub_id, comment="Pay by the 5th")
                await self._open_comment_prompt()
                await self.feed(self.message(word))
                self.assertEqual((await self._sub())["comment"], "")
                self._assert_payment_edit("✅ Comment cleared")

    async def test_a_sticker_like_message_gets_the_text_hint(self):
        await self._open_comment_prompt()
        await self.feed(self.message(""))
        self.assertEqual(self.bot.sent_texts()[-1], COMMENT_TEXT_HINT)
        self.assertEqual((await self._sub())["comment"], "Pay by the 5th")
        self.assertEqual(await self.state().get_state(), SubscriptionEditForm.comment.state)

    async def test_clear_button_on_the_prompt_ends_it_and_edits_payment(self):
        prompt_id = await self._open_comment_prompt()
        prompt = make_message(self.bot, prompt_id, "prompt", from_bot=True)
        await self.feed(self.callback(_cb("comment_clear", self.sub_id), prompt))
        self.assertEqual((await self._sub())["comment"], "")
        edit = self._assert_payment_edit("✅ Comment cleared")
        self.assertTrue(edit.text.endswith("📝 Comment: none"))
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertEqual(self._toasts()[-1], "✅ Comment cleared")
        self.assertIsNone(await self.state().get_state())

    async def test_clear_button_on_an_old_prompt_turns_it_into_payment(self):
        old_prompt = make_message(self.bot, 777, "old prompt", from_bot=True)
        await self.feed(self.callback(_cb("comment_clear", self.sub_id), old_prompt))
        self.assertEqual((await self._sub())["comment"], "")
        self._assert_payment_edit("✅ Comment cleared", message_id=777)
        self.assertEqual(self._toasts()[-1], "✅ Comment cleared")

    async def test_cancel_disarms_the_prompt(self):
        prompt_id = await self._open_comment_prompt()
        prompt = make_message(self.bot, prompt_id, "prompt", from_bot=True)
        await self.feed(self.callback("dialog:cancel", prompt))
        self.assertIsNone(await self.state().get_state())
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertEqual(self._toasts()[-1], CANCELLED_TOAST)
        await self.feed(self.message("this is not a comment"))
        self.assertEqual((await self._sub())["comment"], "Pay by the 5th")

    async def test_a_menu_tap_leaves_the_comment_prompt(self):
        prompt_id = await self._open_comment_prompt()
        await self.feed(self.message("📋 Subscriptions"))
        self.assertEqual((await self._sub())["comment"], "Pay by the 5th")
        self.assertIsNone(await self.state().get_state())
        self.assertIn(prompt_id, self.bot.deleted_ids())

    async def test_a_comment_answered_after_the_subscription_was_deleted(self):
        await self._open_comment_prompt()
        await self.db.delete_subscription(self.sub_id)
        await self.feed(self.message("Pay later"))
        self.assertTrue(self.bot.sent_texts()[-1].startswith("⚠️ That subscription was deleted\n\n📋 <b>Subscriptions</b>"))
        self.assertIsNone(await self.state().get_state())


if __name__ == "__main__":
    unittest.main()
