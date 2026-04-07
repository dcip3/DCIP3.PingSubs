from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from app.core.constants import MONTHLY_PERIOD_SENTINEL, PAYMENT_MODES, PAYMENT_MODE_SPLIT


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
                telegram_id INTEGER UNIQUE,
                full_name TEXT NOT NULL,
                balance REAL NOT NULL DEFAULT 0,
                balance_currency TEXT NOT NULL DEFAULT '',
                invite_token TEXT,
                invite_expires_at TEXT,
                claimed_at TEXT
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
                base_currency TEXT NOT NULL DEFAULT '',
                payment_destination_id INTEGER,
                payment_mode TEXT NOT NULL DEFAULT 'split',
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
                fixed_amount REAL,
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
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS payment_destinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                currency TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                payment_link TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS topup_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                friend_id INTEGER NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
                destination_id INTEGER REFERENCES payment_destinations(id) ON DELETE SET NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                destination_title TEXT NOT NULL,
                destination_details TEXT NOT NULL DEFAULT '',
                destination_link TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                submitted_at TEXT,
                decided_at TEXT,
                approved_by_telegram_id INTEGER,
                rejected_by_telegram_id INTEGER
            );
            """
        )
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscription_cycles (
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                due_date TEXT NOT NULL,
                closed_at TEXT,
                participants_snapshot_ready INTEGER NOT NULL DEFAULT 0,
                settings_snapshot_ready INTEGER NOT NULL DEFAULT 0,
                amount_snapshot REAL,
                currency_snapshot TEXT,
                base_currency_snapshot TEXT,
                payment_destination_id_snapshot INTEGER,
                payment_destination_title_snapshot TEXT,
                payment_destination_details_snapshot TEXT,
                payment_destination_link_snapshot TEXT,
                payment_mode_snapshot TEXT,
                share_limit_snapshot INTEGER,
                comment_snapshot TEXT,
                reminder_time_snapshot TEXT,
                reminder_offsets_snapshot TEXT,
                remind_after_due_snapshot INTEGER,
                PRIMARY KEY (subscription_id, due_date)
            );

            CREATE TABLE IF NOT EXISTS subscription_cycle_participants (
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                due_date TEXT NOT NULL,
                telegram_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                share_weight INTEGER NOT NULL DEFAULT 1,
                fixed_amount REAL,
                PRIMARY KEY (subscription_id, due_date, telegram_id)
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
        await self._ensure_cycle_columns()
        await self._ensure_participant_columns()
        await self._ensure_friend_columns()
        await self._backfill_subscription_base_currencies()
        await self._backfill_subscription_payment_modes()
        await self._backfill_monthly_anchor_days()
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
        await self._ensure_column(
            "subscriptions",
            "monthly_anchor_day",
            "INTEGER",
        )
        await self._ensure_column(
            "subscriptions",
            "base_currency",
            "TEXT NOT NULL DEFAULT ''",
        )
        await self._ensure_column(
            "subscriptions",
            "payment_mode",
            "TEXT NOT NULL DEFAULT 'split'",
        )
        await self._ensure_column(
            "subscriptions",
            "payment_destination_id",
            "INTEGER",
        )

    async def _ensure_participant_columns(self) -> None:
        await self._ensure_column(
            "subscription_participants",
            "share_weight",
            "INTEGER NOT NULL DEFAULT 1",
        )
        await self._ensure_column(
            "subscription_participants",
            "fixed_amount",
            "REAL",
        )
        await self._ensure_column(
            "subscription_cycle_participants",
            "fixed_amount",
            "REAL",
        )

    async def _ensure_friend_columns(self) -> None:
        await self._ensure_friend_schema()
        await self._ensure_column(
            "friends",
            "balance",
            "REAL NOT NULL DEFAULT 0",
        )
        await self._ensure_column(
            "friends",
            "balance_currency",
            "TEXT NOT NULL DEFAULT ''",
        )
        await self._ensure_column(
            "friends",
            "invite_token",
            "TEXT",
        )
        await self._ensure_column(
            "friends",
            "invite_expires_at",
            "TEXT",
        )
        await self._ensure_column(
            "friends",
            "claimed_at",
            "TEXT",
        )
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            """
            UPDATE friends
            SET balance = COALESCE(balance, 0)
            """
        )
        await self._conn.execute(
            """
            UPDATE friends
            SET balance_currency = UPPER(TRIM(balance_currency))
            WHERE balance_currency IS NOT NULL
            """
        )
        await self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS friends_invite_token_unique
            ON friends (invite_token)
            WHERE invite_token IS NOT NULL
            """
        )

    async def _ensure_friend_schema(self) -> None:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute("PRAGMA table_info(friends)")
        columns = await cursor.fetchall()
        telegram_id_column = next((row for row in columns if row[1] == "telegram_id"), None)
        if telegram_id_column is None or int(telegram_id_column[3] or 0) == 0:
            return

        await self._conn.commit()
        await self._conn.execute("PRAGMA foreign_keys = OFF;")
        try:
            await self._conn.execute("BEGIN")
            await self._conn.execute(
                """
                CREATE TABLE friends_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE,
                    full_name TEXT NOT NULL,
                    balance REAL NOT NULL DEFAULT 0,
                    balance_currency TEXT NOT NULL DEFAULT '',
                    invite_token TEXT,
                    invite_expires_at TEXT,
                    claimed_at TEXT
                )
                """
            )
            await self._conn.execute(
                """
                INSERT INTO friends_new (
                    id,
                    telegram_id,
                    full_name,
                    balance,
                    balance_currency
                )
                SELECT
                    id,
                    telegram_id,
                    full_name,
                    COALESCE(balance, 0),
                    COALESCE(balance_currency, '')
                FROM friends
                """
            )
            await self._conn.execute("DROP TABLE friends")
            await self._conn.execute("ALTER TABLE friends_new RENAME TO friends")
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        finally:
            await self._conn.execute("PRAGMA foreign_keys = ON;")

    async def _ensure_cycle_columns(self) -> None:
        await self._ensure_column(
            "subscription_cycles",
            "participants_snapshot_ready",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await self._ensure_column(
            "subscription_cycles",
            "settings_snapshot_ready",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await self._ensure_column(
            "subscription_cycles",
            "amount_snapshot",
            "REAL",
        )
        await self._ensure_column(
            "subscription_cycles",
            "currency_snapshot",
            "TEXT",
        )
        await self._ensure_column(
            "subscription_cycles",
            "base_currency_snapshot",
            "TEXT",
        )
        await self._ensure_column(
            "subscription_cycles",
            "payment_destination_id_snapshot",
            "INTEGER",
        )
        await self._ensure_column(
            "subscription_cycles",
            "payment_destination_title_snapshot",
            "TEXT",
        )
        await self._ensure_column(
            "subscription_cycles",
            "payment_destination_details_snapshot",
            "TEXT",
        )
        await self._ensure_column(
            "subscription_cycles",
            "payment_destination_link_snapshot",
            "TEXT",
        )
        await self._ensure_column(
            "subscription_cycles",
            "payment_mode_snapshot",
            "TEXT",
        )
        await self._ensure_column(
            "subscription_cycles",
            "share_limit_snapshot",
            "INTEGER",
        )
        await self._ensure_column(
            "subscription_cycles",
            "comment_snapshot",
            "TEXT",
        )
        await self._ensure_column(
            "subscription_cycles",
            "reminder_time_snapshot",
            "TEXT",
        )
        await self._ensure_column(
            "subscription_cycles",
            "reminder_offsets_snapshot",
            "TEXT",
        )
        await self._ensure_column(
            "subscription_cycles",
            "remind_after_due_snapshot",
            "INTEGER",
        )

    async def _backfill_monthly_anchor_days(self) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            """
            UPDATE subscriptions
            SET monthly_anchor_day = COALESCE(
                (
                    SELECT MAX(CAST(strftime('%d', due_date) AS INTEGER))
                    FROM subscription_cycles
                    WHERE subscription_id = subscriptions.id
                ),
                CAST(strftime('%d', next_charge_at) AS INTEGER)
            )
            WHERE period_days = ?
              AND (monthly_anchor_day IS NULL OR monthly_anchor_day < 1 OR monthly_anchor_day > 31)
            """,
            (MONTHLY_PERIOD_SENTINEL,),
        )

    async def _backfill_subscription_base_currencies(self) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            """
            UPDATE subscriptions
            SET base_currency = UPPER(TRIM(base_currency))
            WHERE base_currency IS NOT NULL
            """
        )
        global_currency = await self.get_setting("target_currency")
        normalized_global = (global_currency or "").strip().upper()
        if len(normalized_global) == 3 and normalized_global.isalpha():
            await self._conn.execute(
                """
                UPDATE subscriptions
                SET base_currency = ?
                WHERE COALESCE(NULLIF(TRIM(base_currency), ''), '') = ''
                """,
                (normalized_global,),
            )
            return
        await self._conn.execute(
            """
            UPDATE subscriptions
            SET base_currency = UPPER(currency)
            WHERE COALESCE(NULLIF(TRIM(base_currency), ''), '') = ''
            """
        )

    async def _backfill_subscription_payment_modes(self) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            """
            UPDATE subscriptions
            SET payment_mode = LOWER(TRIM(payment_mode))
            WHERE payment_mode IS NOT NULL
            """
        )
        placeholders = ",".join(["?"] * len(PAYMENT_MODES))
        await self._conn.execute(
            f"""
            UPDATE subscriptions
            SET payment_mode = ?
            WHERE payment_mode IS NULL
               OR COALESCE(NULLIF(TRIM(payment_mode), ''), '') = ''
               OR payment_mode NOT IN ({placeholders})
            """,
            (PAYMENT_MODE_SPLIT, *PAYMENT_MODES),
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

    async def create_friend_invite(
        self,
        full_name: str,
        invite_token: str,
        invite_expires_at: str,
    ) -> int:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            INSERT INTO friends (telegram_id, full_name, invite_token, invite_expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (None, full_name, invite_token, invite_expires_at),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def refresh_friend_invite(
        self,
        friend_id: int,
        invite_token: str,
        invite_expires_at: str,
    ) -> bool:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            UPDATE friends
            SET invite_token = ?,
                invite_expires_at = ?
            WHERE id = ? AND telegram_id IS NULL
            """,
            (invite_token, invite_expires_at, friend_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def get_friend_by_invite_token(self, invite_token: str) -> Optional[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            "SELECT * FROM friends WHERE invite_token = ?",
            (invite_token,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def claim_friend_invite(
        self,
        friend_id: int,
        telegram_id: int,
        claimed_at: str,
    ) -> bool:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            UPDATE friends
            SET telegram_id = ?,
                claimed_at = ?,
                invite_token = NULL,
                invite_expires_at = NULL
            WHERE id = ? AND telegram_id IS NULL
            """,
            (telegram_id, claimed_at, friend_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def get_friend_by_telegram(self, telegram_id: Optional[int]) -> Optional[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        if telegram_id is None:
            return None
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

    async def get_friend_balance(self, friend_id: int) -> float:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            "SELECT balance FROM friends WHERE id = ?",
            (friend_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return 0.0
        try:
            return float(row["balance"] or 0.0)
        except (TypeError, ValueError):
            return 0.0

    async def get_friend_balance_currency(
        self,
        friend_id: int,
        default_currency: str = "RUB",
    ) -> str:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            "SELECT balance_currency FROM friends WHERE id = ?",
            (friend_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return default_currency.strip().upper() or "RUB"
        value = str(row["balance_currency"] or "").strip().upper()
        if len(value) == 3 and value.isalpha():
            return value
        return default_currency.strip().upper() or "RUB"

    async def get_friend_balance_by_telegram(self, telegram_id: int) -> float:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            "SELECT balance FROM friends WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return 0.0
        try:
            return float(row["balance"] or 0.0)
        except (TypeError, ValueError):
            return 0.0

    async def get_friend_balance_currency_by_telegram(
        self,
        telegram_id: int,
        default_currency: str = "RUB",
    ) -> str:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            "SELECT balance_currency FROM friends WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return default_currency.strip().upper() or "RUB"
        value = str(row["balance_currency"] or "").strip().upper()
        if len(value) == 3 and value.isalpha():
            return value
        return default_currency.strip().upper() or "RUB"

    async def get_friend_balance_snapshot_by_telegram(
        self,
        telegram_id: int,
        default_currency: str = "RUB",
    ) -> tuple[float, str]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            "SELECT balance, balance_currency FROM friends WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return 0.0, default_currency.strip().upper() or "RUB"
        try:
            balance_value = float(row["balance"] or 0.0)
        except (TypeError, ValueError):
            balance_value = 0.0
        currency_value = str(row["balance_currency"] or "").strip().upper()
        if len(currency_value) != 3 or not currency_value.isalpha():
            currency_value = default_currency.strip().upper() or "RUB"
        return max(balance_value, 0.0), currency_value

    async def add_friend_balance(self, friend_id: int, amount: float) -> Optional[float]:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            """
            UPDATE friends
            SET balance = COALESCE(balance, 0) + ?
            WHERE id = ?
            """,
            (amount, friend_id),
        )
        await self._conn.commit()
        friend = await self.get_friend(friend_id)
        if not friend:
            return None
        try:
            return float(friend.get("balance") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    async def set_friend_balance(self, friend_id: int, amount: float) -> Optional[float]:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            """
            UPDATE friends
            SET balance = ?
            WHERE id = ?
            """,
            (amount, friend_id),
        )
        await self._conn.commit()
        friend = await self.get_friend(friend_id)
        if not friend:
            return None
        try:
            return float(friend.get("balance") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    async def set_friend_balance_currency(
        self,
        friend_id: int,
        currency: str,
    ) -> Optional[str]:
        assert self._conn is not None, "Database is not connected"
        normalized = str(currency or "").strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            return None
        await self._conn.execute(
            """
            UPDATE friends
            SET balance_currency = ?
            WHERE id = ?
            """,
            (normalized, friend_id),
        )
        await self._conn.commit()
        friend = await self.get_friend(friend_id)
        if not friend:
            return None
        value = str(friend.get("balance_currency") or "").strip().upper()
        if len(value) == 3 and value.isalpha():
            return value
        return normalized

    async def consume_friend_balance_by_telegram(
        self,
        telegram_id: int,
        amount: float,
    ) -> tuple[float, float]:
        assert self._conn is not None, "Database is not connected"
        target_amount = max(float(amount), 0.0)
        if target_amount <= 0:
            return 0.0, await self.get_friend_balance_by_telegram(telegram_id)

        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self._conn.execute(
                "SELECT balance FROM friends WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cursor.fetchone()
            if not row:
                await self._conn.rollback()
                return 0.0, 0.0
            current_balance = max(float(row["balance"] or 0.0), 0.0)
            consumed = min(current_balance, target_amount)
            next_balance = current_balance - consumed
            if consumed > 0:
                await self._conn.execute(
                    """
                    UPDATE friends
                    SET balance = ?
                    WHERE telegram_id = ?
                    """,
                    (next_balance, telegram_id),
                )
            await self._conn.commit()
            return consumed, next_balance
        except Exception:
            await self._conn.rollback()
            raise

    async def list_friends(self) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT
                id,
                telegram_id,
                full_name,
                balance,
                balance_currency,
                invite_token,
                invite_expires_at,
                claimed_at
            FROM friends
            ORDER BY full_name
            """
        )
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
        payment_mode: str = PAYMENT_MODE_SPLIT,
    ) -> int:
        assert self._conn is not None, "Database is not connected"
        normalized_mode = str(payment_mode or PAYMENT_MODE_SPLIT).strip().lower()
        if normalized_mode not in PAYMENT_MODES:
            normalized_mode = PAYMENT_MODE_SPLIT
        cursor = await self._conn.execute(
            """
            INSERT INTO subscriptions (
                name,
                amount,
                currency,
                base_currency,
                payment_mode,
                next_charge_at,
                period_days,
                share_limit,
                reminder_time,
                monthly_anchor_day
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                amount,
                currency.upper(),
                currency.upper(),
                normalized_mode,
                due_date.isoformat(),
                period_days,
                share_limit,
                "",
                due_date.day if period_days == MONTHLY_PERIOD_SENTINEL else None,
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
            SELECT s.id, s.name, s.amount, s.currency, s.base_currency, s.next_charge_at, s.period_days,
                   s.share_limit, s.payment_mode, COUNT(sp.friend_id) AS participant_count
            FROM subscriptions s
            LEFT JOIN subscription_participants sp ON sp.subscription_id = s.id
            GROUP BY s.id
            ORDER BY date(s.next_charge_at)
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_subscriptions_for_user(self, telegram_id: Optional[int]) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        if telegram_id is None:
            return []
        cursor = await self._conn.execute(
            """
            SELECT s.id, s.name, s.amount, s.currency, s.base_currency, s.next_charge_at, s.period_days
                   , s.payment_mode
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
                   , sp.fixed_amount
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
                   COALESCE(sp.share_weight, 1) AS share_weight,
                   sp.fixed_amount
            FROM friends f
            LEFT JOIN subscription_participants sp
                ON sp.friend_id = f.id AND sp.subscription_id = ?
            WHERE f.telegram_id IS NOT NULL
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
                INSERT INTO subscription_participants (
                    subscription_id,
                    friend_id,
                    share_weight,
                    fixed_amount
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(subscription_id, friend_id) DO UPDATE
                SET share_weight = excluded.share_weight
                """,
                (subscription_id, friend_id, share_weight, None),
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

    async def update_participant_fixed_amount(
        self,
        subscription_id: int,
        friend_id: int,
        fixed_amount: Optional[float],
    ) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            """
            UPDATE subscription_participants
            SET fixed_amount = ?
            WHERE subscription_id = ? AND friend_id = ?
            """,
            (fixed_amount, subscription_id, friend_id),
        )
        await self._conn.commit()

    async def update_all_participants_fixed_amount(
        self,
        subscription_id: int,
        fixed_amount: Optional[float],
    ) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            """
            UPDATE subscription_participants
            SET fixed_amount = ?
            WHERE subscription_id = ?
            """,
            (fixed_amount, subscription_id),
        )
        await self._conn.commit()

    async def fetch_subscriptions_for_reminders(self) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT id, name, amount, currency, base_currency, payment_destination_id, next_charge_at, period_days, share_limit,
                   reminder_time, reminder_offsets, remind_after_due, comment, monthly_anchor_day,
                   payment_mode
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
            SELECT sp.subscription_id, f.full_name, f.telegram_id, sp.share_weight, sp.fixed_amount
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
                    "fixed_amount": row["fixed_amount"],
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

    @staticmethod
    def _payment_destination_default_key() -> str:
        return "payment_destination:default"

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

    async def list_payment_destinations(self) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT pd.id,
                   pd.title,
                   pd.currency,
                   pd.details,
                   pd.payment_link,
                   pd.created_at,
                   (
                       SELECT COUNT(*)
                       FROM settings s
                       WHERE s.key LIKE 'user:%:payment_destination_id'
                         AND s.value = CAST(pd.id AS TEXT)
                   ) AS assigned_count
            FROM payment_destinations pd
            ORDER BY pd.title, pd.id
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_payment_destination(self, destination_id: int) -> Optional[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT id, title, currency, details, payment_link, created_at
            FROM payment_destinations
            WHERE id = ?
            """,
            (destination_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def create_payment_destination(
        self,
        *,
        title: str,
        currency: str,
        details: str,
        payment_link: str = "",
    ) -> int:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            INSERT INTO payment_destinations (title, currency, details, payment_link)
            VALUES (?, ?, ?, ?)
            """,
            (
                title.strip(),
                currency.strip().upper(),
                details.strip(),
                payment_link.strip(),
            ),
        )
        await self._conn.commit()
        return int(cursor.lastrowid)

    async def update_payment_destination_fields(
        self,
        destination_id: int,
        **fields: Any,
    ) -> Optional[Dict[str, Any]]:
        allowed = {"title", "currency", "details", "payment_link"}
        updates = {
            key: value
            for key, value in fields.items()
            if key in allowed and value is not None
        }
        if not updates:
            return await self.get_payment_destination(destination_id)
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            if key == "currency":
                normalized = str(value).strip().upper()
                assignments.append(f"{key} = ?")
                values.append(normalized)
                continue
            assignments.append(f"{key} = ?")
            values.append(str(value).strip())
        values.append(destination_id)
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            f"""
            UPDATE payment_destinations
            SET {", ".join(assignments)}
            WHERE id = ?
            """,
            values,
        )
        await self._conn.commit()
        return await self.get_payment_destination(destination_id)

    async def get_default_payment_destination_id(self) -> Optional[int]:
        raw_value = await self.get_setting(self._payment_destination_default_key())
        if not raw_value:
            return None
        try:
            return int(raw_value)
        except ValueError:
            return None

    async def set_default_payment_destination_id(self, destination_id: Optional[int]) -> None:
        if destination_id is None:
            assert self._conn is not None, "Database is not connected"
            await self._conn.execute(
                "DELETE FROM settings WHERE key = ?",
                (self._payment_destination_default_key(),),
            )
            await self._conn.commit()
            return
        await self.set_setting(self._payment_destination_default_key(), str(destination_id))

    async def get_user_payment_destination_id(self, telegram_id: int) -> Optional[int]:
        raw_value = await self.get_user_setting(telegram_id, "payment_destination_id")
        if not raw_value:
            return None
        try:
            return int(raw_value)
        except ValueError:
            return None

    async def set_user_payment_destination_id(
        self,
        telegram_id: int,
        destination_id: int,
    ) -> None:
        await self.set_user_setting(telegram_id, "payment_destination_id", str(destination_id))

    async def clear_user_payment_destination_id(self, telegram_id: int) -> None:
        await self.delete_user_setting(telegram_id, "payment_destination_id")

    async def get_effective_payment_destination_for_user(
        self,
        telegram_id: int,
    ) -> Optional[Dict[str, Any]]:
        user_destination_id = await self.get_user_payment_destination_id(telegram_id)
        if user_destination_id is not None:
            user_destination = await self.get_payment_destination(user_destination_id)
            if user_destination:
                return user_destination
        default_destination_id = await self.get_default_payment_destination_id()
        if default_destination_id is None:
            return None
        return await self.get_payment_destination(default_destination_id)

    async def get_effective_subscription_payment_destination(
        self,
        subscription_id: int,
    ) -> Optional[Dict[str, Any]]:
        subscription = await self.get_subscription(subscription_id)
        if not subscription:
            return None
        raw_destination_id = subscription.get("payment_destination_id")
        try:
            destination_id = int(raw_destination_id) if raw_destination_id is not None else None
        except (TypeError, ValueError):
            destination_id = None
        if destination_id is not None:
            destination = await self.get_payment_destination(destination_id)
            if destination:
                return destination
        default_destination_id = await self.get_default_payment_destination_id()
        if default_destination_id is None:
            return None
        return await self.get_payment_destination(default_destination_id)

    async def list_payment_destination_assignees(
        self,
        destination_id: int,
    ) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT f.id, f.telegram_id, f.full_name
            FROM settings s
            JOIN friends f
              ON s.key = 'user:' || f.telegram_id || ':payment_destination_id'
            WHERE s.value = ?
            ORDER BY f.full_name, f.id
            """,
            (str(destination_id),),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def clear_payment_destination_references(self, destination_id: int) -> None:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute(
            """
            DELETE FROM settings
            WHERE key = ?
              AND value = ?
            """,
            (self._payment_destination_default_key(), str(destination_id)),
        )
        await self._conn.execute(
            """
            DELETE FROM settings
            WHERE key LIKE 'user:%:payment_destination_id'
              AND value = ?
            """,
            (str(destination_id),),
        )
        await self._conn.commit()

    async def delete_payment_destination(self, destination_id: int) -> None:
        assert self._conn is not None, "Database is not connected"
        await self.clear_payment_destination_references(destination_id)
        await self._conn.execute(
            "DELETE FROM payment_destinations WHERE id = ?",
            (destination_id,),
        )
        await self._conn.commit()

    async def create_topup_request(
        self,
        *,
        friend_id: int,
        destination_id: Optional[int],
        amount: float,
        currency: str,
        destination_title: str,
        destination_details: str,
        destination_link: str = "",
    ) -> int:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            INSERT INTO topup_requests (
                friend_id,
                destination_id,
                amount,
                currency,
                destination_title,
                destination_details,
                destination_link
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                friend_id,
                destination_id,
                amount,
                currency.strip().upper(),
                destination_title.strip(),
                destination_details.strip(),
                destination_link.strip(),
            ),
        )
        await self._conn.commit()
        return int(cursor.lastrowid)

    async def get_topup_request(self, request_id: int) -> Optional[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT tr.*,
                   f.telegram_id,
                   f.full_name,
                   f.balance,
                   f.balance_currency
            FROM topup_requests tr
            JOIN friends f ON f.id = tr.friend_id
            WHERE tr.id = ?
            """,
            (request_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_pending_topup_requests(self) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT tr.id,
                   tr.friend_id,
                   tr.amount,
                   tr.currency,
                   tr.destination_title,
                   tr.created_at,
                   tr.submitted_at,
                   f.full_name,
                   f.telegram_id
            FROM topup_requests tr
            JOIN friends f ON f.id = tr.friend_id
            WHERE tr.status = 'pending'
            ORDER BY COALESCE(tr.submitted_at, tr.created_at), tr.id
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_topup_request_pending(self, request_id: int) -> Optional[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self._conn.execute(
                """
                SELECT id
                FROM topup_requests
                WHERE id = ? AND status = 'new'
                """,
                (request_id,),
            )
            row = await cursor.fetchone()
            if not row:
                await self._conn.rollback()
                return await self.get_topup_request(request_id)
            await self._conn.execute(
                """
                UPDATE topup_requests
                SET status = 'pending',
                    submitted_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (request_id,),
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return await self.get_topup_request(request_id)

    async def approve_topup_request(
        self,
        request_id: int,
        admin_telegram_id: int,
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self._conn.execute(
                """
                SELECT tr.*,
                       f.telegram_id,
                       f.full_name,
                       f.balance,
                       f.balance_currency
                FROM topup_requests tr
                JOIN friends f ON f.id = tr.friend_id
                WHERE tr.id = ? AND tr.status = 'pending'
                """,
                (request_id,),
            )
            row = await cursor.fetchone()
            if not row:
                await self._conn.rollback()
                return await self.get_topup_request(request_id), "not_pending"

            request = dict(row)
            amount = float(request.get("amount") or 0.0)
            request_currency = str(request.get("currency") or "RUB").strip().upper()
            current_balance = max(float(request.get("balance") or 0.0), 0.0)
            current_currency = str(request.get("balance_currency") or "").strip().upper()
            if len(current_currency) != 3 or not current_currency.isalpha() or current_balance <= 0:
                current_currency = request_currency
            if current_currency != request_currency and current_balance > 0:
                await self._conn.rollback()
                return request, "currency_mismatch"

            await self._conn.execute(
                """
                UPDATE friends
                SET balance = COALESCE(balance, 0) + ?,
                    balance_currency = ?
                WHERE id = ?
                """,
                (amount, request_currency, int(request["friend_id"])),
            )
            await self._conn.execute(
                """
                UPDATE topup_requests
                SET status = 'approved',
                    approved_by_telegram_id = ?,
                    rejected_by_telegram_id = NULL,
                    decided_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (admin_telegram_id, request_id),
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return await self.get_topup_request(request_id), None

    async def reject_topup_request(
        self,
        request_id: int,
        admin_telegram_id: int,
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        assert self._conn is not None, "Database is not connected"
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self._conn.execute(
                """
                SELECT id
                FROM topup_requests
                WHERE id = ? AND status = 'pending'
                """,
                (request_id,),
            )
            row = await cursor.fetchone()
            if not row:
                await self._conn.rollback()
                return await self.get_topup_request(request_id), "not_pending"
            await self._conn.execute(
                """
                UPDATE topup_requests
                SET status = 'rejected',
                    rejected_by_telegram_id = ?,
                    approved_by_telegram_id = NULL,
                    decided_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (admin_telegram_id, request_id),
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return await self.get_topup_request(request_id), None

    async def get_effective_subscription_base_currency(
        self,
        subscription_id: int,
        default_currency: str = "RUB",
    ) -> str:
        subscription = await self.get_subscription(subscription_id)
        if subscription:
            raw_value = str(subscription.get("base_currency") or "").strip().upper()
            if len(raw_value) == 3 and raw_value.isalpha():
                return raw_value
        global_value = await self.get_setting("target_currency")
        if global_value:
            normalized_global = global_value.strip().upper()
            if len(normalized_global) == 3 and normalized_global.isalpha():
                return normalized_global
        return default_currency.strip().upper() or "RUB"

    async def get_effective_target_currency(
        self,
        telegram_id: int,
        subscription_id: int,
        subscription_default: Optional[str] = None,
        default_currency: str = "RUB",
    ) -> str:
        user_override = await self.get_user_subscription_setting(
            telegram_id,
            subscription_id,
            "target_currency",
        )
        if user_override:
            normalized_user = user_override.strip().upper()
            if len(normalized_user) == 3 and normalized_user.isalpha():
                return normalized_user

        normalized_default = (subscription_default or "").strip().upper()
        if len(normalized_default) == 3 and normalized_default.isalpha():
            return normalized_default

        return await self.get_effective_subscription_base_currency(
            subscription_id,
            default_currency,
        )

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

    async def get_effective_base_timezone(
        self,
        default_timezone: str = "Europe/Moscow",
    ) -> str:
        raw_value = await self.get_setting("base_timezone")
        if raw_value:
            return raw_value.strip()
        return default_timezone.strip() or "Europe/Moscow"

    async def get_effective_user_timezone(
        self,
        telegram_id: int,
        default_timezone: str = "Europe/Moscow",
    ) -> str:
        user_value = await self.get_user_setting(telegram_id, "timezone")
        if user_value:
            return user_value.strip()
        return await self.get_effective_base_timezone(default_timezone)

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

    async def freeze_cycle_snapshot(self, subscription_id: int, due_date: date | str) -> None:
        assert self._conn is not None, "Database is not connected"
        due_value = due_date.isoformat() if isinstance(due_date, date) else str(due_date)
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO subscription_cycles (subscription_id, due_date)
            VALUES (?, ?)
            """,
            (subscription_id, due_value),
        )
        await self._ensure_cycle_settings_snapshot_exists(subscription_id, due_value)
        await self._ensure_cycle_participant_snapshot_exists(subscription_id, due_value)
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

    async def reset_cycle(self, subscription_id: int, due_date: date | str) -> None:
        assert self._conn is not None, "Database is not connected"
        due_value = due_date.isoformat() if isinstance(due_date, date) else str(due_date)
        for table_name in (
            "reminder_messages",
            "reminder_suppressions",
            "reminder_user_logs",
            "reminder_logs",
            "payment_logs",
            "subscription_cycle_participants",
            "subscription_cycles",
        ):
            await self._conn.execute(
                f"DELETE FROM {table_name} WHERE subscription_id = ? AND due_date = ?",
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

    async def _snapshot_cycle_participants(self, subscription_id: int, due_value: str) -> None:
        assert self._conn is not None, "Database is not connected"
        participants_cursor = await self._conn.execute(
            """
            SELECT f.telegram_id, f.full_name, sp.share_weight, sp.fixed_amount
            FROM subscription_participants sp
            JOIN friends f ON f.id = sp.friend_id
            WHERE sp.subscription_id = ?
              AND f.telegram_id IS NOT NULL
            """,
            (subscription_id,),
        )
        participants = await participants_cursor.fetchall()
        if not participants:
            return
        await self._conn.executemany(
            """
            INSERT OR IGNORE INTO subscription_cycle_participants (
                subscription_id,
                due_date,
                telegram_id,
                full_name,
                share_weight,
                fixed_amount
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    subscription_id,
                    due_value,
                    int(row["telegram_id"]),
                    str(row["full_name"]),
                    int(row["share_weight"] or 1),
                    row["fixed_amount"],
                )
                for row in participants
            ],
        )

    async def _ensure_cycle_participant_snapshot_exists(self, subscription_id: int, due_value: str) -> None:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT participants_snapshot_ready
            FROM subscription_cycles
            WHERE subscription_id = ? AND due_date = ?
            """,
            (subscription_id, due_value),
        )
        cycle_row = await cursor.fetchone()
        if not cycle_row:
            return
        if int(cycle_row["participants_snapshot_ready"] or 0) == 1:
            return
        await self._snapshot_cycle_participants(subscription_id, due_value)
        await self._conn.execute(
            """
            UPDATE subscription_cycles
            SET participants_snapshot_ready = 1
            WHERE subscription_id = ? AND due_date = ?
            """,
            (subscription_id, due_value),
        )

    async def _ensure_cycle_settings_snapshot_exists(self, subscription_id: int, due_value: str) -> None:
        assert self._conn is not None, "Database is not connected"
        cursor = await self._conn.execute(
            """
            SELECT settings_snapshot_ready
            FROM subscription_cycles
            WHERE subscription_id = ? AND due_date = ?
            """,
            (subscription_id, due_value),
        )
        cycle_row = await cursor.fetchone()
        if not cycle_row:
            return
        if int(cycle_row["settings_snapshot_ready"] or 0) == 1:
            return
        subscription = await self.get_subscription(subscription_id)
        if not subscription:
            return
        destination = await self.get_effective_subscription_payment_destination(subscription_id)
        payment_destination_id_snapshot = None
        payment_destination_title_snapshot = ""
        payment_destination_details_snapshot = ""
        payment_destination_link_snapshot = ""
        if destination:
            payment_destination_id_snapshot = int(destination["id"])
            payment_destination_title_snapshot = str(destination.get("title") or "")
            payment_destination_details_snapshot = str(destination.get("details") or "")
            payment_destination_link_snapshot = str(destination.get("payment_link") or "")
        await self._conn.execute(
            """
            UPDATE subscription_cycles
            SET amount_snapshot = ?,
                currency_snapshot = ?,
                base_currency_snapshot = ?,
                payment_destination_id_snapshot = ?,
                payment_destination_title_snapshot = ?,
                payment_destination_details_snapshot = ?,
                payment_destination_link_snapshot = ?,
                payment_mode_snapshot = ?,
                share_limit_snapshot = ?,
                comment_snapshot = ?,
                reminder_time_snapshot = ?,
                reminder_offsets_snapshot = ?,
                remind_after_due_snapshot = ?,
                settings_snapshot_ready = 1
            WHERE subscription_id = ? AND due_date = ?
            """,
            (
                subscription.get("amount"),
                subscription.get("currency"),
                subscription.get("base_currency"),
                payment_destination_id_snapshot,
                payment_destination_title_snapshot,
                payment_destination_details_snapshot,
                payment_destination_link_snapshot,
                subscription.get("payment_mode"),
                subscription.get("share_limit"),
                subscription.get("comment"),
                subscription.get("reminder_time"),
                subscription.get("reminder_offsets"),
                subscription.get("remind_after_due"),
                subscription_id,
                due_value,
            ),
        )

    async def is_cycle_participant_snapshot_ready(self, subscription_id: int, due_date: date | str) -> bool:
        assert self._conn is not None, "Database is not connected"
        due_value = due_date.isoformat() if isinstance(due_date, date) else str(due_date)
        cursor = await self._conn.execute(
            """
            SELECT participants_snapshot_ready
            FROM subscription_cycles
            WHERE subscription_id = ? AND due_date = ?
            LIMIT 1
            """,
            (subscription_id, due_value),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        return int(row["participants_snapshot_ready"] or 0) == 1

    async def list_cycle_participants(self, subscription_id: int, due_date: date | str) -> List[Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        due_value = due_date.isoformat() if isinstance(due_date, date) else str(due_date)
        cursor = await self._conn.execute(
            """
            SELECT telegram_id, full_name, share_weight, fixed_amount
            FROM subscription_cycle_participants
            WHERE subscription_id = ? AND due_date = ?
            ORDER BY full_name
            """,
            (subscription_id, due_value),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_cycle_participants_for_due_dates(
        self,
        subscription_id: int,
        due_dates: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        assert self._conn is not None, "Database is not connected"
        if not due_dates:
            return {}
        placeholders = ",".join(["?"] * len(due_dates))
        cursor = await self._conn.execute(
            f"""
            SELECT c.due_date,
                   c.participants_snapshot_ready,
                   c.settings_snapshot_ready,
                   c.amount_snapshot,
                   c.currency_snapshot,
                   c.base_currency_snapshot,
                   c.payment_destination_id_snapshot,
                   c.payment_destination_title_snapshot,
                   c.payment_destination_details_snapshot,
                   c.payment_destination_link_snapshot,
                   c.payment_mode_snapshot,
                   c.share_limit_snapshot,
                   c.comment_snapshot,
                   c.reminder_time_snapshot,
                   c.reminder_offsets_snapshot,
                   c.remind_after_due_snapshot,
                   cp.telegram_id,
                   cp.full_name,
                   cp.share_weight,
                   cp.fixed_amount
            FROM subscription_cycles c
            LEFT JOIN subscription_cycle_participants cp
              ON cp.subscription_id = c.subscription_id
             AND cp.due_date = c.due_date
            WHERE c.subscription_id = ? AND c.due_date IN ({placeholders})
            ORDER BY c.due_date, cp.full_name
            """,
            [subscription_id, *due_dates],
        )
        rows = await cursor.fetchall()
        result: Dict[str, Dict[str, Any]] = {
            value: {
                "participants": [],
                "snapshot_ready": False,
                "settings_snapshot_ready": False,
                "amount": None,
                "currency": None,
                "base_currency": None,
                "payment_destination_id": None,
                "payment_destination_title": "",
                "payment_destination_details": "",
                "payment_destination_link": "",
                "payment_mode": PAYMENT_MODE_SPLIT,
                "share_limit": None,
                "comment": "",
                "reminder_time": "",
                "reminder_offsets": None,
                "remind_after_due": None,
            }
            for value in due_dates
        }
        for row in rows:
            due_value = str(row["due_date"])
            state = result.get(due_value)
            if state is None:
                continue
            if int(row["participants_snapshot_ready"] or 0) == 1:
                state["snapshot_ready"] = True
            if int(row["settings_snapshot_ready"] or 0) == 1:
                state["settings_snapshot_ready"] = True
                state["amount"] = row["amount_snapshot"]
                state["currency"] = row["currency_snapshot"]
                state["base_currency"] = row["base_currency_snapshot"]
                state["payment_destination_id"] = row["payment_destination_id_snapshot"]
                state["payment_destination_title"] = row["payment_destination_title_snapshot"] or ""
                state["payment_destination_details"] = row["payment_destination_details_snapshot"] or ""
                state["payment_destination_link"] = row["payment_destination_link_snapshot"] or ""
                state["payment_mode"] = row["payment_mode_snapshot"] or PAYMENT_MODE_SPLIT
                state["share_limit"] = row["share_limit_snapshot"]
                state["comment"] = row["comment_snapshot"] or ""
                state["reminder_time"] = row["reminder_time_snapshot"] or ""
                state["reminder_offsets"] = row["reminder_offsets_snapshot"]
                state["remind_after_due"] = row["remind_after_due_snapshot"]
            if row["telegram_id"] is not None:
                state["participants"].append(
                    {
                        "telegram_id": int(row["telegram_id"]),
                        "full_name": str(row["full_name"]),
                        "share_weight": int(row["share_weight"] or 1),
                        "fixed_amount": row["fixed_amount"],
                    }
                )
        return result
