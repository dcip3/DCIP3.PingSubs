"""WP13 Admin 💳 Payment section: the methods root, a method's card, the
one-message "New payment method" wizard, field edits and delete — texts,
keyboards and flows."""

import unittest

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.handlers.payment_methods import EDIT_DESTINATION_ID
from app.ui.guards import guarded
from app.ui.helpers import METHOD_GONE
from app.ui.keyboards import (
    PAYMENT_WIZARD_SKIP_CALLBACK,
    payment_destination_delete_keyboard,
    payment_destination_detail_keyboard,
    payment_destinations_keyboard,
    payment_link_clear_rows,
    payment_method_button_label,
    payment_wizard_keyboard,
)
from app.ui.payment_screens import (
    DETAILS_MAX_LEN,
    LINK_HINT,
    LINK_NOT_OPENABLE_LINE,
    currency_prompt_text,
    delete_consequence,
    details_prompt_text,
    link_prompt_text,
    method_delete_text,
    method_detail_text,
    payment_root_text,
    payment_wizard_text,
    title_prompt_text,
)
from app.ui.prompts import PROMPT_MESSAGE_ID, WIZARD_MESSAGE_ID
from app.ui.states import PaymentDestinationEditForm, PaymentDestinationForm
from app.ui.style import SUBITEM_INDENT
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import EditMessageText, SendMessage, make_message
from test_keyboards_fit import LONG_METHOD, LONG_REQUISITES, NAME_64, USER_TEXTS, assert_fits

install_keyboard_guard()

IND = SUBITEM_INDENT


