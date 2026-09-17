"""Offline startup checks: no credentials, production database, or Telegram calls."""

import importlib
from pathlib import Path
import tempfile
import unittest

from app.storage.db import Database


class ImportTests(unittest.TestCase):
    def test_entry_point_and_handlers_import(self):
        module = importlib.import_module("main")
        self.assertTrue(callable(module.main))
        self.assertTrue(module.admin_router.message.handlers)
        self.assertTrue(module.public_router.message.handlers)


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_database_can_reopen_and_preserve_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "data" / "test.db")
            await database.connect()
            try:
                await database.set_setting("target_currency", "EUR")
                await database.add_admin(123, "Test admin")
                self.assertTrue(await database.is_admin(123))
                self.assertFalse(await database.is_admin(456))
            finally:
                await database.close()
            await database.connect()
            try:
                self.assertEqual(await database.get_setting("target_currency"), "EUR")
                self.assertTrue(await database.is_admin(123))
            finally:
                await database.close()


if __name__ == "__main__":
    unittest.main()
