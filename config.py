from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(slots=True)
class Settings:
    discord_bot_token: str
    discord_guild_id: int | None
    football_api_key: str
    football_api_base_url: str
    football_api_season: int
    football_data_source: str  # bundled | api | auto
    database_path: str
    command_prefix: str
    log_level: str
    match_tick_seconds: float
    cache_ttl_hours: int
    api_request_timeout: int
    api_max_retries: int
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_timeout: int

    @property
    def has_discord_token(self) -> bool:
        return bool(self.discord_bot_token and self.discord_bot_token not in {"", "your_discord_bot_token_here"})

    @property
    def has_football_api_key(self) -> bool:
        return bool(self.football_api_key and self.football_api_key not in {"", "your_api_football_key_here"})

    @property
    def has_llm(self) -> bool:
        return bool(self.llm_api_key and self.llm_api_key not in {"", "your_llm_api_key_here"})


def load_settings() -> Settings:
    guild_id_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
    guild_id = int(guild_id_raw) if guild_id_raw.isdigit() else None
    db_path = os.getenv("DATABASE_PATH", "./football_bot.sqlite3").strip() or "./football_bot.sqlite3"

    source = os.getenv("FOOTBALL_DATA_SOURCE", "bundled").strip().lower() or "bundled"
    if source not in {"bundled", "api", "auto"}:
        source = "bundled"

    return Settings(
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", "").strip(),
        discord_guild_id=guild_id,
        football_api_key=os.getenv("FOOTBALL_API_KEY", "").strip(),
        football_api_base_url=os.getenv("FOOTBALL_API_BASE_URL", "https://v3.football.api-sports.io").strip().rstrip("/"),
        football_api_season=int(os.getenv("FOOTBALL_API_SEASON", "2026")),
        football_data_source=source,
        database_path=db_path,
        command_prefix=os.getenv("COMMAND_PREFIX", "!").strip() or "!",
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        match_tick_seconds=float(os.getenv("MATCH_TICK_SECONDS", "1.2")),
        cache_ttl_hours=int(os.getenv("CACHE_TTL_HOURS", "24")),
        api_request_timeout=int(os.getenv("API_REQUEST_TIMEOUT", "20")),
        api_max_retries=int(os.getenv("API_MAX_RETRIES", "3")),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        llm_timeout=int(os.getenv("LLM_TIMEOUT", "20")),
    )
