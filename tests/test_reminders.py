"""Exercise current reminder flows with a temporary database and a mocked bot."""

from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from app.services import CurrencyConverter, _run_reminder_pass
from app.storage.db import Database


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.directory.name) / "test.db")
        await self.db.connect()
        self.converter = CurrencyConverter("RUB")
        self.converter.convert_to = AsyncMock(side_effect=AssertionError("Unexpected conversion"))
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)))
        self.friend_id = await self.db.create_friend_invite("Test <User>", "test-invite", "2030-01-01")
        claimed = await self.db.claim_friend_invite(self.friend_id, 123, "2026-09-17")
        self.assertTrue(claimed)

    async def asyncTearDown(self):
        await self.converter.close()
        await self.db.close()
        self.directory.cleanup()

    async def add_subscription(self, name, mode="split"):
        subscription_id = await self.db.create_subscription(
            name=name,
            amount=100,
            currency="RUB",
            due_date=date(2026, 9, 17),
            period_days=30,
            share_limit=4,
            payment_mode=mode,
        )
        await self.db.set_participant(subscription_id, self.friend_id, True, share_weight=2)
        if mode == "fixed":
            await self.db.update_participant_fixed_amount(subscription_id, self.friend_id, 15)
        return subscription_id

    async def send_reminders(self):
        return await _run_reminder_pass(
            self.bot,
            self.db,
            self.converter,
            subscription_id=None,
            now=datetime(2026, 9, 17, 16, tzinfo=timezone.utc),
            rounding_mode="precise",
            enforce_time=False,
            register_reminders=False,
        )

    async def test_single_reminder_preserves_weighted_amount_and_escapes_names(self):
        subscription_id = await self.add_subscription("Plan <A>")
        self.assertEqual(await self.send_reminders(), 1)
        self.bot.send_message.assert_awaited_once()
        call = self.bot.send_message.await_args
        self.assertEqual(call.args[0], 123)
        self.assertIn("Test &lt;User&gt;", call.args[1])
        self.assertIn("Plan &lt;A&gt;", call.args[1])
        self.assertIn("50.00 RUB", call.args[1])
        callbacks = [button.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn(f"remind:{subscription_id}:2026-09-17", callbacks)

    async def test_batch_reminder_combines_split_and_fixed_amounts(self):
        split_id = await self.add_subscription("Split plan")
        fixed_id = await self.add_subscription("Fixed plan", mode="fixed")
        self.assertEqual(await self.send_reminders(), 1)
        self.bot.send_message.assert_awaited_once()
        call = self.bot.send_message.await_args
        self.assertIn("Split plan", call.args[1])
        self.assertIn("Fixed plan", call.args[1])
        self.assertIn("50.00 RUB", call.args[1])
        self.assertIn("30.00 RUB", call.args[1])
        self.assertIn("80.00 RUB", call.args[1])
        callbacks = [button.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn(f"remind:{split_id}:2026-09-17", callbacks)
        self.assertIn(f"remind:{fixed_id}:2026-09-17", callbacks)


if __name__ == "__main__":
    unittest.main()
