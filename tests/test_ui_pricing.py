"""WP4: Pricing & schedule — the screen, its pickers, the typed prompts and the next-charge conflict."""

from datetime import date, datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from aiogram.methods import EditMessageText, SendMessage

from app.core.constants import MONTHLY_PERIOD_SENTINEL
from app.core.reminders import tg_due
from app.handlers.subscriptions import (
    CURRENCY_OTHER_HINT,
    CYCLE_GONE_TOAST,
    CYCLE_MOVED_BANNER,
    MOVE_BLOCKED_ALERT,
    NEXT_CHARGE_HINT,
    PERIOD_EDIT_HINT,
    STALE_STEP_TOAST,
    amount_prompt_text,
    currency_menu_text,
    currency_other_prompt_text,
    due_date_conflict_text,
    due_date_prompt_text,
    dropped_cycles_warning,
    new_cycle_banner,
    next_charge_banner,
    period_edit_prompt_text,
    shown_in_menu_text,
    shown_in_other_prompt_text,
)
from app.ui.guards import ALREADY_RUNNING_TEXT, guarded
from app.ui.helpers import SUBSCRIPTION_GONE, pricing_text, resolve_report_timezone
from app.ui.keyboards import (
    build_due_date_change_keyboard,
    build_period_keyboard,
    pricing_settings_keyboard,
    subscription_base_currency_keyboard,
    subscription_currency_keyboard,
)
from app.ui.states import DueDateChangeAction, SubscriptionAction, SubscriptionEditForm
from app.ui.style import NBSP
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import make_message
from test_keyboards_fit import NAME_64, assert_fits

install_keyboard_guard()

TZ = "UTC"
BIG_ID = 2**31 - 1


