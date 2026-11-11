"""WP14 top-up flow and approvals: the member's picker, amount prompt, request
screen and decision pushes; the admin's review push, reject question and the
📥 Pending top-ups list — texts, keyboards and flows."""

import unittest
from datetime import datetime, timezone
from unittest import mock

from aiogram.methods import EditMessageReplyMarkup
from aiogram.types import InlineKeyboardMarkup

from app.handlers.topups import TOPUP_DESTINATION_ID, TOPUP_FRIEND_ID, parse_topup_amount
from app.ui.guards import ALREADY_RUNNING_TEXT, guarded
from app.ui.keyboards import (
    PENDING_TOPUPS_BUTTONS_MAX,
    topup_approved_push_keyboard,
    topup_destination_keyboard,
    topup_pending_button_label,
    topup_pending_requests_keyboard,
    topup_reject_confirm_keyboard,
    topup_request_review_keyboard,
    topup_request_submit_keyboard,
)
from app.ui.prompts import ORIGIN_MESSAGE_ID, ORIGIN_SCREEN, PROMPT_MESSAGE_ID
from app.ui.states import TopUpForm
from app.ui.topup_screens import (
    AMOUNT_HINT,
    CREATE_FAILED_TEXT,
    TopUpPicker,
    amount_prompt_text,
    approved_push_text,
    decided_banner,
    method_mismatch_toast,
    pending_list_text,
    picker_methods,
    reject_confirm_text,
    rejected_push_text,
    review_final_text,
    review_text,
    submitted_text,
    topup_picker_text,
    topup_request_text,
)
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import USER_ID, EditMessageText, SendMessage, make_message
from test_keyboards_fit import AMOUNT, LONG_METHOD, LONG_NAME, LONG_REQUISITES, NAME_64, USER_TEXTS, assert_fits

install_keyboard_guard()

NBSP = " "
CARD_DETAILS = "Card 2200 1234 5678 9012\nT-Bank, Anna P."
PHONE_DETAILS = "Phone +7 900 000-00-00 (SBP)"
MEMBER_TG = 555
SENT_AT = datetime(2026, 11, 10, 9, 5, tzinfo=timezone.utc)
UNIX = int(SENT_AT.timestamp())


def money(text: str) -> str:
    """'1 000.00 RUB' with the NBSPs format_money uses."""
    return text.replace(" ", NBSP)


