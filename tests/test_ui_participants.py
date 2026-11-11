"""WP5: Participants — the screen, the people picker, Share count and Amount per user with Set all."""

from datetime import date
import unittest

from aiogram.methods import EditMessageText, SendMessage

from app.core.constants import PAYMENT_MODE_FIXED, PAYMENT_MODE_SPLIT
from app.ui.guards import ALREADY_RUNNING_TEXT, guarded
from app.ui.helpers import SUBSCRIPTION_GONE, USER_GONE
from app.ui.keyboards import (
    build_participants_keyboard,
    build_share_limit_keyboard,
    participants_settings_keyboard,
    subscription_user_amounts_keyboard,
    user_amount_all_confirm_keyboard,
    user_amount_clear_keyboard,
)
from app.ui.participant_screens import (
    AMOUNT_HINT,
    AMOUNT_NEGATIVE_HINT,
    MAX_SET_ALL_AMOUNT,
    SHARE_COUNT_HINT,
    participants_text,
    picker_text,
    set_all_prompt_text,
    set_all_question,
    share_count_banner,
    share_count_prompt_text,
    user_amount_prompt_text,
    user_amounts_text,
)
from app.ui.states import ParticipantAction, SubscriptionAction, SubscriptionEditForm
from app.ui.style import NBSP
from dispatcher_harness import RealDispatcherTestCase
from keyboard_guard import install_keyboard_guard
from telegram_fakes import make_message
from test_keyboards_fit import AMOUNT, LONG_NAME, NAME_64, assert_fits

install_keyboard_guard()

BIG_ID = 2**31 - 1


