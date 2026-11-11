"""Prompt lifecycle and flow reset (app/ui/prompts.py) against a recording fake bot."""

import unittest
from unittest import mock

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton

from app.ui import prompts
from app.ui.prompts import (
    CLEANUP_IDS,
    KEPT_FLOW_DATA_KEYS,
    ORIGIN_CHAT_ID,
    ORIGIN_MESSAGE_ID,
    ORIGIN_SCREEN,
    PROMPT_MESSAGE_ID,
    WIZARD_CHAT_ID,
    WIZARD_MESSAGE_ID,
    advance_wizard,
    ask,
    cancel_prompt,
    finish_prompt,
    finish_wizard,
    note_typo,
    origin_of,
    reset_flow_state,
    start_wizard,
    wind_down,
    wizard_of,
)
from keyboard_guard import install_keyboard_guard
from telegram_fakes import (
    CHAT_ID,
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


class Flow(StatesGroup):
    amount = State()
    name = State()
    step_two = State()


def _layout(markup) -> list[list[tuple[str, str | None]]]:
    return [[(button.text, button.callback_data) for button in row] for row in markup.inline_keyboard]


class ResetFlowStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_clears_state_and_data_and_returns_the_old_data(self):
        bot = FakeBot()
        state = make_state(bot)
        await state.set_state(Flow.amount)
        await state.update_data(prompt_message_id=5, edit_subscription_id=3)
        old = await reset_flow_state(state)
        self.assertEqual(old, {"prompt_message_id": 5, "edit_subscription_id": 3})
        self.assertIsNone(await state.get_state())
        self.assertEqual(await state.get_data(), {})

    async def test_whitelist_is_empty_today_and_kept_keys_survive(self):
        self.assertEqual(KEPT_FLOW_DATA_KEYS, frozenset())
        bot = FakeBot()
        state = make_state(bot)
        await state.update_data(last_invoice_at=1, prompt_message_id=5)
        with mock.patch.object(prompts, "KEPT_FLOW_DATA_KEYS", frozenset({"last_invoice_at"})):
            old = await reset_flow_state(state)
        self.assertEqual(old["prompt_message_id"], 5)
        self.assertEqual(await state.get_data(), {"last_invoice_at": 1})


class AskTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_from_a_callback_stores_ids_and_answers_once(self):
        bot = FakeBot()
        state = make_state(bot)
        screen = make_message(bot, 10, "💰 Pricing · Netflix", from_bot=True)
        callback = make_callback(bot, screen, "sub:amount:12")

        prompt = await ask(
            callback,
            state,
            Flow.amount,
            "New amount for Netflix, USD? Now 15.99.",
            origin_screen="pricing:12",
            data={"edit_subscription_id": 12},
        )

        sent = bot.of(SendMessage)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].text, "✏️ New amount for Netflix, USD? Now 15.99.")
        self.assertEqual(_layout(sent[0].reply_markup), [[("✖️ Cancel", "dialog:cancel")]])
        self.assertNotIn("force_reply", sent[0].reply_markup.model_dump(exclude_none=True))
        self.assertEqual(await state.get_state(), Flow.amount.state)
        data = await state.get_data()
        self.assertEqual(data[PROMPT_MESSAGE_ID], prompt.message_id)
        self.assertEqual((data[ORIGIN_CHAT_ID], data[ORIGIN_MESSAGE_ID]), (CHAT_ID, 10))
        self.assertEqual(data[ORIGIN_SCREEN], "pricing:12")
        self.assertEqual(data[CLEANUP_IDS], [])
        self.assertEqual(data["edit_subscription_id"], 12)
        self.assertEqual(origin_of(data), (CHAT_ID, 10))
        self.assertEqual(len(bot.answers()), 1)

    async def test_quick_values_sit_above_cancel_and_the_pencil_is_not_doubled(self):
        bot = FakeBot()
        state = make_state(bot)
        typed = make_message(bot, 20, "⚙️ Settings")
        quick = [InlineKeyboardButton(text="↺ Use default", callback_data="settings_time:default")]
        await ask(typed, state, Flow.amount, "✏️ Reminder time? Now 10:00.", extra_rows=[quick])
        sent = bot.of(SendMessage)[0]
        self.assertEqual(sent.text, "✏️ Reminder time? Now 10:00.")
        self.assertEqual(
            _layout(sent.reply_markup),
            [[("↺ Use default", "settings_time:default")], [("✖️ Cancel", "dialog:cancel")]],
        )
        self.assertNotIn("force_reply", sent.reply_markup.model_dump(exclude_none=True))
        self.assertIsNone(origin_of(await state.get_data()))
        self.assertEqual(bot.answers(), [])

    async def test_a_new_prompt_winds_the_previous_one_down(self):
        bot = FakeBot()
        state = make_state(bot)
        await state.set_state(Flow.name)
        await state.update_data(prompt_message_id=30, cleanup_ids=[31, 32], edit_subscription_id=4)
        typed = make_message(bot, 33, "x")
        await ask(typed, state, Flow.amount, "New amount?")
        self.assertEqual(bot.deleted_ids(), [30, 31, 32])
        data = await state.get_data()
        self.assertNotIn("edit_subscription_id", data)
        self.assertEqual(await state.get_state(), Flow.amount.state)


