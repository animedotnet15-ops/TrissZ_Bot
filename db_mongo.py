"""MongoDB (Motor) persistence backend.

Implements the SAME public async method signatures as `database.Database`
(the SQLite backend) so `bot.py` / `web.py` never need to know which
backend is active. Selected via DB_BACKEND=mongo in the environment;
the default (`sqlite`) is completely unaffected by this file's existence.

Row-like objects: SQLite methods return aiosqlite.Row (dict-like, supports
row["col"] and row[0]). To keep call sites unchanged, Mongo documents are
wrapped in `_Doc`, a minimal dict-like shim supporting the same access
patterns actually used elsewhere in the codebase.
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Iterable

from motor.motor_asyncio import AsyncIOMotorClient

from config import config

LOG = logging.getLogger("db_mongo")


class _Doc(dict):
    """dict subclass so both row["col"] and row.col-style .get() work,
    matching how aiosqlite.Row is used throughout bot.py/web.py."""
    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc


def _wrap(doc: dict | None) -> _Doc | None:
    return _Doc(doc) if doc is not None else None


class MongoDatabase:
    def __init__(self, uri: str, db_name: str) -> None:
        self._uri = uri
        self._db_name = db_name
        self._client: AsyncIOMotorClient | None = None
        self._db = None
        self._initialized = False

    def _c(self, name: str):
        return self._db[name]

    async def init(self) -> None:
        if self._initialized:
            return
        self._client = AsyncIOMotorClient(self._uri, serverSelectionTimeoutMS=8000)
        await self._client.server_info()  # fail fast if unreachable
        self._db = self._client[self._db_name]

        # Indexes — created if missing, never destructive.
        await self._c("users").create_index("user_id", unique=True)
        await self._c("bans").create_index("user_id", unique=True)
        await self._c("strikes").create_index("user_id", unique=True)
        await self._c("bypass_events").create_index(
            [("event_key", 1), ("user_id", 1)], unique=True
        )
        await self._c("settings").create_index("key", unique=True)
        await self._c("stored_files").create_index("storage_message_id", unique=True)
        await self._c("stored_files").create_index("tag")
        await self._c("stored_files").create_index("original_name")
        await self._c("posts").create_index("code", unique=True)
        await self._c("post_files").create_index([("post_id", 1), ("position", 1)])
        await self._c("pending_tokens").create_index("token", unique=True)
        await self._c("pending_tokens").create_index([("user_id", 1), ("post_id", 1)])
        await self._c("protected_sessions").create_index("token", unique=True)
        await self._c("protected_sessions").create_index("post_id")
        await self._c("favorites").create_index(
            [("user_id", 1), ("post_code", 1)], unique=True
        )
        await self._c("download_history").create_index(
            [("user_id", 1), ("created_at", -1)]
        )
        await self._c("download_history").create_index("post_code")
        await self._c("scheduled_deletions").create_index(
            [("chat_id", 1), ("message_id", 1)], unique=True
        )
        await self._c("scheduled_deletions").create_index("delete_at")
        await self._c("link_analytics").create_index([("post_id", 1), ("created_at", 1)])
        await self._c("custom_batches").create_index("id", unique=True)
        await self._c("custom_batches").create_index([("created_by", 1), ("status", 1)])
        await self._c("button_configs").create_index("id", unique=True)
        await self._c("caption_templates").create_index([("scope", 1), ("name", 1)], unique=True)
        await self._c("thumbnail_configs").create_index("scope", unique=True)
        await self._c("broadcasts").create_index("id", unique=True)

        await self._c("settings").update_one(
            {"key": "link_mode"}, {"$setOnInsert": {"key": "link_mode", "value": "direct"}},
            upsert=True,
        )
        await self._c("settings").update_one(
            {"key": "start_photo_spoiler"},
            {"$setOnInsert": {"key": "start_photo_spoiler", "value": "1"}},
            upsert=True,
        )
        self._initialized = True
        LOG.info("MongoDB backend initialised (db=%s).", self._db_name)

    # ------------------------------------------------------------------ #
    #  Users / Bans / Strikes
    # ------------------------------------------------------------------ #
    async def touch_user(self, user_id: int, first_name: str, username: str) -> None:
        now = int(time.time())
        await self._c("users").update_one(
            {"user_id": user_id},
            {
                "$set": {"first_name": first_name, "username": username, "last_seen": now},
                "$setOnInsert": {
                    "user_id": user_id, "created_at": now,
                    "verification_count": 0, "download_count": 0,
                    "referral_count": 0, "referred_by": None, "premium_until": 0,
                },
            },
            upsert=True,
        )

    async def increment_verification_count(self, user_id: int) -> None:
        await self._c("users").update_one(
            {"user_id": user_id}, {"$inc": {"verification_count": 1}}
        )

    async def increment_download_count(self, user_id: int) -> None:
        await self._c("users").update_one(
            {"user_id": user_id}, {"$inc": {"download_count": 1}}
        )

    async def set_referrer(self, user_id: int, referred_by: int) -> bool:
        if user_id == referred_by:
            return False
        user = await self._c("users").find_one({"user_id": user_id})
        if user is None or user.get("referred_by") is not None:
            return False
        result = await self._c("users").update_one(
            {"user_id": user_id, "referred_by": None},
            {"$set": {"referred_by": referred_by}},
        )
        if result.modified_count == 0:
            return False
        await self._c("users").update_one(
            {"user_id": referred_by}, {"$inc": {"referral_count": 1}}
        )
        return True

    async def get_profile(self, user_id: int) -> dict | None:
        user = await self._c("users").find_one({"user_id": user_id})
        if not user:
            return None
        strike = await self._c("strikes").find_one({"user_id": user_id})
        banned = await self._c("bans").find_one({"user_id": user_id})
        return {
            "user_id": user["user_id"],
            "first_name": user.get("first_name", ""),
            "username": user.get("username", ""),
            "joined_at": user.get("created_at", 0),
            "last_seen": user.get("last_seen", 0),
            "verification_count": user.get("verification_count", 0),
            "download_count": user.get("download_count", 0),
            "referral_count": user.get("referral_count", 0),
            "premium_until": user.get("premium_until", 0),
            "warnings": strike["count"] if strike else 0,
            "banned": banned is not None,
        }

    async def is_banned(self, user_id: int) -> bool:
        return await self._c("bans").find_one({"user_id": user_id}) is not None

    async def ban_user(self, user_id: int, reason: str) -> None:
        await self._c("bans").update_one(
            {"user_id": user_id},
            {"$set": {"user_id": user_id, "reason": reason, "created_at": int(time.time())}},
            upsert=True,
        )

    async def unban_user(self, user_id: int) -> bool:
        result = await self._c("bans").delete_one({"user_id": user_id})
        return result.deleted_count > 0

    async def get_strikes(self, user_id: int) -> int:
        row = await self._c("strikes").find_one({"user_id": user_id})
        return int(row["count"]) if row else 0

    async def reset_strikes(self, user_id: int) -> bool:
        result = await self._c("strikes").delete_one({"user_id": user_id})
        return result.deleted_count > 0

    async def record_bypass(self, user_id: int, event_key: str, reason: str) -> tuple[int, bool, bool]:
        now = int(time.time())
        try:
            await self._c("bypass_events").insert_one(
                {"event_key": event_key, "user_id": user_id, "created_at": now}
            )
        except Exception:
            row = await self._c("strikes").find_one({"user_id": user_id})
            count = int(row["count"]) if row else 0
            banned = await self.is_banned(user_id)
            return count, banned, False

        strike_doc = await self._c("strikes").find_one_and_update(
            {"user_id": user_id},
            {"$inc": {"count": 1}, "$set": {"last_reason": reason, "updated_at": now}},
            upsert=True,
            return_document=True,
        )
        count = int(strike_doc["count"])
        banned = count >= config.strike_limit
        if banned:
            await self.ban_user(user_id, f"Auto-ban after {count} bypass strikes")
        return count, banned, True

    async def get_user_by_username(self, username: str):
        username = username.lstrip("@").strip()
        doc = await self._c("users").find_one(
            {"username": {"$regex": f"^{username}$", "$options": "i"}}
        )
        return _wrap(doc)

    async def get_fsub_channels(self) -> list[dict]:
        import json
        raw = await self.get_setting("fsub_channels", "[]")
        try:
            data = json.loads(raw)
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        for ch in data:
            ch.setdefault("enabled", True)
            ch.setdefault("folder", "")
        return data

    async def set_fsub_channels(self, channels: list[dict]) -> None:
        import json
        await self.set_setting("fsub_channels", json.dumps(channels))

    async def get_enabled_fsub_channels(self) -> list[dict]:
        channels = await self.get_fsub_channels()
        for ch in channels:
            ch.setdefault("enabled", True)
            ch.setdefault("folder", "")
        return [c for c in channels if c.get("enabled", True)]

    async def toggle_fsub_channel(self, index: int) -> bool:
        channels = await self.get_fsub_channels()
        if not (0 <= index < len(channels)):
            return False
        channels[index]["enabled"] = not channels[index].get("enabled", True)
        await self.set_fsub_channels(channels)
        return True

    async def update_fsub_channel(self, index: int, **kwargs) -> bool:
        channels = await self.get_fsub_channels()
        if not (0 <= index < len(channels)):
            return False
        channels[index].update(kwargs)
        await self.set_fsub_channels(channels)
        return True

    async def reorder_fsub_channel(self, index: int, direction: int) -> bool:
        channels = await self.get_fsub_channels()
        new_index = index + direction
        if not (0 <= index < len(channels)) or not (0 <= new_index < len(channels)):
            return False
        channels[index], channels[new_index] = channels[new_index], channels[index]
        await self.set_fsub_channels(channels)
        return True

    async def remove_fsub_channel_at(self, index: int) -> bool:
        channels = await self.get_fsub_channels()
        if not (0 <= index < len(channels)):
            return False
        channels.pop(index)
        await self.set_fsub_channels(channels)
        return True

    async def get_moderator_ids(self) -> set[int]:
        import json
        raw = await self.get_setting("moderator_ids", "[]")
        try:
            data = json.loads(raw)
            return {int(x) for x in data} if isinstance(data, list) else set()
        except Exception:
            return set()

    async def add_moderator(self, user_id: int) -> bool:
        import json
        mods = await self.get_moderator_ids()
        if user_id in mods:
            return False
        mods.add(user_id)
        await self.set_setting("moderator_ids", json.dumps(sorted(mods)))
        return True

    async def remove_moderator(self, user_id: int) -> bool:
        import json
        mods = await self.get_moderator_ids()
        if user_id not in mods:
            return False
        mods.discard(user_id)
        await self.set_setting("moderator_ids", json.dumps(sorted(mods)))
        return True

    async def broadcast_user_ids(self) -> list[int]:
        banned_ids = {b["user_id"] async for b in self._c("bans").find({}, {"user_id": 1})}
        ids = []
        async for u in self._c("users").find({}, {"user_id": 1}).sort("user_id", 1):
            if u["user_id"] not in banned_ids:
                ids.append(int(u["user_id"]))
        return ids

    # ------------------------------------------------------------------ #
    #  Settings
    # ------------------------------------------------------------------ #
    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self._c("settings").find_one({"key": key})
        return str(row["value"]) if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self._c("settings").update_one(
            {"key": key}, {"$set": {"value": value}}, upsert=True
        )

    async def get_custom_button(self) -> tuple[str, str]:
        return await self.get_setting("button_text"), await self.get_setting("button_url")

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
    async def add_stored_file(self, storage_message_id: int, original_name: str, tag: str):
        now = int(time.time())
        counter = await self._c("_counters").find_one_and_update(
            {"_id": "stored_files"}, {"$inc": {"seq": 1}},
            upsert=True, return_document=True,
        )
        new_id = int(counter["seq"])
        doc = {
            "id": new_id, "storage_message_id": storage_message_id,
            "original_name": original_name, "tag": tag, "created_at": now,
        }
        await self._c("stored_files").insert_one(doc)
        return _wrap(doc)

    async def get_files_by_storage_range(self, first_id: int, last_id: int) -> list:
        low, high = sorted((first_id, last_id))
        cursor = self._c("stored_files").find(
            {"storage_message_id": {"$gte": low, "$lte": high}}
        ).sort("storage_message_id", 1)
        return [_wrap(d) for d in await cursor.to_list(length=None)]

    async def get_files_by_db_id_range(self, first_id: int, last_id: int) -> list:
        low, high = sorted((first_id, last_id))
        cursor = self._c("stored_files").find({"id": {"$gte": low, "$lte": high}}).sort("id", 1)
        return [_wrap(d) for d in await cursor.to_list(length=None)]

    async def resolve_batch_range(self, first_id: int, last_id: int) -> tuple[list, str]:
        files = await self.get_files_by_storage_range(first_id, last_id)
        if files:
            return files, "storage"
        files = await self.get_files_by_db_id_range(first_id, last_id)
        return files, "db"

    async def create_post(self, kind: str, file_ids: list[int], protected: bool):
        if kind not in {"single", "batch"}:
            raise ValueError(f"Unknown post kind: {kind!r}")
        if not file_ids:
            raise ValueError("A post needs at least one file")
        counter = await self._c("_counters").find_one_and_update(
            {"_id": "posts"}, {"$inc": {"seq": 1}}, upsert=True, return_document=True,
        )
        post_id = int(counter["seq"])
        for _ in range(10):
            code = secrets.token_urlsafe(14).replace("-", "A").replace("_", "B")
            try:
                doc = {
                    "id": post_id, "code": code, "kind": kind,
                    "protected": bool(protected), "created_at": int(time.time()),
                    "file_ids": list(file_ids), "revoked": False,
                }
                await self._c("posts").insert_one(doc)
                return _wrap(doc)
            except Exception:
                continue
        raise RuntimeError("Could not generate a unique share code after 10 attempts")

    async def get_post(self, code: str):
        doc = await self._c("posts").find_one({"code": code})
        if doc and doc.get("revoked"):
            return None
        return _wrap(doc)

    async def revoke_post(self, code: str) -> bool:
        result = await self._c("posts").update_one({"code": code}, {"$set": {"revoked": True}})
        return result.modified_count > 0

    async def get_post_files(self, post_id: int) -> list:
        post = await self._c("posts").find_one({"id": post_id})
        if not post:
            return []
        files = []
        for fid in post.get("file_ids", []):
            f = await self._c("stored_files").find_one({"id": fid})
            if f:
                files.append(_wrap(f))
        return files

    # ------------------------------------------------------------------ #
    #  One-Time Pending Tokens (anti-bypass core — identical timing rules
    #  to the SQLite backend; min/max verification windows unchanged)
    # ------------------------------------------------------------------ #
    async def create_pending_token(self, post_id: int, user_id: int) -> str:
        now = int(time.time())
        validity_seconds = int(await self.get_setting("token_validity_seconds", "300"))
        expires_at = now + validity_seconds
        await self._c("pending_tokens").delete_many(
            {"user_id": user_id, "post_id": post_id, "used": 0}
        )
        for _ in range(10):
            token = secrets.token_urlsafe(24).replace("-", "A").replace("_", "B")
            try:
                await self._c("pending_tokens").insert_one({
                    "token": token, "post_id": post_id, "user_id": user_id,
                    "created_at": now, "expires_at": expires_at, "used": 0,
                    "claimed_at": None,
                })
                return token
            except Exception:
                continue
        raise RuntimeError("Could not generate a unique pending token")

    async def claim_token(self, token: str, user_id: int) -> tuple[str, Any]:
        now = int(time.time())
        min_verify_seconds = int(await self.get_setting("min_verify_seconds", "120"))
        row = await self._c("pending_tokens").find_one({"token": token})
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
            await self._c("pending_tokens").update_one(
                {"token": token}, {"$set": {"used": 1, "claimed_at": now}}
            )
            return "too_fast", None

        await self._c("pending_tokens").update_one(
            {"token": token}, {"$set": {"used": 1, "claimed_at": now}}
        )
        post = await self._c("posts").find_one({"id": int(row["post_id"])})
        return "ok", _wrap(post)

    async def create_verified_session(self, post_id: int, user_id: int) -> str:
        now = int(time.time())
        expires_at = now + config.session_minutes * 60
        for _ in range(10):
            token = secrets.token_urlsafe(24).replace("-", "A").replace("_", "B")
            try:
                await self._c("protected_sessions").insert_one({
                    "token": token, "post_id": post_id, "created_at": now,
                    "expires_at": expires_at, "activated_at": now,
                    "verified_user_id": user_id or None,
                })
                return token
            except Exception:
                continue
        raise RuntimeError("Could not generate a unique session token")

    # ------------------------------------------------------------------ #
    #  Favorites
    # ------------------------------------------------------------------ #
    async def add_favorite(self, user_id: int, post_code: str) -> bool:
        try:
            await self._c("favorites").insert_one(
                {"user_id": user_id, "post_code": post_code, "created_at": int(time.time())}
            )
            return True
        except Exception:
            return False

    async def remove_favorite(self, user_id: int, post_code: str) -> bool:
        result = await self._c("favorites").delete_one(
            {"user_id": user_id, "post_code": post_code}
        )
        return result.deleted_count > 0

    async def is_favorite(self, user_id: int, post_code: str) -> bool:
        return await self._c("favorites").find_one(
            {"user_id": user_id, "post_code": post_code}
        ) is not None

    async def list_favorites(self, user_id: int, limit: int = 20) -> list:
        cursor = self._c("favorites").find({"user_id": user_id}).sort("created_at", -1).limit(limit)
        return [_wrap(d) for d in await cursor.to_list(length=limit)]

    # ------------------------------------------------------------------ #
    #  Download history
    # ------------------------------------------------------------------ #
    async def record_download(self, user_id: int, post_code: str, file_name: str) -> None:
        await self._c("download_history").insert_one({
            "user_id": user_id, "post_code": post_code,
            "file_name": file_name, "created_at": int(time.time()),
        })

    async def get_download_history(self, user_id: int, limit: int = 20) -> list:
        cursor = self._c("download_history").find({"user_id": user_id}) \
            .sort("created_at", -1).limit(limit)
        return [_wrap(d) for d in await cursor.to_list(length=limit)]

    # ------------------------------------------------------------------ #
    #  Smart search / categories
    # ------------------------------------------------------------------ #
    async def search_files(self, query: str, limit: int = 15) -> list:
        q = query.strip()
        cursor = self._c("stored_files").find({
            "$or": [
                {"original_name": {"$regex": q, "$options": "i"}},
                {"tag": {"$regex": q, "$options": "i"}},
            ]
        }).sort("created_at", -1).limit(limit)
        return [_wrap(d) for d in await cursor.to_list(length=limit)]

    async def get_categories(self) -> list[tuple[str, int]]:
        pipeline = [
            {"$match": {"tag": {"$ne": ""}}},
            {"$group": {"_id": "$tag", "cnt": {"$sum": 1}}},
            {"$sort": {"cnt": -1}},
            {"$limit": 25},
        ]
        results = await self._c("stored_files").aggregate(pipeline).to_list(length=25)
        return [(r["_id"], int(r["cnt"])) for r in results]

    async def get_files_by_category(self, tag: str, limit: int = 15) -> list:
        cursor = self._c("stored_files").find({"tag": tag}).sort("created_at", -1).limit(limit)
        return [_wrap(d) for d in await cursor.to_list(length=limit)]

    # ------------------------------------------------------------------ #
    #  Advanced statistics
    # ------------------------------------------------------------------ #
    async def get_advanced_stats(self) -> dict:
        now = int(time.time())
        day = 86400
        today_start = now - (now % day)
        yesterday_start = today_start - day
        week_start = now - 7 * day
        month_start = now - 30 * day

        total_users = await self._c("users").count_documents({})
        today_users = await self._c("users").count_documents({"created_at": {"$gte": today_start}})
        yesterday_users = await self._c("users").count_documents(
            {"created_at": {"$gte": yesterday_start, "$lt": today_start}}
        )
        week_users = await self._c("users").count_documents({"created_at": {"$gte": week_start}})
        month_users = await self._c("users").count_documents({"created_at": {"$gte": month_start}})
        online_users = await self._c("users").count_documents({"last_seen": {"$gte": now - 900}})
        total_files = await self._c("stored_files").count_documents({})
        total_posts = await self._c("posts").count_documents({})
        total_downloads = await self._c("download_history").count_documents({})
        total_bans = await self._c("bans").count_documents({})

        agg = await self._c("users").aggregate(
            [{"$group": {"_id": None, "s": {"$sum": "$verification_count"}}}]
        ).to_list(length=1)
        total_verifications = int(agg[0]["s"]) if agg else 0

        top = await self._c("download_history").aggregate([
            {"$group": {"_id": "$post_code", "cnt": {"$sum": 1}}},
            {"$sort": {"cnt": -1}}, {"$limit": 5},
        ]).to_list(length=5)
        most_downloaded = [(r["_id"], int(r["cnt"])) for r in top]

        return {
            "total_users": total_users, "today_users": today_users,
            "yesterday_users": yesterday_users, "week_users": week_users,
            "month_users": month_users, "online_users": online_users,
            "total_files": total_files, "total_posts": total_posts,
            "total_downloads": total_downloads,
            "total_verifications": total_verifications,
            "total_bans": total_bans, "most_downloaded": most_downloaded,
        }

    # ------------------------------------------------------------------ #
    #  Force-Subscribe folders/labels/enable are stored inside the
    #  `fsub_channels` JSON setting (see get/set/toggle/reorder/update
    #  _fsub_channel above) — already covered by the `settings` collection
    #  backup, no separate collection needed.
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    #  FEATURE 3: Button Manager
    # ------------------------------------------------------------------ #
    async def list_buttons(self) -> list[dict]:
        cursor = self._c("button_configs").find({}).sort("position", 1)
        return [_wrap(d) for d in await cursor.to_list(length=None)]

    async def add_button(self, text: str, url: str = "", callback: str = "") -> int:
        counter = await self._c("_counters").find_one_and_update(
            {"_id": "button_configs"}, {"$inc": {"seq": 1}}, upsert=True, return_document=True,
        )
        new_id = int(counter["seq"])
        last = await self._c("button_configs").find_one(sort=[("position", -1)])
        pos = (last["position"] + 1) if last else 0
        await self._c("button_configs").insert_one({
            "id": new_id, "text": text, "url": url, "callback": callback,
            "position": pos, "enabled": 1,
        })
        return new_id

    async def delete_button(self, btn_id: int) -> bool:
        result = await self._c("button_configs").delete_one({"id": btn_id})
        return result.deleted_count > 0

    async def toggle_button(self, btn_id: int) -> None:
        btn = await self._c("button_configs").find_one({"id": btn_id})
        if btn:
            await self._c("button_configs").update_one(
                {"id": btn_id}, {"$set": {"enabled": 0 if btn.get("enabled", 1) else 1}}
            )

    async def update_button(self, btn_id: int, **kwargs) -> bool:
        allowed = {k: v for k, v in kwargs.items() if k in ("text", "url", "callback")}
        if not allowed:
            return False
        result = await self._c("button_configs").update_one({"id": btn_id}, {"$set": allowed})
        return result.modified_count > 0

    async def reorder_button(self, btn_id: int, direction: int) -> None:
        btn = await self._c("button_configs").find_one({"id": btn_id})
        if not btn:
            return
        cur_pos = btn["position"]
        new_pos = cur_pos + direction
        if new_pos < 0:
            return
        other = await self._c("button_configs").find_one({"position": new_pos})
        if other:
            await self._c("button_configs").update_one(
                {"id": other["id"]}, {"$set": {"position": cur_pos}}
            )
        await self._c("button_configs").update_one({"id": btn_id}, {"$set": {"position": new_pos}})

    # ------------------------------------------------------------------ #
    #  FEATURE 4: Welcome Manager — text/photo/sticker/spoiler continue to
    #  live in `settings` (same as SQLite backend, same reasoning: that's
    #  where they already lived, nothing gets duplicated or lost). The
    #  extra animation/enable toggles live in a single `welcome_config`
    #  document (id=1).
    # ------------------------------------------------------------------ #
    async def get_welcome_config(self) -> dict:
        photo_id, spoiler = await self.start_photo()
        sticker_id = await self.delivery_sticker()
        text = await self.get_setting("custom_welcome_html", "")
        extra = await self._c("welcome_config").find_one({"_id": 1}) or {}
        return {
            "id": 1,
            "text": text,
            "photo_id": photo_id or None,
            "sticker_id": sticker_id or None,
            "spoiler": bool(spoiler),
            "enabled": bool(extra.get("enabled", True)),
            "anim_enabled": bool(extra.get("anim_enabled", True)),
            "anim_type": extra.get("anim_type") or "line",
            "anim_speed": extra.get("anim_speed") or "normal",
            "sticker_anim_enabled": bool(extra.get("sticker_anim_enabled", True)),
            "updated_at": extra.get("updated_at"),
        }

    async def update_welcome_config(self, **kwargs) -> dict:
        if "text" in kwargs:
            await self.set_setting("custom_welcome_html", kwargs.pop("text") or "")
        if "photo_id" in kwargs:
            val = kwargs.pop("photo_id")
            if val:
                await self.set_start_photo(val)
            else:
                await self.clear_start_photo()
        if "sticker_id" in kwargs:
            val = kwargs.pop("sticker_id")
            if val:
                await self.set_delivery_sticker(val)
            else:
                await self.clear_delivery_sticker()
        if "spoiler" in kwargs:
            await self.set_setting("start_photo_spoiler", "1" if kwargs.pop("spoiler") else "0")
        if kwargs:
            allowed = {
                k: (bool(v) if k in ("enabled", "anim_enabled", "sticker_anim_enabled") else v)
                for k, v in kwargs.items()
                if k in ("enabled", "anim_enabled", "anim_type", "anim_speed", "sticker_anim_enabled")
            }
            if allowed:
                allowed["updated_at"] = int(time.time())
                await self._c("welcome_config").update_one(
                    {"_id": 1}, {"$set": allowed}, upsert=True
                )
        return await self.get_welcome_config()

    async def reset_welcome_config(self) -> None:
        await self.set_setting("custom_welcome_html", "")
        await self.clear_start_photo()
        await self.clear_delivery_sticker()
        await self.set_setting("start_photo_spoiler", "1")
        await self._c("welcome_config").delete_one({"_id": 1})

    # ------------------------------------------------------------------ #
    #  FEATURE 2: First Message -> Last Message Custom Batch. One document
    #  per batch (files stored inline) — the Mongo-native equivalent of
    #  the SQLite custom_batches/custom_batch_files pair, same fields.
    # ------------------------------------------------------------------ #
    async def _next_batch_id(self) -> int:
        counter = await self._c("_counters").find_one_and_update(
            {"_id": "custom_batches"}, {"$inc": {"seq": 1}}, upsert=True, return_document=True,
        )
        return int(counter["seq"])

    async def start_message_batch(self, created_by: int, first_message_id: int) -> int:
        batch_id = await self._next_batch_id()
        await self._c("custom_batches").insert_one({
            "id": batch_id, "name": f"batch-{int(time.time())}", "description": "",
            "thumbnail_id": None, "created_by": created_by, "created_at": int(time.time()),
            "first_message_id": first_message_id, "last_message_id": None,
            "status": "collecting", "completed_at": None, "post_code": None,
            "file_ids": [],
        })
        return batch_id

    async def get_active_message_batch(self, created_by: int) -> dict | None:
        doc = await self._c("custom_batches").find_one(
            {"created_by": created_by, "status": "collecting"}, sort=[("id", -1)]
        )
        return dict(doc) if doc else None

    async def add_file_to_custom_batch(self, batch_id: int, file_id: int) -> bool:
        await self._c("custom_batches").update_one(
            {"id": batch_id}, {"$addToSet": {"file_ids": file_id}}
        )
        return True

    async def finish_message_batch(self, batch_id: int, last_message_id: int) -> bool:
        result = await self._c("custom_batches").update_one(
            {"id": batch_id, "status": "collecting"},
            {"$set": {
                "status": "completed", "last_message_id": last_message_id,
                "completed_at": int(time.time()),
            }},
        )
        return result.modified_count > 0

    async def cancel_message_batch(self, batch_id: int) -> bool:
        result = await self._c("custom_batches").update_one(
            {"id": batch_id, "status": "collecting"},
            {"$set": {"status": "cancelled", "completed_at": int(time.time())}},
        )
        return result.modified_count > 0

    async def set_batch_post_code(self, batch_id: int, code: str) -> None:
        await self._c("custom_batches").update_one({"id": batch_id}, {"$set": {"post_code": code}})

    async def get_custom_batch(self, batch_id: int) -> dict | None:
        doc = await self._c("custom_batches").find_one({"id": batch_id})
        if not doc:
            return None
        batch = dict(doc)
        files = []
        for fid in batch.get("file_ids", []):
            f = await self._c("stored_files").find_one({"id": fid})
            if f:
                files.append(_wrap(f))
        batch["files"] = files
        return batch

    async def list_custom_batches(self, created_by: int = None) -> list[dict]:
        query = {"created_by": created_by} if created_by else {}
        cursor = self._c("custom_batches").find(query).sort("id", -1)
        return [dict(d) for d in await cursor.to_list(length=None)]

    async def delete_custom_batch(self, batch_id: int) -> bool:
        result = await self._c("custom_batches").delete_one({"id": batch_id})
        return result.deleted_count > 0

    # ------------------------------------------------------------------ #
    #  Backup / Restore (insert-only) — covers every collection currently
    #  used by the repository, including the ones added for Force-Sub
    #  folders (settings), Custom Batch, Button Manager and Welcome
    #  Manager above.
    # ------------------------------------------------------------------ #
    _BACKUP_COLLECTIONS = (
        "users", "bans", "strikes", "bypass_events", "settings", "stored_files",
        "posts", "pending_tokens", "protected_sessions",
        "favorites", "download_history", "scheduled_deletions",
        "link_analytics", "custom_batches", "caption_templates",
        "thumbnail_configs", "button_configs", "welcome_config", "broadcasts",
    )
    _BACKUP_SCHEMA_VERSION = 2

    async def export_backup(self) -> dict:
        backup: dict = {
            "schema_version": self._BACKUP_SCHEMA_VERSION,
            "backend": "mongo",
            "exported_at": int(time.time()),
            "tables": {},
        }
        for coll in self._BACKUP_COLLECTIONS:
            docs = await self._c(coll).find({}, {"_id": 0}).to_list(length=None)
            backup["tables"][coll] = docs
        return backup

    def _validate_backup(self, backup: dict) -> str | None:
        if not isinstance(backup, dict):
            return "Backup is not a JSON object."
        if "tables" not in backup or not isinstance(backup["tables"], dict):
            return "Backup is missing a 'tables' object."
        if not backup["tables"]:
            return "Backup contains no table data."
        for table, rows in backup["tables"].items():
            if not isinstance(rows, list):
                return f"Table '{table}' in backup is not a list of rows."
        return None

    async def restore_backup(self, backup: dict) -> dict:
        """Insert-only (upsert-with-$setOnInsert) restore. Existing
        documents are never modified. NOTE: unlike the SQLite backend this
        is NOT one atomic transaction (MongoDB multi-document transactions
        require a replica-set deployment, which isn't assumed here) — each
        collection is restored independently. Because every operation is
        an idempotent upsert, a failure partway through only means some
        rows weren't restored yet (safe to re-run /restore with the same
        file), never a corrupted/partial row."""
        error = self._validate_backup(backup)
        if error:
            raise ValueError(f"Backup validation failed: {error}")

        report = {}
        for table, rows in backup.get("tables", {}).items():
            if table not in self._BACKUP_COLLECTIONS or not rows:
                report[table] = 0
                continue
            inserted = 0
            for row in rows:
                try:
                    key = {k: row[k] for k in row if k in ("user_id", "token", "code", "id", "key")} or row
                    result = await self._c(table).update_one(
                        key, {"$setOnInsert": row}, upsert=True,
                    )
                    if result.upserted_id is not None:
                        inserted += 1
                except Exception as exc:
                    LOG.warning(f"Restore skip row in {table}: {exc}")
            report[table] = inserted
        return report

    async def verify_backup_integrity(self) -> dict:
        counts = {}
        for table in self._BACKUP_COLLECTIONS:
            counts[table] = await self._c(table).count_documents({})
        # Relationship check: every post_files-equivalent (file_ids inside
        # posts/custom_batches) should resolve to an existing stored_files doc.
        orphans = {"posts": 0, "custom_batches": 0}
        async for post in self._c("posts").find({}, {"file_ids": 1}):
            for fid in post.get("file_ids", []):
                if not await self._c("stored_files").find_one({"id": fid}):
                    orphans["posts"] += 1
        async for batch in self._c("custom_batches").find({}, {"file_ids": 1}):
            for fid in batch.get("file_ids", []):
                if not await self._c("stored_files").find_one({"id": fid}):
                    orphans["custom_batches"] += 1
        return {"counts": counts, "orphans": orphans, "healthy": all(v == 0 for v in orphans.values())}

    # ------------------------------------------------------------------ #
    #  Persistent auto-delete
    # ------------------------------------------------------------------ #
    async def add_scheduled_deletion(self, chat_id: int, message_id: int, delete_at: int) -> None:
        await self._c("scheduled_deletions").update_one(
            {"chat_id": chat_id, "message_id": message_id},
            {"$set": {"chat_id": chat_id, "message_id": message_id, "delete_at": delete_at}},
            upsert=True,
        )

    async def remove_scheduled_deletion(self, chat_id: int, message_id: int) -> None:
        await self._c("scheduled_deletions").delete_one(
            {"chat_id": chat_id, "message_id": message_id}
        )

    async def get_all_scheduled_deletions(self) -> list:
        cursor = self._c("scheduled_deletions").find({}).sort("delete_at", 1)
        return [_wrap(d) for d in await cursor.to_list(length=None)]

    async def cleanup_expired(self) -> dict:
        now = int(time.time())
        report = {}
        r1 = await self._c("pending_tokens").delete_many(
            {"$or": [{"expires_at": {"$lt": now}}, {"used": 1}]}
        )
        report["pending_tokens"] = r1.deleted_count
        r2 = await self._c("protected_sessions").delete_many({"expires_at": {"$lt": now}})
        report["protected_sessions"] = r2.deleted_count
        r3 = await self._c("bypass_events").delete_many({"created_at": {"$lt": now - 86400}})
        report["bypass_events"] = r3.deleted_count
        r4 = await self._c("scheduled_deletions").delete_many(
            {"delete_at": {"$lt": now - 2 * 86400}}
        )
        report["scheduled_deletions"] = r4.deleted_count
        return report


def get_database():
    """Backend factory. Defaults to the existing SQLite Database class —
    zero behavior change unless DB_BACKEND=mongo is explicitly set."""
    if config.db_backend == "mongo":
        if not config.mongo_uri:
            raise RuntimeError(
                "DB_BACKEND=mongo requires MONGO_URI to be set in the environment."
            )
        LOG.info("Using MongoDB backend.")
        return MongoDatabase(config.mongo_uri, config.mongo_db_name)
    from database import Database
    LOG.info("Using SQLite backend (default).")
    return Database(config.database_path)
