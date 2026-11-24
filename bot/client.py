from __future__ import annotations

import logging

import discord
from discord.ext import commands

from config import Settings
from data.football_data import FootballDataService
from db.database import Database
from services.match_service import MatchService

logger = logging.getLogger(__name__)


class FootballBot(commands.Bot):
    def __init__(self, settings: Settings, db: Database):
        intents = discord.Intents.default()
        intents.message_content = False
        super().__init__(command_prefix=settings.command_prefix, intents=intents)
        self.settings = settings
        self.db = db
        self.football_data = FootballDataService(settings, db)
        self.match_service = MatchService(db)

    async def setup_hook(self) -> None:
        # Ensure data seeded
        try:
            await self.football_data.ensure_seeded()
        except Exception:
            logger.exception("Failed to seed bundled football data")

        for ext in (
            "bot.cogs.sim",
            "bot.cogs.dashboard",
            "bot.cogs.lineup",
            "bot.cogs.tactics",
            "bot.cogs.ratings",
            "bot.cogs.squad",
        ):
            try:
                await self.load_extension(ext)
                logger.info("Loaded extension %s", ext)
            except Exception:
                logger.exception("Failed to load extension %s", ext)

        # Sync commands (guild-specific if configured for faster dev)
        try:
            if self.settings.discord_guild_id:
                guild = discord.Object(id=self.settings.discord_guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info("Synced commands to guild %s", self.settings.discord_guild_id)
            else:
                await self.tree.sync()
                logger.info("Synced global commands")
        except Exception:
            logger.exception("Failed to sync application commands")

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))


def create_bot(settings: Settings, db: Database) -> FootballBot:
    return FootballBot(settings, db)
