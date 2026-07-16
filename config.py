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
    storage_channel_id: int
    base_url: str
    host: str
    port: int
    database_path: str
    protect_content: bool
    session_minutes: int
    strike_limit: int

    @classmethod
    def from_env(cls) -> "Config":
        raw_admins = _required("ADMIN_IDS")
        try:
            admin_ids = {int(item.strip()) for item in raw_admins.split(",") if item.strip()}
        except ValueError as exc:
            raise RuntimeError("ADMIN_IDS must contain numeric Telegram IDs, separated by commas.") from exc
        if not admin_ids:
            raise RuntimeError("ADMIN_IDS cannot be empty.")

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
            storage_channel_id=storage_channel_id,
            base_url=os.getenv("BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/"),
            host=os.getenv("HOST", "0.0.0.0").strip(),
            port=port,
            database_path=os.getenv("DATABASE_PATH", "bot.db").strip(),
            protect_content=_as_bool(os.getenv("PROTECT_CONTENT"), True),
            session_minutes=session_minutes,
            strike_limit=strike_limit,
        )


config = Config.from_env()
