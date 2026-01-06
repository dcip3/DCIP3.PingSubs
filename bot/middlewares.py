from __future__ import annotations

from aiogram import BaseMiddleware
from aiogram.filters import Filter
from aiogram.types import Message

from bot.core.config import Settings
from bot.storage.db import Database
from bot.services import CurrencyConverter


class SettingsMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings, db: Database, converter: CurrencyConverter) -> None:
        super().__init__()
        self.settings = settings
        self.db = db
        self.converter = converter

    async def __call__(self, handler, event, data):  # type: ignore[override]
        data["settings"] = self.settings
        data["db"] = self.db
        data["converter"] = self.converter
        return await handler(event, data)


class AdminFilter(Filter):
    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(self, message: Message) -> bool:  # type: ignore[override]
        return bool(message.from_user and await self.db.is_admin(message.from_user.id))
