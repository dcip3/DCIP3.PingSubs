"""WP11 admin settings: ⚙️ Settings, its pickers and prompts, and 🧪 Send a test reminder."""

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup

from app.core.reminders import tg_clock
from app.handlers import public_router
from app.infrastructure import SettingsMiddleware
from app.services import CurrencyConverter
from app.ui.admin_settings import (
    NOTIFICATIONS_SCREEN_TEXT,
    TIMEZONE_PROMPT,
    TESTS_PER_PAGE,
    AdminSettings,
    admin_settings_text,
    notification_toast,
    notifications_summary,
    reminder_time_text,
    rounding_text,
    rounding_toast,
    shown_page,
    test_failed_alert,
    test_page_of,
    test_people,
    test_picker_text,
    test_sent_banner,
    time_prompt,
    timezone_text,
)
from app.ui.guards import guarded
from app.ui.keyboards import (
    COMMON_TIMEZONES,
    MENU_SETTINGS,
    ROUNDING_OPTIONS,
    TZ_SHORT,
    admin_settings_keyboard,
    settings_notifications_keyboard,
    settings_rounding_keyboard,
    settings_tests_keyboard,
    settings_time_keyboard,
    settings_timezone_keyboard,
)
from app.ui.member_settings import TIME_NOT_TEXT_HINT, TIME_TYPO_HINT, TIMEZONE_TYPO_HINT
from app.ui.prompts import CLEANUP_IDS, ORIGIN_MESSAGE_ID, PROMPT_MESSAGE_ID
from app.ui.states import SettingsForm
from app.ui.style import toast_from_html
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import CHAT_ID, make_message
from test_keyboards_fit import LONG_NAME, NAME_64, USER_TEXTS, assert_fits

install_keyboard_guard()

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
CLOCK = tg_clock("10:00", "Europe/Moscow", "10:00", NOW)

DEFAULTS = AdminSettings(
    time="10:00",
    timezone="Europe/Moscow",
    rounding="precise",
    notifications=(True, True, False),
)

SETTINGS_TEXT = (
    "⚙️ <b>Settings</b>\n\n"
    f"⏰ Reminder time: <b>10:00</b> ({CLOCK} your time)\n"
    "🌍 Timezone: Europe/Moscow (UTC+3)\n"
    "🔢 Rounding: with cents\n"
    "🔔 Admin notifications: 2 of 3 on\n\n"
    "Members can set their own time and timezone."
)
TESTS_TEXT = (
    "🧪 <b>Send a test reminder</b>\n\n"
    "Pick a person. They get a sample reminder; tapping “Paid” records nothing."
)
SETTINGS_LAYOUT = [
    ["⏰ Reminder time"],
    ["🌍 Timezone", "🔢 Rounding"],
    ["🔔 Notifications"],
    ["🧪 Send a test reminder"],
]


