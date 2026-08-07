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
                    CREATE TABLE IF NOT EXISTS favorites (
                        user_id    INTEGER NOT NULL,
                        post_code  TEXT    NOT NULL,
                        created_at INTEGER NOT NULL,
                        PRIMARY KEY(user_id, post_code)
                    );
                    CREATE TABLE IF NOT EXISTS download_history (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id    INTEGER NOT NULL,
                        post_code  TEXT    NOT NULL,
                        file_name  TEXT    NOT NULL DEFAULT '',
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS scheduled_deletions (
                        chat_id    INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        delete_at  INTEGER NOT NULL,
                        PRIMARY KEY(chat_id, message_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_files_storage_msg   ON stored_files(storage_message_id);
                    CREATE INDEX IF NOT EXISTS idx_post_files_post      ON post_files(post_id, position);
                    CREATE INDEX IF NOT EXISTS idx_sessions_post        ON protected_sessions(post_id);
                    CREATE INDEX IF NOT EXISTS idx_pending_tokens_user  ON pending_tokens(user_id, post_id);
                    CREATE INDEX IF NOT EXISTS idx_favorites_user       ON favorites(user_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_downloads_user       ON download_history(user_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_downloads_code       ON download_history(post_code);
                    CREATE INDEX IF NOT EXISTS idx_files_tag            ON stored_files(tag);
                    CREATE INDEX IF NOT EXISTS idx_files_name           ON stored_files(original_name);
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
        raw = await self.get_setting("fsub_channels", "[]")
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    async def set_fsub_channels(self, channels: list[dict]) -> None:
        await self.set_setting("fsub_channels", json.dumps(channels))

    # ------------------------------------------------------------------ #
    #  Moderators (dynamic, DB-managed role tier below ADMIN — see
    #  config.owner_ids / config.admin_ids for the static, env-defined
    #  tiers above it)
    # ------------------------------------------------------------------ #
    async def get_moderator_ids(self) -> set[int]:
        raw = await self.get_setting("moderator_ids", "[]")
        try:
            data = json.loads(raw)
            return {int(x) for x in data} if isinstance(data, list) else set()
        except Exception:
            return set()

    async def add_moderator(self, user_id: int) -> bool:
        mods = await self.get_moderator_ids()
        if user_id in mods:
            return False
        mods.add(user_id)
        await self.set_setting("moderator_ids", json.dumps(sorted(mods)))
        return True

    async def remove_moderator(self, user_id: int) -> bool:
        mods = await self.get_moderator_ids()
        if user_id not in mods:
            return False
        mods.discard(user_id)
        await self.set_setting("moderator_ids", json.dumps(sorted(mods)))
        return True

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

    # ------------------------------------------------------------------ #
    #  Browser-side verification sessions (web.py /g/{code} guard flow).
    #  BUGFIX: web.py called this method but it never existed, so the
    #  guard page's "CLAIM YOUR FILE" click crashed with AttributeError.
    #  Implemented against the pre-existing `protected_sessions` table
    #  (schema was already there, just unused). This does not change the
    #  live shortener/token verification path in bot.py in any way.
    # ------------------------------------------------------------------ #
    async def create_verified_session(self, post_id: int, user_id: int) -> str:
        now = int(time.time())
        expires_at = now + config.session_minutes * 60
        for _ in range(10):
            token = secrets.token_urlsafe(24).replace("-", "A").replace("_", "B")
            try:
                async with self.connection() as db:
                    await db.execute(
                        """
                        INSERT INTO protected_sessions(
                            token, post_id, created_at, expires_at,
                            activated_at, verified_user_id
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (token, post_id, now, expires_at, now, user_id or None),
                    )
                    await db.commit()
                    return token
            except aiosqlite.IntegrityError:
                continue
        raise RuntimeError("Could not generate a unique session token")

    # ------------------------------------------------------------------ #
    #  Favorites
    # ------------------------------------------------------------------ #
    async def add_favorite(self, user_id: int, post_code: str) -> bool:
        async with self.connection() as db:
            try:
                await db.execute(
                    "INSERT INTO favorites(user_id, post_code, created_at) VALUES(?, ?, ?)",
                    (user_id, post_code, int(time.time())),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False  # already favorited — not an error

    async def remove_favorite(self, user_id: int, post_code: str) -> bool:
        async with self.connection() as db:
            cursor = await db.execute(
                "DELETE FROM favorites WHERE user_id=? AND post_code=?",
                (user_id, post_code),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def is_favorite(self, user_id: int, post_code: str) -> bool:
        async with self.connection() as db:
            row = await (
                await db.execute(
                    "SELECT 1 FROM favorites WHERE user_id=? AND post_code=?",
                    (user_id, post_code),
                )
            ).fetchone()
            return row is not None

    async def list_favorites(self, user_id: int, limit: int = 20) -> list[aiosqlite.Row]:
        async with self.connection() as db:
            return await (
                await db.execute(
                    "SELECT * FROM favorites WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit),
                )
            ).fetchall()

    # ------------------------------------------------------------------ #
    #  Download history
    # ------------------------------------------------------------------ #
    async def record_download(self, user_id: int, post_code: str, file_name: str) -> None:
        async with self.connection() as db:
            await db.execute(
                "INSERT INTO download_history(user_id, post_code, file_name, created_at) "
                "VALUES(?, ?, ?, ?)",
                (user_id, post_code, file_name, int(time.time())),
            )
            await db.commit()

    async def get_download_history(self, user_id: int, limit: int = 20) -> list[aiosqlite.Row]:
        async with self.connection() as db:
            return await (
                await db.execute(
                    "SELECT * FROM download_history WHERE user_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit),
                )
            ).fetchall()

    # ------------------------------------------------------------------ #
    #  Smart search / categories (reuses tags.py labels already stored on
    #  stored_files.tag — no change to how files are stored or delivered)
    # ------------------------------------------------------------------ #
    async def search_files(self, query: str, limit: int = 15) -> list[aiosqlite.Row]:
        like = f"%{query.strip()}%"
        async with self.connection() as db:
            return await (
                await db.execute(
                    "SELECT * FROM stored_files "
                    "WHERE original_name LIKE ? COLLATE NOCASE OR tag LIKE ? COLLATE NOCASE "
                    "ORDER BY created_at DESC LIMIT ?",
                    (like, like, limit),
                )
            ).fetchall()

    async def get_categories(self) -> list[tuple[str, int]]:
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    "SELECT tag, COUNT(*) as cnt FROM stored_files "
                    "WHERE tag != '' GROUP BY tag ORDER BY cnt DESC LIMIT 25"
                )
            ).fetchall()
            return [(row["tag"], int(row["cnt"])) for row in rows]

    async def get_files_by_category(self, tag: str, limit: int = 15) -> list[aiosqlite.Row]:
        async with self.connection() as db:
            return await (
                await db.execute(
                    "SELECT * FROM stored_files WHERE tag=? ORDER BY created_at DESC LIMIT ?",
                    (tag, limit),
                )
            ).fetchall()

    # ------------------------------------------------------------------ #
    #  Advanced statistics (admin dashboard)
    # ------------------------------------------------------------------ #
    async def get_advanced_stats(self) -> dict:
        now = int(time.time())
        day = 86400
        today_start = now - (now % day)
        yesterday_start = today_start - day
        week_start = now - 7 * day
        month_start = now - 30 * day

        async with self.connection() as db:
            async def count(sql: str, *params) -> int:
                row = await (await db.execute(sql, params)).fetchone()
                return int(row[0]) if row else 0

            total_users = await count("SELECT COUNT(*) FROM users")
            today_users = await count(
                "SELECT COUNT(*) FROM users WHERE created_at >= ?", today_start
            )
            yesterday_users = await count(
                "SELECT COUNT(*) FROM users WHERE created_at >= ? AND created_at < ?",
                yesterday_start, today_start,
            )
            week_users = await count(
                "SELECT COUNT(*) FROM users WHERE created_at >= ?", week_start
            )
            month_users = await count(
                "SELECT COUNT(*) FROM users WHERE created_at >= ?", month_start
            )
            online_users = await count(
                "SELECT COUNT(*) FROM users WHERE last_seen >= ?", now - 900
            )
            total_files = await count("SELECT COUNT(*) FROM stored_files")
            total_posts = await count("SELECT COUNT(*) FROM posts")
            total_downloads = await count("SELECT COUNT(*) FROM download_history")
            total_verifications = await count(
                "SELECT COALESCE(SUM(verification_count), 0) FROM users"
            )
            total_bans = await count("SELECT COUNT(*) FROM bans")

            top_row = await (
                await db.execute(
                    "SELECT post_code, COUNT(*) as cnt FROM download_history "
                    "GROUP BY post_code ORDER BY cnt DESC LIMIT 5"
                )
            ).fetchall()
            most_downloaded = [(r["post_code"], int(r["cnt"])) for r in top_row]

        return {
            "total_users": total_users,
            "today_users": today_users,
            "yesterday_users": yesterday_users,
            "week_users": week_users,
            "month_users": month_users,
            "online_users": online_users,
            "total_files": total_files,
            "total_posts": total_posts,
            "total_downloads": total_downloads,
            "total_verifications": total_verifications,
            "total_bans": total_bans,
            "most_downloaded": most_downloaded,
        }

    # ------------------------------------------------------------------ #
    #  Backup / Restore — full JSON snapshot, insert-only restore so it
    #  can NEVER overwrite or delete existing rows.
    # ------------------------------------------------------------------ #
    _BACKUP_TABLES = (
        "users", "bans", "strikes", "settings", "stored_files",
        "posts", "post_files", "pending_tokens", "protected_sessions",
        "favorites", "download_history",
    )

    async def export_backup(self) -> dict:
        backup: dict = {"exported_at": int(time.time()), "tables": {}}
        async with self.connection() as db:
            for table in self._BACKUP_TABLES:
                rows = await (await db.execute(f"SELECT * FROM {table}")).fetchall()
                backup["tables"][table] = [dict(row) for row in rows]
        return backup

    async def restore_backup(self, backup: dict) -> dict:
        """Insert-only restore. Existing rows are NEVER touched or overwritten —
        only rows missing from the current database are inserted. Safe to run
        against a live database."""
        report = {}
        async with self.connection() as db:
            for table, rows in backup.get("tables", {}).items():
                if table not in self._BACKUP_TABLES or not rows:
                    report[table] = 0
                    continue
                inserted = 0
                for row in rows:
                    cols = ", ".join(row.keys())
                    placeholders = ", ".join("?" for _ in row)
                    try:
                        await db.execute(
                            f"INSERT OR IGNORE INTO {table}({cols}) VALUES({placeholders})",
                            tuple(row.values()),
                        )
                        inserted += 1
                    except aiosqlite.Error as exc:
                        LOG.warning(f"Restore skip row in {table}: {exc}")
                report[table] = inserted
            await db.commit()
        return report

    # ------------------------------------------------------------------ #
    #  Persistent auto-delete — survives bot restarts. Previously,
    #  scheduled deletions lived only in an in-memory asyncio task, so a
    #  restart before the timer fired meant that message was never
    #  cleaned up. Now every scheduled deletion is durably recorded and
    #  swept back in on startup (see main.py).
    # ------------------------------------------------------------------ #
    async def add_scheduled_deletion(self, chat_id: int, message_id: int, delete_at: int) -> None:
        async with self.connection() as db:
            await db.execute(
                "INSERT OR REPLACE INTO scheduled_deletions(chat_id, message_id, delete_at) "
                "VALUES(?, ?, ?)",
                (chat_id, message_id, delete_at),
            )
            await db.commit()

    async def remove_scheduled_deletion(self, chat_id: int, message_id: int) -> None:
        async with self.connection() as db:
            await db.execute(
                "DELETE FROM scheduled_deletions WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )
            await db.commit()

    async def get_all_scheduled_deletions(self) -> list[aiosqlite.Row]:
        async with self.connection() as db:
            return await (
                await db.execute("SELECT * FROM scheduled_deletions ORDER BY delete_at ASC")
            ).fetchall()

    # ------------------------------------------------------------------ #
    #  Automatic cleanup — purges rows that are provably no longer useful:
    #  expired/used pending tokens, expired protected sessions, and
    #  bypass_events older than 24h (kept only for strike-window de-dup).
    #  NEVER touches users, bans, strikes, posts, stored_files, favorites,
    #  or download_history — those are permanent records.
    # ------------------------------------------------------------------ #
    async def cleanup_expired(self) -> dict[str, int]:
        now = int(time.time())
        report = {}
        async with self.connection() as db:
            cur = await db.execute(
                "DELETE FROM pending_tokens WHERE expires_at < ? OR used = 1", (now,)
            )
            report["pending_tokens"] = cur.rowcount or 0

            cur = await db.execute(
                "DELETE FROM protected_sessions WHERE expires_at < ?", (now,)
            )
            report["protected_sessions"] = cur.rowcount or 0

            cur = await db.execute(
                "DELETE FROM bypass_events WHERE created_at < ?", (now - 86400,)
            )
            report["bypass_events"] = cur.rowcount or 0

            # Belt-and-suspenders: scheduled_deletions rows older than 2 days
            # past their delete_at mean the delete task somehow never ran
            # (e.g. crash loop) — safe to drop the bookkeeping row itself,
            # it does NOT delete the Telegram message.
            cur = await db.execute(
                "DELETE FROM scheduled_deletions WHERE delete_at < ?", (now - 2 * 86400,)
            )
            report["scheduled_deletions"] = cur.rowcount or 0

            await db.commit()
        return report

# --- Backend selection --------------------------------------------------
# Default (no DB_BACKEND env var, or DB_BACKEND=sqlite): behavior is
# IDENTICAL to before this change — a plain SQLite Database() singleton.
# Only if the operator explicitly sets DB_BACKEND=mongo does this switch
# to the Motor-backed MongoDatabase from db_mongo.py. Existing deployments
# are unaffected unless they opt in.
if config.db_backend == "mongo":
    from db_mongo import get_database
    database = get_database()
else:
    database = Database(config.database_path)