def _texts(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _callbacks(markup) -> list[list[str]]:
    return [[button.callback_data for button in row] for row in markup.inline_keyboard]


def _request(**overrides) -> dict:
    request = {
        "id": 42,
        "friend_id": 3,
        "amount": 1000.0,
        "currency": "RUB",
        "destination_title": "Sber",
        "destination_details": PHONE_DETAILS,
        "destination_link": "",
        "status": "pending",
        "created_at": "2026-11-10 09:00:00",
        "submitted_at": "2026-11-10 09:05:00",
        "full_name": "Ivan Petrov",
        "telegram_id": 123,
        "balance": 120.0,
        "balance_currency": "RUB",
    }
    request.update(overrides)
    return request


def _methods(*specs: tuple[str, str]) -> list[dict]:
    return [{"id": index + 1, "title": title, "currency": currency} for index, (title, currency) in enumerate(specs)]


# ----------------------------------------------------------------- keyboards


class TopUpKeyboardFitTests(unittest.TestCase):
    """Every top-up builder fits a phone in every branch, with worst-case data."""

    TITLES = (LONG_METHOD, NAME_64, "W" * 64, LONG_NAME)

    def test_picker_every_branch(self):
        methods = _methods(*((title, "RUB") for title in self.TITLES))
        for destinations in ([], methods[:1], methods):
            for close in (False, True):
                with self.subTest(count=len(destinations), close=close):
                    assert_fits(topup_destination_keyboard(destinations, close=close), USER_TEXTS + self.TITLES)

    def test_picker_layout_and_navigation(self):
        markup = topup_destination_keyboard(_methods(("Sber", "RUB"), ("Revolut", "RUB")), 1)
        self.assertEqual(_texts(markup), [["Sber · RUB"], ["Revolut · RUB"], ["◀️ Back"]])
        self.assertEqual(_callbacks(markup), [["topup_destination:1"], ["topup_destination:2"], ["public_account:show"]])
        spawned = topup_destination_keyboard(_methods(("Sber", "RUB")), close=True)
        self.assertEqual(_texts(spawned)[-1], ["✖️ Close"])
        self.assertEqual(_callbacks(spawned)[-1], ["menu:close"])
        long = topup_destination_keyboard(_methods((LONG_METHOD * 2, "RUB")))
        self.assertTrue(long.inline_keyboard[0][0].text.endswith("… · RUB"), long.inline_keyboard[0][0].text)

    def test_request_keyboard(self):
        for details, link in ((LONG_REQUISITES, "https://pay.example/x"), (PHONE_DETAILS, ""), ("", "not a link")):
            with self.subTest(details=details[:10], link=link):
                assert_fits(topup_request_submit_keyboard(2_000_000_000, details, link))
        markup = topup_request_submit_keyboard(5, CARD_DETAILS, "https://pay.example/t")
        self.assertEqual(
            _texts(markup), [["📋 Copy card number ···9012"], ["🔗 Open payment link"], ["✅ I paid"], ["✖️ Cancel"]]
        )
        self.assertEqual(markup.inline_keyboard[2][0].style, "success")
        self.assertIsNone(markup.inline_keyboard[3][0].style)
        self.assertEqual(markup.inline_keyboard[3][0].callback_data, "topup:cancel:5")

    def test_pending_list_keyboard(self):
        now = datetime(2026, 11, 10, 11, 5, tzinfo=timezone.utc)
        requests = [
            _request(id=index + 1, full_name=name, amount=amount)
            for index, (name, amount) in enumerate(((NAME_64, AMOUNT), ("W" * 64, 99999.99), ("Ivan Petrov", 1000.0)))
        ]
        markup = topup_pending_requests_keyboard(requests, now)
        assert_fits(markup, USER_TEXTS + ("W" * 64,))
        self.assertEqual(markup.inline_keyboard[2][0].text, f"Ivan Petrov · {money('1 000 RUB')} · 2h")
        self.assertTrue(markup.inline_keyboard[0][0].text.endswith(f"… · {money('12 500 RUB')} · 2h"))
        self.assertTrue(markup.inline_keyboard[1][0].text.endswith(f" · {money('99 999.99 RUB')} · 2h"))
        self.assertEqual(_texts(markup)[-1], ["◀️ Back"])
        self.assertEqual(_callbacks(markup)[-1], ["paydest:menu:0"])
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "topup:admin_open:1")
        assert_fits(topup_pending_requests_keyboard([]))
        many = topup_pending_requests_keyboard([_request(id=index) for index in range(1, 60)])
        self.assertEqual(len(many.inline_keyboard), PENDING_TOPUPS_BUTTONS_MAX + 1)

    def test_pending_label_without_time_or_name(self):
        self.assertEqual(
            topup_pending_button_label({"id": 1, "friend_id": 9, "amount": 12.5, "currency": "usd"}),
            f"User 9 · {money('12.50 USD')}",
        )

    def test_review_keyboard_every_branch(self):
        for from_list in (False, True):
            for can_approve in (True, False):
                for friend_id in (None, 2_000_000_000):
                    with self.subTest(from_list=from_list, can_approve=can_approve, friend_id=friend_id):
                        markup = topup_request_review_keyboard(
                            2_000_000_000, friend_id=friend_id, from_list=from_list, can_approve=can_approve
                        )
                        assert_fits(markup)
                        for row in markup.inline_keyboard:
                            for button in row:
                                self.assertLessEqual(len(button.callback_data.encode()), 64)

    def test_review_keyboard_layouts(self):
        push = topup_request_review_keyboard(7, friend_id=3)
        self.assertEqual(_texts(push), [["✅ Approve", "❌ Reject"], ["👤 Card"]])
        self.assertEqual(_callbacks(push), [["topup:approve:7", "topup:reject:7"], ["member:open_new:3"]])
        self.assertEqual([button.style for button in push.inline_keyboard[0]], ["success", None])
        listed = topup_request_review_keyboard(7, friend_id=3, from_list=True)
        self.assertEqual(_texts(listed), [["✅ Approve", "❌ Reject"], ["◀️ To pending"]])
        self.assertEqual(_callbacks(listed)[-1], ["topup:admin_list:0"])
        blocked = topup_request_review_keyboard(7, friend_id=3, can_approve=False)
        self.assertEqual(_texts(blocked), [["❌ Reject"], ["👤 Card"]])

    def test_reject_question_and_push_keyboards(self):
        push = topup_reject_confirm_keyboard(7)
        self.assertEqual(_texts(push), [["Keep", "❌ Reject"]])
        self.assertEqual(_callbacks(push), [["topup:review:7", "topup:reject_yes:7"]])
        self.assertEqual(push.inline_keyboard[0][1].style, "danger")
        self.assertEqual(_callbacks(topup_reject_confirm_keyboard(7, from_list=True))[0][0], "topup:admin_open:7")
        account = topup_approved_push_keyboard()
        self.assertEqual(_texts(account), [["👤 Account"]])
        self.assertEqual(_callbacks(account), [["public_account:show_new"]])
        for markup in (push, topup_reject_confirm_keyboard(2_000_000_000, from_list=True), account):
            assert_fits(markup)


