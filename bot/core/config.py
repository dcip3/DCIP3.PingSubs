from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Settings:
    bot_token: str
    database_path: Path
    reminder_check_interval: int
    target_currency: str
    currency_rounding: str

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()

        token = os.getenv("BOT_TOKEN")
        if not token:
            raise RuntimeError("BOT_TOKEN is missing in .env")

        db_path = Path(os.getenv("DATABASE_PATH", "data/bot.db"))

        interval_raw = os.getenv("REMINDER_CHECK_INTERVAL", "3600")
        try:
            reminder_interval = int(interval_raw)
            if reminder_interval <= 0:
                raise ValueError
        except ValueError as exc:  # pragma: no cover - config validation
            raise RuntimeError("REMINDER_CHECK_INTERVAL must be a positive integer") from exc

        target_currency = os.getenv("TARGET_CURRENCY", "RUB").strip().upper() or "RUB"

        rounding_mode = os.getenv("CURRENCY_ROUNDING", "precise").strip().lower()
        if rounding_mode not in {"precise", "floor", "round", "ceil"}:
            rounding_mode = "precise"

        return cls(
            bot_token=token,
            database_path=db_path,
            reminder_check_interval=reminder_interval,
            target_currency=target_currency,
            currency_rounding=rounding_mode,
        )
