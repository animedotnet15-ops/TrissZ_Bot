"""Entry point — initialise the database then start polling."""
import asyncio
import logging
import sys

from aiogram.exceptions import TelegramAPIError

from bot import bot, dp, router
from config import config
from database import database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOG = logging.getLogger("main")

BOT_VERSION = "3.1.0"


async def validate_startup() -> bool:
    """Runs a clear pass/fail check on everything the bot depends on
    before we start polling. Never prints secrets - only status."""
    all_ok = True

    # 1. Database
    try:
        await database.init()
        LOG.info("🟢 Database: OK")
    except Exception as exc:
        LOG.error(f"🔴 Database: FAILED — {exc}")
        return False  # nothing else can run without the DB

    # 2. Bot token / Telegram connectivity
    try:
        me = await bot.get_me()
        LOG.info(f"🟢 Bot: OK — connected as @{me.username} (id={me.id})")
    except TelegramAPIError as exc:
        LOG.error(f"🔴 Bot: FAILED — could not authenticate with Telegram: {exc}")
        return False  # nothing works without a valid bot token

    # 3. Admin IDs configured
    if not config.admin_ids:
        LOG.warning("🟡 Admins: no ADMIN_IDS configured — the admin panel will be unreachable.")
        all_ok = False
    else:
        LOG.info(f"🟢 Admins: OK — {len(config.admin_ids)} configured")

    # 4. Storage channel access (read-only probe, never modifies anything there)
    try:
        await bot.get_chat(config.storage_channel_id)
        LOG.info("🟢 Storage Channel: OK — accessible")
    except TelegramAPIError as exc:
        LOG.error(
            f"🔴 Storage Channel: FAILED — bot cannot access {config.storage_channel_id}: {exc}\n"
            f"    Make sure the bot is an admin member of that channel."
        )
        all_ok = False

    # 5. Shortener configuration (presence only - a live network test is
    # already available separately via /testshortner, no need to call out
    # to a third-party API on every boot).
    shortener_url = await database.get_setting("shortener_url", "")
    if shortener_url:
        LOG.info(f"🟢 Shortener: configured (domain set) — run /testshortner to verify it's live")
    else:
        LOG.warning("🟡 Shortener: not configured — verification links will not work until set via /setshortner")

    LOG.info(f"ℹ️  Bot Version: {BOT_VERSION}")
    return all_ok


async def main() -> None:
    ok = await validate_startup()
    if not ok:
        LOG.error("❌ Startup validation found blocking issues (see 🔴 lines above). Fix them and restart.")
        sys.exit(1)

    dp.include_router(router)
    LOG.info("Routers registered. Starting polling...")

    # Notify whoever triggered /restart that we're back up (see bot.py's
    # restart_handler for where this flag gets set before the exit).
    pending_chat = await database.get_setting("pending_restart_notify_chat", "")
    if pending_chat:
        await database.set_setting("pending_restart_notify_chat", "")
        try:
            await bot.send_message(int(pending_chat), "✅ <b>Bot restarted successfully.</b>")
        except Exception as exc:
            LOG.warning(f"Could not send restart confirmation to {pending_chat}: {exc}")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