# --------------------------------------------------------------------- texts


class TopUpTextTests(unittest.TestCase):
    def test_picker_texts(self):
        friend = {"id": 3}
        methods = tuple(_methods(("Sber", "RUB")))
        self.assertEqual(
            topup_picker_text(TopUpPicker(friend, 350.0, "RUB", methods, True)),
            f"➕ <b>Top up</b>\n\nBalance: <b>{money('350.00 RUB')}</b>\nPick where you will send the money.",
        )
        self.assertEqual(
            topup_picker_text(TopUpPicker(friend, 350.0, "RUB", (), True)),
            f"➕ <b>Top up</b>\n\nBalance: <b>{money('350.00 RUB')}</b>\nNo payment method in RUB yet. Ask the admin.",
        )
        self.assertEqual(
            topup_picker_text(TopUpPicker(friend, 0.0, "RUB", (), False)),
            f"➕ <b>Top up</b>\n\nBalance: <b>{money('0.00 RUB')}</b>\nNo payment methods yet. Ask the admin.",
        )

    def test_picker_offers_only_the_balance_currency_and_puts_the_own_method_first(self):
        destinations = _methods(("Sber", "RUB"), ("Revolut", "EUR"), ("Tinkoff", "RUB"))
        methods, filtered = picker_methods(destinations, balance=350.0, currency="RUB", preferred_id=3)
        self.assertTrue(filtered)
        self.assertEqual([item["title"] for item in methods], ["Tinkoff", "Sber"])
        # An empty balance takes any currency (approval switches it), so every method is offered.
        methods, filtered = picker_methods(destinations, balance=0.0, currency="RUB", preferred_id=None)
        self.assertFalse(filtered)
        self.assertEqual(len(methods), 3)
        self.assertEqual(method_mismatch_toast(destinations[1], "RUB"), "Revolut takes EUR; your balance is RUB")

    def test_amount_prompt_and_parsing(self):
        self.assertEqual(amount_prompt_text("RUB"), "✏️ How much to top up, RUB? For example <code>1000</code>.")
        self.assertEqual(parse_topup_amount("1 000"), (1000.0, False))
        self.assertEqual(parse_topup_amount("149,99"), (149.99, False))
        self.assertEqual(parse_topup_amount("10.0051"), (10.01, True))
        # '.' + three digits groups thousands, exactly like ',': a RUB user's
        # '1.000' is a thousand, never one ruble.
        self.assertEqual(parse_topup_amount("1.000"), (1000.0, False))
        self.assertEqual(parse_topup_amount("1,000"), (1000.0, False))
        self.assertEqual(parse_topup_amount("10.005"), (10005.0, False))
        for bad in ("0", "-5", "+5", "1e5", "abc", "", "0.001"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_topup_amount(bad))

    def test_request_text_folds_copyable_details(self):
        text = topup_request_text(_request(destination_details=CARD_DETAILS, destination_link="https://pay.example"))
        self.assertEqual(
            text,
            f"💳 <b>Top-up · {money('1 000.00 RUB')}</b>\n\n"
            "Send to Sber (RUB):\n"
            "<blockquote expandable><code>Card 2200 1234 5678 9012</code>\n<code>T-Bank, Anna P.</code></blockquote>\n\n"
            "Then tap “✅ I paid”.",
        )

    def test_request_text_keeps_pre_and_shows_a_link_it_cannot_button(self):
        text = topup_request_text(_request(destination_link="bank app: Sber"), banner="⚠️ Rounded to cents · <b>x</b>")
        self.assertEqual(
            text,
            "⚠️ Rounded to cents · <b>x</b>\n\n"
            f"💳 <b>Top-up · {money('1 000.00 RUB')}</b>\n\n"
            "Send to Sber (RUB):\n"
            f"<pre>{PHONE_DETAILS}</pre>\n"
            "🔗 Link: bank app: Sber\n\n"
            "Then tap “✅ I paid”.",
        )
        self.assertNotIn("TOPUP-", text)
        self.assertNotIn("Status", text)

    def test_request_text_stays_short_with_huge_details(self):
        text = topup_request_text(_request(destination_details="x" * 9000))
        self.assertLess(len(text), 4096)

    def test_submitted_text(self):
        self.assertEqual(
            submitted_text(_request(), "Europe/Moscow"),
            f"⏳ <b>Top-up sent for approval · {money('1 000.00 RUB')}</b>\n\n"
            f'Sber · sent <tg-time unix="{UNIX}" format="t">12:05</tg-time>.\n'
            "You will get a message when the admin confirms.",
        )

    def test_member_pushes(self):
        self.assertEqual(
            approved_push_text(_request(), 1350.0, "RUB"),
            f"✅ <b>Top-up approved · {money('1 000.00 RUB')}</b>\n\nBalance: <b>{money('1 350.00 RUB')}</b>",
        )
        self.assertEqual(
            rejected_push_text(_request()),
            f"❌ <b>Top-up rejected · {money('1 000.00 RUB')}</b>\n\n"
            "Sber. If you did send the money, contact the admin.",
        )

    def test_review_text(self):
        with mock.patch("app.ui.topup_screens.format_relative_past", return_value="2 hours ago"):
            text = review_text(_request(), tz_name="Europe/Moscow", more_waiting=2)
        self.assertEqual(
            text,
            f"📥 <b>Top-up request · {money('1 000.00 RUB')}</b>\n\n"
            'From: <a href="tg://user?id=123">Ivan Petrov</a> · <code>123</code>\n'
            "Payment method: Sber · RUB\n"
            f"Balance: {money('120.00 RUB')} → <b>{money('1 120.00 RUB')}</b> after approval\n"
            f'Sent: <tg-time unix="{UNIX}" format="wdt">Tue, 10.11.2026 12:05</tg-time> '
            f'(<tg-time unix="{UNIX}" format="r">2 hours ago</tg-time>)\n\n'
            "📥 <b>2</b> more waiting",
        )
        self.assertNotIn(PHONE_DETAILS, text)

    def test_review_text_with_a_currency_mismatch(self):
        text = review_text(_request(balance_currency="EUR", submitted_at=None, created_at=None), tz_name="UTC")
        self.assertEqual(
            text,
            f"📥 <b>Top-up request · {money('1 000.00 RUB')}</b>\n\n"
            'From: <a href="tg://user?id=123">Ivan Petrov</a> · <code>123</code>\n'
            "Payment method: Sber · RUB\n"
            f"Balance: {money('120.00 EUR')}\n\n"
            "⚠️ Ivan's balance is in EUR — fix it in “👥 Users” → Ivan Petrov → “💰 Balance” first.",
        )
        # An empty balance takes the top-up's currency: no warning.
        self.assertNotIn("⚠️", review_text(_request(balance=0.0, balance_currency="EUR"), tz_name="UTC"))

    def test_reject_question_and_final_states(self):
        self.assertEqual(
            reject_confirm_text(_request()),
            f"⚠️ <b>Reject {money('1 000.00 RUB')} from Ivan?</b>\nIvan is told the top-up was rejected.",
        )
        approved = _request(status="approved", balance=1120.0)
        # The decided push keeps the member link and says who decided (principle close-the-done-screen).
        self.assertEqual(
            review_final_text(approved, "RUB"),
            f"✅ <b>Top-up approved · {money('1 000.00 RUB')}</b> · by you\n\n"
            '<a href="tg://user?id=123">Ivan Petrov</a> · <code>123</code>\n'
            f"Balance now: <b>{money('1 120.00 RUB')}</b>",
        )
        self.assertEqual(
            review_final_text(_request(status="rejected"), "RUB", decided_by="Boris"),
            f"❌ <b>Top-up rejected · {money('1 000.00 RUB')}</b> · by Boris\n\n"
            '<a href="tg://user?id=123">Ivan Petrov</a> · <code>123</code>',
        )
        self.assertEqual(
            decided_banner(approved), f"✅ Top-up approved · <b>{money('1 000.00 RUB')}</b> · Ivan Petrov"
        )

    def test_pending_list_text(self):
        self.assertEqual(
            pending_list_text([_request(), _request(id=43)]),
            "📥 <b>Pending top-ups</b> · 2\n\nTap a request to approve or reject it.",
        )
        self.assertEqual(pending_list_text([]), "📥 <b>Pending top-ups</b>\n\nNo top-ups waiting.")
        self.assertEqual(
            pending_list_text([], banner="✅ Top-up approved · <b>x</b>"),
            "✅ Top-up approved · <b>x</b>\n\n📥 <b>Pending top-ups</b>\n\nNo top-ups waiting.",
        )


