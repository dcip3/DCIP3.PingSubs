"""WP3: the admin subscription list, creation wizard, card, rename, delete and stale card."""

from datetime import date, datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from aiogram.methods import EditMessageText, SendMessage

from app.core.constants import MONTHLY_PERIOD_SENTINEL, PAYMENT_MODE_FIXED, PAYMENT_MODE_SPLIT
from app.core.reminders import tg_due
from app.handlers.subscriptions import (
    STALE_STEP_TOAST,
    WIZARD_CANCEL_CALLBACK,
    parse_period_input,
    parse_share_limit_input,
    subscription_delete_question,
    wizard_text,
)
from app.ui.guards import ALREADY_RUNNING_TEXT, guarded
from app.ui.helpers import (
    SUBSCRIPTION_GONE,
    _build_subscription_detail_text,
    _build_subscription_list_text,
    build_subscription_card,
    currency_prompt,
    payment_mode_prompt,
    period_prompt,
    share_limit_prompt,
)
from app.ui.keyboards import (
    SUBSCRIPTION_LIST_PAGE_SIZE,
    build_creation_payment_mode_keyboard,
    build_currency_keyboard,
    build_period_keyboard,
    build_share_limit_keyboard,
    build_subscription_list_keyboard,
    subscription_delete_confirm_keyboard,
    subscription_detail_keyboard,
    subscription_list_label,
)
from app.ui.states import SubscriptionAction, SubscriptionForm
from app.ui.style import NBSP, SUBITEM_INDENT
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import make_message
from test_keyboards_fit import AMOUNT, LONG_NAME, LONG_SUB, NAME_64, USER_TEXTS, assert_fits

install_keyboard_guard()

TZ = "UTC"
I = SUBITEM_INDENT


