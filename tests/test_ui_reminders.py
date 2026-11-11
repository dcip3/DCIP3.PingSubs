"""WP6: Reminders, Reminder time, the Days prompt and Send now with its confirmation."""

from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from aiogram.methods import EditMessageText, SendMessage

from app.core.reminders import tg_due
from app.services import SEND_BLOCKED, SEND_ERROR, ReminderSendResult
from app.ui.guards import ALREADY_RUNNING_TEXT, guarded
from app.ui.helpers import SUBSCRIPTION_GONE, resolve_report_timezone
from app.ui.keyboards import (
    reminder_days_quick_rows,
    reminder_send_confirm_keyboard,
    reminder_send_empty_keyboard,
    reminder_send_targets_keyboard,
    reminder_settings_keyboard,
    subscription_reminder_time_keyboard,
)
from app.ui.reminder_screens import (
    DEFAULT_OFFSETS_TEXT,
    REMINDER_DAYS_HINT,
    REMINDER_TIME_HINT,
    SENDING_TOAST,
    base_reminder_time,
    parse_offsets_input,
    reminder_days_prompt_text,
    reminder_time_prompt_text,
    reminder_time_text,
    reminders_text,
    send_confirm_text,
    send_result_banner,
)
from app.ui.states import ReminderSendAction, ReminderSendConfirmAction, SubscriptionAction, SubscriptionEditForm
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import make_message
from test_keyboards_fit import LONG_NAME, NAME_64, assert_fits

install_keyboard_guard()

BIG_ID = 2**31 - 1
BIG_TG = 9_999_999_999