def _texts(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _callbacks(markup) -> list[list[str | None]]:
    return [[button.callback_data for button in row] for row in markup.inline_keyboard]


def _styled(markup) -> list[tuple[str, str]]:
    return [(button.text, button.style) for row in markup.inline_keyboard for button in row if button.style]


def _cb(action: str, subscription_id: int) -> str:
    return SubscriptionAction(action=action, subscription_id=subscription_id).pack()


def _sub(**fields) -> dict:
    sub = {
        "id": 7,
        "name": "Netflix",
        "amount": 15.99,
        "currency": "USD",
        "base_currency": "USD",
        "next_charge_at": "2026-11-15",
        "period_days": MONTHLY_PERIOD_SENTINEL,
        "monthly_anchor_day": 15,
    }
    sub.update(fields)
    return sub


# ------------------------------------------------------------------ keyboards


class PricingKeyboardTests(unittest.TestCase):
    def test_pricing_keyboard_rows_and_unchanged_callbacks(self):
        markup = pricing_settings_keyboard(7)
        assert_fits(markup)
        self.assertEqual(
            _texts(markup),
            [["💰 Amount", "💱 Currency"], ["🌐 Shown in", "📅 Next charge"], ["🔁 Period"], ["◀️ To card"]],
        )
        self.assertEqual(
            _callbacks(markup),
            [
                ["sub:amount:7", "sub:currency:7"],
                ["sub:basecurrency:7", "sub:duedate:7"],
                ["sub:period:7"],
                ["sub:open:7"],
            ],
        )
        self.assertEqual(_styled(markup), [])
        assert_fits(pricing_settings_keyboard(BIG_ID))

    def test_currency_picker_marks_the_current_code_without_colour(self):
        for current in ("USD", "eur", "RUB", "TRY", "CHF", ""):
            with self.subTest(current=current):
                markup = subscription_currency_keyboard(BIG_ID, current)
                assert_fits(markup)
                self.assertEqual(_styled(markup), [])
                self.assertEqual(_texts(markup)[-2:], [["✏️ Other currency"], ["◀️ Back"]])
        self.assertEqual(
            _texts(subscription_currency_keyboard(7, "USD")),
            [["RUB", "✅ USD"], ["EUR", "TRY"], ["✏️ Other currency"], ["◀️ Back"]],
        )
        self.assertEqual(
            _callbacks(subscription_currency_keyboard(7, "CHF")),
            [
                ["sub_currency:7:RUB", "sub_currency:7:USD"],
                ["sub_currency:7:EUR", "sub_currency:7:TRY"],
                ["sub_currency_other:7"],
                ["sub:pricing:7"],
            ],
        )

    def test_shown_in_picker(self):
        for current in ("USD", "EUR", "RUB", "CHF"):
            with self.subTest(current=current):
                markup = subscription_base_currency_keyboard(BIG_ID, current)
                assert_fits(markup)
                self.assertEqual(_styled(markup), [])
        markup = subscription_base_currency_keyboard(7, "usd")
        # One option list and layout for every currency picker.
        self.assertEqual(_texts(markup), [["RUB", "✅ USD"], ["EUR", "TRY"], ["✏️ Other currency"], ["◀️ Back"]])
        self.assertEqual(
            _callbacks(markup),
            [
                ["sub_base_currency:7:RUB", "sub_base_currency:7:USD"],
                ["sub_base_currency:7:EUR", "sub_base_currency:7:TRY"],
                ["sub:basecurrency_other:7"],
                ["sub:pricing:7"],
            ],
        )

    def test_conflict_keyboard(self):
        markup = build_due_date_change_keyboard(BIG_ID, "2026-10-01", "2026-11-15")
        assert_fits(markup)
        self.assertEqual(_texts(markup), [["🔀 Move cycle"], ["🆕 New cycle"], ["✖️ Cancel"]])
        self.assertEqual(
            _callbacks(markup),
            [
                [f"duedate:move:{BIG_ID}:2026-10-01:2026-11-15"],
                [f"duedate:new:{BIG_ID}:2026-10-01:2026-11-15"],
                [f"sub:pricing:{BIG_ID}"],
            ],
        )
        self.assertEqual(_styled(markup), [])
        for row in _callbacks(markup):
            self.assertLess(len(row[0].encode()), 64)

    def test_period_prompt_keyboard_marks_the_current_period(self):
        for current in (7, 14, 30, 90, MONTHLY_PERIOD_SENTINEL, 45):
            with self.subTest(current=current):
                assert_fits(build_period_keyboard(selected=current))
        self.assertEqual(
            _texts(build_period_keyboard(selected=30)),
            [["7 days", "14 days"], ["✅ 30 days", "90 days"], ["Monthly"], ["✖️ Cancel"]],
        )


# ---------------------------------------------------------------------- texts


class PricingTextTests(unittest.TestCase):
    today = date(2026, 11, 10)

    def test_pricing_text_monthly_in_five_days(self):
        due = "2026-11-15"
        self.assertEqual(
            pricing_text(_sub(base_currency="EUR"), TZ, self.today),
            "💰 <b>Pricing · Netflix</b>\n\n"
            f"Amount: <b>15.99{NBSP}USD</b>\n"
            "Shown to members in: EUR\n"
            f"Next charge: <b>{tg_due(due, TZ, 'wD')}</b> ({tg_due(due, TZ, 'r', 'in 5 days')})\n"
            "Repeats: every month on the 15th",
        )

    def test_pricing_text_days_period_today_and_overdue(self):
        today_text = pricing_text(_sub(name="A & B", period_days=30, next_charge_at="2026-11-10", base_currency=None), TZ, self.today)
        self.assertTrue(today_text.startswith("💰 <b>Pricing · A &amp; B</b>\n\n"), today_text)
        self.assertIn("\nShown to members in: USD\n", today_text)
        self.assertIn(f"Next charge: <b>{tg_due('2026-11-10', TZ, 'wD')}</b> (today)\n", today_text)
        self.assertTrue(today_text.endswith("\nRepeats: every 30 days"))
        overdue = pricing_text(_sub(next_charge_at="2026-11-09", amount=12500), TZ, self.today)
        self.assertIn("(overdue by 1 day)", overdue)
        self.assertIn(f"Amount: <b>12{NBSP}500.00{NBSP}USD</b>", overdue)
        self.assertNotIn("<code>", overdue)
        long_name = pricing_text(_sub(name=NAME_64), TZ, self.today)
        self.assertTrue(long_name.startswith(f"💰 <b>Pricing · {NAME_64}</b>\n\n"))
        self.assertLess(len(long_name), 1000)

    def test_picker_texts(self):
        self.assertEqual(
            currency_menu_text(_sub()),
            "💱 <b>Currency · Netflix</b>\n\nNow: USD\n⚠️ Changing the currency does not convert the amount (15.99).",
        )
        self.assertEqual(
            shown_in_menu_text(_sub(base_currency="EUR")),
            "🌐 <b>Shown in · Netflix</b>\n\nNow: EUR\nMembers see their share converted to this currency in "
            "reminders (they can pick their own on their card).",
        )
        self.assertIn("Now: USD\n", shown_in_menu_text(_sub(base_currency=None)))

    def test_prompts(self):
        self.assertEqual(
            amount_prompt_text(_sub()),
            "✏️ New amount for Netflix, USD? Now 15.99.\nExample: <code>149.99</code>",
        )
        self.assertEqual(
            amount_prompt_text(_sub(), "2026-10-01"),
            "✏️ New amount for Netflix, USD? Now 15.99.\nExample: <code>149.99</code>\n"
            "⚠️ The open cycle (01.10) keeps its old amount.",
        )
        self.assertEqual(
            currency_other_prompt_text(_sub()),
            "✏️ Currency for Netflix? Three letters, for example <code>CHF</code>.",
        )
        self.assertEqual(
            shown_in_other_prompt_text(_sub(base_currency="EUR")),
            "✏️ Show Netflix to members in which currency? Now EUR. Three letters, for example <code>EUR</code>.",
        )
        self.assertEqual(
            due_date_prompt_text(_sub(), TZ, []),
            f"✏️ Next charge date for Netflix? Now {tg_due('2026-11-15', TZ, 'wD')}.\n"
            "<code>DD.MM.YYYY</code>, <code>DD.MM</code>, <code>today</code> or <code>tomorrow</code>.",
        )
        self.assertTrue(
            due_date_prompt_text(_sub(), TZ, ["2026-10-01"]).endswith("\n⚠️ The unpaid 01.10 cycle will be dropped.")
        )
        self.assertEqual(
            dropped_cycles_warning(["2026-09-01", "2026-10-01"]), "⚠️ The unpaid 01.09 and 01.10 cycles will be dropped."
        )
        self.assertEqual(
            dropped_cycles_warning(["2026-08-01", "2026-09-01", "2026-10-01"]),
            "⚠️ The unpaid 01.08, 01.09 and 01.10 cycles will be dropped.",
        )
        self.assertEqual(dropped_cycles_warning(["a", "b", "c", "d"]), "⚠️ 4 unpaid cycles will be dropped.")
        self.assertIsNone(dropped_cycles_warning([]))
        self.assertEqual(period_edit_prompt_text(_sub()), "✏️ How often does Netflix repeat? Now monthly.")
        self.assertEqual(period_edit_prompt_text(_sub(period_days=1)), "✏️ How often does Netflix repeat? Now every 1 day.")

    def test_conflict_text_has_no_hard_wraps_and_uses_tg_time(self):
        old, new = "2026-10-01", "2026-11-15"
        self.assertEqual(
            due_date_conflict_text({"due_date": old, "closed": False, "payments": 2}, new, TZ),
            "📅 <b>Change the next charge?</b>\n\n"
            f"Current cycle: {tg_due(old, TZ, 'wD')} · 2 payments recorded\n"
            f"New date: {tg_due(new, TZ, 'wD')}\n\n"
            "🔀 Move cycle — same period, payments carry over.\n"
            "🆕 New cycle — keep the old cycle, ask for payment on the new date.",
        )
        closed = due_date_conflict_text({"due_date": old, "closed": True, "payments": 1}, new, TZ)
        self.assertIn(f"Current cycle: {tg_due(old, TZ, 'wD')} · closed · 1 payment recorded\n", closed)
        self.assertNotIn("<code>", closed)

    def test_banners(self):
        self.assertEqual(next_charge_banner("2026-11-15", TZ), f"✅ Next charge · <b>{tg_due('2026-11-15', TZ, 'wD')}</b>")
        self.assertEqual(new_cycle_banner("2026-11-15", TZ), f"✅ New cycle on {tg_due('2026-11-15', TZ, 'wD')}")
        self.assertEqual(CYCLE_MOVED_BANNER, "✅ Cycle moved · payments kept")


# ----------------------------------------------------------------------- flows


class PricingFlowTests(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin()
        self.tz = await resolve_report_timezone(self.db)
        self.today = datetime.now(ZoneInfo(self.tz)).date()
        self.due = self.today + timedelta(days=5)
        self.sub_id = await self.db.create_subscription("Netflix", 15.99, "USD", self.due, 30, None)
        self.screen = make_message(self.bot, 600, "pricing", from_bot=True)

    def _edits(self) -> list[EditMessageText]:
        return self.bot.of(EditMessageText)

    def _toasts(self) -> list[str | None]:
        return [answer.text for answer in self.bot.answers()]

    async def _sub(self) -> dict:
        return await self.db.get_subscription(self.sub_id)

    async def _open_prompt(self, action: str) -> tuple[SendMessage, int]:
        await self.feed(self.callback(_cb(action, self.sub_id), self.screen))
        prompt = self.bot.of(SendMessage)[-1]
        data = await self.state().get_data()
        self.assertEqual(data["origin_message_id"], 600)
        self.assertEqual(data["origin_screen"], f"pricing:{self.sub_id}")
        return prompt, data["prompt_message_id"]

    def _assert_pricing_edit(self, banner: str) -> EditMessageText:
        edit = self._edits()[-1]
        self.assertEqual(edit.message_id, 600)
        self.assertTrue(edit.text.startswith(f"{banner}\n\n💰 <b>Pricing · Netflix</b>\n\n"), edit.text)
        self.assertEqual(_texts(edit.reply_markup)[-1], ["◀️ To card"])
        return edit

    async def test_pricing_opens_from_the_card_and_goes_back(self):
        await self.feed(self.callback(_cb("pricing", self.sub_id), self.screen))
        edit = self._edits()[-1]
        self.assertEqual(
            edit.text,
            f"💰 <b>Pricing · Netflix</b>\n\nAmount: <b>15.99{NBSP}USD</b>\nShown to members in: USD\n"
            f"Next charge: <b>{tg_due(self.due.isoformat(), self.tz, 'wD')}</b> "
            f"({tg_due(self.due.isoformat(), self.tz, 'r', 'in 5 days')})\nRepeats: every 30 days",
        )
        self.assertEqual(self._toasts(), [None])
        await self.feed(self.callback(_cb("open", self.sub_id), self.screen))
        self.assertTrue(self._edits()[-1].text.startswith("🏷️ <b>Netflix</b>"))

    async def test_amount_prompt_typo_and_success_edit_pricing(self):
        prompt, prompt_id = await self._open_prompt("amount")
        self.assertEqual(prompt.text, "✏️ New amount for Netflix, USD? Now 15.99.\nExample: <code>149.99</code>")
        self.assertEqual(_texts(prompt.reply_markup), [["✖️ Cancel"]])
        self.assertNotIn("force_reply", prompt.reply_markup.model_dump(exclude_none=True))

        typo = self.message("lots")
        await self.feed(typo)
        self.assertEqual(self.bot.sent_texts()[-1], "⚠️ Need a number, for example <code>149.99</code>.")
        hint_id = (await self.state().get_data())["cleanup_ids"][-1]
        zero = self.message("0")
        await self.feed(zero)
        self.assertEqual(self.bot.sent_texts()[-1], "⚠️ The amount must be more than 0, for example <code>149.99</code>.")

        answer = self.message("1 249,99")
        await self.feed(answer)
        self.assertEqual((await self._sub())["amount"], 1249.99)
        self._assert_pricing_edit(f"✅ Amount · <b>1{NBSP}249.99{NBSP}USD</b>")
        for message_id in (prompt_id, typo.message_id, hint_id, zero.message_id, answer.message_id):
            self.assertIn(message_id, self.bot.deleted_ids())
        self.assertIsNone(await self.state().get_state())
        self.assertNotIn("Amount updated.", self.bot.sent_texts())

    async def test_amount_prompt_warns_about_a_frozen_open_cycle(self):
        await self.db.freeze_cycle_snapshot(self.sub_id, self.today - timedelta(days=2))
        prompt, _ = await self._open_prompt("amount")
        expected = (self.today - timedelta(days=2)).strftime("%d.%m")
        self.assertTrue(prompt.text.endswith(f"\n⚠️ The open cycle ({expected}) keeps its old amount."), prompt.text)

    async def test_currency_preset_returns_to_pricing(self):
        await self.feed(self.callback(_cb("currency", self.sub_id), self.screen))
        menu = self._edits()[-1]
        self.assertEqual(
            menu.text,
            "💱 <b>Currency · Netflix</b>\n\nNow: USD\n⚠️ Changing the currency does not convert the amount (15.99).",
        )
        self.assertEqual(_texts(menu.reply_markup)[0], ["RUB", "✅ USD"])

        await self.feed(self.callback(f"sub_currency:{self.sub_id}:EUR", self.screen))
        self.assertEqual((await self._sub())["currency"], "EUR")
        self._assert_pricing_edit("✅ Currency · <b>EUR</b>")
        self.assertIn(f"Amount: <b>15.99{NBSP}EUR</b>", self._edits()[-1].text)
        self.assertEqual(self._toasts()[-1], "Currency: EUR")

    async def test_currency_other_prompt(self):
        await self.feed(self.callback(_cb("currency", self.sub_id), self.screen))
        await self.feed(self.callback(f"sub_currency_other:{self.sub_id}", self.screen))
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, "✏️ Currency for Netflix? Three letters, for example <code>CHF</code>.")
        self.assertEqual(await self.state().get_state(), SubscriptionEditForm.currency.state)
        await self.feed(self.message("francs"))
        self.assertEqual(self.bot.sent_texts()[-1], CURRENCY_OTHER_HINT)
        await self.feed(self.message("chf"))
        self.assertEqual((await self._sub())["currency"], "CHF")
        self._assert_pricing_edit("✅ Currency · <b>CHF</b>")
        self.assertIn(prompt.text, self.bot.sent_texts())
        self.assertIsNone(await self.state().get_state())

    async def test_shown_in_preset_and_other(self):
        await self.feed(self.callback(_cb("basecurrency", self.sub_id), self.screen))
        menu = self._edits()[-1]
        self.assertTrue(menu.text.startswith("🌐 <b>Shown in · Netflix</b>\n\nNow: USD\n"), menu.text)
        self.assertEqual(_texts(menu.reply_markup), [["RUB", "✅ USD"], ["EUR", "TRY"], ["✏️ Other currency"], ["◀️ Back"]])

        await self.feed(self.callback(f"sub_base_currency:{self.sub_id}:EUR", self.screen))
        self.assertEqual((await self._sub())["base_currency"], "EUR")
        edit = self._assert_pricing_edit("✅ Shown in · <b>EUR</b>")
        self.assertIn("\nShown to members in: EUR\n", edit.text)
        self.assertEqual(self._toasts()[-1], "Shown in: EUR")

        await self.feed(self.callback(_cb("basecurrency_other", self.sub_id), self.screen))
        self.assertEqual(
            self.bot.of(SendMessage)[-1].text,
            "✏️ Show Netflix to members in which currency? Now EUR. Three letters, for example <code>EUR</code>.",
        )
        await self.feed(self.message("try"))
        self.assertEqual((await self._sub())["base_currency"], "TRY")
        self._assert_pricing_edit("✅ Shown in · <b>TRY</b>")

    async def test_next_charge_without_conflict_drops_unpaid_cycles(self):
        unpaid = self.today - timedelta(days=3)
        await self.db.ensure_cycle(self.sub_id, unpaid)
        prompt, prompt_id = await self._open_prompt("duedate")
        self.assertEqual(
            prompt.text,
            f"✏️ Next charge date for Netflix? Now {tg_due(self.due.isoformat(), self.tz, 'wD')}.\n"
            "<code>DD.MM.YYYY</code>, <code>DD.MM</code>, <code>today</code> or <code>tomorrow</code>.\n"
            f"⚠️ The unpaid {unpaid.strftime('%d.%m')} cycle will be dropped.",
        )
        await self.feed(self.message("someday"))
        self.assertEqual(self.bot.sent_texts()[-1], NEXT_CHARGE_HINT)

        await self.feed(self.message("tomorrow"))
        tomorrow = (self.today + timedelta(days=1)).isoformat()
        self.assertEqual((await self._sub())["next_charge_at"], tomorrow)
        self.assertEqual(await self.db.list_open_cycles(self.sub_id), [])
        edit = self._assert_pricing_edit(f"✅ Next charge · <b>{tg_due(tomorrow, self.tz, 'wD')}</b>")
        self.assertIn("(tomorrow)", edit.text)
        self.assertIn(prompt_id, self.bot.deleted_ids())

    async def _conflict(self, new_due: date) -> EditMessageText:
        old = self.today - timedelta(days=2)
        await self.db.ensure_cycle(self.sub_id, old)
        await self.db.log_payment(self.sub_id, old, 9001)
        await self.db.log_payment(self.sub_id, old, 9002)
        await self.db.update_subscription_fields(self.sub_id, next_charge_at=old)
        await self._open_prompt("duedate")
        await self.feed(self.message(new_due.strftime("%d.%m.%Y")))
        question = self._edits()[-1]
        self.assertEqual(question.message_id, 600)
        self.assertEqual(
            question.text,
            "📅 <b>Change the next charge?</b>\n\n"
            f"Current cycle: {tg_due(old.isoformat(), self.tz, 'wD')} · 2 payments recorded\n"
            f"New date: {tg_due(new_due.isoformat(), self.tz, 'wD')}\n\n"
            "🔀 Move cycle — same period, payments carry over.\n"
            "🆕 New cycle — keep the old cycle, ask for payment on the new date.",
        )
        self.assertEqual(_texts(question.reply_markup), [["🔀 Move cycle"], ["🆕 New cycle"], ["✖️ Cancel"]])
        self.assertIsNone(await self.state().get_state())
        return question

    def _choice(self, action: str, old: date, new: date) -> str:
        return DueDateChangeAction(
            action=action, subscription_id=self.sub_id, old_due=old.isoformat(), new_due=new.isoformat()
        ).pack()

    async def test_conflict_move_keeps_payments(self):
        old, new = self.today - timedelta(days=2), self.today + timedelta(days=3)
        await self._conflict(new)
        async with guarded(None, ("duedate", self.sub_id)):
            await self.feed(self.callback(self._choice("move", old, new), self.screen))
        self.assertEqual(self._toasts()[-1], ALREADY_RUNNING_TEXT)

        await self.feed(self.callback(self._choice("move", old, new), self.screen))
        self._assert_pricing_edit(CYCLE_MOVED_BANNER)
        self.assertEqual(self._toasts()[-1], CYCLE_MOVED_BANNER)
        self.assertEqual(await self.db.count_cycle_payments(self.sub_id, new), 2)
        self.assertEqual((await self._sub())["next_charge_at"], new.isoformat())

        # A late second tap: the old cycle is gone, so Pricing is redrawn with a toast.
        await self.feed(self.callback(self._choice("move", old, new), self.screen))
        self.assertEqual(self._toasts()[-1], CYCLE_GONE_TOAST)
        self.assertTrue(self._edits()[-1].text.startswith("💰 <b>Pricing · Netflix</b>"))

    async def test_conflict_move_blocked_by_payments_on_the_new_date(self):
        old, new = self.today - timedelta(days=2), self.today + timedelta(days=3)
        await self._conflict(new)
        await self.db.ensure_cycle(self.sub_id, new)
        await self.db.log_payment(self.sub_id, new, 9003)
        edits_before = len(self._edits())
        await self.feed(self.callback(self._choice("move", old, new), self.screen))
        answer = self.bot.answers()[-1]
        self.assertEqual((answer.text, answer.show_alert), (MOVE_BLOCKED_ALERT, True))
        self.assertEqual(len(self._edits()), edits_before)

    async def test_conflict_new_cycle_and_cancel(self):
        old, new = self.today - timedelta(days=2), self.today + timedelta(days=3)
        await self._conflict(new)
        await self.feed(self.callback(_cb("pricing", self.sub_id), self.screen))
        self.assertTrue(self._edits()[-1].text.startswith("💰 <b>Pricing · Netflix</b>"))
        self.assertEqual((await self._sub())["next_charge_at"], old.isoformat())

        await self.feed(self.callback(self._choice("new", old, new), self.screen))
        banner = new_cycle_banner(new.isoformat(), self.tz)
        self._assert_pricing_edit(banner)
        self.assertEqual(self._toasts()[-1], f"✅ New cycle on {new.strftime('%d.%m.%Y')}")
        self.assertEqual((await self._sub())["next_charge_at"], new.isoformat())
        self.assertIn(old.isoformat(), await self.db.list_open_cycles(self.sub_id))

    async def test_period_preset_on_the_prompt_edits_pricing(self):
        prompt, prompt_id = await self._open_prompt("period")
        self.assertEqual(prompt.text, "✏️ How often does Netflix repeat? Now every 30 days.")
        self.assertEqual(
            _texts(prompt.reply_markup),
            [["7 days", "14 days"], ["✅ 30 days", "90 days"], ["Monthly"], ["✖️ Cancel"]],
        )
        prompt_message = make_message(self.bot, prompt_id, prompt.text, from_bot=True)

        stale = make_message(self.bot, prompt_id - 1, "older prompt", from_bot=True)
        await self.feed(self.callback("period:14", stale))
        self.assertEqual(self._toasts()[-1], STALE_STEP_TOAST)
        self.assertEqual((await self._sub())["period_days"], 30)

        await self.feed(self.callback("period:month", prompt_message))
        sub = await self._sub()
        self.assertEqual((sub["period_days"], sub["monthly_anchor_day"]), (MONTHLY_PERIOD_SENTINEL, self.due.day))
        edit = self._assert_pricing_edit("✅ Period · <b>monthly</b>")
        self.assertIn("\nRepeats: every month on the ", edit.text)
        self.assertEqual(self._toasts()[-1], "Period: monthly")
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertIsNone(await self.state().get_state())

    async def test_period_typed_and_typo(self):
        await self._open_prompt("period")
        await self.feed(self.message("often"))
        self.assertEqual(self.bot.sent_texts()[-1], PERIOD_EDIT_HINT)
        await self.feed(self.message("14 days"))
        self.assertEqual((await self._sub())["period_days"], 14)
        self._assert_pricing_edit("✅ Period · <b>every 14 days</b>")
        self.assertNotIn("Period updated.", self.bot.sent_texts())

    async def test_deleted_subscription_is_not_a_dead_end(self):
        for data in (
            _cb("pricing", 999),
            _cb("amount", 999),
            _cb("currency", 999),
            _cb("basecurrency", 999),
            _cb("duedate", 999),
            _cb("period", 999),
            "sub_currency:999:EUR",
            "sub_currency_other:999",
            "sub_base_currency:999:EUR",
            DueDateChangeAction(action="move", subscription_id=999, old_due="2026-10-01", new_due="2026-11-01").pack(),
        ):
            with self.subTest(data=data):
                await self.feed(self.callback(data, self.screen))
                self.assertEqual(self._toasts()[-1], SUBSCRIPTION_GONE)
                self.assertTrue(self._edits()[-1].text.startswith("📋 <b>Subscriptions</b> · 1\n"))

        await self._open_prompt("amount")
        await self.db.delete_subscription(self.sub_id)
        await self.feed(self.message("20"))
        self.assertTrue(self.bot.sent_texts()[-1].startswith("⚠️ That subscription was deleted\n\n📋 <b>Subscriptions</b>"))
        self.assertIsNone(await self.state().get_state())


if __name__ == "__main__":
    unittest.main()