def _texts(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _callbacks(markup) -> list[list[str | None]]:
    return [[button.callback_data for button in row] for row in markup.inline_keyboard]


def _styled(markup) -> list[tuple[str, str]]:
    return [(button.text, button.style) for row in markup.inline_keyboard for button in row if button.style]


def _sub(sub_id: int, name: str, *, amount: float = 15.99, currency: str = "USD", users: int = 2, unpaid=False):
    return {
        "id": sub_id,
        "name": name,
        "amount": amount,
        "currency": currency,
        "participant_count": users,
        "unpaid": unpaid,
    }


# ------------------------------------------------------------------ keyboards


class SubscriptionKeyboardFitTests(unittest.TestCase):
    """Every builder this package touches, with worst-case data, through assert_fits."""

    def test_list_keyboard_with_worst_case_names_amounts_and_paging(self):
        subs = [
            _sub(1, LONG_NAME, amount=AMOUNT, currency="RUB", unpaid=True),
            _sub(2, NAME_64, amount=AMOUNT, currency="RUB"),
            _sub(3, LONG_SUB, users=0),
            _sub(4, "Netflix"),
        ]
        for page, total in ((1, 1), (1, 3), (2, 3), (3, 3)):
            with self.subTest(page=page, total=total):
                markup = build_subscription_list_keyboard(subs, page=page, total_pages=total)
                assert_fits(markup)
                rows = _texts(markup)
                self.assertEqual(rows[0], ["➕ New subscription"])
                self.assertEqual(_styled(markup), [])
                if total > 1:
                    self.assertEqual(rows[-1][1], f"{page} / {total}")
        assert_fits(build_subscription_list_keyboard([]))
        self.assertEqual(_styled(build_subscription_list_keyboard([])), [("➕ New subscription", "primary")])

    def test_list_labels_keep_the_amount_and_cut_the_name(self):
        self.assertEqual(subscription_list_label(_sub(1, "Netflix", unpaid=True)), "⚠️ Netflix · 15.99 USD")
        self.assertEqual(subscription_list_label(_sub(1, "Spotify", amount=299, currency="rub")), "Spotify · 299.00 RUB")
        self.assertEqual(subscription_list_label(_sub(1, "iCloud", users=0)), "👥 iCloud · no participants")
        long_label = subscription_list_label(_sub(1, LONG_NAME, amount=AMOUNT, currency="RUB", unpaid=True))
        self.assertTrue(long_label.startswith("⚠️ WW"), long_label)
        self.assertTrue(long_label.endswith(f"… · 12{NBSP}500.00{NBSP}RUB"), long_label)

    def test_list_paging_uses_the_new_raw_callback(self):
        markup = build_subscription_list_keyboard([_sub(1, "A")], page=2, total_pages=3)
        self.assertEqual(_callbacks(markup)[-1], ["sublist:page:1", None, "sublist:page:3"])
        self.assertEqual(_callbacks(markup)[1], ["sub:open:1"])

    def test_card_keyboard_rows_and_conditional_cta(self):
        empty = subscription_detail_keyboard(7, participant_count=0)
        assert_fits(empty)
        self.assertEqual(
            _texts(empty),
            [
                ["👥 Add participants"],
                ["💰 Pricing", "👥 Participants"],
                ["🔔 Reminders", "💳 Payment"],
                ["📊 Reports", "🗂 Open cycles"],
                ["✏️ Rename", "🗑 Delete"],
                ["◀️ To list"],
            ],
        )
        self.assertEqual(
            _callbacks(empty),
            [
                ["sub:participants_manage:7"],
                ["sub:pricing:7", "sub:participants:7"],
                ["sub:reminders:7", "sub:paymentinfo:7"],
                ["sub:report:7", "sub:cycles:7"],
                ["sub:rename:7", "sub:delete:7"],
                ["sub:back:7"],
            ],
        )
        self.assertEqual(_styled(empty), [("👥 Add participants", "primary")])

        busy = subscription_detail_keyboard(7, participant_count=3)
        assert_fits(busy)
        self.assertEqual(len(busy.inline_keyboard), 5)
        self.assertEqual(_styled(busy), [])

        pushed = subscription_detail_keyboard(7, participant_count=3, close=True)
        assert_fits(pushed)
        self.assertEqual(_texts(pushed)[-1], ["✖️ Close"])
        self.assertEqual(_callbacks(pushed)[-1], ["menu:close"])
        # Old call shape (other packages): no CTA, the list as the way back.
        self.assertEqual(_texts(subscription_detail_keyboard(7))[-1], ["◀️ To list"])

    def test_delete_confirmation_row(self):
        markup = subscription_delete_confirm_keyboard(7)
        assert_fits(markup)
        dumped = markup.model_dump(exclude_none=True)["inline_keyboard"]
        self.assertEqual(
            dumped,
            [
                [
                    {"text": "✖️ Cancel", "callback_data": "sub:open:7"},
                    {"text": "🗑 Delete", "callback_data": "sub:confirm_delete:7", "style": "danger"},
                ]
            ],
        )

    def test_wizard_and_prompt_pickers(self):
        for markup in (
            build_currency_keyboard(("RUB", "USD", "EUR", "TRY")),
            build_currency_keyboard(("RUB", "USD", "EUR", "TRY"), selected="usd"),
            build_period_keyboard(),
            build_share_limit_keyboard(),
            build_share_limit_keyboard(everyone_selected=True),
            build_creation_payment_mode_keyboard(),
            build_creation_payment_mode_keyboard(selected=PAYMENT_MODE_SPLIT),
            build_creation_payment_mode_keyboard(selected=PAYMENT_MODE_FIXED),
            *(build_period_keyboard(selected=days) for days in (7, 14, 30, 90, MONTHLY_PERIOD_SENTINEL)),
        ):
            with self.subTest(rows=_texts(markup)):
                assert_fits(markup, USER_TEXTS)
                self.assertEqual(_texts(markup)[-1], ["✖️ Cancel"])
                self.assertEqual(_styled(markup), [])
                for row in markup.inline_keyboard:
                    for button in row:
                        self.assertNotIn("…", button.text)

        self.assertEqual(_texts(build_currency_keyboard(("RUB", "USD", "EUR", "TRY"))), [["RUB", "USD"], ["EUR", "TRY"], ["✖️ Cancel"]])
        self.assertEqual(
            _texts(build_currency_keyboard(("RUB", "USD", "EUR", "TRY"), selected="USD")),
            [["RUB", "✅ USD"], ["EUR", "TRY"], ["✖️ Cancel"]],
        )
        self.assertEqual(
            _texts(build_period_keyboard(selected=MONTHLY_PERIOD_SENTINEL)),
            [["7 days", "14 days"], ["30 days", "90 days"], ["✅ Monthly"], ["✖️ Cancel"]],
        )
        self.assertEqual(
            _callbacks(build_period_keyboard()),
            [["period:7", "period:14"], ["period:30", "period:90"], ["period:month"], ["dialog:cancel"]],
        )
        self.assertEqual(_callbacks(build_share_limit_keyboard()), [["sharelimit:all"], ["dialog:cancel"]])
        self.assertEqual(
            _callbacks(build_creation_payment_mode_keyboard()),
            [["create_payment_mode:split", "create_payment_mode:fixed"], ["dialog:cancel"]],
        )

    def test_prompt_builders_name_the_current_value_and_take_the_wizard_cancel(self):
        text, markup = currency_prompt(cancel_callback=WIZARD_CANCEL_CALLBACK)
        self.assertEqual(text, "✏️ Currency? Tap one or type three letters.")
        self.assertEqual(_callbacks(markup)[-1], [WIZARD_CANCEL_CALLBACK])
        self.assertNotIn("force_reply", markup.model_dump(exclude_none=True))
        self.assertEqual(currency_prompt("eur")[0], "✏️ Currency? Now EUR. Tap one or type three letters.")
        self.assertNotIn("force_reply", currency_prompt()[1].model_dump(exclude_none=True))

        text, markup = period_prompt(suggested=MONTHLY_PERIOD_SENTINEL)
        self.assertEqual(text, "✏️ How often? Tap one or send a number of days.\nMonthly = the same day each month.")
        self.assertIn(["✅ Monthly"], _texts(markup))
        text, markup = period_prompt(30)
        self.assertTrue(text.startswith("✏️ How often? Now every 30 days."), text)
        self.assertIn(["✅ 30 days", "90 days"], _texts(markup))

        self.assertEqual(
            payment_mode_prompt()[0],
            "✏️ How are amounts split?\n"
            "Split by shares — the amount is divided by weights.\n"
            "Fixed per user — everyone pays a set amount (by default the full amount).",
        )
        text, markup = share_limit_prompt()
        self.assertEqual(text, "✏️ How many people share it? Tap “Everyone” to split among all participants.")
        self.assertNotIn("empty", text)
        self.assertEqual(_texts(markup), [["Everyone"], ["✖️ Cancel"]])
        self.assertTrue(len(SubscriptionAction(action="confirm_delete", subscription_id=2**31).pack()) < 64)


# ---------------------------------------------------------------------- texts


class SubscriptionTextTests(unittest.TestCase):
    def test_list_text_counts_only_what_needs_attention(self):
        self.assertEqual(
            _build_subscription_list_text(4, 1, 1),
            "📋 <b>Subscriptions</b> · 4\n\n⚠️ Unpaid: <b>1</b>\n👥 No participants: <b>1</b>\n\n"
            "Tap a subscription to manage it.",
        )
        self.assertEqual(_build_subscription_list_text(3, 0, 0), "📋 <b>Subscriptions</b> · 3\n\nTap a subscription to manage it.")
        self.assertEqual(_build_subscription_list_text(0, 0, 0), "📋 <b>Subscriptions</b>\n\nNo subscriptions yet.")

    def test_wizard_steps(self):
        self.assertEqual(
            wizard_text({}, TZ, 1),
            "➕ <b>New subscription</b> · step 1 of 6\n\n✏️ Name? For example <code>Netflix</code>.",
        )
        self.assertEqual(
            wizard_text({"subscription_name": "Net <flix>", "amount": 1500.5}, TZ, 3),
            "➕ <b>New subscription</b> · step 3 of 6\n\n"
            f"{I}Name: Net &lt;flix&gt;\n{I}Amount: 1{NBSP}500.50\n\n"
            "✏️ Currency? Tap one or type three letters.",
        )
        data = {
            "subscription_name": "Netflix",
            "amount": 15.99,
            "currency": "USD",
            "due_date": "2026-11-15",
            "period_days": MONTHLY_PERIOD_SENTINEL,
            "payment_mode": PAYMENT_MODE_SPLIT,
        }
        self.assertEqual(
            wizard_text(data, TZ, 7, "⚠️ Need a whole number, for example <code>4</code>, or tap “Everyone”."),
            "➕ <b>New subscription</b> · step 7 of 7\n\n"
            f"{I}Name: Netflix\n{I}Amount: 15.99{NBSP}USD\n"
            f"{I}Next charge: {tg_due('2026-11-15', TZ, 'wD')}\n{I}Repeats: every month\n{I}Amounts: split by shares\n\n"
            "✏️ How many people share it? Tap “Everyone” to split among all participants.\n"
            "⚠️ Need a whole number, for example <code>4</code>, or tap “Everyone”.",
        )
        self.assertIn("step 4 of 6\n", wizard_text({}, TZ, 4))
        self.assertIn(
            "✏️ Next charge date? <code>DD.MM.YYYY</code>, <code>DD.MM</code> or <code>today</code>.",
            wizard_text({}, TZ, 4),
        )
        self.assertIn("Repeats: every 14 days", wizard_text({"period_days": 14}, TZ, 6))

    def test_typed_period_and_share_limit(self):
        self.assertEqual(parse_period_input("Monthly"), MONTHLY_PERIOD_SENTINEL)
        self.assertEqual(parse_period_input(" 30 days "), 30)
        self.assertEqual(parse_period_input("14d"), 14)
        self.assertEqual(parse_period_input("1 day"), 1)
        for bad in ("0", "-5", "abc", "", None, "99999"):
            self.assertIsNone(parse_period_input(bad), bad)
        self.assertEqual(parse_share_limit_input("Everyone"), (True, None))
        self.assertEqual(parse_share_limit_input("4"), (True, 4))
        self.assertEqual(parse_share_limit_input(""), (False, None))
        self.assertEqual(parse_share_limit_input("0"), (False, None))

    def test_delete_question_names_the_object_and_what_goes(self):
        self.assertEqual(
            subscription_delete_question("Net<flix>", 2, 3),
            "⚠️ <b>Delete Net&lt;flix&gt;?</b>\n"
            "Its 2 open cycles and payment history are deleted; members stop getting reminders.",
        )
        self.assertEqual(
            subscription_delete_question("iCloud", 1, 0),
            "⚠️ <b>Delete iCloud?</b>\nIts 1 open cycle and payment history are deleted.",
        )
        self.assertEqual(
            subscription_delete_question("iCloud", 0, 0),
            "⚠️ <b>Delete iCloud?</b>\nIts payment history is deleted.",
        )

    def _card(self, subscription, participants, *, cycle=None, today=date(2026, 11, 10)):
        return _build_subscription_detail_text(
            subscription, participants, "09:30", TZ, 1 if cycle else 0, "Sber · RUB", "", "",
            current_cycle=cycle, today=today,
        )

    def test_card_due_wording_tomorrow_and_folds(self):
        subscription = {
            "id": 1, "name": "Netflix", "amount": 70.0, "currency": "USD", "period_days": 7,
            "next_charge_at": "2026-11-11", "reminder_offsets": "-1,0", "remind_after_due": 0, "comment": "",
        }
        people = [{"full_name": f"Person {n}", "share_weight": 1} for n in range(7)]
        cycle = {
            "due_value": "2026-11-11", "paid": 1, "total": 7,
            "unpaid_names": [f"Person {n}" for n in range(1, 7)],
        }
        text = self._card(subscription, people, cycle=cycle)
        self.assertTrue(text.startswith(f"🏷️ <b>Netflix</b> · <b>70.00{NBSP}USD</b> · every 7 days\n"), text)
        self.assertIn(f"{I}Next charge: <b>{tg_due('2026-11-11', TZ, 'wD')}</b> (tomorrow)\n", text)
        self.assertIn(f"{I}Due: {tg_due('2026-11-11', TZ, 'wD')} (tomorrow)\n", text)
        self.assertIn(f"{I}█░░░░░░░░░ 1 of 7 paid\n", text)
        # Seven participants and six unpaid names fold into expandable quotes; counts stay visible.
        self.assertIn("👥 <b>Participants: 7</b>\n", text)
        self.assertIn("<blockquote expandable>• Person 0 — <b>≈ 10.00", text)
        self.assertIn(f"{I}Unpaid: <b>6</b>\n<blockquote expandable>• Person 1\n", text)
        self.assertIn(f"{I}Payment method: Sber · RUB", text)
        self.assertNotIn("Open cycles:", text)
        self.assertNotIn("<code>", text)
        self.assertLess(len(text), 4096)

        cycle_done = {"due_value": "2026-11-11", "paid": 7, "total": 7, "unpaid_names": []}
        done = self._card(subscription, people, cycle=cycle_done)
        self.assertIn(f"{I}██████████ all paid", done)
        self.assertNotIn("Unpaid", done)

        far = dict(subscription, next_charge_at="2026-11-20", period_days=MONTHLY_PERIOD_SENTINEL, monthly_anchor_day=31)
        text = self._card(far, people[:1])
        self.assertIn(f"({tg_due('2026-11-20', TZ, 'r', 'in 10 days')})", text)
        self.assertIn(f"{I}Repeats: every month on the 31st\n", text)
        self.assertIn(" · monthly\n", text)

    def test_long_comment_folds_and_requisites_show_their_first_line(self):
        subscription = {
            "id": 1, "name": "Netflix", "amount": 10.0, "currency": "USD", "period_days": 30,
            "next_charge_at": "2026-11-20", "comment": "x " * 400,
        }
        text = _build_subscription_detail_text(
            subscription, [], "09:30", TZ, 0, "⭐ Sber · RUB", "Card 2200 0000\nSecond line", "",
            today=date(2026, 11, 10),
        )
        self.assertIn(
            f"{I}Payment method: ⭐ Sber · RUB\n{I}<code>Card 2200 0000</code> (more in “💳 Payment”)\n"
            f"{I}📝 Comment\n<blockquote expandable>",
            text,
        )
        self.assertNotIn("Second line", text)


# ----------------------------------------------------------------------- flows


def _cb(action: str, subscription_id: int) -> str:
    return SubscriptionAction(action=action, subscription_id=subscription_id).pack()


class SubscriptionFlowTests(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin()
        self.today = datetime.now(ZoneInfo("Europe/Moscow")).date()

    def _edits(self) -> list[EditMessageText]:
        return self.bot.of(EditMessageText)

    def _toasts(self) -> list[str | None]:
        return [answer.text for answer in self.bot.answers()]

    async def _netflix(self) -> int:
        return await self.db.create_subscription("Netflix", 15.99, "USD", self.today + timedelta(days=5), 30, None)

    async def test_wizard_is_one_message_from_name_to_card(self):
        await self.feed(self.message("➕ Add subscription"))
        sent = self.bot.of(SendMessage)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].text, "➕ <b>New subscription</b> · step 1 of 6\n\n✏️ Name? For example <code>Netflix</code>.")
        self.assertNotIn("force_reply", sent[0].reply_markup.model_dump(exclude_none=True))
        self.assertEqual(_callbacks(sent[0].reply_markup), [[WIZARD_CANCEL_CALLBACK]])
        state = self.state()
        wizard_id = (await state.get_data())["wizard_message_id"]
        wizard = make_message(self.bot, wizard_id, sent[0].text, from_bot=True)

        name = self.message("Netflix")
        await self.feed(name)
        self.assertIn(name.message_id, self.bot.deleted_ids())
        self.assertEqual(self._edits()[-1].message_id, wizard_id)
        self.assertIn("step 2 of 6", self._edits()[-1].text)

        typo = self.message("lots")
        await self.feed(typo)
        self.assertIn(typo.message_id, self.bot.deleted_ids())
        self.assertTrue(self._edits()[-1].text.endswith("⚠️ Need a number, for example <code>149.99</code>."))
        self.assertEqual(await state.get_state(), SubscriptionForm.amount.state)

        await self.feed(self.message("1 500,50"))
        self.assertIn("step 3 of 6", self._edits()[-1].text)
        self.assertEqual(_texts(self._edits()[-1].reply_markup)[:2], [["RUB", "USD"], ["EUR", "TRY"]])

        await self.feed(self.callback("currency:USD", wizard))
        self.assertEqual(self._toasts()[-1], "Currency: USD")
        self.assertIn(f"Amount: 1{NBSP}500.50{NBSP}USD", self._edits()[-1].text)

        await self.feed(self.message("today"))
        self.assertIn("step 5 of 6", self._edits()[-1].text)
        await self.feed(self.callback("period:month", wizard))
        self.assertIn("step 6 of 6", self._edits()[-1].text)

        sends_before = len(self.bot.of(SendMessage))
        await self.feed(self.callback("create_payment_mode:fixed", wizard))
        self.assertEqual(self._toasts()[-1], "Subscription saved")
        final = self._edits()[-1]
        self.assertEqual(final.message_id, wizard_id)
        self.assertTrue(final.text.startswith("✅ <b>Netflix created</b>\n\n🏷️ <b>Netflix</b> · "), final.text)
        self.assertEqual(_texts(final.reply_markup)[0], ["👥 Add participants"])
        self.assertEqual(len(self.bot.of(SendMessage)), sends_before)  # no "Subscription saved." message
        self.assertIsNone(await state.get_state())
        subs = await self.db.list_subscriptions()
        self.assertEqual([(s["name"], s["amount"], s["currency"], s["period_days"]) for s in subs],
                         [("Netflix", 1500.5, "USD", MONTHLY_PERIOD_SENTINEL)])
        self.assertEqual(subs[0]["payment_mode"], PAYMENT_MODE_FIXED)

        # A late duplicate tap on the old step button never deletes the new card or creates twice.
        await self.feed(self.callback("create_payment_mode:fixed", wizard))
        self.assertNotIn(wizard_id, self.bot.deleted_ids())
        self.assertIsNone(self._toasts()[-1])
        self.assertEqual(len(await self.db.list_subscriptions()), 1)

    async def test_wizard_split_asks_share_limit_and_warns_about_duplicates_and_past_dates(self):
        await self._netflix()
        await self.feed(self.message("➕ Add subscription"))
        state = self.state()
        await self.feed(self.message("netflix"))
        self.assertTrue(self._edits()[-1].text.endswith("⚠️ You already have netflix. Send it again to keep it."))
        self.assertEqual(await state.get_state(), SubscriptionForm.name.state)
        await self.feed(self.message("netflix"))
        self.assertIn("step 2 of 6", self._edits()[-1].text)
        await self.feed(self.message("10"))
        await self.feed(self.message("eur"))
        past = (self.today - timedelta(days=3)).strftime("%d.%m.%Y")
        await self.feed(self.message(past))
        self.assertTrue(self._edits()[-1].text.endswith("⚠️ That date is in the past — send it again to keep it."))
        await self.feed(self.message(past))
        await self.feed(self.message("14 days"))
        await self.feed(self.message("split"))
        self.assertIn("step 7 of 7", self._edits()[-1].text)
        await self.feed(self.message("3"))
        created = [s for s in await self.db.list_subscriptions() if s["name"] == "netflix"]
        self.assertEqual(len(created), 1)
        self.assertEqual((created[0]["share_limit"], created[0]["period_days"]), (3, 14))
        self.assertIn("✅ <b>netflix created</b>", self._edits()[-1].text)

    async def test_wizard_cancel_and_stale_step_buttons(self):
        await self.feed(self.message("➕ Add subscription"))
        state = self.state()
        wizard_id = (await state.get_data())["wizard_message_id"]
        wizard = make_message(self.bot, wizard_id, "wizard", from_bot=True)
        await self.feed(self.callback(WIZARD_CANCEL_CALLBACK, wizard))
        self.assertIn(wizard_id, self.bot.deleted_ids())
        self.assertEqual(self._toasts()[-1], "Cancelled")
        self.assertIsNone(await state.get_state())

        for data in ("currency:USD", "period:30", "sharelimit:all", "create_payment_mode:split"):
            with self.subTest(data=data):
                old = make_message(self.bot, 900 + len(data), "old prompt", from_bot=True)
                await self.feed(self.callback(data, old))
                self.assertEqual(self._toasts()[-1], STALE_STEP_TOAST)
                self.assertIn(old.message_id, self.bot.deleted_ids())
        self.assertEqual(await self.db.list_subscriptions(), [])

    async def test_open_back_and_paging_remember_the_page(self):
        ids = [
            await self.db.create_subscription(f"Sub {n:02d}", 1.0, "USD", self.today + timedelta(days=3), 30, None)
            for n in range(SUBSCRIPTION_LIST_PAGE_SIZE + 3)
        ]
        await self.feed(self.message("📋 Subscriptions"))
        first = self.bot.of(SendMessage)[-1]
        self.assertTrue(first.text.startswith(f"📋 <b>Subscriptions</b> · {len(ids)}\n"), first.text)
        self.assertEqual(_texts(first.reply_markup)[-1], ["◀️", "1 / 2", "▶️"])

        await self.feed(self.callback("sublist:page:2"))
        self.assertEqual(_texts(self._edits()[-1].reply_markup)[-1], ["◀️", "2 / 2", "▶️"])

        await self.feed(self.callback(_cb("open", ids[-1])))
        self.assertTrue(self._edits()[-1].text.startswith("🏷️ <b>Sub 22</b>"))
        await self.feed(self.callback(_cb("back", ids[-1])))
        self.assertEqual(_texts(self._edits()[-1].reply_markup)[-1], ["◀️", "2 / 2", "▶️"])

    async def test_open_new_sends_a_card_with_close_and_leaves_the_push(self):
        sub_id = await self._netflix()
        push = make_message(self.bot, 321, "push", from_bot=True)
        await self.feed(self.callback(_cb("open_new", sub_id), push))
        self.assertEqual(self._edits(), [])
        card = self.bot.of(SendMessage)[-1]
        self.assertTrue(card.text.startswith("🏷️ <b>Netflix</b>"))
        self.assertEqual(_texts(card.reply_markup)[-1], ["✖️ Close"])

        await self.db.delete_subscription(sub_id)
        await self.feed(self.callback(_cb("open_new", sub_id), push))
        self.assertEqual(self._toasts()[-1], SUBSCRIPTION_GONE)
        self.assertEqual(self._edits(), [])

    async def test_stale_card_redraws_the_list(self):
        await self.feed(self.callback(_cb("open", 999)))
        self.assertEqual(self._toasts(), [SUBSCRIPTION_GONE])
        self.assertEqual(self._edits()[-1].text, "📋 <b>Subscriptions</b>\n\nNo subscriptions yet.")
        await self.feed(self.callback(_cb("rename", 999)))
        self.assertEqual(self._toasts()[-1], SUBSCRIPTION_GONE)

    async def test_rename_edits_the_origin_card_with_a_banner(self):
        sub_id = await self._netflix()
        card_message = make_message(self.bot, 555, "card", from_bot=True)
        await self.feed(self.callback(_cb("rename", sub_id), card_message))
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, "✏️ New name for Netflix?")
        self.assertEqual(_texts(prompt.reply_markup), [["✖️ Cancel"]])
        data = await self.state().get_data()
        prompt_id = data["prompt_message_id"]

        blank = self.message("   ")
        await self.feed(blank)
        self.assertEqual(self.bot.sent_texts()[-1], "⚠️ Send text, for example <code>Netflix Premium</code>.")

        answer = self.message("Netflix  Premium")
        await self.feed(answer)
        self.assertEqual((await self.db.get_subscription(sub_id))["name"], "Netflix Premium")
        edit = self._edits()[-1]
        self.assertEqual(edit.message_id, 555)
        self.assertTrue(edit.text.startswith("✅ Renamed · <b>Netflix Premium</b>\n\n🏷️ <b>Netflix Premium</b>"), edit.text)
        for message_id in (prompt_id, answer.message_id, blank.message_id):
            self.assertIn(message_id, self.bot.deleted_ids())
        self.assertNotIn("Name updated.", self.bot.sent_texts())
        self.assertIsNone(await self.state().get_state())

    async def test_delete_confirms_on_the_card_then_lands_on_the_list(self):
        sub_id = await self._netflix()
        card_message = make_message(self.bot, 556, "card", from_bot=True)
        await self.feed(self.callback(_cb("delete", sub_id), card_message))
        question = self._edits()[-1]
        self.assertTrue(
            question.text.startswith("⚠️ <b>Delete Netflix?</b>\nIts payment history is deleted.\n\n🏷️ <b>Netflix</b>"),
            question.text,
        )
        self.assertEqual(_callbacks(question.reply_markup), [[_cb("open", sub_id), _cb("confirm_delete", sub_id)]])

        async with guarded(None, ("sub_delete", sub_id)):
            await self.feed(self.callback(_cb("confirm_delete", sub_id), card_message))
        self.assertEqual(self._toasts()[-1], ALREADY_RUNNING_TEXT)
        self.assertIsNotNone(await self.db.get_subscription(sub_id))

        await self.feed(self.callback(_cb("confirm_delete", sub_id), card_message))
        self.assertIsNone(await self.db.get_subscription(sub_id))
        self.assertEqual(self._toasts()[-1], "✅ Netflix deleted")
        self.assertEqual(self._edits()[-1].text, "✅ Netflix deleted\n\n📋 <b>Subscriptions</b>\n\nNo subscriptions yet.")

        await self.feed(self.callback(_cb("confirm_delete", sub_id), card_message))
        self.assertEqual(self._toasts()[-1], SUBSCRIPTION_GONE)

    async def test_card_reflects_the_cycle_and_open_cycles(self):
        sub_id = await self._netflix()
        friend_id = await self.db.create_friend_invite("Bob Stone", "token-bob", "2099-01-01")
        await self.db.claim_friend_invite(friend_id, 9001, "2026-01-01")
        await self.db.set_participant(sub_id, friend_id, True)
        await self.db.ensure_cycle(sub_id, self.today - timedelta(days=1))
        card = await build_subscription_card(self.db, sub_id)
        text, markup = card
        self.assertIn("🧾 <b>Current cycle</b>\n", text)
        self.assertIn(f"{I}Unpaid: Bob S.\n", text)
        self.assertIn("(overdue by 1 day)", text)
        self.assertEqual(_texts(markup)[0], ["💰 Pricing", "👥 Participants"])


if __name__ == "__main__":
    unittest.main()
