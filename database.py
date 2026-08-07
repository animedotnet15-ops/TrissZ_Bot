"""SQLite persistence for users, stored files, share links, sessions and settings."""
from __future__ import annotations
import asyncio
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
import aiosqlite
from config import config

try:
    from motor.motor_asyncio import AsyncIOMotorClient
    MOTOR_AVAILABLE = True
except ImportError:
    MOTOR_AVAILABLE = False

LOG = logging.getLogger("database")

class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self._mongo_client = None
        self._mongo_db = None
        self._mongo_checked = False

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        try:
            yield db
        finally:
            await db.close()

    async def init(self) -> None:
        async with self._init_lock:
            if self._initialized:
                return
            parent = Path(self.path).parent
            if parent != Path("."):
                parent.mkdir(parents=True, exist_ok=True)
            async with self.connection() as db:
                await db.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA foreign_keys=ON;
                    CREATE TABLE IF NOT EXISTS users (
                        user_id     INTEGER PRIMARY KEY,
                        first_name  TEXT    NOT NULL DEFAULT '',
                        username    TEXT    NOT NULL DEFAULT '',
                        created_at  INTEGER NOT NULL,
                        last_seen   INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS bans (
                        user_id    INTEGER PRIMARY KEY,
                        reason     TEXT    NOT NULL DEFAULT '',
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS strikes (
                        user_id     INTEGER PRIMARY KEY,
                        count       INTEGER NOT NULL DEFAULT 0,
                        last_reason TEXT    NOT NULL DEFAULT '',
                        updated_at  INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS bypass_events (
                        event_key  TEXT    NOT NULL,
                        user_id    INTEGER NOT NULL,
                        created_at INTEGER NOT NULL,
                        PRIMARY KEY(event_key, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS settings (
                        key   TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS stored_files (
                        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                        storage_message_id INTEGER NOT NULL UNIQUE,
                        original_name      TEXT    NOT NULL,
                        tag                TEXT    NOT NULL,
                        created_at         INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS posts (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        code       TEXT    NOT NULL UNIQUE,
                        kind       TEXT    NOT NULL CHECK(kind IN ('single','batch')),
                        protected  INTEGER NOT NULL DEFAULT 0,
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS post_files (
                        post_id  INTEGER NOT NULL,
                        file_id  INTEGER NOT NULL,
                        position INTEGER NOT NULL,
                        PRIMARY KEY(post_id, file_id),
                        FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE,
                        FOREIGN KEY(file_id) REFERENCES stored_files(id) ON DELETE RESTRICT
                    );
                    CREATE TABLE IF NOT EXISTS protected_sessions (
                        token            TEXT    PRIMARY KEY,
                        post_id          INTEGER NOT NULL,
                        created_at       INTEGER NOT NULL,
                        expires_at       INTEGER NOT NULL,
                        activated_at     INTEGER,
                        verified_user_id INTEGER,
                        claimed_by       INTEGER,
                        claimed_at       INTEGER,
                        FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS pending_tokens (
                        token      TEXT    PRIMARY KEY,
                        post_id    INTEGER NOT NULL,
                        user_id    INTEGER NOT NULL,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL,
                        used       INTEGER NOT NULL DEFAULT 0,
                        claimed_at INTEGER,
                        FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_files_storage_msg   ON stored_files(storage_message_id);
                    CREATE INDEX IF NOT EXISTS idx_post_files_post      ON post_files(post_id, position);
                    CREATE INDEX IF NOT EXISTS idx_sessions_post        ON protected_sessions(post_id);
                    CREATE INDEX IF NOT EXISTS idx_pending_tokens_user  ON pending_tokens(user_id, post_id);

                    CREATE TABLE IF NOT EXISTS admin_logs (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        admin_id   INTEGER NOT NULL,
                        action     TEXT    NOT NULL,
                        target     TEXT    NOT NULL DEFAULT '',
                        meta       TEXT    NOT NULL DEFAULT '',
                        created_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_admin_logs_time ON admin_logs(created_at);

                    CREATE TABLE IF NOT EXISTS batch_sessions (
                        admin_id   INTEGER PRIMARY KEY,
                        file_ids   TEXT    NOT NULL DEFAULT '[]',
                        title      TEXT    NOT NULL DEFAULT '',
                        caption    TEXT    NOT NULL DEFAULT '',
                        kind       TEXT    NOT NULL DEFAULT 'batch',
                        updated_at INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS custom_buttons (
                        id       INTEGER PRIMARY KEY AUTOINCREMENT,
                        text     TEXT    NOT NULL,
                        url      TEXT    NOT NULL DEFAULT '',
                        callback TEXT    NOT NULL DEFAULT '',
                        row_pos  INTEGER NOT NULL DEFAULT 0,
                        enabled  INTEGER NOT NULL DEFAULT 1
                    );
                    """
                )
                columns = {
                    row[1]
                    for row in await (
                        await db.execute("PRAGMA table_info(protected_sessions)")
                    ).fetchall()
                }
                if "verified_user_id" not in columns:
                    await db.execute(
                        "ALTER TABLE protected_sessions ADD COLUMN verified_user_id INTEGER"
                    )

                # --- Safe migration: Profile system columns on `users` ---
                # ADD COLUMN only, never touches existing rows/data.
                user_columns = {
                    row[1]
                    for row in await (
                        await db.execute("PRAGMA table_info(users)")
                    ).fetchall()
                }
                profile_columns = {
                    "verification_count": "INTEGER NOT NULL DEFAULT 0",
                    "download_count": "INTEGER NOT NULL DEFAULT 0",
                    "referral_count": "INTEGER NOT NULL DEFAULT 0",
                    "referred_by": "INTEGER",
                    "premium_until": "INTEGER NOT NULL DEFAULT 0",
                }
                for col_name, col_def in profile_columns.items():
                    if col_name not in user_columns:
                        await db.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")

                await db.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES('link_mode', 'direct')"
                )
                await db.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES('start_photo_spoiler', '1')"
                )
                await db.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES('bot_enabled', '1')"
                )
                await db.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES('maintenance_message', "
                    "'🔴 <b>Maintenance</b>\\n\\n<i>The bot is temporarily offline for maintenance. "
                    "Please check back shortly.</i>')"
                )
                await db.commit()
            self._initialized = True

    # ------------------------------------------------------------------ #
    #  Admin activity logs
    # ------------------------------------------------------------------ #
    async def log_admin_action(self, admin_id: int, action: str, target: str = "", meta: str = "") -> None:
        async with self.connection() as db:
            await db.execute(
                "INSERT INTO admin_logs(admin_id, action, target, meta, created_at) VALUES(?, ?, ?, ?, ?)",
                (admin_id, action, target, meta, int(time.time())),
            )
            await db.commit()

    async def get_admin_logs(self, limit: int = 15, offset: int = 0) -> list[aiosqlite.Row]:
        async with self.connection() as db:
            return await (
                await db.execute(
                    "SELECT * FROM admin_logs ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ).fetchall()

    async def count_admin_logs(self) -> int:
        async with self.connection() as db:
            row = await (await db.execute("SELECT COUNT(*) c FROM admin_logs")).fetchone()
            return int(row["c"])

    # ------------------------------------------------------------------ #
    #  Bot ON/OFF (maintenance mode)
    # ------------------------------------------------------------------ #
    async def is_bot_enabled(self) -> bool:
        return await self.get_setting("bot_enabled", "1") == "1"

    async def set_bot_enabled(self, enabled: bool) -> None:
        await self.set_setting("bot_enabled", "1" if enabled else "0")

    async def get_maintenance_message(self) -> str:
        return await self.get_setting(
            "maintenance_message",
            "🔴 <b>Maintenance</b>\n\n<i>The bot is temporarily offline. Please check back shortly.</i>",
        )

    async def set_maintenance_message(self, text: str) -> None:
        await self.set_setting("maintenance_message", text)

    # ------------------------------------------------------------------ #
    #  DB-persisted Batch sessions (survive a bot restart, unlike a plain
    #  in-memory dict)
    # ------------------------------------------------------------------ #
    async def start_batch_session(self, admin_id: int, kind: str = "batch") -> None:
        async with self.connection() as db:
            await db.execute(
                "INSERT INTO batch_sessions(admin_id, file_ids, title, caption, kind, updated_at) "
                "VALUES(?, '[]', '', '', ?, ?) "
                "ON CONFLICT(admin_id) DO UPDATE SET file_ids='[]', title='', caption='', kind=excluded.kind, "
                "updated_at=excluded.updated_at",
                (admin_id, kind, int(time.time())),
            )
            await db.commit()

    async def get_batch_session(self, admin_id: int) -> dict | None:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT * FROM batch_sessions WHERE admin_id=?", (admin_id,))
            ).fetchone()
        if not row:
            return None
        return {
            "admin_id": row["admin_id"],
            "file_ids": json.loads(row["file_ids"]),
            "title": row["title"],
            "caption": row["caption"],
            "kind": row["kind"],
        }

    async def append_batch_file(self, admin_id: int, file_id: int) -> int:
        session = await self.get_batch_session(admin_id)
        if session is None:
            return 0
        file_ids = session["file_ids"]
        file_ids.append(file_id)
        async with self.connection() as db:
            await db.execute(
                "UPDATE batch_sessions SET file_ids=?, updated_at=? WHERE admin_id=?",
                (json.dumps(file_ids), int(time.time()), admin_id),
            )
            await db.commit()
        return len(file_ids)

    async def set_batch_meta(self, admin_id: int, *, title: str | None = None, caption: str | None = None) -> None:
        session = await self.get_batch_session(admin_id)
        if session is None:
            return
        new_title = title if title is not None else session["title"]
        new_caption = caption if caption is not None else session["caption"]
        async with self.connection() as db:
            await db.execute(
                "UPDATE batch_sessions SET title=?, caption=?, updated_at=? WHERE admin_id=?",
                (new_title, new_caption, int(time.time()), admin_id),
            )
            await db.commit()

    async def end_batch_session(self, admin_id: int) -> dict | None:
        session = await self.get_batch_session(admin_id)
        async with self.connection() as db:
            await db.execute("DELETE FROM batch_sessions WHERE admin_id=?", (admin_id,))
            await db.commit()
        return session

    # ------------------------------------------------------------------ #
    #  Multi-button manager (beyond the single legacy custom_button)
    # ------------------------------------------------------------------ #
    async def list_buttons(self) -> list[aiosqlite.Row]:
        async with self.connection() as db:
            return await (
                await db.execute("SELECT * FROM custom_buttons ORDER BY row_pos, id")
            ).fetchall()

    async def add_button(self, text: str, url: str = "", callback: str = "") -> int:
        async with self.connection() as db:
            row = await (await db.execute("SELECT COALESCE(MAX(row_pos), -1) m FROM custom_buttons")).fetchone()
            next_pos = int(row["m"]) + 1
            cursor = await db.execute(
                "INSERT INTO custom_buttons(text, url, callback, row_pos, enabled) VALUES(?, ?, ?, ?, 1)",
                (text, url, callback, next_pos),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def remove_button(self, button_id: int) -> bool:
        async with self.connection() as db:
            cursor = await db.execute("DELETE FROM custom_buttons WHERE id=?", (button_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def toggle_button(self, button_id: int) -> None:
        async with self.connection() as db:
            await db.execute(
                "UPDATE custom_buttons SET enabled = 1 - enabled WHERE id=?", (button_id,)
            )
            await db.commit()

    async def reorder_button(self, button_id: int, direction: int) -> None:
        """direction: -1 to move up, +1 to move down (swaps row_pos with neighbour)."""
        async with self.connection() as db:
            cur = await (
                await db.execute("SELECT id, row_pos FROM custom_buttons ORDER BY row_pos, id")
            ).fetchall()
            ids = [r["id"] for r in cur]
            if button_id not in ids:
                return
            idx = ids.index(button_id)
            new_idx = idx + direction
            if new_idx < 0 or new_idx >= len(ids):
                return
            ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
            for pos, bid in enumerate(ids):
                await db.execute("UPDATE custom_buttons SET row_pos=? WHERE id=?", (pos, bid))
            await db.commit()

    # ------------------------------------------------------------------ #
    #  Statistics
    # ------------------------------------------------------------------ #
    async def get_statistics(self) -> dict:
        now = int(time.time())
        day, week, month = now - 86400, now - 7 * 86400, now - 30 * 86400
        async with self.connection() as db:
            async def scalar(query: str, params: tuple = ()) -> int:
                row = await (await db.execute(query, params)).fetchone()
                return int(row[0]) if row and row[0] is not None else 0

            total_users = await scalar("SELECT COUNT(*) FROM users")
            new_today = await scalar("SELECT COUNT(*) FROM users WHERE created_at >= ?", (day,))
            new_week = await scalar("SELECT COUNT(*) FROM users WHERE created_at >= ?", (week,))
            new_month = await scalar("SELECT COUNT(*) FROM users WHERE created_at >= ?", (month,))
            total_files = await scalar("SELECT COUNT(*) FROM stored_files")
            total_links = await scalar("SELECT COUNT(*) FROM posts WHERE kind='single'")
            total_batches = await scalar("SELECT COUNT(*) FROM posts WHERE kind='batch'")
            banned = await scalar("SELECT COUNT(*) FROM bans")
            deliveries = await scalar("SELECT COALESCE(SUM(download_count),0) FROM users")
            verifications = await scalar("SELECT COALESCE(SUM(verification_count),0) FROM users")
            failed_verifications = await scalar(
                "SELECT COUNT(*) FROM pending_tokens WHERE used=1"
            ) - verifications
            if failed_verifications < 0:
                failed_verifications = 0

        return {
            "total_users": total_users,
            "new_today": new_today,
            "new_week": new_week,
            "new_month": new_month,
            "total_files": total_files,
            "total_links": total_links,
            "total_batches": total_batches,
            "banned": banned,
            "deliveries": deliveries,
            "verifications": verifications,
            "failed_verifications": failed_verifications,
        }

    # ------------------------------------------------------------------ #
    #  Backup / Restore
    # ------------------------------------------------------------------ #
    BACKUP_TABLES = (
        "users", "bans", "strikes", "settings", "stored_files", "posts",
        "post_files", "custom_buttons", "admin_logs",
    )

    async def export_backup(self) -> dict:
        backup: dict = {"version": 1, "created_at": int(time.time()), "tables": {}}
        async with self.connection() as db:
            for table in self.BACKUP_TABLES:
                rows = await (await db.execute(f"SELECT * FROM {table}")).fetchall()
                backup["tables"][table] = [dict(r) for r in rows]
        return backup

    async def restore_backup(self, backup: dict) -> dict:
        """Restores rows from a previously exported backup. Existing rows with
        matching primary keys are replaced; nothing outside BACKUP_TABLES is
        touched. Caller is responsible for taking a fresh safety backup first."""
        if not isinstance(backup, dict) or "tables" not in backup:
            raise ValueError("Invalid backup file format.")
        tables = backup["tables"]
        restored = {}
        async with self.connection() as db:
            for table in self.BACKUP_TABLES:
                rows = tables.get(table)
                if not rows:
                    restored[table] = 0
                    continue
                count = 0
                for row in rows:
                    cols = list(row.keys())
                    placeholders = ",".join("?" for _ in cols)
                    col_list = ",".join(cols)
                    await db.execute(
                        f"INSERT OR REPLACE INTO {table}({col_list}) VALUES({placeholders})",
                        tuple(row[c] for c in cols),
                    )
                    count += 1
                restored[table] = count
            await db.commit()
        return restored

    # ------------------------------------------------------------------ #
    #  Legacy compatibility shim
    # ------------------------------------------------------------------ #
    async def create_verified_session(self, post_id: int, user_id: int) -> None:
        """web.py's standalone /g/<code>/verify page called this method but it
        never existed in this module — any hit on that route was crashing with
        AttributeError. That HTML guard page is NOT wired into the bot's actual
        protected-link flow (bot.py's tok_ handler + shorten_with_arolinks is
        the real, active verification path), so this is kept only as a safe
        no-op to stop the crash rather than a real second verification system."""
        return None

    # ------------------------------------------------------------------ #
    #  Users / Bans / Strikes
    # ------------------------------------------------------------------ #
    async def touch_user(self, user_id: int, first_name: str, username: str) -> None:
        now = int(time.time())
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO users(user_id, first_name, username, created_at, last_seen)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    first_name = excluded.first_name,
                    username   = excluded.username,
                    last_seen  = excluded.last_seen
                """,
                (user_id, first_name, username, now, now),
            )
            await db.commit()

    # --- Profile system: increments + full profile lookup ---
    async def increment_verification_count(self, user_id: int) -> None:
        async with self.connection() as db:
            await db.execute(
                "UPDATE users SET verification_count = verification_count + 1 WHERE user_id=?",
                (user_id,),
            )
            await db.commit()

    async def increment_download_count(self, user_id: int) -> None:
        async with self.connection() as db:
            await db.execute(
                "UPDATE users SET download_count = download_count + 1 WHERE user_id=?",
                (user_id,),
            )
            await db.commit()

    async def set_referrer(self, user_id: int, referred_by: int) -> bool:
        """Sets referred_by only if not already set (first-touch wins), and
        increments the referrer's referral_count. Returns True if applied."""
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,))
            ).fetchone()
            if row is None or row["referred_by"] is not None or user_id == referred_by:
                return False
            await db.execute(
                "UPDATE users SET referred_by=? WHERE user_id=?", (referred_by, user_id)
            )
            await db.execute(
                "UPDATE users SET referral_count = referral_count + 1 WHERE user_id=?",
                (referred_by,),
            )
            await db.commit()
            return True

    async def get_profile(self, user_id: int) -> dict | None:
        async with self.connection() as db:
            user = await (
                await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
            ).fetchone()
            if not user:
                return None
            strike_row = await (
                await db.execute("SELECT count FROM strikes WHERE user_id=?", (user_id,))
            ).fetchone()
            banned = await (
                await db.execute("SELECT 1 FROM bans WHERE user_id=?", (user_id,))
            ).fetchone()
            return {
                "user_id": user["user_id"],
                "first_name": user["first_name"],
                "username": user["username"],
                "joined_at": user["created_at"],
                "last_seen": user["last_seen"],
                "verification_count": user["verification_count"],
                "download_count": user["download_count"],
                "referral_count": user["referral_count"],
                "premium_until": user["premium_until"],
                "warnings": strike_row["count"] if strike_row else 0,
                "banned": banned is not None,
            }

    async def is_banned(self, user_id: int) -> bool:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT 1 FROM bans WHERE user_id=?", (user_id,))
            ).fetchone()
            return row is not None

    async def ban_user(self, user_id: int, reason: str) -> None:
        async with self.connection() as db:
            await db.execute(
                "INSERT OR REPLACE INTO bans(user_id, reason, created_at) VALUES(?, ?, ?)",
                (user_id, reason, int(time.time())),
            )
            await db.commit()

    async def unban_user(self, user_id: int) -> bool:
        async with self.connection() as db:
            cursor = await db.execute("DELETE FROM bans WHERE user_id=?", (user_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def get_strikes(self, user_id: int) -> int:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT count FROM strikes WHERE user_id=?", (user_id,))
            ).fetchone()
            return int(row["count"]) if row else 0

    async def reset_strikes(self, user_id: int) -> bool:
        async with self.connection() as db:
            cursor = await db.execute("DELETE FROM strikes WHERE user_id=?", (user_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def record_bypass(
        self, user_id: int, event_key: str, reason: str
    ) -> tuple[int, bool, bool]:
        now = int(time.time())
        async with self.connection() as db:
            try:
                await db.execute(
                    "INSERT INTO bypass_events(event_key, user_id, created_at) VALUES(?, ?, ?)",
                    (event_key, user_id, now),
                )
            except aiosqlite.IntegrityError:
                row = await (
                    await db.execute("SELECT count FROM strikes WHERE user_id=?", (user_id,))
                ).fetchone()
                count = int(row["count"]) if row else 0
                banned = await self.is_banned(user_id)
                return count, banned, False

            await db.execute(
                """
                INSERT INTO strikes(user_id, count, last_reason, updated_at)
                VALUES(?, 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    count       = count + 1,
                    last_reason = excluded.last_reason,
                    updated_at  = excluded.updated_at
                """,
                (user_id, reason, now),
            )
            row = await (
                await db.execute("SELECT count FROM strikes WHERE user_id=?", (user_id,))
            ).fetchone()
            count = int(row["count"])
            banned = count >= config.strike_limit
            if banned:
                await self.ban_user(user_id, f"Auto-ban after {count} bypass strikes")
            await db.commit()
            return count, banned, True

    async def get_user_by_username(self, username: str) -> aiosqlite.Row | None:
        username = username.lstrip("@").strip()
        async with self.connection() as db:
            return await (
                await db.execute(
                    "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                    (username,),
                )
            ).fetchone()

    async def get_fsub_channels(self) -> list[dict]:
        """Each entry: {"chat": "@name or -100id", "name": display, "link": join url,
        "folder": optional folder label, "enabled": bool}"""
        raw = await self.get_setting("fsub_channels", "[]")
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    async def set_fsub_channels(self, channels: list[dict]) -> None:
        await self.set_setting("fsub_channels", json.dumps(channels))

    async def add_fsub_channel(self, chat: str, name: str, link: str = "", folder: str = "") -> None:
        channels = await self.get_fsub_channels()
        channels.append({"chat": chat, "name": name, "link": link, "folder": folder, "enabled": True})
        await self.set_fsub_channels(channels)

    async def remove_fsub_channel(self, index: int) -> bool:
        channels = await self.get_fsub_channels()
        if 0 <= index < len(channels):
            channels.pop(index)
            await self.set_fsub_channels(channels)
            return True
        return False

    async def toggle_fsub_channel(self, index: int) -> bool:
        channels = await self.get_fsub_channels()
        if 0 <= index < len(channels):
            channels[index]["enabled"] = not channels[index].get("enabled", True)
            await self.set_fsub_channels(channels)
            return True
        return False

    # ------------------------------------------------------------------ #
    #  Shortener settings service — single source of truth, never
    #  hard-coded in handlers. Existing keys (shortener_url,
    #  arolinks_api_token, min_verify_seconds, token_validity_seconds)
    #  are reused so old data keeps working.
    # ------------------------------------------------------------------ #
    async def get_shortener_settings(self) -> dict:
        return {
            "domain": await self.get_setting("shortener_url", "arolinks.com"),
            "api_token": await self.get_setting("arolinks_api_token", ""),
            "enabled": (await self.get_setting("link_mode", "direct")) == "shortener",
            "min_seconds": int(await self.get_setting("min_verify_seconds", "120")),
            "max_seconds": int(await self.get_setting("token_validity_seconds", "300")),
        }

    async def set_shortener_domain(self, domain: str) -> None:
        await self.set_setting("shortener_url", domain)

    async def set_shortener_token(self, token: str) -> None:
        await self.set_setting("arolinks_api_token", token)

    async def set_shortener_enabled(self, enabled: bool) -> None:
        await self.set_setting("link_mode", "shortener" if enabled else "direct")

    async def set_shortener_min_seconds(self, seconds: int) -> None:
        await self.set_setting("min_verify_seconds", str(int(seconds)))

    async def set_shortener_max_seconds(self, seconds: int) -> None:
        await self.set_setting("token_validity_seconds", str(int(seconds)))

    async def broadcast_user_ids(self) -> list[int]:
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    "SELECT user_id FROM users "
                    "WHERE user_id NOT IN (SELECT user_id FROM bans) "
                    "ORDER BY user_id"
                )
            ).fetchall()
            return [int(row["user_id"]) for row in rows]

    # ------------------------------------------------------------------ #
    #  Settings
    # ------------------------------------------------------------------ #
    async def get_setting(self, key: str, default: str = "") -> str:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            ).fetchone()
            return str(row["value"]) if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self.connection() as db:
            await db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await db.commit()

    async def get_custom_button(self) -> tuple[str, str]:
        return (
            await self.get_setting("button_text"),
            await self.get_setting("button_url"),
        )

    async def set_custom_button(self, text: str, url: str) -> None:
        await self.set_setting("button_text", text)
        await self.set_setting("button_url", url)

    async def clear_custom_button(self) -> None:
        await self.set_custom_button("", "")

    async def start_photo(self) -> tuple[str, bool]:
        file_id = await self.get_setting("start_photo_file_id")
        spoiler = await self.get_setting("start_photo_spoiler", "1") == "1"
        return file_id, spoiler

    async def set_start_photo(self, file_id: str) -> None:
        await self.set_setting("start_photo_file_id", file_id)

    async def clear_start_photo(self) -> None:
        await self.set_setting("start_photo_file_id", "")

    async def delivery_sticker(self) -> str:
        return await self.get_setting("delivery_sticker_file_id", "")

    async def set_delivery_sticker(self, file_id: str) -> None:
        await self.set_setting("delivery_sticker_file_id", file_id)

    async def clear_delivery_sticker(self) -> None:
        await self.set_setting("delivery_sticker_file_id", "")

    # ------------------------------------------------------------------ #
    #  Stored Files / Posts
    # ------------------------------------------------------------------ #
    async def add_stored_file(
        self, storage_message_id: int, original_name: str, tag: str
    ) -> aiosqlite.Row:
        async with self.connection() as db:
            cursor = await db.execute(
                "INSERT INTO stored_files(storage_message_id, original_name, tag, created_at) "
                "VALUES(?, ?, ?, ?)",
                (storage_message_id, original_name, tag, int(time.time())),
            )
            file_id = int(cursor.lastrowid)
            await db.commit()
            row = await (
                await db.execute("SELECT * FROM stored_files WHERE id=?", (file_id,))
            ).fetchone()
            assert row is not None
            return row

    async def get_stored_files_page(self, offset: int, limit: int) -> list[aiosqlite.Row]:
        async with self.connection() as db:
            return await (
                await db.execute(
                    "SELECT * FROM stored_files ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ).fetchall()

    async def count_stored_files(self) -> int:
        async with self.connection() as db:
            row = await (await db.execute("SELECT COUNT(*) c FROM stored_files")).fetchone()
            return int(row["c"])

    async def get_stored_file(self, file_id: int) -> aiosqlite.Row | None:
        async with self.connection() as db:
            return await (
                await db.execute("SELECT * FROM stored_files WHERE id=?", (file_id,))
            ).fetchone()

    async def delete_stored_file(self, file_id: int) -> bool:
        """Deletes the DB record only (does not delete the message from the
        storage channel — Telegram gives bots no bulk-safe way to guarantee
        that deletion won't break a still-shared link, so this only removes
        the file from being selectable for new links/batches)."""
        async with self.connection() as db:
            cursor = await db.execute("DELETE FROM stored_files WHERE id=?", (file_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def get_files_by_storage_range(
        self, first_id: int, last_id: int
    ) -> list[aiosqlite.Row]:
        low, high = sorted((first_id, last_id))
        async with self.connection() as db:
            return await (
                await db.execute(
                    "SELECT * FROM stored_files "
                    "WHERE storage_message_id BETWEEN ? AND ? "
                    "ORDER BY storage_message_id",
                    (low, high),
                )
            ).fetchall()

    async def get_files_by_db_id_range(
        self, first_id: int, last_id: int
    ) -> list[aiosqlite.Row]:
        low, high = sorted((first_id, last_id))
        async with self.connection() as db:
            return await (
                await db.execute(
                    "SELECT * FROM stored_files WHERE id BETWEEN ? AND ? ORDER BY id",
                    (low, high),
                )
            ).fetchall()

    async def resolve_batch_range(
        self, first_id: int, last_id: int
    ) -> tuple[list[aiosqlite.Row], str]:
        files = await self.get_files_by_storage_range(first_id, last_id)
        if files:
            return files, "storage"
        files = await self.get_files_by_db_id_range(first_id, last_id)
        return files, "db"

    # ------------------------------------------------------------------ #
    #  Optional MongoDB mirror (resilience layer — old links survive a
    #  fresh/reset SQLite database as long as Mongo still has them)
    # ------------------------------------------------------------------ #
    async def _get_mongo(self):
        if not MOTOR_AVAILABLE:
            return None
        if self._mongo_db is not None:
            return self._mongo_db
        if self._mongo_checked:
            return None  # already tried and failed/unset this run
        self._mongo_checked = True
        uri = await self.get_setting("mongo_uri", "")
        if not uri:
            return None
        try:
            dbname = await self.get_setting("mongo_db_name", "filesharebot")
            client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
            await client.server_info()  # force a connection check
            self._mongo_client = client
            self._mongo_db = client[dbname]
            LOG.info("Connected to MongoDB mirror.")
            return self._mongo_db
        except Exception as e:
            LOG.warning(f"MongoDB connection failed, continuing SQLite-only: {e}")
            return None

    async def reset_mongo_connection(self) -> None:
        """Call after changing mongo_uri/mongo_db_name so the new settings take effect."""
        self._mongo_client = None
        self._mongo_db = None
        self._mongo_checked = False

    async def _mirror_post_to_mongo(self, post_row: aiosqlite.Row, files: list[aiosqlite.Row]) -> None:
        mongo_db = await self._get_mongo()
        if mongo_db is None:
            return
        try:
            await mongo_db.posts.update_one(
                {"code": post_row["code"]},
                {"$set": {
                    "code": post_row["code"],
                    "kind": post_row["kind"],
                    "protected": bool(post_row["protected"]),
                    "created_at": int(post_row["created_at"]),
                    "files": [
                        {
                            "storage_message_id": int(f["storage_message_id"]),
                            "original_name": f["original_name"],
                            "tag": f["tag"],
                        }
                        for f in files
                    ],
                }},
                upsert=True,
            )
        except Exception as e:
            LOG.warning(f"Mongo mirror write failed: {e}")

    async def _get_stored_file_by_storage_id(self, storage_message_id: int) -> aiosqlite.Row | None:
        async with self.connection() as db:
            return await (
                await db.execute(
                    "SELECT * FROM stored_files WHERE storage_message_id=?",
                    (storage_message_id,),
                )
            ).fetchone()

    async def _create_post_with_code(
        self, code: str, kind: str, file_ids: list[int], protected: bool, created_at: int
    ) -> aiosqlite.Row:
        async with self.connection() as db:
            cursor = await db.execute(
                "INSERT INTO posts(code, kind, protected, created_at) VALUES(?, ?, ?, ?)",
                (code, kind, 1 if protected else 0, created_at),
            )
            post_id = int(cursor.lastrowid)
            for position, file_id in enumerate(file_ids, start=1):
                await db.execute(
                    "INSERT INTO post_files(post_id, file_id, position) VALUES(?, ?, ?)",
                    (post_id, file_id, position),
                )
            await db.commit()
            row = await (
                await db.execute("SELECT * FROM posts WHERE id=?", (post_id,))
            ).fetchone()
            assert row is not None
            return row

    async def _rehydrate_post_from_mongo(self, code: str) -> aiosqlite.Row | None:
        mongo_db = await self._get_mongo()
        if mongo_db is None:
            return None
        try:
            doc = await mongo_db.posts.find_one({"code": code})
        except Exception as e:
            LOG.warning(f"Mongo lookup failed: {e}")
            return None
        if not doc:
            return None

        file_ids = []
        for f in doc.get("files", []):
            storage_message_id = int(f["storage_message_id"])
            existing = await self._get_stored_file_by_storage_id(storage_message_id)
            if existing:
                file_ids.append(int(existing["id"]))
                continue
            try:
                file_row = await self.add_stored_file(
                    storage_message_id=storage_message_id,
                    original_name=f.get("original_name", "File"),
                    tag=f.get("tag", "single"),
                )
                file_ids.append(int(file_row["id"]))
            except Exception as e:
                LOG.warning(f"Could not rehydrate file {storage_message_id}: {e}")

        if not file_ids:
            return None

        return await self._create_post_with_code(
            code=doc["code"],
            kind=doc.get("kind", "single"),
            file_ids=file_ids,
            protected=bool(doc.get("protected", False)),
            created_at=int(doc.get("created_at", int(time.time()))),
        )

    async def create_post(
        self, kind: str, file_ids: list[int], protected: bool
    ) -> aiosqlite.Row:
        if kind not in {"single", "batch"}:
            raise ValueError(f"Unknown post kind: {kind!r}")
        if not file_ids:
            raise ValueError("A post needs at least one file")
        for _ in range(10):
            code = secrets.token_urlsafe(14).replace("-", "A").replace("_", "B")
            try:
                row = await self._create_post_with_code(
                    code=code, kind=kind, file_ids=file_ids,
                    protected=protected, created_at=int(time.time())
                )
                files = await self.get_post_files(int(row["id"]))
                await self._mirror_post_to_mongo(row, files)
                return row
            except aiosqlite.IntegrityError:
                continue
        raise RuntimeError("Could not generate a unique share code after 10 attempts")

    async def get_post(self, code: str) -> aiosqlite.Row | None:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT * FROM posts WHERE code=?", (code,))
            ).fetchone()
        if row:
            return row
        # Not in SQLite (e.g. fresh/reset DB) — try to recover it from the Mongo mirror
        return await self._rehydrate_post_from_mongo(code)

    async def get_post_files(self, post_id: int) -> list[aiosqlite.Row]:
        async with self.connection() as db:
            return await (
                await db.execute(
                    """
                    SELECT f.* FROM post_files pf
                    JOIN stored_files f ON f.id = pf.file_id
                    WHERE pf.post_id = ?
                    ORDER BY pf.position
                    """,
                    (post_id,),
                )
            ).fetchall()

    # ------------------------------------------------------------------ #
    #  One-Time Pending Tokens (anti-bypass core)
    # ------------------------------------------------------------------ #
    async def create_pending_token(self, post_id: int, user_id: int) -> str:
        now = int(time.time())
        validity_seconds = int(await self.get_setting("token_validity_seconds", "300"))
        expires_at = now + validity_seconds
        async with self.connection() as db:
            await db.execute(
                "DELETE FROM pending_tokens WHERE user_id=? AND post_id=? AND used=0",
                (user_id, post_id),
            )
            await db.commit()
        for _ in range(10):
            token = secrets.token_urlsafe(24).replace("-", "A").replace("_", "B")
            try:
                async with self.connection() as db:
                    await db.execute(
                        """
                        INSERT INTO pending_tokens(
                            token, post_id, user_id, created_at, expires_at, used
                        ) VALUES(?, ?, ?, ?, ?, 0)
                        """,
                        (token, post_id, user_id, now, expires_at),
                    )
                    await db.commit()
                    return token
            except aiosqlite.IntegrityError:
                continue
        raise RuntimeError("Could not generate a unique pending token")

    async def claim_token(
        self, token: str, user_id: int
    ) -> tuple[str, aiosqlite.Row | None]:
        now = int(time.time())
        min_verify_seconds = int(await self.get_setting("min_verify_seconds", "120"))
        async with self.connection() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM pending_tokens WHERE token=?", (token,)
                )
            ).fetchone()
            if not row:
                return "missing", None
            if int(row["expires_at"]) < now:
                return "expired", None
            if int(row["user_id"]) != user_id:
                return "user_mismatch", None
            if int(row["used"]) == 1:
                return "used", None

            elapsed = now - int(row["created_at"])
            if elapsed < min_verify_seconds:
                # Claimed way too fast to have gone through the shortener page —
                # mark it used so the token can't be replayed, but don't deliver.
                await db.execute(
                    "UPDATE pending_tokens SET used=1, claimed_at=? WHERE token=?",
                    (now, token),
                )
                await db.commit()
                return "too_fast", None

            await db.execute(
                "UPDATE pending_tokens SET used=1, claimed_at=? WHERE token=?",
                (now, token),
            )
            await db.commit()
            post = await (
                await db.execute(
                    "SELECT * FROM posts WHERE id=?", (int(row["post_id"]),)
                )
            ).fetchone()
            return "ok", post

database = Database(config.database_path)
