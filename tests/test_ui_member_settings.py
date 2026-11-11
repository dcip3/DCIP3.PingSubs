"""WP12 member settings: ⚙️ Settings, ⏰ Reminder time, 🌍 Timezone and their prompts."""

import unittest
from datetime import datetime, timezone

from aiogram.types import InlineKeyboardMarkup

from app.ui.keyboards import (
    COMMON_TIMEZONES,
    MENU_SETTINGS,
    TZ_SHORT,
    public_settings_keyboard,
    public_settings_time_keyboard,
    public_settings_timezone_keyboard,
    timezone_city,
    use_default_rows,
)
from app.ui.member_settings import (
    TIME_NOT_TEXT_HINT,
    TIME_TYPO_HINT,
    TIMEZONE_PROMPT,
    TIMEZONE_TYPO_HINT,
    MemberSettings,
    member_settings_text,
    reminder_time_prompt,
    reminder_time_text,
    time_banner,
    time_toast,
    timezone_banner,
    timezone_text,
    timezone_toast,
)
from app.ui.prompts import CLEANUP_IDS, ORIGIN_MESSAGE_ID, PROMPT_MESSAGE_ID, prompt_markup
from app.ui.states import PublicSettingsForm
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import CHAT_ID, USER_ID, EditMessageText, SendMessage, make_message
from test_keyboards_fit import USER_TEXTS, assert_fits

install_keyboard_guard()

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
OTHER_ADMIN = 1

DEFAULTS = MemberSettings(
    time="10:00",
    default_time="10:00",
    has_time_override=False,
    timezone="Europe/Moscow",
    default_timezone="Europe/Moscow",
    has_timezone_override=False,
)
OWN = MemberSettings(
    time="12:00",
    default_time="10:00",
    has_time_override=True,
    timezone="Asia/Dubai",
    default_timezone="Europe/Moscow",
    has_timezone_override=True,
)

SETTINGS_TEXT_DEFAULTS = (
    "⚙️ <b>Settings</b>\n\n"
    "⏰ Reminder time: <b>10:00</b> your time (default)\n"
    "🌍 Timezone: Europe/Moscow (UTC+3, default)\n\n"
    "Each subscription can have its own time: open it in “📋 Subscriptions”."
)