def _texts(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


def _methods(*titles: str) -> list[dict]:
    return [
        {"id": index + 1, "title": title, "currency": ("RUB", "EUR", "USD")[index % 3], "details": "", "payment_link": ""}
        for index, title in enumerate(titles)
    ]


# ----------------------------------------------------------------- keyboards


class PaymentKeyboardFitTests(unittest.TestCase):
    """Every Payment builder fits a phone in every branch, with worst-case data."""

    TITLES = (LONG_METHOD, NAME_64, "W" * 64, "Sber", "Revolut", "Mmmmmmmmmmmmmmmmmmmmmmmm")

    def test_root_every_branch(self):
        methods = _methods(*self.TITLES)
        for destinations in ([], methods[:1], methods):
            for default in (None, 1, 3):
                for pending in (0, 1, 99999):
                    with self.subTest(count=len(destinations), default=default, pending=pending):
                        assert_fits(payment_destinations_keyboard(destinations, default, pending), USER_TEXTS + self.TITLES)

    def test_root_layout(self):
        methods = _methods("Sber", "Revolut")
        markup = payment_destinations_keyboard(methods, 1, 2)
        self.assertEqual(
            _texts(markup),
            [["📥 Pending top-ups (2)"], ["⭐ Sber · RUB"], ["Revolut · EUR"], ["➕ Add payment method"]],
        )
        self.assertEqual(
            _callbacks(markup),
            ["topup:admin_list:0", "paydest:open:1", "paydest:open:2", "paydest:create:0"],
        )
        # No pending top-ups: no pending button; no Close anywhere (top-level screen).
        markup = payment_destinations_keyboard(methods, None, 0)
        self.assertEqual(_texts(markup), [["Sber · RUB"], ["Revolut · EUR"], ["➕ Add payment method"]])
        self.assertNotIn("menu:close", _callbacks(markup))

    def test_long_title_keeps_the_currency_tail(self):
        label = payment_method_button_label({"id": 1, "title": "W" * 64, "currency": "rub"}, 1)
        self.assertTrue(label.startswith("⭐ WW"), label)
        self.assertTrue(label.endswith("… · RUB"), label)

    def test_detail_every_branch(self):
        for is_default in (True, False):
            with self.subTest(is_default=is_default):
                assert_fits(payment_destination_detail_keyboard(2_147_483_647, is_default=is_default))

    def test_detail_layout(self):
        markup = payment_destination_detail_keyboard(7, is_default=False)
        self.assertEqual(
            _texts(markup),
            [["✏️ Title", "💱 Currency"], ["📝 Details", "🔗 Link"], ["⭐ Make default"], ["🗑 Delete"], ["◀️ Back"]],
        )
        self.assertEqual(
            _callbacks(markup),
            [
                "paydest:edit_title:7",
                "paydest:edit_currency:7",
                "paydest:edit_details:7",
                "paydest:edit_link:7",
                "paydest:set_default:7",
                "paydest:delete:7",
                "paydest:menu:0",
            ],
        )
        default = payment_destination_detail_keyboard(7, is_default=True)
        self.assertEqual(_texts(default)[2:], [["🗑 Delete"], ["◀️ Back"]])
        self.assertEqual([b for row in default.inline_keyboard for b in row if b.style], [])

    def test_delete_confirmation(self):
        markup = payment_destination_delete_keyboard(9)
        assert_fits(markup)
        self.assertEqual(_texts(markup), [["✖️ Cancel", "🗑 Delete"]])
        self.assertEqual(_callbacks(markup), ["paydest:open:9", "paydest:confirm_delete:9"])

    def test_wizard_and_prompt_keyboards(self):
        for step in (1, 2, 3, 4):
            with self.subTest(step=step):
                assert_fits(payment_wizard_keyboard(step))
        self.assertNotIn("force_reply", payment_wizard_keyboard(1).model_dump(exclude_none=True))
        self.assertNotIn("force_reply", payment_wizard_keyboard(2).model_dump(exclude_none=True))
        self.assertEqual(_texts(payment_wizard_keyboard(1)), [["✖️ Cancel"]])
        self.assertEqual(_texts(payment_wizard_keyboard(2)), [["RUB", "USD"], ["EUR", "TRY"], ["✖️ Cancel"]])
        self.assertEqual(
            _callbacks(payment_wizard_keyboard(2)),
            ["paydest_wiz_cur:RUB", "paydest_wiz_cur:USD", "paydest_wiz_cur:EUR", "paydest_wiz_cur:TRY", "dialog:cancel"],
        )
        self.assertEqual(_texts(payment_wizard_keyboard(4)), [["⏭ Skip"], ["✖️ Cancel"]])
        self.assertEqual(_callbacks(payment_wizard_keyboard(4)), ["paydest_wiz_skip", "dialog:cancel"])
        rows = [*payment_link_clear_rows(5), [InlineKeyboardButton(text="✖️ Cancel", callback_data="dialog:cancel")]]
        assert_fits(InlineKeyboardMarkup(inline_keyboard=rows))
        self.assertEqual(rows[0][0].callback_data, "paydest:clear_link:5")

    def test_callback_data_stays_short(self):
        big = 2_147_483_647
        markups = (
            payment_destinations_keyboard(_methods(*self.TITLES) + [{"id": big, "title": "x", "currency": "RUB"}], big, 5),
            payment_destination_detail_keyboard(big, is_default=False),
            payment_destination_delete_keyboard(big),
            InlineKeyboardMarkup(inline_keyboard=payment_link_clear_rows(big)),
            payment_wizard_keyboard(2),
            payment_wizard_keyboard(4),
        )
        for markup in markups:
            for data in _callbacks(markup):
                self.assertLessEqual(len(data.encode()), 64, data)


# --------------------------------------------------------------------- texts


class PaymentTextTests(unittest.TestCase):
    def test_root_texts(self):
        methods = _methods("Sber", "Revolut")
        self.assertEqual(
            payment_root_text(methods, 1, 2),
            "💳 <b>Payment</b>\n\n📥 Top-ups waiting: <b>2</b>\n🏦 Methods: <b>2</b> · default Sber\n\n"
            "Members pay to these; ⭐ is the default.",
        )
        self.assertEqual(
            payment_root_text(methods, None, 0),
            "💳 <b>Payment</b>\n\n🏦 Methods: <b>2</b> · no default\n\n"
            "Members pay to these; open one to make it the default.",
        )
        self.assertEqual(
            payment_root_text([], None, 0),
            "💳 <b>Payment</b>\n\nNo payment methods yet. Add one so members know how to pay.",
        )
        self.assertEqual(
            payment_root_text(_methods("<b>&"), 1, 0).splitlines()[2],
            "🏦 Methods: <b>1</b> · default &lt;b&gt;&amp;",
        )

    def test_detail_text(self):
        destination = {
            "id": 1,
            "title": "Sber",
            "currency": "RUB",
            "details": "Card 2200 1234 5678 9012\n T-Bank \n",
            "payment_link": "https://pay.example.com/x",
        }
        self.assertEqual(
            method_detail_text(destination, is_default=True, assigned_count=2),
            "🏦 <b>Sber</b> · RUB\n\n⭐ Default method\nAssigned to users: <b>2</b>\n\n"
            f"📝 <b>Details</b>\n{IND}<code>Card 2200 1234 5678 9012</code>\n{IND}<code>T-Bank</code>\n\n"
            "🔗 Link: https://pay.example.com/x",
        )
        bare = {"id": 2, "title": "Cash", "currency": "eur", "details": "", "payment_link": "pay.example"}
        self.assertEqual(
            method_detail_text(bare, is_default=False, assigned_count=0),
            f"🏦 <b>Cash</b> · EUR\n\n📝 Details: none\n\n🔗 Link: pay.example\n{LINK_NOT_OPENABLE_LINE}",
        )
        self.assertEqual(
            LINK_NOT_OPENABLE_LINE, "⚠️ The link is not a web or Telegram link, so members get no button."
        )

    def test_long_details_fold_and_stay_under_the_limit(self):
        details = "\n".join(f"Line {index} " + LONG_REQUISITES for index in range(40))
        text = method_detail_text(
            {"id": 1, "title": NAME_64, "currency": "RUB", "details": details, "payment_link": ""},
            is_default=False,
            assigned_count=0,
        )
        self.assertIn("📝 <b>Details</b> · 40 lines\n<blockquote expandable><code>Line 0 ", text)
        self.assertIn("more lines</blockquote>", text)
        self.assertLess(len(text.encode("utf-16-le")) // 2, 4096)
        five = method_detail_text(
            {"id": 1, "title": "Sber", "currency": "RUB", "details": "a\nb\nc\nd\ne", "payment_link": ""},
            is_default=False,
            assigned_count=0,
        )
        self.assertIn(
            "📝 <b>Details</b> · 5 lines\n<blockquote expandable><code>a</code>\n<code>b</code>\n"
            "<code>c</code>\n<code>d</code>\n<code>e</code></blockquote>",
            five,
        )

    def test_delete_question(self):
        revolut = {"id": 2, "title": "Revolut", "currency": "EUR"}
        sber = {"id": 1, "title": "Sber", "currency": "RUB"}
        self.assertEqual(
            method_delete_text(revolut, is_default=False, is_only=False, assigned_count=2, default_destination=sber),
            "⚠️ <b>Delete Revolut?</b>\nIts 2 users and any subscriptions on it switch to the default method (Sber).",
        )
        self.assertEqual(
            delete_consequence(is_default=False, is_only=False, assigned_count=1, default_destination=sber),
            "Its 1 user and any subscriptions on it switch to the default method (Sber).",
        )
        self.assertEqual(
            delete_consequence(is_default=False, is_only=False, assigned_count=0, default_destination=sber),
            "Anything that uses it switches to the default method (Sber).",
        )
        self.assertEqual(
            delete_consequence(is_default=False, is_only=False, assigned_count=3, default_destination=None),
            "Its 3 users and any subscriptions on it are left without a payment method.",
        )
        self.assertEqual(
            delete_consequence(is_default=True, is_only=False, assigned_count=0, default_destination=sber),
            "It is the default, so anyone without their own method sees none until you make another one default.",
        )
        self.assertEqual(
            delete_consequence(is_default=True, is_only=True, assigned_count=0, default_destination=sber),
            "Members see no payment details until you add a new method.",
        )

    def test_wizard_texts(self):
        self.assertEqual(
            payment_wizard_text({}, 1),
            "➕ <b>New payment method</b> · step 1 of 4\n\n✏️ Title? For example <code>Main RUB card</code>.",
        )
        data = {
            "payment_destination_title": "Sber",
            "payment_destination_currency": "RUB",
            "payment_destination_details": "Card 2200 1234 5678 9012\nT-Bank",
        }
        self.assertEqual(
            payment_wizard_text(data, 4, LINK_HINT),
            "➕ <b>New payment method</b> · step 4 of 4\n\n"
            f"{IND}Title: Sber\n{IND}Currency: RUB\n{IND}Details: <code>Card 2200 1234 5678 9012</code> · 2 lines\n\n"
            "✏️ Payment link? Optional.\n⚠️ Links must start with https:// or tg:// — send it again to keep it.",
        )
        self.assertTrue(
            payment_wizard_text({"payment_destination_title": "Sber"}, 2).endswith(
                "✏️ Currency? Tap one or send three letters."
            )
        )
        self.assertTrue(
            payment_wizard_text({}, 3).endswith(
                "✏️ Payment details? Several lines are fine. A card number or IBAN gets a copy button for members."
            )
        )

    def test_field_prompts(self):
        sber = {"id": 1, "title": "Sber", "currency": "RUB", "details": "Card 1\nT-Bank", "payment_link": "https://x.io"}
        self.assertEqual(title_prompt_text(sber), "✏️ New title for Sber? Now: Sber.")
        self.assertEqual(
            currency_prompt_text(sber, 2),
            "✏️ New currency for Sber? Now RUB.\n⚠️ 2 users and pending top-ups keep their amounts; nothing is converted.",
        )
        self.assertEqual(
            currency_prompt_text(sber, 0),
            "✏️ New currency for Sber? Now RUB.\n⚠️ Balances and pending top-ups keep their amounts; nothing is converted.",
        )
        self.assertEqual(
            details_prompt_text(sber), "✏️ New details for Sber? Now:\n<code>Card 1</code>\n<code>T-Bank</code>"
        )
        self.assertEqual(link_prompt_text(sber), "✏️ New link for Sber? Now: https://x.io")
        self.assertEqual(link_prompt_text({**sber, "payment_link": ""}), "✏️ New link for Sber? Now: none.")


# --------------------------------------------------------------------- flows


class PaymentFlowTests(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin()

    def screen(self, message_id: int, markup: InlineKeyboardMarkup | None = None):
        message = make_message(self.bot, message_id, "screen", from_bot=True)
        if markup is not None:
            message = message.model_copy(update={"reply_markup": markup}).as_(self.bot)
        return message

    def edits(self, message_id: int) -> list[str]:
        return [call.text for call in self.bot.of(EditMessageText) if call.message_id == message_id]

    def last_edit_markup(self):
        return self.bot.of(EditMessageText)[-1].reply_markup

    def last_answer(self):
        answers = self.bot.answers()
        self.assertTrue(answers, "the tap was not answered")
        return answers[-1]

    async def _method(self, title: str = "Sber", currency: str = "RUB", *, default: bool = False, **extra) -> int:
        destination_id = await self.db.create_payment_destination(
            title=title,
            currency=currency,
            details=extra.get("details", "Card 2200 1234 5678 9012"),
            payment_link=extra.get("payment_link", ""),
        )
        if default:
            await self.db.set_default_payment_destination_id(destination_id)
        return destination_id

    async def test_reply_menu_and_alias_send_the_root(self):
        await self._method(default=True)
        for label in ("💳 Payment", "💳 Payment methods"):
            await self.feed(self.message(label))
        texts = self.bot.sent_texts()
        self.assertEqual(len(texts), 2)
        self.assertEqual(
            texts[0],
            "💳 <b>Payment</b>\n\n🏦 Methods: <b>1</b> · default Sber\n\nMembers pay to these; ⭐ is the default.",
        )
        self.assertEqual(_texts(self.bot.of(SendMessage)[0].reply_markup), [["⭐ Sber · RUB"], ["➕ Add payment method"]])

    async def test_open_detail_and_stale_detail(self):
        sber = await self._method(default=True, payment_link="https://pay.example.com")
        await self.feed(self.callback(f"paydest:open:{sber}", self.screen(900)))
        (card,) = self.edits(900)
        self.assertEqual(
            card,
            "🏦 <b>Sber</b> · RUB\n\n⭐ Default method\n\n"
            f"📝 <b>Details</b>\n{IND}<code>Card 2200 1234 5678 9012</code>\n\n🔗 Link: https://pay.example.com",
        )
        self.assertEqual(len(self.bot.answers()), 1)

        for action in ("open", "edit_title", "set_default", "delete", "confirm_delete", "clear_link"):
            with self.subTest(action=action):
                await self.feed(self.callback(f"paydest:{action}:999", self.screen(901)))
                self.assertTrue(self.edits(901)[-1].startswith("💳 <b>Payment</b>"), self.edits(901))
                self.assertEqual(self.last_answer().text, METHOD_GONE)

    async def test_make_default_and_old_default_button(self):
        await self._method("Sber", default=True)
        revolut = await self._method("Revolut", "EUR")
        await self.feed(self.callback(f"paydest:set_default:{revolut}", self.screen(910)))
        (card,) = self.edits(910)
        self.assertTrue(card.startswith("✅ Default method · <b>Revolut</b>\n\n🏦 <b>Revolut</b> · EUR\n\n⭐ Default method"))
        self.assertEqual(self.last_answer().text, "✅ Default method · Revolut")
        self.assertNotIn("⭐ Make default", sum(_texts(self.last_edit_markup()), []))
        self.assertEqual(await self.db.get_default_payment_destination_id(), revolut)

        # An old card still showing '⭐ Default': no repeated change, just the card.
        await self.feed(self.callback(f"paydest:set_default:{revolut}", self.screen(911)))
        self.assertTrue(self.edits(911)[0].startswith("🏦 <b>Revolut</b>"))
        self.assertEqual(self.last_answer().text, "Already the default")

    async def test_wizard_end_to_end(self):
        await self.feed(self.callback("paydest:create:0", self.screen(920)))
        (wizard,) = self.bot.of(SendMessage)
        self.assertEqual(
            wizard.text, "➕ <b>New payment method</b> · step 1 of 4\n\n✏️ Title? For example <code>Main RUB card</code>."
        )
        self.assertNotIn("force_reply", wizard.reply_markup.model_dump(exclude_none=True))
        self.assertEqual(await self.state().get_state(), PaymentDestinationForm.title.state)
        self.assertEqual(self.edits(920), [])  # the root stays as it is
        wizard_id = (await self.state().get_data())[WIZARD_MESSAGE_ID]

        title = self.message("  Sber   card ")
        await self.feed(title)
        self.assertEqual(
            self.edits(wizard_id)[-1],
            f"➕ <b>New payment method</b> · step 2 of 4\n\n{IND}Title: Sber card\n\n"
            "✏️ Currency? Tap one or send three letters.",
        )
        self.assertIn(title.message_id, self.bot.deleted_ids())

        typo = self.message("rubles")
        await self.feed(typo)
        self.assertTrue(self.edits(wizard_id)[-1].endswith("\n⚠️ Need three letters, for example <code>USD</code>."))
        self.assertIn(typo.message_id, self.bot.deleted_ids())

        await self.feed(self.callback("paydest_wiz_cur:RUB", self.screen(wizard_id, payment_wizard_keyboard(2))))
        self.assertIn(f"{IND}Currency: RUB", self.edits(wizard_id)[-1])
        self.assertEqual(await self.state().get_state(), PaymentDestinationForm.details.state)

        await self.feed(self.message("Card 2200 1234 5678 9012\nT-Bank"))
        self.assertTrue(self.edits(wizard_id)[-1].endswith("✏️ Payment link? Optional."))
        self.assertEqual(_texts(self.last_edit_markup()), [["⏭ Skip"], ["✖️ Cancel"]])

        await self.feed(self.message("pay.example"))
        self.assertTrue(self.edits(wizard_id)[-1].endswith(LINK_HINT))
        self.assertEqual(await self.db.list_payment_destinations(), [])
        await self.feed(self.message("pay.example"))

        (destination,) = await self.db.list_payment_destinations()
        self.assertEqual(destination["payment_link"], "pay.example")
        self.assertEqual(await self.db.get_default_payment_destination_id(), destination["id"])
        card = self.edits(wizard_id)[-1]
        self.assertTrue(
            card.startswith(
                "✅ <b>Sber card added</b> · it is the default method\n\n🏦 <b>Sber card</b> · RUB\n\n⭐ Default method\n\n"
            ),
            card,
        )
        self.assertTrue(card.endswith(f"🔗 Link: pay.example\n{LINK_NOT_OPENABLE_LINE}"), card)
        self.assertIsNone(await self.state().get_state())
        self.assertNotIn("Payment method saved.", self.bot.sent_texts())
        self.assertEqual(len(self.bot.of(SendMessage)), 1)  # only the wizard itself

    async def test_wizard_skip_finishes_without_link(self):
        await self._method("Sber", default=True)
        await self.feed(self.callback("paydest:create:0", self.screen(930)))
        wizard_id = (await self.state().get_data())[WIZARD_MESSAGE_ID]
        for text in ("Revolut", "eur", "IBAN DE89 3704 0044 0532 0130 00"):
            await self.feed(self.message(text))
        wizard = self.screen(wizard_id, payment_wizard_keyboard(4))
        await self.feed(self.callback(PAYMENT_WIZARD_SKIP_CALLBACK, wizard))
        card = self.edits(wizard_id)[-1]
        self.assertTrue(card.startswith("✅ <b>Revolut added</b>\n\n🏦 <b>Revolut</b> · EUR\n\n📝 <b>Details</b>"), card)
        self.assertEqual(self.last_answer().text, "✅ Revolut added")
        self.assertEqual(_texts(self.last_edit_markup())[2], ["⭐ Make default"])

        # A late second tap on the same (now finished) wizard leaves the card alone.
        await self.feed(self.callback(PAYMENT_WIZARD_SKIP_CALLBACK, wizard))
        self.assertNotIn(wizard_id, self.bot.deleted_ids())
        self.assertEqual(len(await self.db.list_payment_destinations()), 2)

    async def test_stale_wizard_buttons_are_answered_and_removed(self):
        await self.feed(self.callback("paydest_wiz_cur:USD", self.screen(940, payment_wizard_keyboard(2))))
        self.assertEqual(self.last_answer().text, "This step is out of date")
        self.assertIn(940, self.bot.deleted_ids())
        await self.feed(self.callback(PAYMENT_WIZARD_SKIP_CALLBACK, self.screen(941, payment_wizard_keyboard(4))))
        self.assertEqual(self.last_answer().text, "This step is out of date")
        self.assertEqual(await self.db.list_payment_destinations(), [])

    async def test_wizard_cancel_deletes_it(self):
        await self.feed(self.callback("paydest:create:0", self.screen(945)))
        wizard_id = (await self.state().get_data())[WIZARD_MESSAGE_ID]
        await self.feed(self.message("Sber"))
        await self.feed(self.callback("dialog:cancel", self.screen(wizard_id, payment_wizard_keyboard(2))))
        self.assertIn(wizard_id, self.bot.deleted_ids())
        self.assertIsNone(await self.state().get_state())
        self.assertEqual(self.last_answer().text, "Cancelled")

    async def test_wizard_finish_is_guarded(self):
        await self.feed(self.callback("paydest:create:0", self.screen(946)))
        wizard_id = (await self.state().get_data())[WIZARD_MESSAGE_ID]
        for text in ("Sber", "RUB", "Card 1"):
            await self.feed(self.message(text))
        async with guarded(None, ("paydest_create", 4242)):
            await self.feed(self.callback(PAYMENT_WIZARD_SKIP_CALLBACK, self.screen(wizard_id, payment_wizard_keyboard(4))))
        self.assertEqual(self.last_answer().text, "⏳ Already running")
        self.assertEqual(await self.db.list_payment_destinations(), [])

    async def test_title_edit_prompt_typo_and_success(self):
        sber = await self._method(default=True)
        await self.feed(self.callback(f"paydest:edit_title:{sber}", self.screen(950)))
        (prompt,) = self.bot.of(SendMessage)
        self.assertEqual(prompt.text, "✏️ New title for Sber? Now: Sber.")
        self.assertEqual(_texts(prompt.reply_markup), [["✖️ Cancel"]])
        self.assertEqual(await self.state().get_state(), PaymentDestinationEditForm.title.state)
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]

        typo = self.message("   ")
        await self.feed(typo)
        self.assertEqual(self.bot.sent_texts()[-1], "⚠️ Send text, for example <code>Main RUB card</code>.")

        answer = self.message("Main card")
        await self.feed(answer)
        (card,) = self.edits(950)
        self.assertTrue(card.startswith("✅ Title · <b>Main card</b>\n\n🏦 <b>Main card</b> · RUB"), card)
        for message_id in (prompt_id, typo.message_id, answer.message_id):
            self.assertIn(message_id, self.bot.deleted_ids())
        self.assertNotIn("Payment method updated.", self.bot.sent_texts())
        self.assertIsNone(await self.state().get_state())

    async def test_currency_details_and_link_edits(self):
        sber = await self._method(default=True, payment_link="https://pay.example.com")
        await self.feed(self.callback(f"paydest:edit_currency:{sber}", self.screen(960)))
        self.assertEqual(
            self.bot.sent_texts()[-1],
            "✏️ New currency for Sber? Now RUB.\n⚠️ Balances and pending top-ups keep their amounts; nothing is converted.",
        )
        await self.feed(self.message("usd"))
        self.assertTrue(self.edits(960)[-1].startswith("✅ Currency · <b>USD</b>\n\n🏦 <b>Sber</b> · USD"))

        await self.feed(self.callback(f"paydest:edit_details:{sber}", self.screen(961)))
        self.assertEqual(
            self.bot.sent_texts()[-1], "✏️ New details for Sber? Now:\n<code>Card 2200 1234 5678 9012</code>"
        )
        await self.feed(self.message("x" * (DETAILS_MAX_LEN + 1)))
        self.assertEqual(self.bot.sent_texts()[-1], f"⚠️ Keep the details under {DETAILS_MAX_LEN} characters.")
        await self.feed(self.message("IBAN DE89 3704 0044 0532 0130 00"))
        self.assertTrue(self.edits(961)[-1].startswith("✅ Details updated\n\n🏦 <b>Sber</b>"))

        await self.feed(self.callback(f"paydest:edit_link:{sber}", self.screen(962)))
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, "✏️ New link for Sber? Now: https://pay.example.com")
        self.assertEqual(_texts(prompt.reply_markup), [["🗑 Clear link"], ["✖️ Cancel"]])
        await self.feed(self.message("tg://resolve?domain=pay"))
        self.assertTrue(self.edits(962)[-1].startswith("✅ Link · <b>tg://resolve?domain=pay</b>\n\n"))

        # The link prompt's [🗑 Clear link] clears it and redraws the card.
        await self.feed(self.callback(f"paydest:edit_link:{sber}", self.screen(963)))
        prompt_message = self.bot.of(SendMessage)[-1]
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]
        tap = self.screen(prompt_id, prompt_message.reply_markup)
        await self.feed(self.callback(f"paydest:clear_link:{sber}", tap))
        self.assertTrue(self.edits(963)[-1].startswith("✅ Link cleared\n\n🏦 <b>Sber</b>"), self.edits(963))
        self.assertTrue(self.edits(963)[-1].endswith("🔗 Link: none"))
        self.assertEqual(self.last_answer().text, "✅ Link cleared")
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertIsNone(await self.state().get_state())
        self.assertEqual((await self.db.get_payment_destination(sber))["payment_link"], "")

    async def test_link_edit_accepts_unusual_link_on_second_send_and_dash_clears(self):
        sber = await self._method(default=True)
        await self.feed(self.callback(f"paydest:edit_link:{sber}", self.screen(970)))
        self.assertEqual(_texts(self.bot.of(SendMessage)[-1].reply_markup), [["✖️ Cancel"]])
        await self.feed(self.message("pay.example"))
        self.assertEqual(self.bot.sent_texts()[-1], LINK_HINT)
        self.assertEqual(self.edits(970), [])
        await self.feed(self.message("pay.example"))
        self.assertTrue(self.edits(970)[-1].startswith("✅ Link · <b>pay.example</b>"))

        await self.feed(self.callback(f"paydest:edit_link:{sber}", self.screen(971)))
        await self.feed(self.message("-"))
        self.assertTrue(self.edits(971)[-1].startswith("✅ Link cleared"))

    async def test_prompt_for_a_deleted_method_shows_the_root(self):
        sber = await self._method(default=True)
        await self.feed(self.callback(f"paydest:edit_title:{sber}", self.screen(975)))
        await self.db.delete_payment_destination(sber)
        await self.feed(self.message("New"))
        self.assertTrue(self.edits(975)[-1].startswith("⚠️ This payment method was deleted\n\n💳 <b>Payment</b>"))
        self.assertIsNone(await self.state().get_state())

    async def test_prompt_without_flow_data_is_out_of_date(self):
        await self.set_state(PaymentDestinationEditForm.title)
        await self.feed(self.message("New"))
        self.assertEqual(self.bot.sent_texts()[-1], "⌛️ This prompt is out of date. Open “💳 Payment” again.")
        await self.set_state(PaymentDestinationEditForm.details, **{EDIT_DESTINATION_ID: 0})
        await self.feed(self.message("New"))
        self.assertIsNone(await self.state().get_state())

    async def test_delete_asks_then_returns_to_the_root(self):
        sber = await self._method("Sber", default=True)
        revolut = await self._method("Revolut", "EUR")
        await self.feed(self.callback(f"paydest:delete:{revolut}", self.screen(980)))
        self.assertEqual(
            self.edits(980),
            ["⚠️ <b>Delete Revolut?</b>\nAnything that uses it switches to the default method (Sber)."],
        )
        self.assertEqual(_texts(self.last_edit_markup()), [["✖️ Cancel", "🗑 Delete"]])

        await self.feed(self.callback(f"paydest:confirm_delete:{revolut}", self.screen(981)))
        (root,) = self.edits(981)
        self.assertEqual(
            root,
            "✅ Revolut deleted\n\n💳 <b>Payment</b>\n\n🏦 Methods: <b>1</b> · default Sber\n\n"
            "Members pay to these; ⭐ is the default.",
        )
        self.assertEqual(self.last_answer().text, "✅ Revolut deleted")
        self.assertIsNone(await self.db.get_payment_destination(revolut))
        self.assertTrue(await self.db.get_payment_destination(sber))

    async def test_delete_double_tap_is_guarded(self):
        sber = await self._method(default=True)
        async with guarded(None, ("paydest_delete", sber)):
            await self.feed(self.callback(f"paydest:confirm_delete:{sber}", self.screen(985)))
        self.assertEqual(self.last_answer().text, "⏳ Already running")
        self.assertTrue(await self.db.get_payment_destination(sber))

    async def test_every_tap_is_answered_once(self):
        sber = await self._method(default=True)
        for data in (
            "paydest:menu:0",
            f"paydest:open:{sber}",
            f"paydest:delete:{sber}",
            f"paydest:set_default:{sber}",
            f"paydest:edit_title:{sber}",
            f"paydest:edit_currency:{sber}",
            f"paydest:edit_details:{sber}",
            f"paydest:edit_link:{sber}",
            "paydest:create:0",
        ):
            with self.subTest(data=data):
                before = len(self.bot.answers())
                await self.feed(self.callback(data, self.screen(990)))
                self.assertEqual(len(self.bot.answers()), before + 1)


if __name__ == "__main__":
    unittest.main()
