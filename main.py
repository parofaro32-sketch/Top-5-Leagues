from __future__ import annotations

import asyncio
import logging

from bot.client import create_bot
from config import load_settings
from db.database import Database


async def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("football-bot")

    db = Database(settings.database_path)
    await db.connect()
    await db.initialize()

    bot = create_bot(settings=settings, db=db)
    if not settings.has_discord_token:
        logger.error("DISCORD_BOT_TOKEN is missing or placeholder. Set it in .env or hosting environment variables.")
        logger.info("Database initialized. Core modules can be imported and tests can run without a Discord token.")
        await db.close()
        return

    async with bot:
        await bot.start(settings.discord_bot_token)


if __name__ == "__main__":
    asyncio.run(main())