def _layout(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _callbacks(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[button.callback_data for button in row] for row in markup.inline_keyboard]


# ------------------------------------------------------------------- texts


class ScreenTextTests(unittest.TestCase):
    def test_settings_with_defaults(self):
        self.assertEqual(member_settings_text(DEFAULTS, now=NOW), SETTINGS_TEXT_DEFAULTS)

    def test_settings_with_own_values(self):
        self.assertEqual(
            member_settings_text(OWN, now=NOW),
            "⚙️ <b>Settings</b>\n\n"
            "⏰ Reminder time: <b>12:00</b> your time\n"
            "🌍 Timezone: Asia/Dubai (UTC+4)\n\n"
            "Each subscription can have its own time: open it in “📋 Subscriptions”.",
        )

    def test_settings_utc_has_no_offset_repeat(self):
        utc = MemberSettings("10:00", "10:00", False, "UTC", "UTC", False)
        self.assertIn("\n🌍 Timezone: UTC (default)\n", member_settings_text(utc, now=NOW))
        own_utc = MemberSettings("10:00", "10:00", False, "UTC", "Europe/Moscow", True)
        self.assertIn("\n🌍 Timezone: UTC\n", member_settings_text(own_utc, now=NOW))

    def test_reminder_time_states_the_default_in_the_members_timezone(self):
        text = reminder_time_text(OWN)
        self.assertEqual(
            text,
            "⏰ <b>Reminder time</b>\n\n"
            "Now: <b>12:00</b> in your timezone (Asia/Dubai)\n"
            "Default: 10:00 in your timezone",
        )
        self.assertNotIn("admin", text.lower())

    def test_timezone_screen(self):
        los_angeles = MemberSettings("10:00", "10:00", False, "America/Los_Angeles", "Europe/Moscow", True)
        self.assertEqual(
            timezone_text(los_angeles, now=NOW),
            "🌍 <b>Timezone</b>\n\n"
            "Now: America/Los_Angeles (UTC-8)\n"
            "Default: Europe/Moscow\n"
            "Reminders arrive at your reminder time in this timezone.",
        )
        self.assertNotIn("IANA", timezone_text(los_angeles, now=NOW))

    def test_prompts_and_hints(self):
        self.assertEqual(
            reminder_time_prompt("Asia/Dubai"),
            "✏️ Your reminder time? <code>HH:MM</code> in Asia/Dubai, for example <code>16:00</code>.",
        )
        self.assertEqual(
            TIMEZONE_PROMPT,
            "✏️ Your timezone? A city zone like <code>Europe/Paris</code> or an offset like <code>UTC+3</code>.",
        )
        for hint in (TIME_TYPO_HINT, TIME_NOT_TEXT_HINT, TIMEZONE_TYPO_HINT):
            with self.subTest(hint=hint):
                self.assertTrue(hint.startswith("⚠️ "))
                self.assertTrue(hint.endswith("</code>."))
                self.assertNotIn("\n", hint)

    def test_toasts_and_banners(self):
        self.assertEqual(time_toast("12:00"), "Reminder time: 12:00")
        self.assertEqual(time_toast(None), "Reminder time: default")
        self.assertEqual(timezone_toast("Asia/Dubai", now=NOW), "Timezone: Asia/Dubai (UTC+4)")
        self.assertEqual(timezone_toast(None), "Timezone: default")
        self.assertEqual(time_banner("16:30"), "✅ Reminder time · <b>16:30</b>")
        self.assertEqual(time_banner("10:00", default=True), "✅ Reminder time · <b>10:00</b> (default)")
        self.assertEqual(timezone_banner("Asia/Bangkok", now=NOW), "✅ Timezone · <b>Asia/Bangkok (UTC+7)</b>")
        self.assertEqual(
            timezone_banner("Europe/Moscow", default=True, now=NOW),
            "✅ Timezone · <b>Europe/Moscow (UTC+3)</b> (default)",
        )


# --------------------------------------------------------------- keyboards


class KeyboardTests(unittest.TestCase):
    def test_settings_menu_has_no_nav_row(self):
        markup = public_settings_keyboard()
        self.assertEqual(_layout(markup), [["⏰ Reminder time"], ["🌍 Timezone"]])
        self.assertEqual(_callbacks(markup), [["public_settings:time"], ["public_settings:timezone"]])
        assert_fits(markup)

    def test_time_picker_default_selected_without_override(self):
        markup = public_settings_time_keyboard("10:00", "10:00", has_override=False)
        self.assertEqual(
            _layout(markup),
            [["✅ Default (10:00)"], ["09:00", "12:00"], ["16:00", "20:00"], ["✏️ Other time"], ["◀️ Back"]],
        )
        self.assertEqual(
            _callbacks(markup),
            [
                ["public_settings:time_reset"],
                ["public_settings_time:09:00", "public_settings_time:12:00"],
                ["public_settings_time:16:00", "public_settings_time:20:00"],
                ["public_settings:time_other"],
                ["public_settings:menu"],
            ],
        )
        assert_fits(markup)

    def test_time_picker_marks_the_stored_preset(self):
        markup = public_settings_time_keyboard("12:00", "10:00", has_override=True)
        self.assertEqual(_layout(markup)[:2], [["Default (10:00)"], ["09:00", "✅ 12:00"]])
        assert_fits(markup)

    def test_time_picker_override_equal_to_default_is_not_default(self):
        markup = public_settings_time_keyboard("09:00", "09:00", has_override=True)
        self.assertEqual(_layout(markup)[:2], [["Default (09:00)"], ["✅ 09:00", "12:00"]])

    def test_time_picker_never_styles_a_selection(self):
        for override in (False, True):
            markup = public_settings_time_keyboard("16:00", "10:00", has_override=override)
            self.assertEqual([b.style for row in markup.inline_keyboard for b in row if b.style], [])

    def test_timezone_picker_cities_two_per_row(self):
        markup = public_settings_timezone_keyboard("Europe/Moscow", "Europe/Moscow", has_override=False)
        self.assertEqual(
            _layout(markup),
            [
                # The default zone is the Default option only: no second [Moscow].
                ["✅ Default (Moscow)"],
                ["London", "Berlin"],
                ["Dubai", "Almaty"],
                ["Tokyo", "New York"],
                ["Chicago", "Denver"],
                ["Los Angeles", "UTC"],
                ["✏️ Other timezone"],
                ["◀️ Back"],
            ],
        )
        zone_callbacks = [data for row in _callbacks(markup)[1:6] for data in row]
        self.assertEqual(
            zone_callbacks,
            [f"public_settings_timezone:{zone}" for zone in COMMON_TIMEZONES if zone != "Europe/Moscow"],
        )
        # An own zone equal to the default keeps its city button, marked ✅.
        own = public_settings_timezone_keyboard("Europe/Moscow", "Europe/Moscow", has_override=True)
        self.assertEqual(_layout(own)[:2], [["Default (Moscow)"], ["✅ Moscow", "London"]])
        self.assertEqual(_callbacks(markup)[0], ["public_settings:timezone_reset"])
        self.assertEqual(_callbacks(markup)[-2:], [["public_settings:timezone_other"], ["public_settings:menu"]])
        assert_fits(markup)

    def test_every_timezone_selected_fits(self):
        for zone in COMMON_TIMEZONES:
            with self.subTest(zone=zone):
                markup = public_settings_timezone_keyboard(zone, "America/Los_Angeles", has_override=True)
                labels = [label for row in _layout(markup) for label in row]
                self.assertIn(f"✅ {TZ_SHORT[zone]}", labels)
                self.assertEqual(labels[0], "Default (Los Angeles)")
                assert_fits(markup)

    def test_long_or_odd_default_zone(self):
        self.assertEqual(timezone_city("America/Argentina/ComodRivadavia"), "ComodRivadavia")
        self.assertEqual(timezone_city("Etc/GMT-3"), "UTC+3")
        long_zone = "Atlantis/" + "W" * 30
        markup = public_settings_timezone_keyboard(long_zone, long_zone, has_override=False)
        default_label = markup.inline_keyboard[0][0].text
        self.assertTrue(default_label.startswith("✅ Default (W") and default_label.endswith("…)"))
        assert_fits(markup, (*USER_TEXTS, timezone_city(long_zone)))
        # A zone outside the presets marks no preset and keeps all labels whole.
        markup = public_settings_timezone_keyboard("Asia/Bangkok", "Europe/Moscow", has_override=True)
        self.assertFalse([b.text for row in markup.inline_keyboard for b in row if b.text.startswith("✅")])
        assert_fits(markup)

    def test_prompt_keyboards(self):
        for reset in ("public_settings:time_reset", "public_settings:timezone_reset"):
            markup = prompt_markup(use_default_rows(reset))
            self.assertEqual(_layout(markup), [["↺ Use default"], ["✖️ Cancel"]])
            self.assertEqual(_callbacks(markup), [[reset], ["dialog:cancel"]])
            self.assertNotIn("force_reply", markup.model_dump(exclude_none=True))
            assert_fits(markup)

    def test_no_ellipsis_in_fixed_labels(self):
        markups = (
            public_settings_keyboard(),
            public_settings_time_keyboard("12:00", "10:00"),
            public_settings_timezone_keyboard("Asia/Tokyo", "Europe/Moscow"),
        )
        for markup in markups:
            for row in markup.inline_keyboard:
                for button in row:
                    self.assertNotIn("…", button.text)


# ---------------------------------------------------------------- handlers


class MemberSettingsFlowTests(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin(OTHER_ADMIN)
        friend_id = await self.db.create_friend_invite("Anna", "join_anna", "2099-01-01T00:00:00")
        await self.db.claim_friend_invite(friend_id, USER_ID, "2026-11-01T00:00:00")

    def _screen(self, text: str = "screen"):
        return make_message(self.bot, 7001, text, from_bot=True)

    def _single_answer(self):
        answers = self.bot.answers()
        self.assertEqual(len(answers), 1, [a.text for a in answers])
        return answers[0]

    async def test_reply_menu_opens_settings(self):
        await self.feed(self.message(MENU_SETTINGS))
        call = self.bot.of(SendMessage)[-1]
        self.assertEqual(call.text, member_settings_text(DEFAULTS))
        self.assertEqual(_layout(call.reply_markup), [["⏰ Reminder time"], ["🌍 Timezone"]])

    async def test_back_to_settings_answers_once(self):
        await self.feed(self.callback("public_settings:menu", self._screen()))
        self.assertEqual(self.bot.of(EditMessageText)[-1].text, member_settings_text(DEFAULTS))
        self.assertIsNone(self._single_answer().text)

    async def test_time_picker_edits_in_place(self):
        await self.db.set_user_setting(USER_ID, "timezone", "Asia/Dubai")
        await self.feed(self.callback("public_settings:time", self._screen()))
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7001)
        self.assertEqual(
            edit.text,
            # The value is the default: said once.
            "⏰ <b>Reminder time</b>\n\nNow: <b>10:00</b> in your timezone (Asia/Dubai) (default)",
        )
        self.assertEqual(edit.reply_markup.inline_keyboard[0][0].text, "✅ Default (10:00)")
        self._single_answer()

    async def test_preset_returns_to_settings_with_toast(self):
        await self.feed(self.callback("public_settings_time:12:00", self._screen()))
        self.assertEqual(await self.db.get_user_setting(USER_ID, "base_reminder_time"), "12:00")
        # A preset gets the same result banner as typed input.
        self.assertTrue(
            self.bot.of(EditMessageText)[-1].text.startswith(
                "✅ Reminder time · <b>12:00</b>\n\n⚙️ <b>Settings</b>\n\n⏰ Reminder time: <b>12:00</b> your time\n"
            )
        )
        self.assertEqual(self._single_answer().text, "Reminder time: 12:00")

    async def test_default_resets_with_toast(self):
        await self.db.set_user_setting(USER_ID, "base_reminder_time", "12:00")
        await self.feed(self.callback("public_settings:time_reset", self._screen()))
        self.assertIsNone(await self.db.get_user_setting(USER_ID, "base_reminder_time"))
        self.assertEqual(
            self.bot.of(EditMessageText)[-1].text,
            "✅ Reminder time · <b>10:00</b> (default)\n\n" + member_settings_text(DEFAULTS),
        )
        self.assertEqual(self._single_answer().text, "Reminder time: default")

    async def test_timezone_preset_and_reset(self):
        await self.feed(self.callback("public_settings_timezone:Asia/Dubai", self._screen()))
        self.assertEqual(await self.db.get_user_setting(USER_ID, "timezone"), "Asia/Dubai")
        self.assertEqual(self._single_answer().text, "Timezone: Asia/Dubai (UTC+4)")
        await self.feed(self.callback("public_settings:timezone_reset", self._screen()))
        self.assertIsNone(await self.db.get_user_setting(USER_ID, "timezone"))
        self.assertEqual(self.bot.answers()[-1].text, "Timezone: default")

    async def test_timezone_picker_marks_stored_city(self):
        await self.db.set_user_setting(USER_ID, "timezone", "Asia/Tokyo")
        await self.feed(self.callback("public_settings:timezone", self._screen()))
        edit = self.bot.of(EditMessageText)[-1]
        labels = [b.text for row in edit.reply_markup.inline_keyboard for b in row]
        self.assertEqual(labels[0], "Default (Moscow)")
        self.assertIn("✅ Tokyo", labels)

    async def test_typed_time_edits_the_origin_with_a_banner(self):
        await self.feed(self.callback("public_settings:time_other", self._screen()))
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, reminder_time_prompt("Europe/Moscow"))
        self.assertEqual(_layout(prompt.reply_markup), [["↺ Use default"], ["✖️ Cancel"]])
        self.assertEqual(await self.state().get_state(), PublicSettingsForm.base_time.state)
        data = await self.state().get_data()
        self.assertEqual(data[ORIGIN_MESSAGE_ID], 7001)
        prompt_id = data[PROMPT_MESSAGE_ID]

        typo = self.message("25:99")
        await self.feed(typo)
        self.assertEqual(self.bot.of(SendMessage)[-1].text, TIME_TYPO_HINT)
        self.assertEqual(await self.state().get_state(), PublicSettingsForm.base_time.state)
        hint_id = (await self.state().get_data())[CLEANUP_IDS][-1]

        answer = self.message("1630")
        sent_before = len(self.bot.of(SendMessage))
        await self.feed(answer)
        self.assertEqual(await self.db.get_user_setting(USER_ID, "base_reminder_time"), "16:30")
        self.assertIsNone(await self.state().get_state())
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7001)
        self.assertTrue(edit.text.startswith("✅ Reminder time · <b>16:30</b>\n\n⚙️ <b>Settings</b>\n\n"))
        self.assertEqual(len(self.bot.of(SendMessage)), sent_before, "no extra confirmation message")
        self.assertEqual(
            sorted(self.bot.deleted_ids()),
            sorted([prompt_id, typo.message_id, hint_id, answer.message_id]),
        )

    async def test_typed_default_word_resets(self):
        await self.db.set_user_setting(USER_ID, "base_reminder_time", "12:00")
        await self.feed(self.callback("public_settings:time_other", self._screen()))
        await self.feed(self.message("default"))
        self.assertIsNone(await self.db.get_user_setting(USER_ID, "base_reminder_time"))
        self.assertTrue(
            self.bot.of(EditMessageText)[-1].text.startswith("✅ Reminder time · <b>10:00</b> (default)\n\n")
        )

    async def test_use_default_on_the_prompt(self):
        await self.db.set_user_setting(USER_ID, "base_reminder_time", "12:00")
        await self.feed(self.callback("public_settings:time_other", self._screen()))
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]
        prompt = make_message(self.bot, prompt_id, "prompt", from_bot=True)
        await self.feed(self.callback("public_settings:time_reset", prompt))
        self.assertIsNone(await self.db.get_user_setting(USER_ID, "base_reminder_time"))
        self.assertIsNone(await self.state().get_state())
        self.assertIn(prompt_id, self.bot.deleted_ids())
        edit = self.bot.of(EditMessageText)[-1]
        self.assertEqual(edit.message_id, 7001)
        self.assertTrue(edit.text.startswith("✅ Reminder time · <b>10:00</b> (default)\n\n⚙️ <b>Settings</b>"))
        self.assertEqual(self.bot.answers()[-1].text, "✅ Reminder time · 10:00 (default)")

    async def test_preset_tap_winds_a_pending_prompt_down(self):
        await self.feed(self.callback("public_settings:time_other", self._screen()))
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]
        await self.feed(self.callback("public_settings_time:09:00", self._screen()))
        self.assertIsNone(await self.state().get_state())
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertEqual(self.bot.answers()[-1].text, "Reminder time: 09:00")

    async def test_typed_timezone_offset(self):
        await self.feed(self.callback("public_settings:timezone_other", self._screen()))
        prompt = self.bot.of(SendMessage)[-1]
        self.assertEqual(prompt.text, TIMEZONE_PROMPT)
        self.assertEqual(_callbacks(prompt.reply_markup), [["public_settings:timezone_reset"], ["dialog:cancel"]])
        await self.feed(self.message("nowhere"))
        self.assertEqual(self.bot.of(SendMessage)[-1].text, TIMEZONE_TYPO_HINT)
        await self.feed(self.message("utc+4"))
        self.assertEqual(await self.db.get_user_setting(USER_ID, "timezone"), "Etc/GMT-4")
        self.assertTrue(
            self.bot.of(EditMessageText)[-1].text.startswith("✅ Timezone · <b>Etc/GMT-4 (UTC+4)</b>\n\n⚙️ <b>Settings</b>")
        )

    async def test_typed_city_in_any_case(self):
        await self.feed(self.callback("public_settings:timezone_other", self._screen()))
        await self.feed(self.message("asia/dubai"))
        self.assertEqual(await self.db.get_user_setting(USER_ID, "timezone"), "Asia/Dubai")

    async def test_origin_gone_sends_settings_anew(self):
        await self.feed(self.callback("public_settings:time_other", self._screen()))
        self.bot.fail(EditMessageText, "Bad Request: message to edit not found", message_id=7001)
        await self.feed(self.message("9"))
        last = self.bot.of(SendMessage)[-1]
        self.assertEqual(last.text.split("\n\n", 1)[0], "✅ Reminder time · <b>09:00</b>")
        self.assertEqual(int(last.chat_id), CHAT_ID)

    async def test_prompt_from_before_a_restart(self):
        await self.set_state(PublicSettingsForm.base_time)
        answer = self.message("20:00")
        await self.feed(answer)
        self.assertEqual(await self.db.get_user_setting(USER_ID, "base_reminder_time"), "20:00")
        self.assertTrue(self.bot.of(SendMessage)[-1].text.startswith("✅ Reminder time · <b>20:00</b>"))
        self.assertIn(answer.message_id, self.bot.deleted_ids())

    async def test_menu_tap_leaves_the_prompt(self):
        await self.feed(self.callback("public_settings:time_other", self._screen()))
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]
        await self.feed(self.message(MENU_SETTINGS))
        self.assertIsNone(await self.state().get_state())
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertIsNone(await self.db.get_user_setting(USER_ID, "base_reminder_time"))

    async def test_cancel_removes_the_prompt(self):
        await self.feed(self.callback("public_settings:timezone_other", self._screen()))
        prompt_id = (await self.state().get_data())[PROMPT_MESSAGE_ID]
        await self.feed(self.callback("dialog:cancel", make_message(self.bot, prompt_id, "p", from_bot=True)))
        self.assertIsNone(await self.state().get_state())
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertEqual(self.bot.answers()[-1].text, "Cancelled")


if __name__ == "__main__":
    unittest.main()
