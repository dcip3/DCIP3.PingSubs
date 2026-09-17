from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv
from app.core.reminders import (
    DEFAULT_REMINDER_TIME,
    DEFAULT_REMINDER_TIMEZONE,
    normalize_time_string,
    normalize_timezone_name,
)


@dataclass
class Settings:
    bot_token: str
    database_path: Path
    reminder_check_interval: int
    target_currency: str
    currency_rounding: str
    base_reminder_time: str
    base_timezone: str
    admin_ids: Tuple[int, ...]

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()

        token = os.getenv("BOT_TOKEN")
        if not token:
            raise RuntimeError("BOT_TOKEN is missing in .env")

        db_path = Path(os.getenv("DATABASE_PATH", "data/app.db"))

        interval_raw = os.getenv("REMINDER_CHECK_INTERVAL", "60")
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

        base_reminder_time = normalize_time_string(os.getenv("BASE_REMINDER_TIME"))
        if base_reminder_time is None:
            base_reminder_time = DEFAULT_REMINDER_TIME

        base_timezone = normalize_timezone_name(os.getenv("BASE_TIMEZONE"), DEFAULT_REMINDER_TIMEZONE)
        if base_timezone is None:
            base_timezone = DEFAULT_REMINDER_TIMEZONE

        admin_ids_raw = os.getenv("ADMIN_IDS", "").strip()
        admin_ids: Tuple[int, ...] = ()
        if admin_ids_raw:
            values = []
            for part in admin_ids_raw.replace(";", ",").split(","):
                piece = part.strip()
                if not piece:
                    continue
                try:
                    values.append(int(piece))
                except ValueError as exc:
                    raise RuntimeError("ADMIN_IDS must be comma-separated integers") from exc
            admin_ids = tuple(dict.fromkeys(values))

        return cls(
            bot_token=token,
            database_path=db_path,
            reminder_check_interval=reminder_interval,
            target_currency=target_currency,
            currency_rounding=rounding_mode,
            base_reminder_time=base_reminder_time,
            base_timezone=base_timezone,
            admin_ids=admin_ids,
        )