class TypoAndFinishTests(unittest.IsolatedAsyncioTestCase):
    async def test_typos_and_hints_are_remembered_then_everything_is_cleaned(self):
        bot = FakeBot()
        state = make_state(bot)
        screen = make_message(bot, 10, "screen", from_bot=True)
        prompt = await ask(make_callback(bot, screen), state, Flow.amount, "New amount?", origin_screen="pricing:1")

        typo = make_message(bot, 50, "abc")
        hint = await note_typo(typo, state, "⚠️ Need a number, for example <code>149.99</code>.")
        self.assertEqual(hint.text, "⚠️ Need a number, for example <code>149.99</code>.")
        self.assertEqual((await state.get_data())[CLEANUP_IDS], [50, hint.message_id])
        self.assertEqual(await state.get_state(), Flow.amount.state)

        good = make_message(bot, 51, "149.99")
        data = await finish_prompt(good, state)
        self.assertEqual(bot.deleted_ids(), [prompt.message_id, 50, hint.message_id, 51])
        self.assertEqual(data[ORIGIN_SCREEN], "pricing:1")
        self.assertEqual(origin_of(data), (CHAT_ID, 10))
        self.assertIsNone(await state.get_state())
        self.assertEqual(await state.get_data(), {})


class CancelAndWindDownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_without_fsm_data_still_removes_its_message(self):
        bot = FakeBot()
        state = make_state(bot)  # a restart emptied MemoryStorage
        old_prompt = make_message(bot, 60, "✏️ New amount?", from_bot=True)
        await cancel_prompt(make_callback(bot, old_prompt, "dialog:cancel"), state)
        self.assertEqual(bot.deleted_ids(), [60])
        self.assertEqual([answer.text for answer in bot.answers()], ["Cancelled"])

    async def test_cancel_deletes_prompt_typos_and_hints_once(self):
        bot = FakeBot()
        state = make_state(bot)
        await state.set_state(Flow.amount)
        await state.update_data(prompt_message_id=61, cleanup_ids=[62, 63])
        prompt = make_message(bot, 61, "✏️ New amount?", from_bot=True)
        await cancel_prompt(make_callback(bot, prompt, "dialog:cancel"), state)
        self.assertEqual(sorted(bot.deleted_ids()), [61, 62, 63])
        self.assertIsNone(await state.get_state())
        self.assertEqual(len(bot.answers()), 1)

    async def test_old_prompt_loses_its_buttons_when_it_cannot_be_deleted(self):
        bot = FakeBot()
        bot.fail(DeleteMessage, "Bad Request: message can't be deleted", message_id=70)
        await wind_down(bot, CHAT_ID, {PROMPT_MESSAGE_ID: 70, CLEANUP_IDS: [71]})
        self.assertEqual(bot.deleted_ids(), [70, 71])
        stripped = bot.of(EditMessageReplyMarkup)
        self.assertEqual([(call.message_id, call.reply_markup) for call in stripped], [(70, None)])

    async def test_gone_messages_and_empty_data_are_quiet(self):
        bot = FakeBot()
        bot.fail(DeleteMessage, "Bad Request: message to delete not found")
        await wind_down(bot, CHAT_ID, {PROMPT_MESSAGE_ID: 72})
        await wind_down(bot, CHAT_ID, {})
        await wind_down(bot, CHAT_ID, None)
        self.assertEqual(bot.of(EditMessageReplyMarkup), [])

    async def test_abandoned_wizard_is_deleted_or_marked_cancelled(self):
        bot = FakeBot()
        await wind_down(bot, CHAT_ID, {WIZARD_MESSAGE_ID: 80, WIZARD_CHAT_ID: CHAT_ID})
        self.assertEqual(bot.deleted_ids(), [80])

        bot = FakeBot()
        bot.fail(DeleteMessage, "Bad Request: message can't be deleted")
        await wind_down(bot, CHAT_ID, {WIZARD_MESSAGE_ID: 81, WIZARD_CHAT_ID: CHAT_ID})
        edits = bot.of(EditMessageText)
        self.assertEqual([(call.message_id, call.text, call.reply_markup) for call in edits], [(81, "✖️ Cancelled", None)])


class WizardTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_message_wizard(self):
        bot = FakeBot()
        state = make_state(bot)
        start = make_callback(bot, make_message(bot, 90, "📋 Subscriptions", from_bot=True), "sub:new")
        wizard = await start_wizard(start, state, Flow.name, "➕ <b>New subscription</b> · step 1 of 6")
        self.assertEqual(wizard_of(await state.get_data()), (CHAT_ID, wizard.message_id))
        self.assertEqual(len(bot.answers()), 1)

        typo = make_message(bot, 91, "")
        await note_typo(typo, state, "⚠️ Send text, for example <code>Netflix</code>.")
        answer = make_message(bot, 92, "Netflix")
        wizard_id = await advance_wizard(
            bot, state, "➕ <b>New subscription</b> · step 2 of 6", answer=answer, new_state=Flow.step_two
        )
        self.assertEqual(wizard_id, wizard.message_id)
        self.assertEqual(sorted(bot.deleted_ids())[:2], [91, 92])
        edit = bot.of(EditMessageText)[-1]
        self.assertEqual((edit.message_id, edit.text), (wizard.message_id, "➕ <b>New subscription</b> · step 2 of 6"))
        self.assertEqual(await state.get_state(), Flow.step_two.state)
        self.assertEqual((await state.get_data())[CLEANUP_IDS], [])

        last = make_message(bot, 93, "15.99")
        data = await finish_wizard(state, answer=last)
        self.assertIn(93, bot.deleted_ids())
        self.assertNotIn(wizard.message_id, bot.deleted_ids())
        self.assertEqual(wizard_of(data), (CHAT_ID, wizard.message_id))
        self.assertIsNone(await state.get_state())

    async def test_gone_wizard_continues_in_a_new_message(self):
        bot = FakeBot()
        state = make_state(bot)
        wizard = await start_wizard(make_message(bot, 95, "x"), state, Flow.name, "step 1")
        bot.fail(EditMessageText, "Bad Request: message to edit not found")
        wizard_id = await advance_wizard(bot, state, "step 2", answer=make_message(bot, 96, "Netflix"))
        self.assertNotEqual(wizard_id, wizard.message_id)
        self.assertEqual(bot.sent_texts()[-1], "step 2")
        self.assertEqual(wizard_of(await state.get_data()), (CHAT_ID, wizard_id))

    async def test_advance_without_a_wizard_returns_none(self):
        bot = FakeBot()
        state = make_state(bot)
        self.assertIsNone(await advance_wizard(bot, state, "step 2"))

    async def test_a_new_prompt_retires_a_running_wizard(self):
        bot = FakeBot()
        state = make_state(bot)
        wizard = await start_wizard(make_message(bot, 97, "x"), state, Flow.name, "step 1")
        await ask(make_message(bot, 98, "y"), state, Flow.amount, "New amount?")
        self.assertIn(wizard.message_id, bot.deleted_ids())
        self.assertIsNone(wizard_of(await state.get_data()))


if __name__ == "__main__":
    unittest.main()