def _texts(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _callbacks(markup) -> list[list[str | None]]:
    return [[button.callback_data for button in row] for row in markup.inline_keyboard]


def _styled(markup) -> list[tuple[str, str]]:
    return [(button.text, button.style) for row in markup.inline_keyboard for button in row if button.style]


def _cb(action: str, subscription_id: int) -> str:
    return SubscriptionAction(action=action, subscription_id=subscription_id).pack()


def _sub(**fields) -> dict:
    sub = {"id": 7, "name": "Netflix", "reminder_time": "", "reminder_offsets": "[-1, 0]", "remind_after_due": 1}
    sub.update(fields)
    return sub


# ------------------------------------------------------------------ keyboards


class ReminderKeyboardTests(unittest.TestCase):
    def test_reminders_keyboard_rows_toggle_and_unchanged_callbacks(self):
        on = reminder_settings_keyboard(7, after_due=True)
        self.assertEqual(
            _texts(on),
            # "⏰ Reminder time" is the one name of the setting (card, screens, member card);
            # too wide to share a row, it leads alone.
            [["⏰ Reminder time"], ["📅 Days"], ["📣 After due date · 🟢"], ["📬 Send now"], ["◀️ To card"]],
        )
        self.assertEqual(
            _callbacks(on),
            [["sub:remindertime:7"], ["sub:reminderdays:7"], ["sub:reminderloop:7"], ["sub:reminders_send:7"], ["sub:open:7"]],
        )
        self.assertEqual(_texts(reminder_settings_keyboard(7))[2], ["📣 After due date · ⚪"])
        self.assertEqual(_styled(on), [])
        for after_due in (True, False):
            assert_fits(reminder_settings_keyboard(BIG_ID, after_due=after_due))

    def test_reminder_time_default_is_a_radio_option(self):
        markup = subscription_reminder_time_keyboard(7, "10:00", "10:00", has_override=False)
        self.assertEqual(
            _texts(markup),
            [["✅ Default (10:00)"], ["09:00", "12:00"], ["16:00", "20:00"], ["✏️ Other time"], ["◀️ To reminders"]],
        )
        self.assertEqual(
            _callbacks(markup),
            [
                ["sub:remindertime_default:7"],
                ["sub_remindertime:7:09:00", "sub_remindertime:7:12:00"],
                ["sub_remindertime:7:16:00", "sub_remindertime:7:20:00"],
                ["sub_remindertime_other:7"],
                ["sub:reminders:7"],
            ],
        )
        self.assertEqual(_styled(markup), [])

    def test_an_override_equal_to_the_default_is_not_shown_as_default(self):
        markup = subscription_reminder_time_keyboard(7, "12:00", "12:00", has_override=True)
        self.assertEqual(_texts(markup)[0], ["Default (12:00)"])
        self.assertEqual(_texts(markup)[1], ["09:00", "✅ 12:00"])
        # The old call shape still guesses from current != default.
        self.assertEqual(_texts(subscription_reminder_time_keyboard(7, "16:00", "10:00"))[2], ["✅ 16:00", "20:00"])
        for current, override in (("10:00", False), ("20:00", True), ("07:45", True)):
            assert_fits(subscription_reminder_time_keyboard(BIG_ID, current, "10:00", has_override=override))

    def test_days_quick_value_is_built_from_the_default_offsets(self):
        rows = reminder_days_quick_rows(BIG_ID, DEFAULT_OFFSETS_TEXT)
        self.assertEqual([[b.text for b in row] for row in rows], [["↺ 1 day before + on the day"]])
        self.assertEqual(rows[0][0].callback_data, f"remdays_default:{BIG_ID}")

    def test_send_targets_unpaid_first_names_cut_tails_kept(self):
        people = [
            {"full_name": NAME_64, "telegram_id": BIG_TG, "status": "unpaid"},
            {"full_name": "Bob", "telegram_id": 43, "status": "paid"},
            {"full_name": LONG_NAME, "telegram_id": 44, "status": None},
            {"full_name": "Carol", "telegram_id": None, "status": "unpaid"},
        ]
        markup = reminder_send_targets_keyboard(BIG_ID, people)
        assert_fits(markup)
        texts = _texts(markup)
        self.assertEqual(texts[0], ["📬 Remind unpaid (1)"])
        self.assertTrue(texts[1][0].startswith("⚠️ Alexandra") and texts[1][0].endswith("… · unpaid"), texts[1])
        self.assertEqual(texts[2], ["✅ Bob · paid"])
        self.assertTrue(texts[3][0].endswith("…"), texts[3])
        self.assertEqual(texts[-1], ["◀️ To reminders"])
        self.assertEqual(len(texts), 5)  # Carol has not signed in: no button
        self.assertEqual(_styled(markup), [("📬 Remind unpaid (1)", "primary")])
        callbacks = _callbacks(markup)
        self.assertEqual(callbacks[0], [f"remsend:{BIG_ID}:0"])
        self.assertEqual(callbacks[1], [f"remsend:{BIG_ID}:{BIG_TG}"])
        self.assertEqual(callbacks[-1], [f"sub:reminders:{BIG_ID}"])

    def test_send_targets_without_unpaid_have_no_call_to_action(self):
        markup = reminder_send_targets_keyboard(7, [{"full_name": "Bob", "telegram_id": 43, "status": "paid"}])
        self.assertEqual(_texts(markup), [["✅ Bob · paid"], ["◀️ To reminders"]])
        self.assertEqual(_styled(markup), [])
        many = [{"full_name": f"{LONG_NAME}{n}", "telegram_id": 100 + n, "status": "unpaid"} for n in range(30)]
        assert_fits(reminder_send_targets_keyboard(BIG_ID, many), user_texts=[p["full_name"] for p in many])

    def test_confirm_and_empty_keyboards(self):
        confirm = reminder_send_confirm_keyboard(BIG_ID, BIG_TG)
        assert_fits(confirm)
        self.assertEqual(_texts(confirm), [["✖️ Cancel", "📬 Send"]])
        self.assertEqual(_callbacks(confirm), [[f"sub:reminders_send:{BIG_ID}", f"remsendok:{BIG_ID}:{BIG_TG}"]])
        self.assertEqual(_styled(confirm), [("📬 Send", "danger")])
        self.assertLess(len(confirm.inline_keyboard[0][1].callback_data.encode()), 64)
        self.assertEqual(ReminderSendConfirmAction.unpack(f"remsendok:{BIG_ID}:0").telegram_id, 0)

        empty = reminder_send_empty_keyboard(BIG_ID)
        assert_fits(empty)
        self.assertEqual(_texts(empty), [["👥 Add participants"], ["◀️ To reminders"]])
        self.assertEqual(_callbacks(empty), [[f"sub:participants_manage:{BIG_ID}"], [f"sub:reminders:{BIG_ID}"]])
        self.assertEqual(_styled(empty), [("👥 Add participants", "primary")])


# ---------------------------------------------------------------------- texts


class ReminderTextTests(unittest.TestCase):
    def test_reminders_text_default_and_override(self):
        self.assertEqual(
            reminders_text(_sub(), "10:00"),
            "🔔 <b>Reminders · Netflix</b>\n\n"
            "Reminder time: <b>10:00</b> in each member's timezone (default)\n"
            "Days: 1 day before, on the day\n"
            "After due date: daily until paid",
        )
        text = reminders_text(_sub(name="A<B>", reminder_time="16:00", remind_after_due=0, reminder_offsets="[0, 2]"), "10:00")
        self.assertEqual(
            text,
            "🔔 <b>Reminders · A&lt;B&gt;</b>\n\n"
            "Reminder time: <b>16:00</b> in each member's timezone\n"
            "Days: on the day, 2 days after\n"
            "After due date: off",
        )

    def test_reminder_time_text(self):
        self.assertEqual(
            reminder_time_text(_sub(), "10:00"),
            "⏰ <b>Reminder time · Netflix</b>\n\nNow: 10:00 (default)\nApplied in each member's own timezone.",
        )
        self.assertIn("\nNow: 12:00\n", reminder_time_text(_sub(reminder_time="12:00"), "12:00"))

    def test_prompts(self):
        self.assertEqual(
            reminder_time_prompt_text(_sub()),
            "✏️ Reminder time for Netflix? <code>HH:MM</code>, for example <code>16:00</code>.",
        )
        self.assertEqual(
            reminder_days_prompt_text(_sub()),
            "✏️ When should Netflix remind? Now: 1 day before, on the day.\n"
            "Send day offsets: <code>-1 0</code> = 1 day before and on the day; "
            "<code>0 2</code> = on the day and 2 days after.",
        )

    def test_offsets_input_is_forgiving(self):
        self.assertEqual(parse_offsets_input("-1 0 2"), [-1, 0, 2])
        self.assertEqual(parse_offsets_input("2, −1; 0 0 +3"), [-1, 0, 2, 3])
        for bad in ("", "soon", "1.5", "-1 x", "400"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_offsets_input(bad))

    def test_confirm_question_names_the_subscription_and_the_count(self):
        self.assertEqual(
            send_confirm_text(_sub(), count=2),
            "⚠️ <b>Send the Netflix reminder to 2 people now?</b>\nThey get it right away, outside the schedule.",
        )
        self.assertEqual(
            send_confirm_text(_sub(), count=1),
            "⚠️ <b>Send the Netflix reminder to 1 person now?</b>\nThey get it right away, outside the schedule.",
        )
        self.assertIn("reminder to Anna &lt;K&gt; now?", send_confirm_text(_sub(), person_name="Anna <K>"))

    def test_result_banner_counts_members_only_and_says_why_nothing_went(self):
        self.assertEqual(send_result_banner(ReminderSendResult(sent={1, 2})), "✅ Reminder sent to <b>2</b> people")
        self.assertEqual(send_result_banner(ReminderSendResult(sent={1})), "✅ Reminder sent to <b>1</b> person")
        self.assertEqual(
            send_result_banner(ReminderSendResult(failed={3: SEND_BLOCKED})),
            "⚠️ 1 could not be delivered (blocked the bot)",
        )
        self.assertEqual(
            send_result_banner(ReminderSendResult(sent={1}, failed={3: SEND_BLOCKED, 4: SEND_ERROR})),
            "✅ Reminder sent to <b>1</b> person\n⚠️ 2 could not be delivered (1 blocked the bot)",
        )
        self.assertEqual(send_result_banner(ReminderSendResult(failed={4: SEND_ERROR})), "⚠️ 1 could not be delivered")
        self.assertEqual(
            send_result_banner(ReminderSendResult(skipped_paid={1}, not_due={2})), "⚠️ Nothing sent: no one is due yet"
        )
        self.assertEqual(send_result_banner(ReminderSendResult(skipped_paid={1})), "⚠️ Nothing sent: everyone has paid")
        self.assertEqual(send_result_banner(ReminderSendResult()), "⚠️ Nothing sent")


# ----------------------------------------------------------------------- flows


class ReminderFlowTests(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin()
        self.tz = await resolve_report_timezone(self.db)
        self.today = datetime.now(ZoneInfo(self.tz)).date()
        self.sub_id = await self.db.create_subscription("Netflix", 300.0, "RUB", self.today, 30, None)
        self.screen = make_message(self.bot, 700, "reminders", from_bot=True)
        self.base = await base_reminder_time(self.db)

    async def _friend(self, name: str, telegram_id: int | None) -> int:
        friend_id = await self.db.create_friend_invite(name, f"token-{name}", "2099-01-01")
        if telegram_id:
            await self.db.claim_friend_invite(friend_id, telegram_id, "2026-01-01")
        await self.db.set_participant(self.sub_id, friend_id, True)
        return friend_id

    def _edits(self) -> list[EditMessageText]:
        return self.bot.of(EditMessageText)

    def _toasts(self) -> list[str | None]:
        return [answer.text for answer in self.bot.answers()]

    async def _sub(self) -> dict:
        return await self.db.get_subscription(self.sub_id)

    def _assert_reminders_edit(self, banner: str | None = None, message_id: int = 700) -> EditMessageText:
        edit = self._edits()[-1]
        self.assertEqual(edit.message_id, message_id)
        head = f"{banner}\n\n" if banner else ""
        self.assertTrue(edit.text.startswith(f"{head}🔔 <b>Reminders · Netflix</b>\n\n"), edit.text)
        self.assertEqual(_texts(edit.reply_markup)[-1], ["◀️ To card"])
        return edit

    # ------------------------------------------------------------- Reminders

    async def test_reminders_open_from_the_card_and_toggle_after_due_date(self):
        await self.db.update_subscription_fields(self.sub_id, remind_after_due=0)
        await self.feed(self.callback(_cb("reminders", self.sub_id), self.screen))
        edit = self._assert_reminders_edit()
        self.assertEqual(
            edit.text,
            f"🔔 <b>Reminders · Netflix</b>\n\nReminder time: <b>{self.base}</b> in each member's timezone (default)\n"
            "Days: 1 day before, on the day\nAfter due date: off",
        )
        self.assertEqual(self._toasts(), [None])

        await self.feed(self.callback(_cb("reminderloop", self.sub_id), self.screen))
        self.assertEqual((await self._sub())["remind_after_due"], 1)
        self.assertEqual(self._toasts()[-1], "After due date: daily until paid")
        self.assertEqual(_texts(self._edits()[-1].reply_markup)[2], ["📣 After due date · 🟢"])
        self.assertTrue(self._edits()[-1].text.endswith("After due date: daily until paid"))
        await self.feed(self.callback(_cb("reminderloop", self.sub_id), self.screen))
        self.assertEqual(self._toasts()[-1], "After due date: off")

    async def test_deleted_subscription_redraws_the_list(self):
        await self.db.delete_subscription(self.sub_id)
        for data in (_cb("reminders", self.sub_id), _cb("reminderloop", self.sub_id), _cb("remindertime", self.sub_id)):
            with self.subTest(data=data):
                await self.feed(self.callback(data, self.screen))
                self.assertEqual(self._toasts()[-1], SUBSCRIPTION_GONE)
                self.assertTrue(self._edits()[-1].text.startswith("📋 <b>Subscriptions</b>"))

    # --------------------------------------------------------- Reminder time

    async def test_reminder_time_preset_and_default_return_to_reminders(self):
        await self.feed(self.callback(_cb("remindertime", self.sub_id), self.screen))
        menu = self._edits()[-1]
        self.assertEqual(
            menu.text,
            f"⏰ <b>Reminder time · Netflix</b>\n\nNow: {self.base} (default)\nApplied in each member's own timezone.",
        )
        self.assertEqual(_texts(menu.reply_markup)[0], [f"✅ Default ({self.base})"])
        self.assertEqual(_texts(menu.reply_markup)[-1], ["◀️ To reminders"])

        await self.feed(self.callback(f"sub_remindertime:{self.sub_id}:12:00", self.screen))
        self.assertEqual((await self._sub())["reminder_time"], "12:00")
        self._assert_reminders_edit("✅ Reminder time · <b>12:00</b>")
        self.assertEqual(self._toasts()[-1], "Reminder time: 12:00")

        await self.feed(self.callback(_cb("remindertime", self.sub_id), self.screen))
        self.assertIn(["09:00", "✅ 12:00"], _texts(self._edits()[-1].reply_markup))

        await self.feed(self.callback(_cb("remindertime_default", self.sub_id), self.screen))
        self.assertEqual((await self._sub())["reminder_time"], "")
        self._assert_reminders_edit(f"✅ Reminder time · <b>{self.base}</b> (default)")
        self.assertEqual(self._toasts()[-1], f"Reminder time: {self.base} (default)")

    async def test_an_override_equal_to_the_default_is_selected_as_itself(self):
        await self.db.update_subscription_fields(self.sub_id, reminder_time="12:00")
        await self.db.set_setting("base_reminder_time", "12:00")
        await self.feed(self.callback(_cb("remindertime", self.sub_id), self.screen))
        texts = _texts(self._edits()[-1].reply_markup)
        self.assertEqual(texts[0], ["Default (12:00)"])
        self.assertEqual(texts[1], ["09:00", "✅ 12:00"])

    async def test_other_time_prompt_typo_and_success_edit_reminders(self):
        await self.feed(self.callback(f"sub_remindertime_other:{self.sub_id}", self.screen))
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, "✏️ Reminder time for Netflix? <code>HH:MM</code>, for example <code>16:00</code>.")
        self.assertEqual(_texts(prompt.reply_markup), [["✖️ Cancel"]])
        data = await self.state().get_data()
        self.assertEqual(data["origin_screen"], f"reminders:{self.sub_id}")
        prompt_id = data["prompt_message_id"]

        typo = self.message("noonish")
        await self.feed(typo)
        self.assertEqual(self.bot.sent_texts()[-1], REMINDER_TIME_HINT)
        answer = self.message("9.30")
        await self.feed(answer)
        self.assertEqual((await self._sub())["reminder_time"], "09:30")
        self._assert_reminders_edit("✅ Reminder time · <b>09:30</b>")
        for message_id in (prompt_id, typo.message_id, answer.message_id):
            self.assertIn(message_id, self.bot.deleted_ids())
        self.assertIsNone(await self.state().get_state())
        self.assertNotIn("Reminder time updated.", self.bot.sent_texts())

    # ------------------------------------------------------------------ Days

    async def test_days_prompt_typo_and_typed_success(self):
        await self.feed(self.callback(_cb("reminderdays", self.sub_id), self.screen))
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, reminder_days_prompt_text(await self._sub()))
        self.assertEqual(_texts(prompt.reply_markup), [["↺ 1 day before + on the day"], ["✖️ Cancel"]])
        self.assertNotIn("force_reply", prompt.reply_markup.model_dump(exclude_none=True))

        await self.feed(self.message("every day"))
        self.assertEqual(self.bot.sent_texts()[-1], REMINDER_DAYS_HINT)
        await self.feed(self.message("0, 2"))
        self.assertEqual((await self._sub())["reminder_offsets"], "[0, 2]")
        edit = self._assert_reminders_edit("✅ Days · <b>on the day, 2 days after</b>")
        self.assertIn("\nDays: on the day, 2 days after\n", edit.text)
        self.assertNotIn("Reminder schedule updated.", self.bot.sent_texts())

    async def test_days_quick_value_on_the_prompt(self):
        await self.db.update_subscription_fields(self.sub_id, reminder_offsets="[0, 3]")
        await self.feed(self.callback(_cb("reminderdays", self.sub_id), self.screen))
        prompt_id = (await self.state().get_data())["prompt_message_id"]
        prompt_message = make_message(self.bot, prompt_id, "prompt", from_bot=True)
        await self.feed(self.callback(f"remdays_default:{self.sub_id}", prompt_message))
        self.assertEqual((await self._sub())["reminder_offsets"], "[-1, 0]")
        self._assert_reminders_edit("✅ Days · <b>1 day before, on the day</b>")
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertEqual(self._toasts()[-1], "Days: 1 day before, on the day")
        self.assertIsNone(await self.state().get_state())

    async def test_days_quick_value_on_an_old_prompt_turns_it_into_reminders(self):
        old_prompt = make_message(self.bot, 777, "old prompt", from_bot=True)
        await self.feed(self.callback(f"remdays_default:{self.sub_id}", old_prompt))
        self._assert_reminders_edit("✅ Days · <b>1 day before, on the day</b>", message_id=777)

    # -------------------------------------------------------------- Send now

    async def _people(self) -> None:
        await self.db.update_subscription_fields(self.sub_id, reminder_offsets="[-1, 0, 1]", remind_after_due=1)
        await self._friend("Alice Smith", 501)
        await self._friend("Bob", 502)
        await self._friend("Carol", None)
        await self.db.ensure_cycle(self.sub_id, self.today)
        await self.db.log_payment(self.sub_id, self.today, 502)

    async def test_send_now_screen_lists_unpaid_first(self):
        await self._people()
        await self.feed(self.callback(_cb("reminders_send", self.sub_id), self.screen))
        edit = self._edits()[-1]
        self.assertEqual(
            edit.text,
            "📬 <b>Send now · Netflix</b>\n\n"
            f"Cycle: {tg_due(self.today.isoformat(), self.tz, 'wD')} · 1 of 2 paid\n"
            "⏳ Not signed in yet: Carol\n"
            "People who have paid do not get it.",
        )
        self.assertEqual(
            _texts(edit.reply_markup),
            [["📬 Remind unpaid (1)"], ["⚠️ Alice Smith · unpaid"], ["✅ Bob · paid"], ["◀️ To reminders"]],
        )
        assert_fits(edit.reply_markup)

    async def test_send_now_without_participants(self):
        await self.feed(self.callback(_cb("reminders_send", self.sub_id), self.screen))
        edit = self._edits()[-1]
        self.assertEqual(edit.text, "📬 <b>Send now · Netflix</b>\n\nNo participants yet.")
        self.assertEqual(_texts(edit.reply_markup), [["👥 Add participants"], ["◀️ To reminders"]])
        # A legacy "All" tap on an old picker does not send anything either.
        await self.feed(self.callback(ReminderSendAction(subscription_id=self.sub_id, telegram_id=0).pack(), self.screen))
        self.assertEqual(self._toasts()[-1], "List updated")

    async def test_paid_person_only_gets_a_toast(self):
        await self._people()
        await self.feed(self.callback(ReminderSendAction(subscription_id=self.sub_id, telegram_id=502).pack(), self.screen))
        self.assertEqual(self._toasts()[-1], "Bob has already paid")
        self.assertTrue(self._edits()[-1].text.startswith("📬 <b>Send now · Netflix</b>"))

    async def test_every_recipient_tap_asks_first_then_sends(self):
        await self._people()
        await self.db.set_setting("notify_admin_reminders", "0")
        await self.feed(self.callback(ReminderSendAction(subscription_id=self.sub_id, telegram_id=0).pack(), self.screen))
        question = self._edits()[-1]
        self.assertEqual(
            question.text,
            "⚠️ <b>Send the Netflix reminder to 1 person now?</b>\nThey get it right away, outside the schedule.",
        )
        self.assertEqual(
            _callbacks(question.reply_markup),
            [[_cb("reminders_send", self.sub_id), f"remsendok:{self.sub_id}:0"]],
        )
        self.assertEqual(self.bot.of(SendMessage), [])  # nothing sent yet

        await self.feed(self.callback(f"remsendok:{self.sub_id}:0", self.screen))
        self.assertEqual(self._toasts()[-1], SENDING_TOAST)
        self.assertEqual([int(call.chat_id) for call in self.bot.of(SendMessage)], [501])
        self._assert_reminders_edit("✅ Reminder sent to <b>1</b> person")
        self.assertNotIn("notification(s)", " ".join(str(t) for t in self._toasts()))

    async def test_single_recipient_confirmation_names_the_person(self):
        await self._people()
        await self.feed(self.callback(ReminderSendAction(subscription_id=self.sub_id, telegram_id=501).pack(), self.screen))
        self.assertTrue(self._edits()[-1].text.startswith("⚠️ <b>Send the Netflix reminder to Alice Smith now?</b>"))
        self.assertEqual(_callbacks(self._edits()[-1].reply_markup)[0][1], f"remsendok:{self.sub_id}:501")

    async def test_blocked_member_is_reported(self):
        await self._people()
        await self.db.set_setting("notify_admin_reminders", "0")
        self.bot.fail(SendMessage, "Forbidden: bot was blocked by the user", forbidden=True)
        await self.feed(self.callback(f"remsendok:{self.sub_id}:501", self.screen))
        self._assert_reminders_edit("⚠️ 1 could not be delivered (blocked the bot)")

    async def test_nothing_due_says_so(self):
        await self.db.update_subscription_fields(self.sub_id, next_charge_at=(self.today + timedelta(days=10)).isoformat())
        await self._friend("Alice Smith", 501)
        await self.feed(self.callback(f"remsendok:{self.sub_id}:0", self.screen))
        self._assert_reminders_edit("⚠️ Nothing sent: no one is due yet")
        self.assertNotIn(501, [int(call.chat_id) for call in self.bot.of(SendMessage)])

    async def test_everyone_paid(self):
        await self._people()
        await self.db.log_payment(self.sub_id, self.today, 501)
        await self.feed(self.callback(ReminderSendAction(subscription_id=self.sub_id, telegram_id=0).pack(), self.screen))
        self.assertEqual(self._toasts()[-1], "Everyone has paid")
        await self.feed(self.callback(f"remsendok:{self.sub_id}:0", self.screen))
        self._assert_reminders_edit("⚠️ Nothing sent: everyone has paid")

    async def test_send_is_guarded_against_double_taps(self):
        await self._people()
        async with guarded(None, ("remsend", self.sub_id)):
            await self.feed(self.callback(f"remsendok:{self.sub_id}:0", self.screen))
        self.assertEqual(self._toasts()[-1], ALREADY_RUNNING_TEXT)
        self.assertEqual(self.bot.of(SendMessage), [])

    async def test_send_after_the_subscription_was_deleted(self):
        await self.db.delete_subscription(self.sub_id)
        await self.feed(self.callback(f"remsendok:{self.sub_id}:0", self.screen))
        self.assertEqual(self._toasts()[-1], SUBSCRIPTION_GONE)
        self.assertEqual(self.bot.of(SendMessage), [])

    async def test_menu_tap_on_a_pending_days_prompt_does_not_save(self):
        await self.feed(self.callback(_cb("reminderdays", self.sub_id), self.screen))
        self.assertEqual(await self.state().get_state(), SubscriptionEditForm.reminder_offsets.state)
        await self.feed(self.message("📋 Subscriptions"))
        self.assertIsNone(await self.state().get_state())
        self.assertEqual((await self._sub())["reminder_offsets"], "[-1, 0]")
