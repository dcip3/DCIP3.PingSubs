"""Semantic button colours, prompts without a dead force_reply, stable pagination rows and the invite copy button."""

import unittest

from app.ui.keyboards import (
    COPY_TEXT_MAX_LEN,
    build_currency_keyboard,
    build_members_list_keyboard,
    build_period_keyboard,
    build_share_limit_keyboard,
    build_subscription_list_keyboard,
    comment_edit_keyboard,
    dialog_cancel_inline_keyboard,
    invite_link_keyboard,
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
from keyboard_guard import install_keyboard_guard

install_keyboard_guard()

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

    def test_new_subscription_is_the_primary_action_only_while_the_list_is_empty(self):
        self._assert_single_primary(build_subscription_list_keyboard([]), "➕ New subscription")
        markup = build_subscription_list_keyboard(
            [{"id": 1, "name": "Netflix", "amount": 9.5, "currency": "USD", "participant_count": 1}]
        )
        self.assertEqual([button for button in _buttons(markup) if button.get("style")], [])
        self.assertEqual(_buttons(markup)[0]["text"], "➕ New subscription")

    def test_add_user_is_the_primary_action_only_while_there_are_no_users(self):
        self._assert_single_primary(build_members_list_keyboard([], total_users=0), "➕ Add user")
        markup = build_members_list_keyboard([{"id": 1, "full_name": "Anna", "telegram_id": 42}], total_users=1)
        self.assertEqual([button for button in _buttons(markup) if button.get("style")], [])
        self.assertEqual(_buttons(markup)[0]["text"], "➕ Add user")

    def test_add_payment_method_is_the_primary_action_only_while_there_are_no_methods(self):
        self._assert_single_primary(payment_destinations_keyboard([], None, 0), "➕ Add payment method")
        markup = payment_destinations_keyboard([{"id": 1, "title": "Card", "currency": "RUB"}], 1, 0)
        self.assertEqual([button for button in _buttons(markup) if button.get("style")], [])
        self.assertEqual(_buttons(markup)[-1]["text"], "➕ Add payment method")

    def test_pending_top_ups_are_the_primary_action_while_some_wait(self):
        markup = payment_destinations_keyboard([{"id": 1, "title": "Card", "currency": "RUB"}], 1, 2)
        self._assert_single_primary(markup, "📥 Pending top-ups (2)")
        self.assertEqual(_buttons(markup)[0]["callback_data"], "topup:admin_list:0")
        # With no method yet the waiting top-ups stay the one call to action.
        self._assert_single_primary(payment_destinations_keyboard([], None, 1), "📥 Pending top-ups (1)")

    def test_top_up_is_the_primary_action(self):
        self._assert_single_primary(public_account_keyboard(), "➕ Top up")

    def test_balance_screen_has_no_coloured_button(self):
        # Setting a balance is an everyday edit, not a call to action that is due now.
        markup = member_balance_keyboard(7)
        self.assertEqual([button for button in _buttons(markup) if button.get("style")], [])
        self.assertEqual(_button(markup, "✏️ Set amount")["callback_data"], "member:balance_set:7")

    def test_amount_per_user_has_no_coloured_button(self):
        # Setting amounts is an everyday edit; "Set all" asks a red confirmation instead (WP5).
        participants = [{"id": 1, "full_name": "Anna", "share_weight": 1, "fixed_amount": None}]
        markup = subscription_user_amounts_keyboard(3, participants, "usd", 10.0)
        self.assertEqual([button for button in _buttons(markup) if button.get("style")], [])
        self.assertEqual(_button(markup, "✏️ Set all")["callback_data"], "sub_user_amount_all:3")

    def test_reminders_screen_has_no_coloured_button(self):
        # WP6: "Send now" is a way into the picker, not the call to action; that is "Remind unpaid".
        markup = reminder_settings_keyboard(3)
        self.assertEqual([button for button in _buttons(markup) if button.get("style")], [])
        self.assertEqual(_button(markup, "📬 Send now")["callback_data"], "sub:reminders_send:3")

    def test_remind_unpaid_is_the_primary_action_only_while_someone_is_unpaid(self):
        markup = reminder_send_targets_keyboard(3, [{"full_name": "Anna", "telegram_id": 42, "status": "unpaid"}])
        self._assert_single_primary(markup, "📬 Remind unpaid (1)")
        self.assertEqual(_button(markup, "📬 Remind unpaid (1)")["callback_data"], "remsend:3:0")
        paid = reminder_send_targets_keyboard(3, [{"full_name": "Anna", "telegram_id": 42, "status": "paid"}])
        self.assertEqual([button for button in _buttons(paid) if button.get("style")], [])

    def test_send_test_reminders_is_the_primary_action(self):
        self._assert_single_primary(tests_menu_keyboard(), "📬 Send test reminders")


class DeleteConfirmationTests(unittest.TestCase):
    def test_member_delete_confirm_is_red_with_bin_label(self):
        button = _button(member_delete_confirm_keyboard(5), "🗑 Delete")
        self.assertEqual(button["style"], "danger")
        self.assertEqual(button["callback_data"], "member:confirm_delete:5")
        self.assertEqual(
            _dump(member_delete_confirm_keyboard(5))["inline_keyboard"],
            [
                [
                    {"text": "✖️ Cancel", "callback_data": "member:open:5"},
                    {"text": "🗑 Delete", "callback_data": "member:confirm_delete:5", "style": "danger"},
                ]
            ],
        )

    def test_payment_destination_delete_confirm_is_red_with_bin_label(self):
        button = _button(payment_destination_delete_keyboard(9), "🗑 Delete")
        self.assertEqual(button["style"], "danger")
        self.assertEqual(button["callback_data"], "paydest:confirm_delete:9")
        self.assertEqual(
            _dump(payment_destination_delete_keyboard(9))["inline_keyboard"],
            [
                [
                    {"text": "✖️ Cancel", "callback_data": "paydest:open:9"},
                    {"text": "🗑 Delete", "callback_data": "paydest:confirm_delete:9", "style": "danger"},
                ]
            ],
        )

    def test_dialog_cancel_is_a_plain_way_out(self):
        # Cancel only drops the prompt: it is a way out, never styled (principle button-styles).
        button = _button(dialog_cancel_inline_keyboard(), "✖️ Cancel")
        self.assertEqual(button, {"text": "✖️ Cancel", "callback_data": "dialog:cancel"})


class ForceReplyTests(unittest.TestCase):
    """A message carries one reply_markup: with the inline [✖️ Cancel] a ForceReply
    can never reach the client, so no prompt keyboard sends the dead extra."""

    def test_prompt_keyboard_is_inline_cancel_only(self):
        dumped = _dump(dialog_cancel_inline_keyboard())
        self.assertNotIn("force_reply", dumped)
        self.assertEqual(dumped["inline_keyboard"], [[{"text": "✖️ Cancel", "callback_data": "dialog:cancel"}]])

    def test_builder_based_prompts_never_send_force_reply(self):
        comment_markup = comment_edit_keyboard(3, True)
        for markup in (
            build_currency_keyboard(("usd", "eur")),
            build_period_keyboard(),
            build_share_limit_keyboard(),
            user_amount_clear_keyboard(3, 4),
            comment_markup,
        ):
            with self.subTest(markup=markup):
                dumped = _dump(markup)
                self.assertNotIn("force_reply", dumped)
                self.assertEqual(dumped["inline_keyboard"][-1][0]["callback_data"], "dialog:cancel")
        # WP7: no "✖️ Close" (it left the input armed); "🗑 Clear comment" is too wide to share
        # a row with Cancel, so it sits above it like any quick value.
        self.assertEqual(
            _dump(comment_markup)["inline_keyboard"],
            [
                [{"text": "🗑 Clear comment", "callback_data": "sub:comment_clear:3"}],
                [{"text": "✖️ Cancel", "callback_data": "dialog:cancel"}],
            ],
        )
        self.assertEqual(
            _dump(comment_edit_keyboard(3, False))["inline_keyboard"],
            [[{"text": "✖️ Cancel", "callback_data": "dialog:cancel"}]],
        )

    def test_currency_keyboard_callbacks_are_unchanged(self):
        dumped = _dump(build_currency_keyboard(("usd", "eur", "rub")))
        # Two per row, like every currency picker; the callbacks are unchanged.
        self.assertEqual(
            [button["callback_data"] for row in dumped["inline_keyboard"][:-1] for button in row],
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
