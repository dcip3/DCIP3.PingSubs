from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.handlers import admin_router, public_router
from app.infrastructure import AdminFilter, SettingsMiddleware
from app.core.config import Settings
from app.storage.db import Database
from app.services import CurrencyConverter, reminder_worker


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = Settings.load()
    db = Database(settings.database_path)
    await db.connect()
    for admin_id in settings.admin_ids:
        await db.add_admin(admin_id, f"Admin {admin_id}")
    stored_currency = await db.get_setting("target_currency")
    if stored_currency:
        settings.target_currency = stored_currency.upper()
    else:
        await db.set_setting("target_currency", settings.target_currency)
    stored_rounding = await db.get_setting("currency_rounding")
    if stored_rounding:
        settings.currency_rounding = stored_rounding
    else:
        await db.set_setting("currency_rounding", settings.currency_rounding)
    converter = CurrencyConverter(settings.target_currency)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    shared_middleware = SettingsMiddleware(settings, db, converter)
    public_router.message.middleware(shared_middleware)
    public_router.callback_query.middleware(shared_middleware)
    admin_router.message.middleware(shared_middleware)
    admin_router.callback_query.middleware(shared_middleware)

    admin_filter = AdminFilter(db)
    admin_router.message.filter(admin_filter)
    admin_router.callback_query.filter(admin_filter)

    dp.include_router(public_router)
    dp.include_router(admin_router)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Launch the bot"),
            BotCommand(command="help", description="Explain what the bot can do"),
        ]
    )

    worker_task = asyncio.create_task(
        reminder_worker(
            bot,
            db,
            converter,
            interval_seconds=settings.reminder_check_interval,
            rounding_mode=settings.currency_rounding,
        )
    )

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await converter.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