# -------------------------------------------------------------------- flows


def _with_markup(message, markup):
    return message.model_copy(update={"reply_markup": markup}).as_(message.bot)


class _TopUpCase(RealDispatcherTestCase):
    def _screen(self, message_id: int = 7001, markup: InlineKeyboardMarkup | None = None, text: str = "screen"):
        message = make_message(self.bot, message_id, text, from_bot=True)
        return _with_markup(message, markup) if markup is not None else message

    def _single_answer(self):
        answers = self.bot.answers()
        self.assertEqual(len(answers), 1, [answer.text for answer in answers])
        return answers[0]

    async def _method(self, title: str = "Sber", currency: str = "RUB", details: str = CARD_DETAILS) -> int:
        return await self.db.create_payment_destination(title=title, currency=currency, details=details)


class MemberTopUpFlowTests(_TopUpCase):
    """The member (USER_ID) tops up; admin 1 gets the review push."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin(1)
        self.friend_id = await self.db.create_friend_invite("Anna Petrova", "join_anna", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(self.friend_id, USER_ID, "2026-11-01T00:00:00")
        await self.db.set_friend_balance(self.friend_id, 350.0)
        await self.db.set_friend_balance_currency(self.friend_id, "RUB")

    async def test_account_top_up_edits_the_account_into_the_picker(self):
        await self._method("Sber")
        own = await self._method("Tinkoff")
        await self._method("Revolut", "EUR")
        await self.db.set_user_payment_destination_id(USER_ID, own)

        await self.feed(self.callback("topup:start:0", self._screen()))

        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7001)
        self.assertEqual(
            edit.text, f"➕ <b>Top up</b>\n\nBalance: <b>{money('350.00 RUB')}</b>\nPick where you will send the money."
        )
        self.assertEqual(_texts(edit.reply_markup), [["Tinkoff · RUB"], ["Sber · RUB"], ["◀️ Back"]])
        self._single_answer()

    async def test_one_method_skips_the_picker(self):
        destination = await self._method("Sber")
        await self._method("Revolut", "EUR")

        await self.feed(self.callback("topup:start:0", self._screen()))

        self.assertEqual(self.bot.of(EditMessageText), [])
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, "✏️ How much to top up, RUB? For example <code>1000</code>.")
        self.assertEqual(_texts(prompt.reply_markup), [["✖️ Cancel"]])
        state = self.state()
        self.assertEqual(await state.get_state(), TopUpForm.amount.state)
        data = await state.get_data()
        self.assertEqual((data[TOPUP_DESTINATION_ID], data[TOPUP_FRIEND_ID]), (destination, self.friend_id))
        self.assertEqual((data[ORIGIN_MESSAGE_ID], data[ORIGIN_SCREEN]), (7001, "topup"))
        self._single_answer()

    async def test_no_method_in_the_balance_currency(self):
        await self._method("Revolut", "EUR")
        await self.feed(self.callback("topup:start:0", self._screen()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertTrue(edit.text.endswith("No payment method in RUB yet. Ask the admin."), edit.text)
        self.assertEqual(_texts(edit.reply_markup), [["◀️ Back"]])

    async def test_push_button_opens_the_picker_as_a_new_message(self):
        await self._method("Sber")
        await self._method("Tinkoff")
        await self.feed(self.callback("topup:start_new:0", self._screen()))
        self.assertEqual(self.bot.of(EditMessageText), [])
        sent = self.bot.of(SendMessage)[-1]
        self.assertTrue(sent.text.startswith("➕ <b>Top up</b>"))
        self.assertEqual(_texts(sent.reply_markup)[-1], ["✖️ Close"])
        self._single_answer()

    async def test_stale_picker_buttons_redraw_the_picker(self):
        await self._method("Sber")
        await self._method("Tinkoff")
        euro = await self._method("Revolut", "EUR")
        await self.feed(self.callback(f"topup_destination:{euro}", self._screen()))
        self.assertEqual(self._single_answer().text, "Revolut takes EUR; your balance is RUB")
        self.assertEqual(_texts(self.bot.of(EditMessageText)[-1].reply_markup)[-1], ["◀️ Back"])

        self.bot.calls.clear()
        await self.feed(self.callback("topup_destination:999", self._screen()))
        self.assertEqual(self._single_answer().text, "Payment method was deleted")
        self.assertIsNone(await self.state().get_state())

    async def test_amount_flow_turns_the_origin_into_the_request(self):
        destination = await self._method("Sber")
        await self._method("Tinkoff")
        await self.feed(self.callback(f"topup_destination:{destination}", self._screen(7002)))
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]

        typo = self.message("lots")
        await self.feed(typo)
        self.assertEqual(self.bot.sent_texts()[-1], AMOUNT_HINT)
        self.assertEqual(await self.state().get_state(), TopUpForm.amount.state)

        answer = self.message("1000.555")
        await self.feed(answer)

        self.assertIsNone(await self.state().get_state())
        self.assertTrue({prompt_id, typo.message_id, answer.message_id} <= set(self.bot.deleted_ids()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7002)
        self.assertEqual(
            edit.text,
            f"⚠️ Rounded to cents · <b>{money('1 000.56 RUB')}</b>\n\n"
            f"💳 <b>Top-up · {money('1 000.56 RUB')}</b>\n\n"
            "Send to Sber (RUB):\n"
            "<blockquote expandable><code>Card 2200 1234 5678 9012</code>\n<code>T-Bank, Anna P.</code></blockquote>\n\n"
            "Then tap “✅ I paid”.",
        )
        self.assertEqual(_texts(edit.reply_markup), [["📋 Copy card number ···9012"], ["✅ I paid"], ["✖️ Cancel"]])
        requests = await self.db.list_pending_topup_requests()
        self.assertEqual(requests, [])  # created, not sent yet

    async def test_fatal_error_keeps_the_screen(self):
        destination = await self._method("Sber")
        await self._method("Tinkoff")
        await self.feed(self.callback(f"topup_destination:{destination}", self._screen(7002)))
        await self.db.delete_payment_destination(destination)
        edits_before = len(self.bot.of(EditMessageText))

        await self.feed(self.message("1000"))

        self.assertEqual(self.bot.sent_texts()[-1], CREATE_FAILED_TEXT)
        self.assertEqual(len(self.bot.of(EditMessageText)), edits_before)
        self.assertIsNone(await self.state().get_state())

    async def _request_screen(self) -> tuple[int, object]:
        destination = await self._method("Sber")
        request_id = await self.db.create_topup_request(
            friend_id=self.friend_id,
            destination_id=destination,
            amount=1000.0,
            currency="RUB",
            destination_title="Sber",
            destination_details=CARD_DETAILS,
        )
        markup = topup_request_submit_keyboard(request_id, CARD_DETAILS)
        return request_id, self._screen(7003, markup)

    async def test_cancel_deletes_the_request_screen(self):
        request_id, screen = await self._request_screen()
        await self.feed(self.callback(f"topup:cancel:{request_id}", screen))
        self.assertIn(7003, self.bot.deleted_ids())
        self.assertEqual(self._single_answer().text, "Top-up cancelled")
        self.assertEqual((await self.db.get_topup_request(request_id))["status"], "new")

    async def test_i_paid_closes_the_screen_and_alerts_the_admin(self):
        request_id, screen = await self._request_screen()
        await self.feed(self.callback(f"topup:submit:{request_id}", screen))

        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7003)
        self.assertTrue(
            edit.text.startswith(f"⏳ <b>Top-up sent for approval · {money('1 000.00 RUB')}</b>\n\nSber · sent <tg-time"),
            edit.text,
        )
        self.assertTrue(edit.text.endswith("You will get a message when the admin confirms."))
        self.assertIsNone(edit.reply_markup)
        self.assertEqual(self._single_answer().text, "⏳ Sent for approval")
        push = [call for call in self.bot.of(SendMessage) if int(call.chat_id) == 1][-1]
        self.assertTrue(push.text.startswith(f"📥 <b>Top-up request · {money('1 000.00 RUB')}</b>\n\n"), push.text)
        self.assertNotIn("2200", push.text)
        self.assertEqual(_texts(push.reply_markup), [["✅ Approve", "❌ Reject"], ["👤 Card"]])
        self.assertEqual(push.reply_markup.inline_keyboard[1][0].callback_data, f"member:open_new:{self.friend_id}")

        # A second tap on an old copy shows where it stands.
        self.bot.calls.clear()
        await self.feed(self.callback(f"topup:submit:{request_id}", screen))
        self.assertEqual(self._single_answer().text, "Already sent for approval")
        self.assertEqual([call for call in self.bot.of(SendMessage) if int(call.chat_id) == 1], [])

    async def test_i_paid_is_guarded(self):
        request_id, screen = await self._request_screen()
        async with guarded(None, ("topup_submit", request_id)):
            await self.feed(self.callback(f"topup:submit:{request_id}", screen))
        self.assertEqual(self._single_answer().text, ALREADY_RUNNING_TEXT)
        self.assertEqual((await self.db.get_topup_request(request_id))["status"], "new")


class AdminTopUpFlowTests(_TopUpCase):
    """The admin (USER_ID) decides a request of member 555."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin()
        await self.db.add_admin(9001, "Boris Admin")
        self.friend_id = await self.db.create_friend_invite("Ivan Petrov", "join_ivan", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(self.friend_id, MEMBER_TG, "2026-11-01T00:00:00")
        await self.db.set_friend_balance(self.friend_id, 120.0)
        await self.db.set_friend_balance_currency(self.friend_id, "RUB")
        destination = await self._method("Sber")
        self.request_id = await self._pending(destination)
        self.destination = destination

    async def _pending(self, destination: int, amount: float = 1000.0) -> int:
        request_id = await self.db.create_topup_request(
            friend_id=self.friend_id,
            destination_id=destination,
            amount=amount,
            currency="RUB",
            destination_title="Sber",
            destination_details=CARD_DETAILS,
        )
        await self.db.mark_topup_request_pending(request_id)
        return request_id

    def _push(self, request_id: int | None = None):
        request_id = request_id or self.request_id
        return self._screen(7100, topup_request_review_keyboard(request_id, friend_id=self.friend_id))

    def _listed(self, request_id: int | None = None):
        request_id = request_id or self.request_id
        return self._screen(7200, topup_request_review_keyboard(request_id, friend_id=self.friend_id, from_list=True))

    def _member_pushes(self):
        return [call for call in self.bot.of(SendMessage) if int(call.chat_id) == MEMBER_TG]

    async def test_approve_on_the_push_makes_it_final_and_tells_the_member(self):
        await self.feed(self.callback(f"topup:approve:{self.request_id}", self._push()))

        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7100)
        self.assertEqual(
            edit.text,
            f"✅ <b>Top-up approved · {money('1 000.00 RUB')}</b> · by you\n\n"
            f'<a href="tg://user?id={MEMBER_TG}">Ivan Petrov</a> · <code>{MEMBER_TG}</code>\n'
            f"Balance now: <b>{money('1 120.00 RUB')}</b>",
        )
        self.assertIsNone(edit.reply_markup)
        self.assertEqual(self._single_answer().text, "✅ Top-up approved")
        (push,) = self._member_pushes()
        self.assertEqual(
            push.text,
            f"✅ <b>Top-up approved · {money('1 000.00 RUB')}</b>\n\nBalance: <b>{money('1 120.00 RUB')}</b>",
        )
        self.assertEqual(_texts(push.reply_markup), [["👤 Account"]])
        self.assertEqual((await self.db.get_friend(self.friend_id))["balance"], 1120.0)

    async def test_approve_from_the_list_redraws_the_list_with_a_banner(self):
        other = await self._pending(self.destination, 50.0)
        await self.feed(self.callback(f"topup:approve:{self.request_id}", self._listed()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(
            edit.text,
            f"✅ Top-up approved · <b>{money('1 000.00 RUB')}</b> · Ivan Petrov\n\n"
            "📥 <b>Pending top-ups</b> · 1\n\nTap a request to approve or reject it.",
        )
        self.assertEqual(_callbacks(edit.reply_markup), [[f"topup:admin_open:{other}"], ["paydest:menu:0"]])
        self.assertEqual(self._single_answer().text, "✅ Top-up approved")

    async def test_reject_asks_first_then_keep_and_reject(self):
        await self.feed(self.callback(f"topup:reject:{self.request_id}", self._push()))
        question = self.bot.of(EditMessageText)[-1]
        self.assertEqual(
            question.text,
            f"⚠️ <b>Reject {money('1 000.00 RUB')} from Ivan?</b>\nIvan is told the top-up was rejected.",
        )
        self.assertEqual(
            _callbacks(question.reply_markup), [[f"topup:review:{self.request_id}", f"topup:reject_yes:{self.request_id}"]]
        )
        self.assertEqual((await self.db.get_topup_request(self.request_id))["status"], "pending")

        self.bot.calls.clear()
        await self.feed(self.callback(f"topup:review:{self.request_id}", self._screen(7100, question.reply_markup)))
        review = self.bot.of(EditMessageText)[-1]
        self.assertTrue(review.text.startswith("📥 <b>Top-up request"), review.text)
        self.assertEqual(_texts(review.reply_markup), [["✅ Approve", "❌ Reject"], ["👤 Card"]])

        self.bot.calls.clear()
        await self.feed(self.callback(f"topup:reject_yes:{self.request_id}", self._screen(7100, question.reply_markup)))
        final = self.bot.of(EditMessageText)[-1]
        self.assertEqual(
            final.text,
            f"❌ <b>Top-up rejected · {money('1 000.00 RUB')}</b> · by you\n\n"
            f'<a href="tg://user?id={MEMBER_TG}">Ivan Petrov</a> · <code>{MEMBER_TG}</code>',
        )
        self.assertIsNone(final.reply_markup)
        self.assertEqual(self._single_answer().text, "❌ Top-up rejected")
        (push,) = self._member_pushes()
        self.assertEqual(
            push.text,
            f"❌ <b>Top-up rejected · {money('1 000.00 RUB')}</b>\n\nSber. If you did send the money, contact the admin.",
        )
        self.assertIsNone(push.reply_markup)

    async def test_reject_from_the_list_goes_back_to_the_list(self):
        await self.feed(self.callback(f"topup:reject:{self.request_id}", self._listed()))
        question = self.bot.of(EditMessageText)[-1]
        self.assertEqual(_callbacks(question.reply_markup)[0][0], f"topup:admin_open:{self.request_id}")
        self.bot.calls.clear()
        await self.feed(self.callback(f"topup:reject_yes:{self.request_id}", self._screen(7200, question.reply_markup)))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertTrue(edit.text.startswith(f"❌ Top-up rejected · <b>{money('1 000.00 RUB')}</b> · Ivan Petrov\n\n"))
        self.assertTrue(edit.text.endswith("No top-ups waiting."), edit.text)

    async def test_another_admins_stale_copy_becomes_final(self):
        await self.db.approve_topup_request(self.request_id, 9001)
        await self.feed(self.callback(f"topup:approve:{self.request_id}", self._push()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertTrue(edit.text.startswith("✅ <b>Top-up approved"), edit.text)
        self.assertIn("</b> · by Boris\n\n", edit.text)
        self.assertIsNone(edit.reply_markup)
        answer = self._single_answer()
        self.assertEqual(answer.text, "Already approved by Boris")
        self.assertFalse(answer.show_alert)
        self.assertEqual(self._member_pushes(), [])

        self.bot.calls.clear()
        await self.feed(self.callback(f"topup:reject:{self.request_id}", self._listed()))
        self.assertEqual(self._single_answer().text, "Already approved by Boris")
        self.assertTrue(self.bot.of(EditMessageText)[-1].text.startswith("📥 <b>Pending top-ups</b>"))

    async def test_decisions_are_guarded(self):
        async with guarded(None, ("topup_decide", self.request_id)):
            await self.feed(self.callback(f"topup:approve:{self.request_id}", self._push()))
        self.assertEqual(self._single_answer().text, ALREADY_RUNNING_TEXT)
        self.assertEqual((await self.db.get_topup_request(self.request_id))["status"], "pending")

    async def test_currency_mismatch_hides_approve(self):
        await self.db.set_friend_balance_currency(self.friend_id, "EUR")
        await self.feed(self.callback(f"topup:admin_open:{self.request_id}", self._screen()))
        review = self.bot.of(EditMessageText)[-1]
        self.assertIn("⚠️ Ivan's balance is in EUR", review.text)
        self.assertEqual(_texts(review.reply_markup), [["❌ Reject"], ["◀️ To pending"]])

        self.bot.calls.clear()
        await self.feed(self.callback(f"topup:approve:{self.request_id}", self._push()))
        self.assertEqual(self._single_answer().text, "The balance is in another currency")
        self.assertEqual((await self.db.get_topup_request(self.request_id))["status"], "pending")

    async def test_pending_list_is_answered_once(self):
        await self.feed(self.callback("topup:admin_list:0", self._screen()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.text, "📥 <b>Pending top-ups</b> · 1\n\nTap a request to approve or reject it.")
        self.assertEqual(_texts(edit.reply_markup)[-1], ["◀️ Back"])
        self._single_answer()

    async def test_review_from_the_list(self):
        await self.feed(self.callback(f"topup:admin_open:{self.request_id}", self._screen()))
        review = self.bot.of(EditMessageText)[-1]
        self.assertTrue(review.text.startswith(f"📥 <b>Top-up request · {money('1 000.00 RUB')}</b>\n\n"))
        self.assertNotIn("more waiting", review.text)
        self.assertEqual(_texts(review.reply_markup), [["✅ Approve", "❌ Reject"], ["◀️ To pending"]])

    async def test_deleted_member_request_is_handled(self):
        await self.db.delete_friend(self.friend_id)
        await self.feed(self.callback(f"topup:approve:{self.request_id}", self._push()))
        self.assertEqual(self._single_answer().text, "Request already handled")
        self.assertTrue(self.bot.of(EditMessageReplyMarkup))
        self.bot.calls.clear()
        await self.feed(self.callback(f"topup:admin_open:{self.request_id}", self._screen()))
        self.assertEqual(self._single_answer().text, "Request already handled")
        self.assertTrue(self.bot.of(EditMessageText)[-1].text.endswith("No top-ups waiting."))

    async def test_member_who_is_an_admin_gets_no_account_button(self):
        await self.db.add_admin(MEMBER_TG, "Ivan")
        await self.feed(self.callback(f"topup:approve:{self.request_id}", self._push()))
        (push,) = self._member_pushes()
        self.assertIsNone(push.reply_markup)

    async def test_failed_member_push_is_logged(self):
        from app.handlers import topups

        with mock.patch.object(topups, "send_message_with_retry", mock.AsyncMock(return_value=None)) as sender:
            with self.assertLogs("app.handlers.topups", level="WARNING") as logs:
                await self.feed(self.callback(f"topup:approve:{self.request_id}", self._push()))
        sender.assert_awaited_once()
        self.assertIn("could not tell user", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
