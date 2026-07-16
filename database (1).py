"""SQLite persistence for users, stored files, share links, sessions and settings."""
from __future__ import annotations
import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
import aiosqlite
from config import config

class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init_lock = asyncio.Lock()
        self._initialized = False

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
                await db.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES('link_mode', 'direct')"
                )
                await db.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES('start_photo_spoiler', '1')"
                )
                await db.commit()
            self._initialized = True

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
        raw = await self.get_setting("fsub_channels", "[]")
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    async def set_fsub_channels(self, channels: list[dict]) -> None:
        await self.set_setting("fsub_channels", json.dumps(channels))

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
                async with self.connection() as db:
                    cursor = await db.execute(
                        "INSERT INTO posts(code, kind, protected, created_at) VALUES(?, ?, ?, ?)",
                        (code, kind, 1 if protected else 0, int(time.time())),
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
            except aiosqlite.IntegrityError:
                continue
        raise RuntimeError("Could not generate a unique share code after 10 attempts")

    async def get_post(self, code: str) -> aiosqlite.Row | None:
        async with self.connection() as db:
            return await (
                await db.execute("SELECT * FROM posts WHERE code=?", (code,))
            ).fetchone()

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
