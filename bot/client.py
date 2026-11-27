from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import Settings
from data.football_data import FootballDataService
from db.database import Database
from services.llm_service import LLMService
from services.match_service import MatchService
from services.season_service import SeasonService

logger = logging.getLogger(__name__)


class FootballBot(commands.Bot):
    def __init__(self, settings: Settings, db: Database):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=settings.command_prefix, intents=intents)
        self.settings = settings
        self.db = db
        self.football_data = FootballDataService(settings, db)
        self.match_service = MatchService(db)
        self.season_service = SeasonService(db)
        self.llm_service = LLMService(settings)

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
            "bot.cogs.season",
            "bot.cogs.club",
            "bot.cogs.transfers",
            "bot.cogs.press",
        ):
            try:
                await self.load_extension(ext)
                logger.info("Loaded extension %s", ext)
            except Exception:
                logger.exception("Failed to load extension %s", ext)

        # Allow commands in DMs where possible (discord.py 2.4+ contexts/install types)
        try:
            dm_contexts = None
            user_installs = None
            try:
                dm_contexts = app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True)
            except Exception:
                try:
                    dm_contexts = app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True)
                except Exception:
                    dm_contexts = None
            try:
                user_installs = app_commands.AppInstallationType(guild=True, user=True)
            except Exception:
                user_installs = None
            for cmd in self.tree.walk_commands():
                try:
                    if dm_contexts is not None and getattr(cmd, "allowed_contexts", None) is None:
                        cmd.allowed_contexts = dm_contexts
                    if user_installs is not None and getattr(cmd, "allowed_installs", None) is None:
                        cmd.allowed_installs = user_installs
                except Exception:
                    pass
        except Exception:
            logger.exception("Failed to set DM command contexts")

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

    async def close(self) -> None:
        try:
            await self.llm_service.close()
        except Exception:
            logger.exception("Failed to close LLM service")
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))
        if self.llm_service.enabled:
            providers = []
            if self.settings.llm_api_keys:
                providers.append(f"gemini:{self.settings.llm_model}")
            if getattr(self.settings, "openrouter_api_keys", None):
                or_models = getattr(self.settings, "openrouter_models", None) or ["nvidia/nemotron-3.5-lightning"]
                providers.append(f"openrouter:{or_models[0]}")
            if getattr(self.settings, "groq_api_keys", None):
                providers.append("groq")
            logger.info("LLM enabled providers=%s primary_model=%s base_url=%s", ",".join(providers) or "unknown", self.settings.llm_model, self.settings.llm_base_url)
        else:
            logger.warning("LLM disabled (set LLM_API_KEY / OPENROUTER_API_KEY / GROQ_API_KEY to enable LLM negotiation replies)")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        orig = getattr(error, "original", error)
        logger.exception("App command error: %s", error, exc_info=orig)
        msg = "That command didn't go through. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


def create_bot(settings: Settings, db: Database) -> FootballBot:
    return FootballBot(settings, db)
