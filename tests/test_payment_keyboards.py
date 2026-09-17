"""Copy-requisite / payment-link keyboard rows and the greyed-out Paid button."""

from datetime import date
import unittest

from app.handlers.reminders import mark_paid_button
from app.services import _build_batch_reminder_message, _build_reminder_message
from app.ui.keyboards import (
    build_batch_payment_confirmation_keyboard,
    build_payment_confirmation_keyboard,
    build_test_payment_confirmation_keyboard,
    extract_copyable_requisite,
    is_openable_payment_link,
    topup_request_submit_keyboard,
)

CARD_DETAILS = "Sber card 2202 2032 1234 5678\nAnna K."
IBAN_DETAILS = "IBAN DE89370400440532013000 BIC COBADEFFXXX"
PHONE_DETAILS = "SBP +7 999 123-45-67 Tinkoff"


def _item(subscription_id: int, name: str, due: date, details: str, link: str = "") -> dict[str, object]:
    return {
        "subscription_id": subscription_id,
        "subscription_name": name,
        "due_date": due,
        "due_value": due.isoformat(),
        "due_date_text": due.strftime("%d.%m.%Y"),
        "status_text": "Due today",
        "amount_text": "10.00 RUB",
        "payment_label": "Sber",
        "payment_details": details,
        "payment_link": link,
        "tz_name": "Europe/Moscow",
        "today": date(2026, 9, 17),
    }


class ExtractCopyableRequisiteTests(unittest.TestCase):
    def test_card_number_wins_and_is_copied_as_digits(self):
        self.assertEqual(
            extract_copyable_requisite(CARD_DETAILS),
            ("📋 Copy card number ···5678", "2202203212345678"),
        )
        self.assertEqual(extract_copyable_requisite("2202-2032-1234-5678")[1], "2202203212345678")
        self.assertEqual(extract_copyable_requisite("Card: 4276380012345678")[1], "4276380012345678")

    def test_iban_copies_the_whole_details_trimmed(self):
        label, text = extract_copyable_requisite(IBAN_DETAILS)
        self.assertEqual(label, "📋 Copy details")
        self.assertEqual(text, IBAN_DETAILS)
        _, long_text = extract_copyable_requisite(IBAN_DETAILS + " " + "x" * 400)
        self.assertEqual(len(long_text), 256)

    def test_grouped_iban_is_not_mistaken_for_a_card(self):
        label, _ = extract_copyable_requisite("IBAN: DE89 3704 0044 0532 0130 00")
        self.assertEqual(label, "📋 Copy details")
        lowercase_label, _ = extract_copyable_requisite("iban de89 3704 0044 0532 0130 00")
        self.assertEqual(lowercase_label, "📋 Copy details")

    def test_no_button_without_card_or_iban(self):
        self.assertIsNone(extract_copyable_requisite(""))
        self.assertIsNone(extract_copyable_requisite(PHONE_DETAILS))
        self.assertIsNone(extract_copyable_requisite("р/с 40817810099910004312"))

    def test_grouped_bank_account_is_not_truncated_into_a_card(self):
        self.assertIsNone(extract_copyable_requisite("р/с 4081 7810 0999 1000 4312"))
        self.assertIsNone(extract_copyable_requisite("Account 4081 7810 0999 1000 4312 Sberbank"))
        self.assertEqual(extract_copyable_requisite("Card: 2200123456789012.")[1], "2200123456789012")


