"""Entry point — initialise the database then start polling."""
import asyncio
import logging

from bot import bot, dp, router
from database import database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOG = logging.getLogger("main")


async def main() -> None:
    await database.init()
    LOG.info("Database initialised.")

    dp.include_router(router)
    LOG.info("Routers registered. Starting polling...")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())