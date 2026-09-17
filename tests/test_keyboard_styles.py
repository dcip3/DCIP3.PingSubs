"""Semantic button colours, force_reply prompts, stable pagination rows and the invite copy button."""

import unittest

from app.handlers.members import invite_link_keyboard
from app.ui.keyboards import (
    COPY_TEXT_MAX_LEN,
    build_currency_keyboard,
    build_members_list_keyboard,
    build_period_keyboard,
    build_share_limit_keyboard,
    build_subscription_list_keyboard,
    comment_edit_keyboard,
    dialog_cancel_inline_keyboard,
    member_balance_keyboard,
    member_delete_confirm_keyboard,
    pagination_row,
    payment_destination_delete_keyboard,
    payment_destinations_keyboard,
    public_account_keyboard,
    reminder_send_targets_keyboard,
    reminder_settings_keyboard,
    settings_tests_keyboard,
    subscription_user_amounts_keyboard,
    tests_menu_keyboard,
    user_amount_clear_keyboard,
)

NAV_TEXTS = {"⬅️ Back", "✖️ Close", "Next ▶️", "◀️", "▶️"}


def _dump(markup) -> dict:
    return markup.model_dump(exclude_none=True)


def _buttons(markup) -> list[dict]:
    return [button for row in _dump(markup)["inline_keyboard"] for button in row]


def _button(markup, text: str) -> dict:
    for button in _buttons(markup):
        if button["text"] == text:
            return button
    raise AssertionError(f"button {text!r} not found in {_buttons(markup)!r}")


class PrimaryActionTests(unittest.TestCase):
    def _assert_single_primary(self, markup, text: str) -> None:
        primary = [button["text"] for button in _buttons(markup) if button.get("style") == "primary"]
        self.assertEqual(primary, [text])
        for button in _buttons(markup):
            if button["text"] in NAV_TEXTS:
                self.assertNotIn("style", button, button)

    def test_new_subscription_is_the_primary_action(self):
        markup = build_subscription_list_keyboard([{"id": 1, "name": "Netflix", "amount": 9.5, "currency": "USD"}])
        self._assert_single_primary(markup, "➕ New subscription")

    def test_add_user_is_the_primary_action(self):
        markup = build_members_list_keyboard([{"id": 1, "full_name": "Anna"}], total_users=1)
        self._assert_single_primary(markup, "➕ Add user")

    def test_add_payment_method_is_the_primary_action(self):
        markup = payment_destinations_keyboard([{"id": 1, "title": "Card", "currency": "RUB"}], 1, 0)
        self._assert_single_primary(markup, "➕ Add payment method")

    def test_top_up_is_the_primary_action(self):
        self._assert_single_primary(public_account_keyboard(), "➕ Top up")

    def test_balance_set_is_the_primary_action(self):
        self._assert_single_primary(member_balance_keyboard(7), "✏️ Set")

    def test_set_all_is_the_primary_action(self):
        participants = [{"id": 1, "full_name": "Anna", "share_weight": 1, "fixed_amount": None}]
        self._assert_single_primary(subscription_user_amounts_keyboard(3, participants, "usd", 10.0), "✏️ Set all")

    def test_send_reminders_now_is_the_primary_action(self):
        self._assert_single_primary(reminder_settings_keyboard(3), "📬 Send reminders now")

    def test_send_to_all_is_the_primary_action(self):
        markup = reminder_send_targets_keyboard(3, [{"full_name": "Anna", "telegram_id": 42}])
        self._assert_single_primary(markup, "📬 All")

    def test_send_test_reminders_is_the_primary_action(self):
        self._assert_single_primary(tests_menu_keyboard(), "📬 Send test reminders")


class DeleteConfirmationTests(unittest.TestCase):
    def test_member_delete_confirm_is_red_with_bin_label(self):
        button = _button(member_delete_confirm_keyboard(5), "🗑 Yes, delete")
        self.assertEqual(button["style"], "danger")
        self.assertEqual(button["callback_data"], "member:confirm_delete:5")
        self.assertNotIn("style", _button(member_delete_confirm_keyboard(5), "⬅️ Back"))

    def test_payment_destination_delete_confirm_is_red_with_bin_label(self):
        button = _button(payment_destination_delete_keyboard(9), "🗑 Yes, delete")
        self.assertEqual(button["style"], "danger")
        self.assertEqual(button["callback_data"], "paydest:confirm_delete:9")

    def test_dialog_cancel_stays_red(self):
        self.assertEqual(_button(dialog_cancel_inline_keyboard(), "Cancel")["style"], "danger")