class PaymentKeyboardTests(unittest.TestCase):
    def test_copy_and_link_rows_are_prepended(self):
        markup = build_payment_confirmation_keyboard(7, date(2026, 9, 17), CARD_DETAILS, "https://pay.example/x")
        rows = markup.model_dump(exclude_none=True)["inline_keyboard"]
        self.assertEqual(rows[0], [{"text": "📋 Copy card number ···5678", "copy_text": {"text": "2202203212345678"}}])
        self.assertEqual(rows[1], [{"text": "🔗 Open payment link", "url": "https://pay.example/x"}])
        self.assertEqual(rows[2][0]["callback_data"], "remind:7:2026-09-17")

    def test_non_web_links_and_plain_details_add_no_rows(self):
        markup = build_payment_confirmation_keyboard(7, "2026-09-17", PHONE_DETAILS, "javascript:alert(1)")
        self.assertEqual(len(markup.inline_keyboard), 1)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "remind:7:2026-09-17")

    def test_malformed_links_stay_out_of_url_buttons(self):
        for bad_link in ("https://", "https://pay.example/x y", "https://my bank.ru/pay"):
            with self.subTest(link=bad_link):
                self.assertFalse(is_openable_payment_link(bad_link))
                markup = build_payment_confirmation_keyboard(7, "2026-09-17", PHONE_DETAILS, bad_link)
                self.assertEqual(len(markup.inline_keyboard), 1)
                self.assertIsNone(markup.inline_keyboard[0][0].url)
        for good_link in ("https://ok.example/x", "tg://resolve?domain=x"):
            with self.subTest(link=good_link):
                self.assertTrue(is_openable_payment_link(good_link))
                markup = build_payment_confirmation_keyboard(7, "2026-09-17", PHONE_DETAILS, good_link)
                self.assertEqual(len(markup.inline_keyboard), 2)
                self.assertEqual(markup.inline_keyboard[0][0].url, good_link)

    def test_batch_test_and_topup_keyboards_share_the_rows(self):
        items = [_item(1, "A", date(2026, 9, 17), CARD_DETAILS), _item(2, "B", date(2026, 9, 18), CARD_DETAILS)]
        batch = build_batch_payment_confirmation_keyboard(items, payment_details=CARD_DETAILS, payment_link="tg://resolve?domain=x")
        self.assertIsNotNone(batch.inline_keyboard[0][0].copy_text)
        self.assertEqual(batch.inline_keyboard[1][0].url, "tg://resolve?domain=x")
        self.assertEqual(
            [row[0].callback_data for row in batch.inline_keyboard[2:]],
            ["remind:1:2026-09-17", "remind:2:2026-09-18"],
        )
        test_markup = build_test_payment_confirmation_keyboard(0, date(2026, 9, 17), IBAN_DETAILS)
        self.assertEqual(test_markup.inline_keyboard[0][0].copy_text.text, IBAN_DETAILS)
        self.assertEqual(test_markup.inline_keyboard[-1][0].callback_data, "testpaid:0:2026-09-17")
        topup = topup_request_submit_keyboard(5, CARD_DETAILS, "https://pay.example/t")
        self.assertEqual([row[0].callback_data for row in topup.inline_keyboard[2:]], ["topup:submit:5", "menu:close"])


class MarkPaidButtonTests(unittest.TestCase):
    def test_tapped_batch_button_is_greyed_and_only_callbacks_are_counted(self):
        items = [_item(1, "Netflix", date(2026, 9, 17), CARD_DETAILS), _item(2, "Spotify", date(2026, 9, 20), CARD_DETAILS)]
        markup = build_batch_payment_confirmation_keyboard(items, payment_details=CARD_DETAILS, payment_link="https://pay.example/a")
        updated, remaining = mark_paid_button(markup, "remind:2:2026-09-20")
        rows = updated.model_dump(exclude_none=True)["inline_keyboard"]
        self.assertEqual(rows[3], [{"text": "☑️ Paid · Spotify · 20.09.2026", "disabled": {}}])
        self.assertEqual(rows[0][0]["copy_text"], {"text": "2202203212345678"})
        self.assertEqual(rows[1][0]["url"], "https://pay.example/a")
        self.assertEqual(rows[2][0]["callback_data"], "remind:1:2026-09-17")
        self.assertEqual(remaining, 1)

    def test_single_reminder_keeps_copy_rows_and_reports_nothing_left(self):
        markup = build_payment_confirmation_keyboard(7, "2026-09-17", CARD_DETAILS, "https://pay.example/x")
        updated, remaining = mark_paid_button(markup, "remind:7:2026-09-17")
        rows = updated.model_dump(exclude_none=True)["inline_keyboard"]
        self.assertEqual(rows[-1], [{"text": "☑️ Paid", "disabled": {}}])
        self.assertEqual(len(rows), 3)
        self.assertEqual(remaining, 0)

    def test_unknown_or_missing_markup_is_left_alone(self):
        markup = build_payment_confirmation_keyboard(7, "2026-09-17")
        self.assertEqual(mark_paid_button(markup, "remind:9:2026-09-17"), (None, 1))
        self.assertEqual(mark_paid_button(None, "remind:7:2026-09-17"), (None, 0))


