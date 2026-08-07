"""Environment-backed configuration for the Songoku-style file-share bot."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    bot_username: str
    admin_ids: set[int]
    owner_ids: set[int]
    storage_channel_id: int
    base_url: str
    host: str
    port: int
    database_path: str
    protect_content: bool
    session_minutes: int
    strike_limit: int
    # --- MongoDB backend (opt-in; default keeps existing SQLite behavior) ---
    db_backend: str
    mongo_uri: str
    mongo_db_name: str

    @classmethod
    def from_env(cls) -> "Config":
        raw_admins = _required("ADMIN_IDS")
        try:
            admin_ids = {int(item.strip()) for item in raw_admins.split(",") if item.strip()}
        except ValueError as exc:
            raise RuntimeError("ADMIN_IDS must contain numeric Telegram IDs, separated by commas.") from exc
        if not admin_ids:
            raise RuntimeError("ADMIN_IDS cannot be empty.")

        # OWNER_IDS is optional. If unset, the first ID listed in ADMIN_IDS
        # becomes the owner — guarantees there's always at least one owner
        # so owner-gated commands (e.g. /restore) are never accidentally
        # locked out on existing deployments that only ever set ADMIN_IDS.
        raw_owners = os.getenv("OWNER_IDS", "").strip()
        if raw_owners:
            try:
                owner_ids = {int(item.strip()) for item in raw_owners.split(",") if item.strip()}
            except ValueError as exc:
                raise RuntimeError("OWNER_IDS must contain numeric Telegram IDs, separated by commas.") from exc
        else:
            owner_ids = {sorted(admin_ids)[0]}
        owner_ids |= (owner_ids & admin_ids)  # owners are implicitly admins too, no-op if already overlapping
        admin_ids = admin_ids | owner_ids  # every owner is also an admin

        try:
            storage_channel_id = int(_required("STORAGE_CHANNEL_ID"))
        except ValueError as exc:
            raise RuntimeError("STORAGE_CHANNEL_ID must be a numeric Telegram channel ID.") from exc

        try:
            port = int(os.getenv("PORT", "8000"))
            session_minutes = int(os.getenv("SESSION_MINUTES", "10"))
            strike_limit = int(os.getenv("STRIKE_LIMIT", "3"))
        except ValueError as exc:
            raise RuntimeError("PORT, SESSION_MINUTES and STRIKE_LIMIT must be whole numbers.") from exc

        if not 1 <= session_minutes <= 60:
            raise RuntimeError("SESSION_MINUTES must be between 1 and 60.")
        if not 1 <= strike_limit <= 10:
            raise RuntimeError("STRIKE_LIMIT must be between 1 and 10.")

        return cls(
            bot_token=_required("BOT_TOKEN"),
            bot_username=_required("BOT_USERNAME").lstrip("@"),
            admin_ids=admin_ids,
            owner_ids=owner_ids,
            storage_channel_id=storage_channel_id,
            base_url=os.getenv("BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/"),
            host=os.getenv("HOST", "0.0.0.0").strip(),
            port=port,
            database_path=os.getenv("DATABASE_PATH", "bot.db").strip(),
            protect_content=_as_bool(os.getenv("PROTECT_CONTENT"), True),
            session_minutes=session_minutes,
            strike_limit=strike_limit,
            # Defaults preserve the existing SQLite-only behavior for every
            # deployment that doesn't explicitly opt in to Mongo.
            db_backend=os.getenv("DB_BACKEND", "sqlite").strip().lower(),
            mongo_uri=os.getenv("MONGO_URI", "").strip(),
            mongo_db_name=os.getenv("MONGO_DB_NAME", "trissz_bot").strip(),
        )


config = Config.from_env()