class ForceReplyTests(unittest.TestCase):
    def test_sent_prompt_keyboard_carries_force_reply(self):
        dumped = _dump(dialog_cancel_inline_keyboard())
        self.assertIs(dumped["force_reply"], True)
        self.assertEqual(dumped["inline_keyboard"], [[{"text": "Cancel", "callback_data": "dialog:cancel", "style": "danger"}]])

    def test_edit_variant_has_no_force_reply(self):
        dumped = _dump(dialog_cancel_inline_keyboard(force_reply=False))
        self.assertNotIn("force_reply", dumped)
        self.assertEqual(dumped["inline_keyboard"][0][0]["callback_data"], "dialog:cancel")

    def test_builder_based_prompts_keep_force_reply(self):
        comment_markup = comment_edit_keyboard(3, True, force_reply=True)
        for markup in (
            build_currency_keyboard(("usd", "eur")),
            build_period_keyboard(),
            build_share_limit_keyboard(),
            user_amount_clear_keyboard(3, 4),
            comment_markup,
        ):
            with self.subTest(markup=markup):
                dumped = _dump(markup)
                self.assertIs(dumped["force_reply"], True)
                cancel_row = -2 if markup is comment_markup else -1
                self.assertEqual(dumped["inline_keyboard"][cancel_row][0]["callback_data"], "dialog:cancel")

    def test_builder_based_prompts_drop_force_reply_on_edit_paths(self):
        for markup in (
            build_currency_keyboard(("usd",), force_reply=False),
            build_period_keyboard(force_reply=False),
            build_share_limit_keyboard(force_reply=False),
            user_amount_clear_keyboard(3, 4, force_reply=False),
            comment_edit_keyboard(3, False),
        ):
            with self.subTest(markup=markup):
                self.assertNotIn("force_reply", _dump(markup))

    def test_currency_keyboard_callbacks_are_unchanged(self):
        dumped = _dump(build_currency_keyboard(("usd", "eur", "rub")))
        self.assertEqual(
            [button["callback_data"] for button in dumped["inline_keyboard"][0]],
            ["currency:USD", "currency:EUR", "currency:RUB"],
        )


class PaginationRowTests(unittest.TestCase):
    def _nav_row(self, markup) -> list[dict]:
        rows = _dump(markup)["inline_keyboard"]
        nav_rows = [row for row in rows if any(button["text"] in {"◀️", "▶️"} for button in row)]
        self.assertLessEqual(len(nav_rows), 1)
        return nav_rows[0] if nav_rows else []

    def test_first_page_disables_previous_arrow(self):
        row = self._nav_row(build_members_list_keyboard([], page=1, total_pages=3))
        self.assertEqual(
            row,
            [
                {"text": "◀️", "disabled": {}},
                {"text": "1 / 3", "disabled": {}},
                {"text": "▶️", "callback_data": "member:page:2"},
            ],
        )

    def test_middle_page_enables_both_arrows(self):
        row = self._nav_row(build_members_list_keyboard([], page=2, total_pages=3))
        self.assertEqual(
            row,
            [
                {"text": "◀️", "callback_data": "member:page:1"},
                {"text": "2 / 3", "disabled": {}},
                {"text": "▶️", "callback_data": "member:page:3"},
            ],
        )

    def test_last_page_disables_next_arrow(self):
        row = self._nav_row(settings_tests_keyboard([], page=3, total_pages=3))
        self.assertEqual(
            row,
            [
                {"text": "◀️", "callback_data": "testlist:page:2"},
                {"text": "3 / 3", "disabled": {}},
                {"text": "▶️", "disabled": {}},
            ],
        )

    def test_single_page_renders_no_pagination_row(self):
        self.assertEqual(self._nav_row(build_members_list_keyboard([], page=1, total_pages=1)), [])
        self.assertEqual(self._nav_row(settings_tests_keyboard([], page=1, total_pages=1)), [])

    def test_pagination_row_helper_shape(self):
        row = pagination_row(1, 1, previous_callback="x:0", next_callback="x:2")
        self.assertEqual(len(row), 3)
        self.assertTrue(all(button.model_dump(exclude_none=True).get("disabled") == {} for button in row))

    def test_other_buttons_are_unchanged(self):
        markup = settings_tests_keyboard([{"full_name": "Anna", "telegram_id": 42}], page=1, total_pages=2, total_users=1)
        rows = _dump(markup)["inline_keyboard"]
        self.assertEqual(rows[0], [{"text": "Anna", "callback_data": "testsend:42"}])
        self.assertEqual(
            rows[-1],
            [
                {"text": "⬅️ Back", "callback_data": "settings:tests"},
                {"text": "✖️ Close", "callback_data": "menu:close"},
            ],
        )


class InviteCopyLinkTests(unittest.TestCase):
    def test_copy_link_button(self):
        link = "https://t.me/pingsubs_bot?start=join_abc"
        dumped = _dump(invite_link_keyboard(link))
        self.assertEqual(
            dumped,
            {"inline_keyboard": [[{"text": "📋 Copy link", "style": "primary", "copy_text": {"text": link}}]]},
        )

    def test_copy_text_is_trimmed_to_telegram_limit(self):
        link = "https://t.me/pingsubs_bot?start=" + "x" * 300
        dumped = _dump(invite_link_keyboard(link))
        self.assertEqual(len(dumped["inline_keyboard"][0][0]["copy_text"]["text"]), COPY_TEXT_MAX_LEN)


if __name__ == "__main__":
    unittest.main()