class PaymentDetailsRenderingTests(unittest.TestCase):
    def test_details_collapse_only_when_a_copy_button_exists(self):
        kwargs = dict(
            person_name="Anna",
            subscription_name="Netflix",
            status_text="Due today",
            due_date_text="17.09.2026",
            amount_text="10.00 RUB",
            converted_text=None,
            payment_label="Sber",
            payment_link="",
            comment="",
            footer="",
        )
        with_card = _build_reminder_message(payment_details="card <2202 2032 1234 5678>", copyable=True, **kwargs)
        self.assertIn("<blockquote expandable>card &lt;2202 2032 1234 5678&gt;</blockquote>", with_card)
        self.assertNotIn("<pre>", with_card)
        plain = _build_reminder_message(payment_details=PHONE_DETAILS, **kwargs)
        self.assertIn(f"<pre>{PHONE_DETAILS}</pre>", plain)
        self.assertNotIn("<blockquote", plain)
        # Auto-paid notices go out without a keyboard: card details stay monospace.
        no_button = _build_reminder_message(payment_details=CARD_DETAILS, copyable=False, **kwargs)
        self.assertIn(f"<pre>{CARD_DETAILS}</pre>", no_button)
        self.assertNotIn("<blockquote", no_button)
        # A mixed batch collapses only the details the batch button copies.
        other_card = "Alfa card 4276 3800 1234 5678"
        mixed = [
            _item(1, "Netflix", date(2026, 9, 17), PHONE_DETAILS),
            _item(2, "Spotify", date(2026, 9, 20), CARD_DETAILS),
            _item(3, "YouTube", date(2026, 9, 21), other_card),
        ]
        untouched = _build_batch_reminder_message(person_name="Anna", items=mixed, total_text=None, footer="")
        self.assertNotIn("<blockquote expandable>", untouched)
        self.assertIn(f"<pre>{CARD_DETAILS}</pre>", untouched)
        mixed[1]["copyable"] = True
        collapsed = _build_batch_reminder_message(person_name="Anna", items=mixed, total_text=None, footer="")
        self.assertEqual(collapsed.count("<blockquote expandable>"), 1)
        self.assertIn(f"<blockquote expandable>{CARD_DETAILS}</blockquote>", collapsed)
        self.assertIn(f"<pre>{PHONE_DETAILS}</pre>", collapsed)
        self.assertIn(f"<pre>{other_card}</pre>", collapsed)

    def test_batch_renders_identical_details_once(self):
        items = [
            _item(1, "Netflix", date(2026, 9, 17), CARD_DETAILS, "https://pay.example/a"),
            _item(2, "Spotify", date(2026, 9, 20), CARD_DETAILS + "  "),
            _item(3, "YouTube", date(2026, 9, 21), PHONE_DETAILS),
        ]
        for item in items[:2]:
            item["copyable"] = True
        text = _build_batch_reminder_message(person_name="Anna", items=items, total_text=None, footer="")
        self.assertEqual(text.count("<blockquote expandable>"), 1)
        self.assertEqual(text.count("🏦 Method: Sber <i>(details above)</i>"), 1)
        self.assertIn(f"<pre>{PHONE_DETAILS}</pre>", text)
        self.assertLess(text.index("<blockquote expandable>"), text.index("(details above)"))


if __name__ == "__main__":
    unittest.main()
