"""Entry point — initialise the database, start the (previously unwired)
FastAPI health/guard server, then start Telegram polling. All three run
concurrently in one process, which is what single-dyno Railway/Render
free-tier deployments need."""
import asyncio
import logging

import uvicorn

from bot import bot, dp, router, resume_scheduled_deletions
from config import config
from database import database
from web import app as web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOG = logging.getLogger("main")


CLEANUP_INTERVAL_SECONDS = 3600  # hourly


async def cleanup_loop() -> None:
    """Runs forever in the background. Never lets an exception kill the
    bot's main polling loop — a failed cleanup pass just gets retried
    next hour."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            report = await database.cleanup_expired()
            total = sum(report.values())
            if total:
                LOG.info(f"DB cleanup: purged {total} expired rows -> {report}")
        except Exception as e:
            LOG.warning(f"DB cleanup pass failed (will retry next interval): {e}")


async def main() -> None:
    await database.init()
    LOG.info("Database initialised.")

    resumed = await resume_scheduled_deletions()
    if resumed:
        LOG.info(f"Resumed {resumed} persisted auto-delete job(s) after restart.")

    # Run one cleanup pass immediately at startup too, then hourly after.
    try:
        report = await database.cleanup_expired()
        total = sum(report.values())
        if total:
            LOG.info(f"Startup DB cleanup: purged {total} expired rows -> {report}")
    except Exception as e:
        LOG.warning(f"Startup DB cleanup failed (non-fatal): {e}")
    asyncio.create_task(cleanup_loop())

    # Start the FastAPI health/guard server in the background, on the same
    # process as bot polling — was previously defined but never actually
    # started anywhere in the project.
    uvicorn_config = uvicorn.Config(
        web_app, host=config.host, port=config.port, log_level="warning"
    )
    web_server = uvicorn.Server(uvicorn_config)
    asyncio.create_task(web_server.serve())
    LOG.info(f"Web server starting on {config.host}:{config.port} (health/status/guard endpoints).")

    dp.include_router(router)
    LOG.info("Routers registered. Starting polling...")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())