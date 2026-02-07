from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS friends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL UNIQUE,
                full_name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admins (
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                next_charge_at TEXT NOT NULL,
                period_days INTEGER NOT NULL DEFAULT 30,
                share_limit INTEGER,
                comment TEXT NOT NULL DEFAULT '',
                reminder_time TEXT NOT NULL DEFAULT '16:00',
                reminder_offsets TEXT NOT NULL DEFAULT '[-1, 0]',
                remind_after_due INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS subscription_participants (
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                friend_id INTEGER NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
                PRIMARY KEY (subscription_id, friend_id)
            );
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_logs (
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                due_date TEXT NOT NULL,
                offset INTEGER NOT NULL,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (subscription_id, due_date, offset)
            );
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_user_logs (
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                due_date TEXT NOT NULL,
                offset INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (subscription_id, due_date, offset, telegram_id)
            );
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_suppressions (
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                due_date TEXT NOT NULL,
                telegram_id INTEGER NOT NULL,
                sent_on TEXT NOT NULL,
                PRIMARY KEY (subscription_id, due_date, telegram_id, sent_on)
            );
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_messages (
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                due_date TEXT NOT NULL,
                telegram_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                share_amount REAL NOT NULL,
                share_currency TEXT NOT NULL,
                converted_amount REAL,
                converted_currency TEXT,
                converted_display TEXT,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_id, message_id)
            );
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                due_date TEXT NOT NULL,
                paid_by_telegram_id INTEGER,
                paid_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscription_cycles (
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                due_date TEXT NOT NULL,
                closed_at TEXT,
                PRIMARY KEY (subscription_id, due_date)
            );
            """
        )
        await self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS payment_unique
            ON payment_logs (subscription_id, due_date, paid_by_telegram_id);
            """
        )
        await self._ensure_subscription_columns()
        await self._ensure_participant_columns()
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    async def _ensure_subscription_columns(self) -> None:
        await self._ensure_column(
            "subscriptions",
            "reminder_time",
            "TEXT NOT NULL DEFAULT '16:00'",
        )
        await self._ensure_column(
            "subscriptions",
            "reminder_offsets",
            "TEXT NOT NULL DEFAULT '[-1, 0]'",
        )
        await self._ensure_column(
            "subscriptions",
            "remind_after_due",
            "INTEGER NOT NULL DEFAULT 1",
        )
        await self._ensure_column(
            "subscriptions",
            "comment",
            "TEXT NOT NULL DEFAULT ''",
        )

    async def _ensure_participant_columns(self) -> None:
        await self._ensure_column(
            "subscription_participants",
            "share_weight",
            "INTEGER NOT NULL DEFAULT 1",
        )

    async def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in await cursor.fetchall()]
        if column in columns:
            return
        await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    async def upsert_friend(self, telegram_id: int, full_name: str) -> int:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            "SELECT id FROM friends WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if row:
            await self._conn.execute(
                "UPDATE friends SET full_name = ? WHERE telegram_id = ?",
                (full_name, telegram_id),
            )
            await self._conn.commit()
            return row["id"]

        cursor = await self._conn.execute(
            "INSERT INTO friends (telegram_id, full_name) VALUES (?, ?)",
            (telegram_id, full_name),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_friend_by_telegram(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            "SELECT * FROM friends WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_friend(self, friend_id: int) -> Optional[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            "SELECT * FROM friends WHERE id = ?",
            (friend_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_friends(self) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute("SELECT id, telegram_id, full_name FROM friends ORDER BY full_name")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_friend_name(self, friend_id: int, full_name: str) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            "UPDATE friends SET full_name = ? WHERE id = ?",
            (full_name, friend_id),
        )
        await self._conn.commit()

    async def delete_friend(self, friend_id: int) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute("DELETE FROM friends WHERE id = ?", (friend_id,))
        await self._conn.commit()

    async def create_subscription(
        self,
        name: str,
        amount: float,
        currency: str,
        due_date: date,
        period_days: int,
        share_limit: Optional[int],
    ) -> int:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            INSERT INTO subscriptions (
                name,
                amount,
                currency,
                next_charge_at,
                period_days,
                share_limit,
                reminder_time
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                amount,
                currency.upper(),
                due_date.isoformat(),
                period_days,
                share_limit,
                "",
            ),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def update_subscription_fields(self, subscription_id: int, **fields: Any) -> None:
        assert self._conn is not None, "Database is not connected"
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [
            value.isoformat() if isinstance(value, date) else value
            for value in fields.values()
        ]
        values.append(subscription_id)
        await self._conn.execute(
            f"UPDATE subscriptions SET {assignments} WHERE id = ?",
            values,
        )
        if "next_charge_at" in fields:
            await self._conn.execute("DELETE FROM reminder_logs WHERE subscription_id = ?", (subscription_id,))
            await self._conn.execute("DELETE FROM reminder_user_logs WHERE subscription_id = ?", (subscription_id,))
        await self._conn.commit()

    async def delete_subscription(self, subscription_id: int) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))
        await self._conn.commit()

    async def list_subscriptions(self) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT s.id, s.name, s.amount, s.currency, s.next_charge_at, s.period_days,
                   s.share_limit, COUNT(sp.friend_id) AS participant_count
            FROM subscriptions s
            LEFT JOIN subscription_participants sp ON sp.subscription_id = s.id
            GROUP BY s.id
            ORDER BY date(s.next_charge_at)
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_subscriptions_for_user(self, telegram_id: int) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT s.id, s.name, s.amount, s.currency, s.next_charge_at, s.period_days
            FROM subscriptions s
            JOIN subscription_participants sp ON sp.subscription_id = s.id
            JOIN friends f ON f.id = sp.friend_id
            WHERE f.telegram_id = ?
            ORDER BY date(s.next_charge_at)
            """,
            (telegram_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_subscription(self, subscription_id: int) -> Optional[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute("SELECT * FROM subscriptions WHERE id = ?", (subscription_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_subscription_participants(self, subscription_id: int) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT f.id, f.full_name, f.telegram_id, sp.share_weight
            FROM subscription_participants sp
            JOIN friends f ON f.id = sp.friend_id
            WHERE sp.subscription_id = ?
            ORDER BY f.full_name
            """,
            (subscription_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_friends_with_membership(self, subscription_id: int) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT f.id, f.full_name, f.telegram_id,
                   CASE WHEN sp.friend_id IS NULL THEN 0 ELSE 1 END AS is_member,
                   COALESCE(sp.share_weight, 1) AS share_weight
            FROM friends f
            LEFT JOIN subscription_participants sp
                ON sp.friend_id = f.id AND sp.subscription_id = ?
            ORDER BY f.full_name
            """,
            (subscription_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def set_participant(
        self,
        subscription_id: int,
        friend_id: int,
        enabled: bool,
        share_weight: int = 1,
    ) -> None:
        assert self._conn is not None, "Database is not connected"
        if enabled:
            await self._conn.execute(
                """
                INSERT OR REPLACE INTO subscription_participants (subscription_id, friend_id, share_weight)
                VALUES (?, ?, ?)
                """,
                (subscription_id, friend_id, share_weight),
            )
        else:
            await self._conn.execute(
                "DELETE FROM subscription_participants WHERE subscription_id = ? AND friend_id = ?",
                (subscription_id, friend_id),
            )
        await self._conn.commit()

    async def update_participant_weight(
        self,
        subscription_id: int,
        friend_id: int,
        share_weight: int,
    ) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            """
            UPDATE subscription_participants
            SET share_weight = ?
            WHERE subscription_id = ? AND friend_id = ?
            """,
            (share_weight, subscription_id, friend_id),
        )
        await self._conn.commit()

    async def fetch_subscriptions_for_reminders(self) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT id, name, amount, currency, next_charge_at, period_days, share_limit,
                   reminder_time, reminder_offsets, remind_after_due, comment
            FROM subscriptions
            """,
        )
        subscriptions = [dict(row) for row in await cursor.fetchall()]
        if not subscriptions:
            return []

        ids = [sub["id"] for sub in subscriptions]
        placeholders = ",".join(["?"] * len(ids))
        participants_cursor = await self._conn.execute(
            f"""
            SELECT sp.subscription_id, f.full_name, f.telegram_id, sp.share_weight
            FROM subscription_participants sp
            JOIN friends f ON f.id = sp.friend_id
            WHERE sp.subscription_id IN ({placeholders})
            ORDER BY f.full_name
            """,
            ids,
        )
        per_subscription: Dict[int, List[Dict[str, Any]]] = {sub_id: [] for sub_id in ids}
        for row in await participants_cursor.fetchall():
            per_subscription[row["subscription_id"]].append(
                {
                    "full_name": row["full_name"],
                    "telegram_id": row["telegram_id"],
                    "share_weight": row["share_weight"],
                }
            )

        for sub in subscriptions:
            sub["participants"] = per_subscription.get(sub["id"], [])

        return subscriptions

    async def postpone_subscription(self, subscription_id: int, next_charge: date) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            "UPDATE subscriptions SET next_charge_at = ? WHERE id = ?",
            (next_charge.isoformat(), subscription_id),
        )
        await self._conn.execute("DELETE FROM reminder_logs WHERE subscription_id = ?", (subscription_id,))
        await self._conn.execute("DELETE FROM reminder_user_logs WHERE subscription_id = ?", (subscription_id,))
        await self._conn.commit()

    async def has_admins(self) -> bool:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute("SELECT 1 FROM admins LIMIT 1")
        return await cursor.fetchone() is not None

    async def ensure_first_admin(self, telegram_id: int, full_name: str) -> bool:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            INSERT INTO admins (telegram_id, full_name)
            SELECT ?, ?
            WHERE NOT EXISTS (SELECT 1 FROM admins)
            """,
            (telegram_id, full_name),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def add_admin(self, telegram_id: int, full_name: str) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            "INSERT OR REPLACE INTO admins (telegram_id, full_name) VALUES (?, ?)",
            (telegram_id, full_name),
        )
        await self._conn.commit()

    async def list_admin_ids(self) -> List[int]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute("SELECT telegram_id FROM admins")
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def is_admin(self, telegram_id: int) -> bool:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            "SELECT 1 FROM admins WHERE telegram_id = ?",
            (telegram_id,),
        )
        return await cursor.fetchone() is not None

    async def register_reminder_if_new(self, subscription_id: int, due_date: date, offset: int) -> bool:
        assert self._conn is not None, "Database is not connected"
        try:
            await self._conn.execute(
                "INSERT INTO reminder_logs (subscription_id, due_date, offset) VALUES (?, ?, ?)",
                (subscription_id, due_date.isoformat(), offset),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def register_user_reminder_if_new(
        self,
        subscription_id: int,
        due_date: date,
        offset: int,
        telegram_id: int,
    ) -> bool:
        assert self._conn is not None, "Database is not connected"
        try:
            await self._conn.execute(
                """
                INSERT INTO reminder_user_logs (subscription_id, due_date, offset, telegram_id)
                VALUES (?, ?, ?, ?)
                """,
                (subscription_id, due_date.isoformat(), offset, telegram_id),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def register_reminder_suppression(
        self,
        subscription_id: int,
        due_date: date,
        telegram_id: int,
        sent_on: date,
    ) -> None:
        assert self._conn is not None, "Database is not connected"
        try:
            await self._conn.execute(
                """
                INSERT INTO reminder_suppressions (subscription_id, due_date, telegram_id, sent_on)
                VALUES (?, ?, ?, ?)
                """,
                (subscription_id, due_date.isoformat(), telegram_id, sent_on.isoformat()),
            )
            await self._conn.commit()
        except aiosqlite.IntegrityError:
            pass

    async def list_reminder_suppressed_users(
        self,
        subscription_id: int,
        due_date: date,
        sent_on: date,
    ) -> List[int]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT telegram_id
            FROM reminder_suppressions
            WHERE subscription_id = ? AND due_date = ? AND sent_on = ?
            """,
            (subscription_id, due_date.isoformat(), sent_on.isoformat()),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def record_reminder_message(
        self,
        subscription_id: int,
        due_date: date | str,
        telegram_id: int,
        message_id: int,
        share_amount: float,
        share_currency: str,
        converted_amount: Optional[float],
        converted_currency: Optional[str],
        converted_display: Optional[str],
    ) -> None:
        assert self._conn is not None, "Database is not connected"
        due_value = due_date.isoformat() if isinstance(due_date, date) else str(due_date)
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO reminder_messages (
                subscription_id,
                due_date,
                telegram_id,
                message_id,
                share_amount,
                share_currency,
                converted_amount,
                converted_currency,
                converted_display
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subscription_id,
                due_value,
                telegram_id,
                message_id,
                share_amount,
                share_currency,
                converted_amount,
                converted_currency,
                converted_display,
            ),
        )
        await self._conn.commit()

    async def get_reminder_message(
        self,
        telegram_id: int,
        message_id: int,
    ) -> Optional[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM reminder_messages
            WHERE telegram_id = ? AND message_id = ?
            """,
            (telegram_id, message_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def clear_reminder_logs(self, subscription_id: int) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute("DELETE FROM reminder_logs WHERE subscription_id = ?", (subscription_id,))
        await self._conn.execute("DELETE FROM reminder_user_logs WHERE subscription_id = ?", (subscription_id,))
        await self._conn.commit()

    async def get_setting(self, key: str) -> Optional[str]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return str(row[0]) if row else None

    @staticmethod
    def _user_setting_key(telegram_id: int, key: str) -> str:
        return f"user:{telegram_id}:{key}"

    @staticmethod
    def _user_subscription_setting_key(telegram_id: int, subscription_id: int, key: str) -> str:
        return f"user:{telegram_id}:subscription:{subscription_id}:{key}"

    async def get_user_setting(self, telegram_id: int, key: str) -> Optional[str]:
        return await self.get_setting(self._user_setting_key(telegram_id, key))

    async def set_user_setting(self, telegram_id: int, key: str, value: str) -> None:
        await self.set_setting(self._user_setting_key(telegram_id, key), value)

    async def delete_user_setting(self, telegram_id: int, key: str) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            "DELETE FROM settings WHERE key = ?",
            (self._user_setting_key(telegram_id, key),),
        )
        await self._conn.commit()

    async def get_user_subscription_setting(
        self,
        telegram_id: int,
        subscription_id: int,
        key: str,
    ) -> Optional[str]:
        return await self.get_setting(
            self._user_subscription_setting_key(telegram_id, subscription_id, key)
        )

    async def set_user_subscription_setting(
        self,
        telegram_id: int,
        subscription_id: int,
        key: str,
        value: str,
    ) -> None:
        await self.set_setting(
            self._user_subscription_setting_key(telegram_id, subscription_id, key),
            value,
        )

    async def delete_user_subscription_setting(
        self,
        telegram_id: int,
        subscription_id: int,
        key: str,
    ) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            "DELETE FROM settings WHERE key = ?",
            (self._user_subscription_setting_key(telegram_id, subscription_id, key),),
        )
        await self._conn.commit()

    async def get_effective_target_currency(
        self,
        telegram_id: int,
        default_currency: str = "RUB",
    ) -> str:
        user_value = await self.get_user_setting(telegram_id, "target_currency")
        if user_value:
            return user_value.strip().upper()
        global_value = await self.get_setting("target_currency")
        if global_value:
            return global_value.strip().upper()
        return default_currency.strip().upper() or "RUB"

    async def get_effective_base_reminder_time(
        self,
        default_time: str = "16:00",
    ) -> str:
        raw_value = await self.get_setting("base_reminder_time")
        if raw_value:
            return raw_value.strip()
        return default_time.strip() or "16:00"

    async def get_effective_user_base_reminder_time(
        self,
        telegram_id: int,
        default_time: str = "16:00",
    ) -> str:
        user_value = await self.get_user_setting(telegram_id, "base_reminder_time")
        if user_value:
            return user_value.strip()
        return await self.get_effective_base_reminder_time(default_time)

    async def set_setting(self, key: str, value: str) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self._conn.commit()

    async def get_setting_bool(self, key: str, default: bool = True) -> bool:
        raw = await self.get_setting(key)
        if raw is None:
            return default
        return raw.strip().lower() not in {"0", "false", "no", "off"}

    async def log_payment(
        self,
        subscription_id: int,
        due_date: date | str,
        paid_by_telegram_id: Optional[int],
    ) -> None:
        assert self._conn is not None, "Database is not connected"
        due_value = due_date.isoformat() if isinstance(due_date, date) else str(due_date)
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO payment_logs (subscription_id, due_date, paid_by_telegram_id)
            VALUES (?, ?, ?)
            """,
            (subscription_id, due_value, paid_by_telegram_id),
        )
        await self._conn.commit()

    async def list_payment_summary_by_month(self) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT substr(paid_at, 1, 7) AS month,
                   COUNT(DISTINCT paid_by_telegram_id) AS people_count
            FROM payment_logs
            GROUP BY month
            ORDER BY month DESC
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_payment_activity(self, start_date: str) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT subscription_id,
                   paid_by_telegram_id,
                   substr(paid_at, 1, 7) AS month
            FROM payment_logs
            WHERE paid_at >= ? AND paid_by_telegram_id IS NOT NULL
            GROUP BY subscription_id, paid_by_telegram_id, month
            """,
            (start_date,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_payment_activity_by_due_month(
        self,
        subscription_id: int,
        year: int,
    ) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        start_date = f"{year:04d}-01-01"
        end_date = f"{year:04d}-12-31"
        cursor = await self._conn.execute(
            """
            SELECT paid_by_telegram_id,
                   substr(due_date, 1, 7) AS month
            FROM payment_logs
            WHERE subscription_id = ?
              AND due_date >= ?
              AND due_date <= ?
              AND paid_by_telegram_id IS NOT NULL
            GROUP BY paid_by_telegram_id, month
            """,
            (subscription_id, start_date, end_date),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def ensure_cycle(self, subscription_id: int, due_date: date | str) -> None:
        assert self._conn is not None, "Database is not connected"
        due_value = due_date.isoformat() if isinstance(due_date, date) else str(due_date)
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO subscription_cycles (subscription_id, due_date)
            VALUES (?, ?)
            """,
            (subscription_id, due_value),
        )
        await self._conn.commit()

    async def close_cycle(self, subscription_id: int, due_date: date | str) -> None:
        assert self._conn is not None, "Database is not connected"
        due_value = due_date.isoformat() if isinstance(due_date, date) else str(due_date)
        await self._conn.execute(
            """
            UPDATE subscription_cycles
            SET closed_at = CURRENT_TIMESTAMP
            WHERE subscription_id = ? AND due_date = ?
            """,
            (subscription_id, due_value),
        )
        await self._conn.commit()

    async def list_open_cycles(self, subscription_id: int) -> List[str]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT due_date
            FROM subscription_cycles
            WHERE subscription_id = ? AND closed_at IS NULL
            ORDER BY due_date
            """,
            (subscription_id,),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def list_payments_for_cycles(
        self,
        subscription_id: int,
        due_dates: List[str],
    ) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        if not due_dates:
            return []
        placeholders = ",".join(["?"] * len(due_dates))
        cursor = await self._conn.execute(
            f"""
            SELECT due_date, paid_by_telegram_id
            FROM payment_logs
            WHERE subscription_id = ? AND due_date IN ({placeholders})
            """,
            [subscription_id, *due_dates],
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