def _texts(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _callbacks(markup) -> list[list[str | None]]:
    return [[button.callback_data for button in row] for row in markup.inline_keyboard]


def _styled(markup) -> list[tuple[str, str]]:
    return [(button.text, button.style) for row in markup.inline_keyboard for button in row if button.style]


def _cb(action: str, subscription_id: int) -> str:
    return SubscriptionAction(action=action, subscription_id=subscription_id).pack()


def _money(text: str) -> str:
    return text.replace(" ", NBSP)


def _sub(**fields) -> dict:
    sub = {"id": 7, "name": "Netflix", "amount": 15.99, "currency": "USD", "payment_mode": PAYMENT_MODE_SPLIT}
    sub.update(fields)
    return sub


def _person(friend_id: int, name: str, weight: int = 1, fixed=None, member: bool = True) -> dict:
    return {
        "id": friend_id,
        "full_name": name,
        "share_weight": weight,
        "fixed_amount": fixed,
        "is_member": 1 if member else 0,
        "telegram_id": 1000 + friend_id,
    }


# ------------------------------------------------------------------ keyboards


class ParticipantsKeyboardTests(unittest.TestCase):
    def test_participants_keyboard_split_and_fixed(self):
        split = participants_settings_keyboard(7, PAYMENT_MODE_SPLIT, participant_count=3)
        self.assertEqual(
            _texts(split),
            [["👥 Choose people"], ["✅ Split by shares"], ["Fixed per user"], ["➗ Share count"], ["◀️ To card"]],
        )
        self.assertEqual(
            _callbacks(split),
            [
                ["sub:participants_manage:7"],
                ["sub_payment_mode:7:split"],
                ["sub_payment_mode:7:fixed"],
                ["sub:share:7"],
                ["sub:open:7"],
            ],
        )
        self.assertEqual(_styled(split), [])

        fixed = participants_settings_keyboard(7, PAYMENT_MODE_FIXED, participant_count=3)
        self.assertEqual(
            _texts(fixed),
            [["👥 Choose people"], ["Split by shares"], ["✅ Fixed per user"], ["💵 Amount per user"], ["◀️ To card"]],
        )
        self.assertEqual(_callbacks(fixed)[3], ["sub:useramounts:7"])
        for mode in (PAYMENT_MODE_SPLIT, PAYMENT_MODE_FIXED, "", "weird"):
            for count in (0, 1, None):
                with self.subTest(mode=mode, count=count):
                    assert_fits(participants_settings_keyboard(BIG_ID, mode, participant_count=count))

    def test_choose_people_is_primary_only_while_nobody_is_in(self):
        # Empty: the CTA has the card's name, "👥 Add participants" (same callback).
        empty = participants_settings_keyboard(7, PAYMENT_MODE_SPLIT, participant_count=0)
        self.assertEqual(_styled(empty), [("👥 Add participants", "primary")])
        self.assertEqual(_callbacks(empty)[0], ["sub:participants_manage:7"])
        self.assertEqual(_styled(participants_settings_keyboard(7, PAYMENT_MODE_SPLIT)), [])

    def test_picker_rows_split_and_fixed(self):
        friends = [
            _person(1, "Alice Smith", weight=2),
            _person(2, "Carol", member=False),
        ]
        split = build_participants_keyboard(friends, 7)
        self.assertEqual(_texts(split), [["Alice Smith · 🟢", "×2"], ["Carol · ⚪"], ["◀️ Back"]])
        self.assertEqual(
            _callbacks(split),
            [["spart:toggle:7:1", "spart:weight:7:1"], ["spart:toggle:7:2"], ["sub:participants:7"]],
        )
        self.assertEqual(_styled(split), [])
        fixed = build_participants_keyboard(friends, 7, payment_mode=PAYMENT_MODE_FIXED)
        self.assertEqual(_texts(fixed), [["Alice Smith · 🟢"], ["Carol · ⚪"], ["◀️ Back"]])

    def test_picker_worst_case_names_and_paging(self):
        friends = [
            _person(1, NAME_64, weight=5),
            _person(2, LONG_NAME, weight=3),
            _person(3, NAME_64, member=False),
            _person(BIG_ID, LONG_NAME, member=False),
        ]
        for mode in (PAYMENT_MODE_SPLIT, PAYMENT_MODE_FIXED):
            markup = build_participants_keyboard(friends, BIG_ID, payment_mode=mode, page=2, total_pages=3)
            with self.subTest(mode=mode):
                assert_fits(markup)
                for row in _texts(markup)[:4]:
                    self.assertTrue(row[0].endswith((" · 🟢", " · ⚪")), row)
                self.assertEqual(_texts(markup)[-2], ["◀️", "2 / 3", "▶️"])
                self.assertEqual(
                    _callbacks(markup)[-2], [f"spart_page:{BIG_ID}:1", None, f"spart_page:{BIG_ID}:3"]
                )
                self.assertEqual(_texts(markup)[-1], ["◀️ Back"])
        self.assertTrue(all(len(data.encode()) <= 64 for row in _callbacks(markup) for data in row if data))

    def test_user_amount_buttons(self):
        people = [
            _person(1, "Alice Smith", fixed=10.0),
            _person(2, "Bob"),
            _person(3, "Boris", weight=2),
            _person(4, "Zero", fixed=0.0),
        ]
        markup = subscription_user_amounts_keyboard(7, people, "USD", 15.99)
        self.assertEqual(
            _texts(markup),
            [
                ["Alice Smith · 10.00 ✏️"],
                ["Bob · 15.99"],
                ["Boris · 15.99 ×2"],
                ["Zero · 0.00 ✏️"],
                ["✏️ Set all"],
                ["◀️ Back"],
            ],
        )
        self.assertEqual(_callbacks(markup)[0], ["sub_user_amount:7:1"])
        self.assertEqual(_callbacks(markup)[-2:], [["sub_user_amount_all:7"], ["sub:participants:7"]])
        self.assertEqual(_styled(markup), [])
        # Nobody in it: never a dead end, the way forward is to add people.
        self.assertEqual(
            _texts(subscription_user_amounts_keyboard(7, [], "USD", 15.99)), [["👥 Add participants"], ["◀️ Back"]]
        )

        worst = [_person(1, NAME_64, weight=5, fixed=AMOUNT), _person(2, LONG_NAME, weight=4), _person(BIG_ID, NAME_64)]
        markup = subscription_user_amounts_keyboard(BIG_ID, worst, "RUB", AMOUNT)
        assert_fits(markup)
        self.assertTrue(_texts(markup)[0][0].endswith(f" · 12{NBSP}500.00 ×5 ✏️"), _texts(markup)[0][0])

    def test_user_amount_prompt_keyboard_is_one_row_cancel_left(self):
        markup = user_amount_clear_keyboard(3, 4)
        dumped = markup.model_dump(exclude_none=True)
        self.assertEqual(
            dumped["inline_keyboard"],
            [
                [
                    {"text": "✖️ Cancel", "callback_data": "dialog:cancel"},
                    {"text": "↺ Use default", "callback_data": "sub_user_amount_clear:3:4"},
                ]
            ],
        )
        self.assertNotIn("force_reply", dumped)
        assert_fits(user_amount_clear_keyboard(BIG_ID, BIG_ID))

    def test_set_all_confirmation(self):
        markup = user_amount_all_confirm_keyboard(7, 30000)
        self.assertEqual(_texts(markup), [["✖️ Cancel", "✏️ Set all"]])
        self.assertEqual(_callbacks(markup), [["sub:useramounts:7", "sub_user_amount_all_yes:7:30000"]])
        self.assertEqual(_styled(markup), [("✏️ Set all", "danger")])
        worst = user_amount_all_confirm_keyboard(BIG_ID, MAX_SET_ALL_AMOUNT * 100)
        assert_fits(worst)
        self.assertLessEqual(len(_callbacks(worst)[0][1].encode()), 64)

    def test_share_count_prompt_keyboard(self):
        self.assertEqual(_texts(build_share_limit_keyboard(everyone_selected=True)), [["✅ Everyone"], ["✖️ Cancel"]])
        assert_fits(build_share_limit_keyboard(everyone_selected=True))


# ---------------------------------------------------------------------- texts


class ParticipantsTextTests(unittest.TestCase):
    def test_split_screen(self):
        people = [_person(1, "Alice Smith", weight=2), _person(2, "Bob <B>")]
        self.assertEqual(
            participants_text(_sub(amount=24.0), people),
            "👥 <b>Participants · Netflix</b> · 2\n\n"
            "Mode: split by shares · 3 shares\n"
            f"• Alice Smith ×2 — <b>≈ {_money('16.00 USD')}</b>\n"
            f"• Bob &lt;B&gt; — <b>≈ {_money('8.00 USD')}</b>\n\n"
            "Each person's amount changes with the mode.",
        )
        limited = participants_text(_sub(amount=24.0, share_limit=4), people)
        self.assertIn("Mode: split by shares · 4 shares\n", limited)
        self.assertIn(f"Alice Smith ×2 — <b>≈ {_money('12.00 USD')}</b>", limited)

    def test_fixed_screen_warns_when_the_total_differs(self):
        people = [_person(1, "Alice Smith", fixed=20.0), _person(2, "Bob")]
        self.assertEqual(
            participants_text(_sub(payment_mode=PAYMENT_MODE_FIXED), people),
            "👥 <b>Participants · Netflix</b> · 2\n\n"
            f"Mode: fixed per user · total <b>{_money('35.99 USD')}</b>\n"
            f"⚠️ Total differs from the subscription ({_money('15.99 USD')}).\n"
            f"• Alice Smith — <b>{_money('20.00 USD')}</b>\n"
            f"• Bob — <b>{_money('15.99 USD')}</b>\n\n"
            "Each person's amount changes with the mode.",
        )
        same = participants_text(_sub(payment_mode=PAYMENT_MODE_FIXED), [_person(1, "Alice")])
        self.assertNotIn("⚠️", same)

    def test_empty_screen_and_fold(self):
        self.assertEqual(
            participants_text(_sub(), []),
            "👥 <b>Participants · Netflix</b>\n\nMode: split by shares\nNo one yet — nobody gets reminders.",
        )
        self.assertEqual(
            participants_text(_sub(payment_mode=PAYMENT_MODE_FIXED), []),
            "👥 <b>Participants · Netflix</b>\n\nMode: fixed per user\nNo one yet — nobody gets reminders.",
        )
        many = participants_text(_sub(), [_person(i, f"Person {i}") for i in range(1, 9)])
        self.assertIn("· 8\n\nMode: split by shares · 8 shares\n<blockquote expandable>• Person 1 — ", many)
        self.assertTrue(many.endswith("</blockquote>\n\nEach person's amount changes with the mode."), many)
        huge = participants_text(_sub(), [_person(i, NAME_64) for i in range(1, 200)])
        self.assertIn("…and ", huge)
        self.assertLess(len(huge), 4096)

    def test_picker_texts(self):
        self.assertEqual(
            picker_text(_sub(), 2, 5, has_unsigned=True),
            "👥 <b>Choose people · Netflix</b> · 2 of 5\n\n"
            "Tap a name to add or remove, ×N to change a share (1–5); people who have not signed in are not listed.",
        )
        # The chosen people are named in full above the buttons (which cut long names).
        long_name = "Alexandra Konstantinopolskaya-Wolfeschlegel"
        chosen = [{"full_name": long_name, "share_weight": 2}, {"full_name": "Bob <B>", "share_weight": 1}]
        self.assertEqual(
            picker_text(_sub(), 2, 5, chosen=chosen),
            "👥 <b>Choose people · Netflix</b> · 2 of 5\n\n"
            f"Chosen: {long_name} ×2, Bob &lt;B&gt; ×1\n\n"
            "Tap a name to add or remove, ×N to change a share (1–5).",
        )
        many = picker_text(_sub(), 8, 8, chosen=[{"full_name": NAME_64, "share_weight": 1}] * 8)
        self.assertIn("Chosen: <b>8</b>\n<blockquote expandable>• ", many)
        self.assertEqual(
            picker_text(_sub(payment_mode=PAYMENT_MODE_FIXED), 0, 5),
            "👥 <b>Choose people · Netflix</b> · 0 of 5\n\nTap a name to add or remove.",
        )
        self.assertEqual(
            picker_text(_sub(), 0, 0),
            "👥 <b>Choose people · Netflix</b>\n\nNo users yet. Add people in “👥 Users” first.",
        )
        self.assertEqual(
            picker_text(_sub(), 0, 0, has_unsigned=True),
            "👥 <b>Choose people · Netflix</b>\n\nNo one has signed in yet. People appear here once they sign in.",
        )

    def test_amount_per_user_text(self):
        people = [_person(1, "Alice Smith", fixed=20.0), _person(2, "Bob")]
        self.assertEqual(
            user_amounts_text(_sub(payment_mode=PAYMENT_MODE_FIXED), people),
            "💵 <b>Amount per user · Netflix</b>\n\n"
            f"Subscription: <b>{_money('15.99 USD')}</b>\nAssigned total: <b>{_money('35.99 USD')}</b>\n"
            "⚠️ The total differs from the subscription.\n\n"
            "✏️ marks a set amount; others pay the full subscription amount.",
        )
        self.assertNotIn("⚠️", user_amounts_text(_sub(payment_mode=PAYMENT_MODE_FIXED), [_person(2, "Bob")]))

    def test_prompts_and_banners(self):
        sub = _sub(payment_mode=PAYMENT_MODE_FIXED)
        self.assertEqual(
            user_amount_prompt_text(sub, _person(1, "Alice Smith", fixed=10.0)),
            "✏️ Amount for Alice Smith, USD? Now 10.00 (set).\nSend <code>0</code> or more.",
        )
        self.assertEqual(
            user_amount_prompt_text(sub, _person(2, "Bob <B>")),
            "✏️ Amount for Bob &lt;B&gt;, USD? Now 15.99 (default).\nSend <code>0</code> or more.",
        )
        self.assertEqual(set_all_prompt_text(sub, 3), "✏️ One amount for all 3 people in Netflix, USD?")
        self.assertEqual(set_all_prompt_text(sub, 1), "✏️ One amount for all 1 person in Netflix, USD?")
        self.assertEqual(
            set_all_question(300.0, "USD", 3),
            f"⚠️ <b>Set {_money('300.00 USD')} for all 3 people?</b>\nTheir own amounts are replaced.",
        )
        people = [_person(1, "Alice", weight=3), _person(2, "Bob")]
        self.assertEqual(
            share_count_prompt_text(_sub(), people),
            "✏️ How many people share Netflix? Now everyone (4 shares).\nTap “Everyone” or send a number.",
        )
        self.assertEqual(
            share_count_prompt_text(_sub(share_limit=3), people),
            "✏️ How many people share Netflix? Now 3.\nTap “Everyone” or send a number.",
        )
        self.assertIn("Now everyone.\n", share_count_prompt_text(_sub(), []))
        self.assertEqual(share_count_banner(3), "✅ Share count · <b>3</b>")
        self.assertEqual(share_count_banner(None), "✅ Share count · <b>everyone</b>")
        self.assertEqual(SHARE_COUNT_HINT, "⚠️ Need a whole number, for example <code>3</code>.")


# ----------------------------------------------------------------------- flows


class ParticipantsFlowTests(RealDispatcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.make_admin()
        self.sub_id = await self.db.create_subscription("Netflix", 15.99, "USD", date(2099, 1, 15), 30, None)
        self.alice = await self._friend("Alice Smith", 1001)
        self.bob = await self._friend("Bob", 1002)
        self.carol = await self._friend("Carol", 1003)
        await self.db.create_friend_invite("Dan", "token-dan", "2099-01-01")
        for friend_id in (self.alice, self.bob):
            await self.db.set_participant(self.sub_id, friend_id, True)
        self.screen = make_message(self.bot, 600, "screen", from_bot=True)

    async def _friend(self, name: str, telegram_id: int) -> int:
        friend_id = await self.db.create_friend_invite(name, f"token-{telegram_id}", "2099-01-01")
        await self.db.claim_friend_invite(friend_id, telegram_id, "2026-01-01")
        return friend_id

    def _edits(self) -> list[EditMessageText]:
        return self.bot.of(EditMessageText)

    def _toasts(self) -> list[str | None]:
        return [answer.text for answer in self.bot.answers()]

    async def _amount_of(self, friend_id: int):
        people = await self.db.list_subscription_participants(self.sub_id)
        return next(person for person in people if person["id"] == friend_id)["fixed_amount"]

    async def _open_prompt(self, data: str) -> tuple[SendMessage, int]:
        await self.feed(self.callback(data, self.screen))
        prompt = self.bot.of(SendMessage)[-1]
        state = await self.state().get_data()
        self.assertEqual(state["origin_message_id"], 600)
        return prompt, state["prompt_message_id"]

    async def test_participants_screen_and_mode_switch(self):
        await self.feed(self.callback(_cb("participants", self.sub_id), self.screen))
        edit = self._edits()[-1]
        self.assertEqual(
            edit.text,
            "👥 <b>Participants · Netflix</b> · 2\n\nMode: split by shares · 2 shares\n"
            f"• Alice Smith — <b>≈ {_money('8.00 USD')}</b>\n• Bob — <b>≈ {_money('8.00 USD')}</b>\n\n"
            "Each person's amount changes with the mode.",
        )
        self.assertEqual(self._toasts(), [None])

        await self.feed(self.callback(f"sub_payment_mode:{self.sub_id}:fixed", self.screen))
        self.assertEqual((await self.db.get_subscription(self.sub_id))["payment_mode"], PAYMENT_MODE_FIXED)
        edit = self._edits()[-1]
        self.assertIn(f"Mode: fixed per user · total <b>{_money('31.98 USD')}</b>\n⚠️ Total differs", edit.text)
        self.assertEqual(_texts(edit.reply_markup)[3], ["💵 Amount per user"])
        self.assertEqual(self._toasts()[-1], "Payment mode: fixed")

        await self.feed(self.callback(f"sub_payment_mode:{self.sub_id}:split", self.screen))
        self.assertEqual(self._toasts()[-1], "Payment mode: split")

    async def test_picker_toggle_and_weight(self):
        await self.feed(self.callback(_cb("participants_manage", self.sub_id), self.screen))
        edit = self._edits()[-1]
        self.assertEqual(
            edit.text,
            "👥 <b>Choose people · Netflix</b> · 2 of 3\n\nChosen: Alice Smith ×1, Bob ×1\n\n"
            "Tap a name to add or remove, ×N to change a share (1–5); people who have not signed in are not listed.",
        )
        self.assertEqual(
            _texts(edit.reply_markup),
            [["Alice Smith · 🟢", "×1"], ["Bob · 🟢", "×1"], ["Carol · ⚪"], ["◀️ Back"]],
        )

        toggle = ParticipantAction(action="toggle", subscription_id=self.sub_id, friend_id=self.carol).pack()
        await self.feed(self.callback(toggle, self.screen))
        self.assertEqual(self._toasts()[-1], "Carol added")
        self.assertTrue(self._edits()[-1].text.startswith("👥 <b>Choose people · Netflix</b> · 3 of 3\n"))
        await self.feed(self.callback(toggle, self.screen))
        self.assertEqual(self._toasts()[-1], "Carol removed")

        weight = ParticipantAction(action="weight", subscription_id=self.sub_id, friend_id=self.alice).pack()
        await self.feed(self.callback(weight, self.screen))
        self.assertEqual(self._toasts()[-1], "Alice Smith: 2 shares")
        self.assertEqual(_texts(self._edits()[-1].reply_markup)[0], ["Alice Smith · 🟢", "×2"])

        not_in = ParticipantAction(action="weight", subscription_id=self.sub_id, friend_id=self.carol).pack()
        await self.feed(self.callback(not_in, self.screen))
        self.assertEqual(self._toasts()[-1], "List updated")

        gone = ParticipantAction(action="toggle", subscription_id=self.sub_id, friend_id=9999).pack()
        await self.feed(self.callback(gone, self.screen))
        self.assertEqual(self._toasts()[-1], USER_GONE)
        self.assertTrue(self._edits()[-1].text.startswith("👥 <b>Choose people · Netflix</b>"))

    async def test_picker_pages_and_stays_on_the_tapped_page(self):
        for index in range(25):
            await self._friend(f"Zed {index:02d}", 2000 + index)
        await self.feed(self.callback(_cb("participants_manage", self.sub_id), self.screen))
        rows = _texts(self._edits()[-1].reply_markup)
        self.assertEqual(rows[-2], ["◀️", "1 / 2", "▶️"])
        self.assertEqual(len(rows), 22)

        await self.feed(self.callback(f"spart_page:{self.sub_id}:2", self.screen))
        markup = self._edits()[-1].reply_markup
        self.assertEqual(_texts(markup)[-2], ["◀️", "2 / 2", "▶️"])
        last = ParticipantAction.unpack(_callbacks(markup)[0][0])
        await self.feed(self.callback(last.pack(), self.screen))
        self.assertEqual(_texts(self._edits()[-1].reply_markup)[-2], ["◀️", "2 / 2", "▶️"])

    async def test_picker_without_users_is_not_a_dead_end(self):
        for friend in await self.db.list_friends():
            await self.db.delete_friend(friend["id"])
        await self.feed(self.callback(_cb("participants_manage", self.sub_id), self.screen))
        edit = self._edits()[-1]
        self.assertEqual(edit.text, "👥 <b>Choose people · Netflix</b>\n\nNo users yet. Add people in “👥 Users” first.")
        self.assertEqual(_callbacks(edit.reply_markup), [[f"sub:participants:{self.sub_id}"]])

    async def test_share_count_typed_and_everyone(self):
        prompt, prompt_id = await self._open_prompt(_cb("share", self.sub_id))
        self.assertEqual(
            prompt.text,
            "✏️ How many people share Netflix? Now everyone (2 shares).\nTap “Everyone” or send a number.",
        )
        self.assertEqual(_texts(prompt.reply_markup), [["✅ Everyone"], ["✖️ Cancel"]])
        typo = self.message("lots")
        await self.feed(typo)
        self.assertEqual(self.bot.sent_texts()[-1], SHARE_COUNT_HINT)

        answer = self.message("3")
        await self.feed(answer)
        self.assertEqual((await self.db.get_subscription(self.sub_id))["share_limit"], 3)
        edit = self._edits()[-1]
        self.assertEqual(edit.message_id, 600)
        self.assertTrue(edit.text.startswith("✅ Share count · <b>3</b>\n\n👥 <b>Participants · Netflix</b> · 2\n"))
        self.assertIn("Mode: split by shares · 3 shares\n", edit.text)
        for message_id in (prompt_id, typo.message_id, answer.message_id):
            self.assertIn(message_id, self.bot.deleted_ids())
        self.assertIsNone(await self.state().get_state())
        self.assertNotIn("Split limit updated.", self.bot.sent_texts())

        prompt, prompt_id = await self._open_prompt(_cb("share", self.sub_id))
        self.assertIn("Now 3.", prompt.text)
        self.assertEqual(_texts(prompt.reply_markup), [["Everyone"], ["✖️ Cancel"]])
        await self.feed(self.callback("sharelimit:all", make_message(self.bot, prompt_id, prompt.text, from_bot=True)))
        self.assertIsNone((await self.db.get_subscription(self.sub_id))["share_limit"])
        self.assertTrue(self._edits()[-1].text.startswith("✅ Share count · <b>everyone</b>\n\n👥 <b>Participants"))
        self.assertEqual(self._edits()[-1].message_id, 600)
        self.assertEqual(self._toasts()[-1], "✅ Share count · everyone")
        self.assertIn(prompt_id, self.bot.deleted_ids())

    async def test_amount_per_user_prompt_set_and_use_default(self):
        await self.db.update_subscription_fields(self.sub_id, payment_mode=PAYMENT_MODE_FIXED)
        await self.feed(self.callback(_cb("useramounts", self.sub_id), self.screen))
        screen = self._edits()[-1]
        self.assertTrue(screen.text.startswith("💵 <b>Amount per user · Netflix</b>\n\n"), screen.text)
        self.assertEqual(_texts(screen.reply_markup), [["Alice Smith · 15.99"], ["Bob · 15.99"], ["✏️ Set all"], ["◀️ Back"]])

        prompt, prompt_id = await self._open_prompt(f"sub_user_amount:{self.sub_id}:{self.alice}")
        self.assertEqual(prompt.text, "✏️ Amount for Alice Smith, USD? Now 15.99 (default).\nSend <code>0</code> or more.")
        self.assertEqual(_texts(prompt.reply_markup), [["✖️ Cancel"]])
        self.assertEqual(await self.state().get_state(), SubscriptionEditForm.user_amount.state)
        await self.feed(self.message("much"))
        self.assertEqual(self.bot.sent_texts()[-1], AMOUNT_HINT)
        await self.feed(self.message("-5"))
        self.assertEqual(self.bot.sent_texts()[-1], AMOUNT_NEGATIVE_HINT)
        await self.feed(self.message("12"))
        self.assertEqual(await self._amount_of(self.alice), 12.0)
        edit = self._edits()[-1]
        self.assertEqual(edit.message_id, 600)
        self.assertTrue(edit.text.startswith(f"✅ Alice Smith · <b>{_money('12.00 USD')}</b>\n\n💵 <b>Amount per user"))
        self.assertEqual(_texts(edit.reply_markup)[0], ["Alice Smith · 12.00 ✏️"])
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertNotIn("Fixed amount updated.", self.bot.sent_texts())

        prompt, prompt_id = await self._open_prompt(f"sub_user_amount:{self.sub_id}:{self.alice}")
        self.assertIn("Now 12.00 (set).", prompt.text)
        self.assertEqual(_texts(prompt.reply_markup), [["✖️ Cancel", "↺ Use default"]])
        prompt_message = make_message(self.bot, prompt_id, prompt.text, from_bot=True)
        await self.feed(self.callback(f"sub_user_amount_clear:{self.sub_id}:{self.alice}", prompt_message))
        self.assertIsNone(await self._amount_of(self.alice))
        edit = self._edits()[-1]
        self.assertEqual(edit.message_id, 600)
        self.assertTrue(edit.text.startswith("✅ Alice Smith pays the default\n\n💵 <b>Amount per user"))
        self.assertEqual(self._toasts()[-1], "✅ Alice Smith pays the default")
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertIsNone(await self.state().get_state())

        # Typing a default word works too.
        await self.db.update_participant_fixed_amount(self.sub_id, self.bob, 5.0)
        await self._open_prompt(f"sub_user_amount:{self.sub_id}:{self.bob}")
        await self.feed(self.message("default"))
        self.assertIsNone(await self._amount_of(self.bob))
        self.assertTrue(self._edits()[-1].text.startswith("✅ Bob pays the default\n\n"))

    async def test_use_default_on_an_old_prompt_becomes_the_screen(self):
        await self.db.update_subscription_fields(self.sub_id, payment_mode=PAYMENT_MODE_FIXED)
        await self.db.update_participant_fixed_amount(self.sub_id, self.bob, 5.0)
        old_prompt = make_message(self.bot, 777, "👥 Amount per user:", from_bot=True)
        await self.feed(self.callback(f"sub_user_amount_clear:{self.sub_id}:{self.bob}", old_prompt))
        self.assertIsNone(await self._amount_of(self.bob))
        edit = self._edits()[-1]
        self.assertEqual(edit.message_id, 777)
        self.assertTrue(edit.text.startswith("✅ Bob pays the default\n\n💵 <b>Amount per user"))

    async def test_set_all_asks_then_applies(self):
        await self.db.update_subscription_fields(self.sub_id, payment_mode=PAYMENT_MODE_FIXED)
        prompt, prompt_id = await self._open_prompt(f"sub_user_amount_all:{self.sub_id}")
        self.assertEqual(prompt.text, "✏️ One amount for all 2 people in Netflix, USD?")
        self.assertEqual(_texts(prompt.reply_markup), [["✖️ Cancel"]])
        await self.feed(self.message("zero"))
        await self.feed(self.message("1 300"))
        question = self._edits()[-1]
        self.assertEqual(question.message_id, 600)
        self.assertEqual(
            question.text, f"⚠️ <b>Set {_money('1 300.00 USD')} for all 2 people?</b>\nTheir own amounts are replaced."
        )
        self.assertEqual(
            _callbacks(question.reply_markup),
            [[f"sub:useramounts:{self.sub_id}", f"sub_user_amount_all_yes:{self.sub_id}:130000"]],
        )
        self.assertIsNone(await self._amount_of(self.alice))
        self.assertIn(prompt_id, self.bot.deleted_ids())
        self.assertIsNone(await self.state().get_state())

        yes = f"sub_user_amount_all_yes:{self.sub_id}:130000"
        async with guarded(None, ("user_amount_all", self.sub_id)):
            await self.feed(self.callback(yes, self.screen))
        self.assertEqual(self._toasts()[-1], ALREADY_RUNNING_TEXT)
        self.assertIsNone(await self._amount_of(self.alice))

        await self.feed(self.callback(yes, self.screen))
        self.assertEqual(await self._amount_of(self.alice), 1300.0)
        self.assertEqual(await self._amount_of(self.bob), 1300.0)
        edit = self._edits()[-1]
        self.assertTrue(edit.text.startswith(f"✅ All set to <b>{_money('1 300.00 USD')}</b>\n\n💵 <b>Amount per user"))
        self.assertEqual(self._toasts()[-1], f"✅ All set to {_money('1 300.00 USD')}")

    async def test_set_all_cancel_returns_to_amount_per_user(self):
        await self.db.update_subscription_fields(self.sub_id, payment_mode=PAYMENT_MODE_FIXED)
        await self.feed(self.callback(_cb("useramounts", self.sub_id), self.screen))
        self.assertTrue(self._edits()[-1].text.startswith("💵 <b>Amount per user · Netflix</b>"))

    async def test_amount_per_user_in_split_mode_shows_participants(self):
        await self.feed(self.callback(_cb("useramounts", self.sub_id), self.screen))
        self.assertTrue(self._edits()[-1].text.startswith("👥 <b>Participants · Netflix</b>"))
        self.assertEqual(self._toasts()[-1], "Payment mode: split")

    async def test_deleted_subscription_is_not_a_dead_end(self):
        for data in (
            _cb("participants", 999),
            _cb("participants_manage", 999),
            _cb("share", 999),
            _cb("useramounts", 999),
            "sub_payment_mode:999:fixed",
            "spart_page:999:2",
            ParticipantAction(action="toggle", subscription_id=999, friend_id=self.alice).pack(),
            ParticipantAction(action="weight", subscription_id=999, friend_id=self.alice).pack(),
            f"sub_user_amount:999:{self.alice}",
            f"sub_user_amount_clear:999:{self.alice}",
            "sub_user_amount_all:999",
            "sub_user_amount_all_yes:999:100",
        ):
            with self.subTest(data=data):
                await self.feed(self.callback(data, self.screen))
                self.assertEqual(self._toasts()[-1], SUBSCRIPTION_GONE)
                self.assertTrue(self._edits()[-1].text.startswith("📋 <b>Subscriptions</b> · 1\n"))

        await self.db.update_subscription_fields(self.sub_id, payment_mode=PAYMENT_MODE_FIXED)
        await self._open_prompt(f"sub_user_amount:{self.sub_id}:{self.alice}")
        await self.db.delete_subscription(self.sub_id)
        await self.feed(self.message("20"))
        self.assertTrue(self.bot.sent_texts()[-1].startswith("⚠️ That subscription was deleted\n\n📋 <b>Subscriptions</b>"))
        self.assertIsNone(await self.state().get_state())


if __name__ == "__main__":
    unittest.main()