def _layout(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _callbacks(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[button.callback_data for button in row] for row in markup.inline_keyboard]


def _friends(names: list[str], first_id: int = 9001) -> list[dict]:
    return [
        {"id": index + 1, "full_name": name, "telegram_id": first_id + index}
        for index, name in enumerate(names)
    ]


# ------------------------------------------------------------------- texts


class ScreenTextTests(unittest.TestCase):
    def test_settings_screen(self):
        self.assertTrue(CLOCK.startswith("<tg-time unix=") and CLOCK.endswith(">10:00</tg-time>"), CLOCK)
        self.assertEqual(admin_settings_text(DEFAULTS, now=NOW), SETTINGS_TEXT)
        self.assertNotIn("<code>", SETTINGS_TEXT)
        self.assertNotIn("Base time", SETTINGS_TEXT)

    def test_settings_screen_utc_and_other_rounding(self):
        snapshot = AdminSettings("09:00", "UTC", "round", (True, True, True))
        text = admin_settings_text(snapshot, now=NOW)
        self.assertIn("\n🌍 Timezone: UTC\n", text)
        self.assertIn("\n🔢 Rounding: round to nearest\n", text)
        self.assertIn("\n🔔 Admin notifications: all on\n", text)

    def test_notifications_summary(self):
        self.assertEqual(notifications_summary((True, False, True)), "2 of 3 on")
        self.assertEqual(notifications_summary((False, False, True)), "1 of 3 on")
        self.assertEqual(notifications_summary((True, True, True)), "all on")
        self.assertEqual(notifications_summary((False, False, False)), "all off")

    def test_reminder_time_screen(self):
        self.assertEqual(
            reminder_time_text(DEFAULTS, now=NOW),
            "⏰ <b>Reminder time</b>\n\n"
            f"Now: <b>10:00</b> (Europe/Moscow) · {CLOCK} your time\n"
            "The default for all reminders; members and subscriptions can override it.",
        )

    def test_timezone_screen(self):
        dubai = AdminSettings("10:00", "Asia/Dubai", "precise", (True, True, True))
        text = timezone_text(dubai, now=NOW)
        self.assertEqual(
            text,
            "🌍 <b>Timezone</b>\n\nNow: Asia/Dubai (UTC+4)\nUsed for the default reminder time and dates.",
        )
        self.assertNotIn("IANA", text)

    def test_rounding_screen(self):
        self.assertEqual(
            rounding_text(DEFAULTS),
            "🔢 <b>Rounding</b>\n\nNow: with cents\nApplies to converted amounts in reminders.",
        )
        self.assertEqual(
            rounding_text(AdminSettings("10:00", "UTC", "unknown-mode", (True,) * 3)).split("\n")[2],
            "Now: with cents",
        )

    def test_notifications_screen(self):
        self.assertEqual(
            NOTIFICATIONS_SCREEN_TEXT,
            "🔔 <b>Admin notifications</b>\n\nWhat the bot tells admins. Each toggle saves at once.",
        )

    def test_prompts(self):
        self.assertEqual(
            time_prompt("Europe/Moscow"),
            "✏️ Default reminder time? <code>HH:MM</code> in Europe/Moscow, for example <code>16:00</code>.",
        )
        self.assertEqual(
            TIMEZONE_PROMPT,
            "✏️ Timezone? A city zone like <code>Europe/Paris</code> or an offset like <code>UTC+3</code>.",
        )

    def test_toasts(self):
        self.assertEqual(rounding_toast("floor"), "Rounding: round down")
        self.assertEqual(rounding_toast("round"), "Rounding: round to nearest")
        self.assertEqual(notification_toast("paid", True), "Payment recorded: on")
        self.assertEqual(notification_toast("reminders", False), "Reminders sent: off")
        self.assertEqual(notification_toast("closed", True), "Cycle closed: on")

    def test_tests_picker_texts(self):
        self.assertEqual(test_picker_text(), TESTS_TEXT)
        self.assertEqual(
            test_picker_text(empty=True),
            "🧪 <b>Send a test reminder</b>\n\nNo signed-in users yet. Add people in “👥 Users” first.",
        )
        banner = test_sent_banner("Anna <K>")
        self.assertEqual(banner, "✅ Test sent to <b>Anna &lt;K&gt;</b>")
        self.assertEqual(toast_from_html(banner), "✅ Test sent to Anna <K>")
        self.assertEqual(test_picker_text(banner=banner), f"{banner}\n\n{TESTS_TEXT}")
        self.assertEqual(test_failed_alert("Anna"), "Could not send the test to Anna. Try again.")

    def test_people_and_pages(self):
        friends = [*_friends(["bob", "Anna", "Carl"]), {"id": 9, "full_name": "Pending", "telegram_id": None}]
        people = test_people(friends)
        self.assertEqual([person["full_name"] for person in people], ["Anna", "bob", "Carl"])
        many = test_people(_friends([f"User {index:02d}" for index in range(14)]))
        self.assertEqual(test_page_of(many, 9001), 1)
        self.assertEqual(test_page_of(many, 9001 + TESTS_PER_PAGE), 2)
        self.assertEqual(test_page_of(many, 9014), 3)
        self.assertIsNone(test_page_of(many, 1))

    def test_shown_page_reads_the_counter(self):
        markup = settings_tests_keyboard(_friends(["Anna"]), page=2, total_pages=3)
        message = make_message(None, 1, "x", from_bot=True).model_copy(update={"reply_markup": markup})
        self.assertEqual(shown_page(message), 2)
        self.assertEqual(shown_page(make_message(None, 1, "x", from_bot=True)), 1)
        self.assertEqual(shown_page(None), 1)


# --------------------------------------------------------------- keyboards


class KeyboardTests(unittest.TestCase):
    def test_settings_menu(self):
        markup = admin_settings_keyboard()
        self.assertEqual(_layout(markup), SETTINGS_LAYOUT)
        self.assertEqual(
            _callbacks(markup),
            [["settings:time"], ["settings:timezone", "settings:rounding"], ["settings:notifications"], ["settings:tests"]],
        )
        assert_fits(markup)

    def test_time_picker(self):
        markup = settings_time_keyboard("09:00")
        self.assertEqual(
            _layout(markup), [["✅ 09:00", "12:00"], ["16:00", "20:00"], ["✏️ Other time"], ["◀️ Back"]]
        )
        self.assertEqual(
            _callbacks(markup),
            [
                ["settings_time:09:00", "settings_time:12:00"],
                ["settings_time:16:00", "settings_time:20:00"],
                ["settings:time_other"],
                ["settings:menu"],
            ],
        )
        assert_fits(markup)
        # A time outside the presets marks nothing; a selection is never coloured.
        other = settings_time_keyboard("10:30")
        self.assertFalse([b for row in other.inline_keyboard for b in row if b.text.startswith("✅")])
        for value in ("09:00", "12:00", "16:00", "20:00", "10:30"):
            markup = settings_time_keyboard(value)
            self.assertEqual([b.style for row in markup.inline_keyboard for b in row if b.style], [])
            assert_fits(markup)

    def test_timezone_picker(self):
        markup = settings_timezone_keyboard("Europe/Moscow")
        self.assertEqual(
            _layout(markup),
            [
                ["✅ Moscow", "London"],
                ["Berlin", "Dubai"],
                ["Almaty", "Tokyo"],
                ["New York", "Chicago"],
                ["Denver", "Los Angeles"],
                ["UTC"],
                ["✏️ Other timezone"],
                ["◀️ Back"],
            ],
        )
        zone_callbacks = [data for row in _callbacks(markup)[:6] for data in row]
        self.assertEqual(zone_callbacks, [f"settings_timezone:{zone}" for zone in COMMON_TIMEZONES])
        self.assertEqual(_callbacks(markup)[-2:], [["settings:timezone_other"], ["settings:menu"]])
        self.assertTrue(all(len(data.encode()) <= 64 for row in _callbacks(markup) for data in row))

    def test_every_timezone_selected_fits(self):
        for zone in COMMON_TIMEZONES:
            with self.subTest(zone=zone):
                markup = settings_timezone_keyboard(zone)
                labels = [label for row in _layout(markup) for label in row]
                self.assertIn(f"✅ {TZ_SHORT[zone]}", labels)
                self.assertEqual(sum(label.startswith("✅") for label in labels), 1)
                assert_fits(markup)
        markup = settings_timezone_keyboard("Asia/Bangkok")
        self.assertFalse([b for row in markup.inline_keyboard for b in row if b.text.startswith("✅")])
        assert_fits(markup)

    def test_rounding_picker(self):
        markup = settings_rounding_keyboard("precise")
        self.assertEqual(
            _layout(markup),
            [["✅ With cents"], ["Round down"], ["Round to nearest"], ["Round up"], ["◀️ Back"]],
        )
        self.assertEqual(
            _callbacks(markup),
            [
                ["settings_rounding:precise"],
                ["settings_rounding:floor"],
                ["settings_rounding:round"],
                ["settings_rounding:ceil"],
                ["settings:menu"],
            ],
        )
        for mode, label in ROUNDING_OPTIONS:
            with self.subTest(mode=mode):
                markup = settings_rounding_keyboard(mode)
                labels = [label for row in _layout(markup) for label in row]
                self.assertIn(f"✅ {label}", labels)
                self.assertFalse([text for text in labels if "☐" in text])
                self.assertEqual([b.style for row in markup.inline_keyboard for b in row if b.style], [])
                assert_fits(markup)

    def test_notifications_toggles(self):
        markup = settings_notifications_keyboard(True, True, False)
        self.assertEqual(
            _layout(markup),
            [["📬 Reminders sent · 🟢"], ["💸 Payment recorded · 🟢"], ["🔒 Cycle closed · ⚪"], ["◀️ Back"]],
        )
        self.assertEqual(
            _callbacks(markup),
            [["settings_notify:reminders"], ["settings_notify:paid"], ["settings_notify:closed"], ["settings:menu"]],
        )
        for states in ((True, True, True), (False, False, False)):
            markup = settings_notifications_keyboard(*states)
            self.assertEqual([b.style for row in markup.inline_keyboard for b in row if b.style], [])
            assert_fits(markup)

    def test_tests_picker(self):
        friends = _friends(["Anna", LONG_NAME, NAME_64, "W" * 20])
        markup = settings_tests_keyboard(friends, page=2, total_pages=3, total_users=20)
        rows = _layout(markup)
        self.assertEqual([len(row) for row in rows[:4]], [1, 1, 1, 1], "one person per row")
        self.assertEqual(rows[0], ["Anna"])
        self.assertTrue(rows[1][0].endswith("…"))
        self.assertEqual(rows[-2], ["◀️", "2 / 3", "▶️"])
        self.assertEqual(rows[-1], ["◀️ Back"])
        self.assertEqual(_callbacks(markup)[0], ["testsend:9001"])
        self.assertEqual(_callbacks(markup)[-1], ["settings:menu"])
        self.assertEqual(
            [b.callback_data for b in markup.inline_keyboard[-2] if b.callback_data],
            ["testlist:page:1", "testlist:page:3"],
        )
        assert_fits(markup, (*USER_TEXTS, "W" * 20))
        huge = settings_tests_keyboard(_friends([NAME_64], first_id=2_000_000_000), page=1, total_pages=1)
        self.assertTrue(all(len(data.encode()) <= 64 for row in _callbacks(huge) for data in row))
        assert_fits(huge)

    def test_tests_picker_empty(self):
        markup = settings_tests_keyboard([], page=1, total_pages=1)
        self.assertEqual(_layout(markup), [["◀️ Back"]])
        assert_fits(markup)

    def test_no_ellipsis_or_old_labels_in_fixed_labels(self):
        markups = (
            admin_settings_keyboard(),
            settings_time_keyboard("12:00"),
            settings_timezone_keyboard("Asia/Tokyo"),
            settings_rounding_keyboard("ceil"),
            settings_notifications_keyboard(False, True, False),
            settings_tests_keyboard([], page=1, total_pages=1),
        )
        for markup in markups:
            for row in markup.inline_keyboard:
                for button in row:
                    self.assertNotIn("…", button.text)
                    self.assertNotIn(button.text, {"⬅️ Back", "✖️ Close", "Other", "⏰ Base time", "🧪 Tests"})


# ---------------------------------------------------------------- handlers


def _shared_settings():
    for middleware in public_router.callback_query.middleware:
        if isinstance(middleware, SettingsMiddleware):
            return middleware.settings
    raise AssertionError("SettingsMiddleware is not registered")


class AdminSettingsFlowTests(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin()
        self.settings = _shared_settings()
        saved = (self.settings.base_reminder_time, self.settings.base_timezone, self.settings.currency_rounding)

        def restore() -> None:
            (
                self.settings.base_reminder_time,
                self.settings.base_timezone,
                self.settings.currency_rounding,
            ) = saved

        self.addCleanup(restore)

    def _screen(self, text: str = "screen", markup: InlineKeyboardMarkup | None = None):
        message = make_message(self.bot, 7001, text, from_bot=True)
        if markup is not None:
            message = message.model_copy(update={"reply_markup": markup}).as_(self.bot)
        return message

    def _single_answer(self):
        answers = self.bot.answers()
        self.assertEqual(len(answers), 1, [a.text for a in answers])
        return answers[0]

    def _settings_head(self, text: str) -> str:
        return text.split("\n🔢", 1)[0]

    async def test_reply_menu_opens_settings(self):
        await self.feed(self.message(MENU_SETTINGS))
        call = self.bot.of(SendMessage)[-1]
        self.assertTrue(call.text.startswith("⚙️ <b>Settings</b>\n\n⏰ Reminder time: <b>10:00</b> (<tg-time"), call.text)
        self.assertTrue(
            call.text.endswith(
                " your time)\n🌍 Timezone: Europe/Moscow (UTC+3)\n🔢 Rounding: with cents\n"
                "🔔 Admin notifications: all on\n\nMembers can set their own time and timezone."
            ),
            call.text,
        )
        self.assertEqual(_layout(call.reply_markup), SETTINGS_LAYOUT)

    async def test_back_to_settings_edits_in_place_and_answers_once(self):
        for data in ("settings:menu", "public_settings:menu"):
            with self.subTest(data=data):
                self.bot.calls.clear()
                await self.feed(self.callback(data, self._screen()))
                edit = self.bot.of(EditMessageText)[-1]
                self.assertEqual(edit.message_id, 7001)
                self.assertTrue(edit.text.startswith("⚙️ <b>Settings</b>\n\n"))
                self.assertEqual(_layout(edit.reply_markup), SETTINGS_LAYOUT)
                self.assertIsNone(self._single_answer().text)

    async def test_time_preset_returns_to_settings_with_toast(self):
        await self.feed(self.callback("settings:time", self._screen()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertTrue(edit.text.startswith("⏰ <b>Reminder time</b>\n\nNow: <b>10:00</b> (Europe/Moscow) · <tg-time"))
        self.assertEqual(_layout(edit.reply_markup)[0], ["09:00", "12:00"])
        self.bot.calls.clear()
        await self.feed(self.callback("settings_time:12:00", self._screen()))
        self.assertEqual(await self.db.get_setting("base_reminder_time"), "12:00")
        self.assertEqual(self.settings.base_reminder_time, "12:00")
        # A preset gets the same result banner as typed input.
        self.assertTrue(
            self.bot.of(EditMessageText)[-1].text.startswith(
                "✅ Reminder time · <b>12:00</b>\n\n⚙️ <b>Settings</b>\n\n⏰ Reminder time: <b>12:00</b>"
            )
        )
        self.assertEqual(self._single_answer().text, "Reminder time: 12:00")
        self.assertEqual(self.bot.of(SendMessage), [], "no extra confirmation message")

    async def test_bad_time_value_redraws_settings_with_a_toast(self):
        await self.feed(self.callback("settings_time:99:99", self._screen()))
        answer = self._single_answer()
        self.assertFalse(answer.show_alert)
        self.assertEqual(answer.text, "This button no longer works")
        self.assertTrue(self.bot.of(EditMessageText)[-1].text.startswith("⚙️ <b>Settings</b>"))

    async def test_typed_time_edits_the_origin_with_a_banner(self):
        await self.feed(self.callback("settings:time_other", self._screen()))
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, time_prompt("Europe/Moscow"))
        self.assertEqual(_layout(prompt.reply_markup), [["✖️ Cancel"]])
        self.assertNotIn("force_reply", prompt.reply_markup.model_dump(exclude_none=True))
        self.assertEqual(await self.state().get_state(), SettingsForm.base_time.state)
        data = await self.state().get_data()
        self.assertEqual(data[ORIGIN_MESSAGE_ID], 7001)
        prompt_id = data[PROMPT_MESSAGE_ID]

        typo = self.message("25:99")
        await self.feed(typo)
        self.assertEqual(self.bot.of(SendMessage)[-1].text, TIME_TYPO_HINT)
        self.assertEqual(await self.state().get_state(), SettingsForm.base_time.state)
        hint_id = (await self.state().get_data())[CLEANUP_IDS][-1]

        answer = self.message("1630")
        sent_before = len(self.bot.of(SendMessage))
        await self.feed(answer)
        self.assertEqual(await self.db.get_setting("base_reminder_time"), "16:30")
        self.assertEqual(self.settings.base_reminder_time, "16:30")
        self.assertIsNone(await self.state().get_state())
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7001)
        self.assertTrue(
            edit.text.startswith("✅ Reminder time · <b>16:30</b>\n\n⚙️ <b>Settings</b>\n\n⏰ Reminder time: <b>16:30</b>"),
            edit.text,
        )
        self.assertEqual(_layout(edit.reply_markup), SETTINGS_LAYOUT)
        self.assertEqual(len(self.bot.of(SendMessage)), sent_before, "no 'Base time set to' message")
        self.assertEqual(
            sorted(self.bot.deleted_ids()),
            sorted([prompt_id, typo.message_id, hint_id, answer.message_id]),
        )

    async def test_non_text_answer_gets_a_hint(self):
        await self.feed(self.callback("settings:time_other", self._screen()))
        sticker = self.message("").model_copy(update={"text": None}).as_(self.bot)
        await self.feed(sticker)
        self.assertEqual(self.bot.of(SendMessage)[-1].text, TIME_NOT_TEXT_HINT)
        self.assertEqual(await self.state().get_state(), SettingsForm.base_time.state)

    async def test_typed_time_origin_gone_sends_settings_anew(self):
        await self.feed(self.callback("settings:time_other", self._screen()))
        self.bot.fail(EditMessageText, "Bad Request: message to edit not found", message_id=7001)
        await self.feed(self.message("9"))
        last = self.bot.of(SendMessage)[-1]
        self.assertEqual(last.text.split("\n\n", 1)[0], "✅ Reminder time · <b>09:00</b>")
        self.assertEqual(int(last.chat_id), CHAT_ID)

    async def test_prompt_from_before_a_restart(self):
        await self.set_state(SettingsForm.base_timezone)
        answer = self.message("Asia/Tokyo")
        await self.feed(answer)
        self.assertEqual(await self.db.get_setting("base_timezone"), "Asia/Tokyo")
        self.assertTrue(self.bot.of(SendMessage)[-1].text.startswith("✅ Timezone · <b>Asia/Tokyo (UTC+9)</b>"))
        self.assertIn(answer.message_id, self.bot.deleted_ids())

    async def test_timezone_picker_and_preset(self):
        await self.db.set_setting("base_timezone", "Asia/Tokyo")
        await self.feed(self.callback("settings:timezone", self._screen()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(
            edit.text, "🌍 <b>Timezone</b>\n\nNow: Asia/Tokyo (UTC+9)\nUsed for the default reminder time and dates."
        )
        labels = [b.text for row in edit.reply_markup.inline_keyboard for b in row]
        self.assertIn("✅ Tokyo", labels)
        self.bot.calls.clear()
        await self.feed(self.callback("settings_timezone:Asia/Dubai", self._screen()))
        self.assertEqual(await self.db.get_setting("base_timezone"), "Asia/Dubai")
        self.assertEqual(self.settings.base_timezone, "Asia/Dubai")
        self.assertIn("\n🌍 Timezone: Asia/Dubai (UTC+4)\n", self.bot.of(EditMessageText)[-1].text)
        self.assertEqual(self._single_answer().text, "Timezone: Asia/Dubai (UTC+4)")

    async def test_typed_timezone_offset(self):
        await self.feed(self.callback("settings:timezone_other", self._screen()))
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, TIMEZONE_PROMPT)
        self.assertEqual(_callbacks(prompt.reply_markup), [["dialog:cancel"]])
        await self.feed(self.message("nowhere"))
        self.assertEqual(self.bot.of(SendMessage)[-1].text, TIMEZONE_TYPO_HINT)
        await self.feed(self.message("utc+7"))
        self.assertEqual(await self.db.get_setting("base_timezone"), "Etc/GMT-7")
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7001)
        self.assertTrue(
            edit.text.startswith("✅ Timezone · <b>Etc/GMT-7 (UTC+7)</b>\n\n⚙️ <b>Settings</b>"), edit.text
        )

    async def test_typed_city_in_any_case(self):
        await self.feed(self.callback("settings:timezone_other", self._screen()))
        await self.feed(self.message("asia/bangkok"))
        self.assertEqual(await self.db.get_setting("base_timezone"), "Asia/Bangkok")
        self.assertTrue(self.bot.of(EditMessageText)[-1].text.startswith("✅ Timezone · <b>Asia/Bangkok (UTC+7)</b>"))

    async def test_preset_tap_winds_a_pending_prompt_down(self):
        await self.feed(self.callback("settings:time_other", self._screen()))
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]
        await self.feed(self.callback("settings_time:20:00", self._screen()))
        self.assertIsNone(await self.state().get_state())
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertEqual(self.bot.answers()[-1].text, "Reminder time: 20:00")

    async def test_menu_tap_and_cancel_leave_the_prompt(self):
        await self.feed(self.callback("settings:timezone_other", self._screen()))
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]
        await self.feed(self.message(MENU_SETTINGS))
        self.assertIsNone(await self.state().get_state())
        self.assertIn(prompt_id, self.bot.deleted_ids())
        await self.feed(self.callback("settings:time_other", self._screen()))
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]
        await self.feed(self.callback("dialog:cancel", make_message(self.bot, prompt_id, "p", from_bot=True)))
        self.assertIsNone(await self.state().get_state())
        self.assertEqual(self.bot.answers()[-1].text, "Cancelled")
        self.assertEqual(await self.db.get_setting("base_reminder_time"), None)

    async def test_rounding_pick(self):
        await self.feed(self.callback("settings:rounding", self._screen()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.text, "🔢 <b>Rounding</b>\n\nNow: with cents\nApplies to converted amounts in reminders.")
        self.assertEqual(_layout(edit.reply_markup)[0], ["✅ With cents"])
        self.bot.calls.clear()
        await self.feed(self.callback("settings_rounding:floor", self._screen()))
        self.assertEqual(await self.db.get_setting("currency_rounding"), "floor")
        self.assertEqual(self.settings.currency_rounding, "floor")
        self.assertIn("\n🔢 Rounding: round down\n", self.bot.of(EditMessageText)[-1].text)
        self.assertEqual(self._single_answer().text, "Rounding: round down")
        self.bot.calls.clear()
        await self.feed(self.callback("settings_rounding:bogus", self._screen()))
        self.assertEqual((self._single_answer().text, self._single_answer().show_alert),
                         ("This button no longer works", False))
        self.assertTrue(self.bot.of(EditMessageText)[-1].text.startswith("⚙️ <b>Settings</b>"))
        self.assertEqual(self.settings.currency_rounding, "floor")

    async def test_notification_toggle_redraws_with_toast(self):
        await self.feed(self.callback("settings:notifications", self._screen()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.text, NOTIFICATIONS_SCREEN_TEXT)
        self.assertEqual(_layout(edit.reply_markup)[1], ["💸 Payment recorded · 🟢"])
        self.bot.calls.clear()
        await self.feed(self.callback("settings_notify:paid", self._screen()))
        self.assertFalse(await self.db.get_setting_bool("notify_admin_paid", True))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.text, NOTIFICATIONS_SCREEN_TEXT)
        self.assertEqual(_layout(edit.reply_markup)[1], ["💸 Payment recorded · ⚪"])
        self.assertEqual(self._single_answer().text, "Payment recorded: off")
        self.bot.calls.clear()
        await self.feed(self.callback("settings_notify:paid", self._screen()))
        self.assertTrue(await self.db.get_setting_bool("notify_admin_paid", False))
        self.assertEqual(self._single_answer().text, "Payment recorded: on")
        await self.feed(self.callback("settings:menu", self._screen()))
        self.assertIn("\n🔔 Admin notifications: all on\n", self.bot.of(EditMessageText)[-1].text)


class TestReminderFlowTests(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin()
        patcher = patch.object(CurrencyConverter, "convert_to", AsyncMock(return_value=None))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _person(self, name: str, telegram_id: int) -> None:
        friend_id = await self.db.create_friend_invite(name, f"join_{telegram_id}", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(friend_id, telegram_id, "2026-11-01T00:00:00")

    def _picker(self, page: int = 1, total_pages: int = 1):
        markup = settings_tests_keyboard([], page=page, total_pages=total_pages)
        message = make_message(self.bot, 7002, "picker", from_bot=True)
        return message.model_copy(update={"reply_markup": markup}).as_(self.bot)

    def _single_answer(self):
        answers = self.bot.answers()
        self.assertEqual(len(answers), 1, [a.text for a in answers])
        return answers[0]

    async def test_settings_tests_opens_the_picker_directly(self):
        await self._person("Anna", 9001)
        await self.db.create_friend_invite("Pending", "join_pending", "2099-01-01T00:00:00")
        for data in ("settings:tests", "tests:send"):
            with self.subTest(data=data):
                self.bot.calls.clear()
                await self.feed(self.callback(data, self._picker()))
                edit = self.bot.of(EditMessageText)[-1]
                self.assertEqual(edit.text, TESTS_TEXT)
                self.assertEqual(_layout(edit.reply_markup), [["Anna"], ["◀️ Back"]])
                self.assertEqual(_callbacks(edit.reply_markup), [["testsend:9001"], ["settings:menu"]])
                self._single_answer()

    async def test_empty_picker(self):
        await self.feed(self.callback("settings:tests", self._picker()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.text, "🧪 <b>Send a test reminder</b>\n\nNo signed-in users yet. Add people in “👥 Users” first.")
        self.assertEqual(_layout(edit.reply_markup), [["◀️ Back"]])

    async def test_paging(self):
        for index in range(8):
            await self._person(f"User {index}", 9100 + index)
        await self.feed(self.callback("testlist:page:2", self._picker()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(_layout(edit.reply_markup), [["User 6"], ["User 7"], ["◀️", "2 / 2", "▶️"], ["◀️ Back"]])

    async def test_send_stays_on_the_same_page_with_a_banner(self):
        for index in range(8):
            await self._person(f"User {index}", 9100 + index)
        await self.feed(self.callback("testsend:9107", self._picker(2, 2)))
        pushes = [call for call in self.bot.of(SendMessage) if int(call.chat_id) == 9107]
        self.assertEqual(len(pushes), 1)
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.text, f"✅ Test sent to <b>User 7</b>\n\n{TESTS_TEXT}")
        self.assertEqual(_layout(edit.reply_markup)[-2], ["◀️", "2 / 2", "▶️"])
        self.assertEqual(self._single_answer().text, "✅ Test sent to User 7")

    async def test_failed_send_is_an_alert_and_keeps_the_screen(self):
        await self._person("Anna", 9001)
        self.bot.fail(SendMessage, "Forbidden: bot was blocked by the user", forbidden=True)
        await self.feed(self.callback("testsend:9001", self._picker()))
        self.assertEqual(self.bot.of(EditMessageText), [])
        answer = self._single_answer()
        self.assertTrue(answer.show_alert)
        self.assertEqual(answer.text, "Could not send the test to Anna. Try again.")

    async def test_user_gone_redraws_the_list_with_a_toast(self):
        await self._person("Anna", 9001)
        await self.feed(self.callback("testsend:424242", self._picker()))
        self.assertEqual([call for call in self.bot.of(SendMessage) if int(call.chat_id) == 424242], [])
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.text, TESTS_TEXT)
        answer = self._single_answer()
        self.assertEqual(answer.text, "User was deleted")
        self.assertFalse(answer.show_alert)

    async def test_double_tap_is_guarded(self):
        await self._person("Anna", 9001)
        async with guarded(None, ("testsend", 9001)):
            await self.feed(self.callback("testsend:9001", self._picker()))
        self.assertEqual(self.bot.of(SendMessage), [])
        self.assertEqual(self.bot.of(EditMessageText), [])
        self.assertEqual(self._single_answer().text, "⏳ Already running")
        # Released afterwards: the next tap sends.
        self.bot.calls.clear()
        await self.feed(self.callback("testsend:9001", self._picker()))
        self.assertEqual(len([c for c in self.bot.of(SendMessage) if int(c.chat_id) == 9001]), 1)


if __name__ == "__main__":
    unittest.main()
