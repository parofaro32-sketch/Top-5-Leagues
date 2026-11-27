from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    llm_api_keys: list[str] = field(default_factory=list)
    llm_models: list[str] = field(default_factory=list)
    # Optional fallback providers (used when primary Gemini provider fails/exhausts)
    openrouter_api_key: str = ""
    openrouter_api_keys: list[str] = field(default_factory=list)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_models: list[str] = field(default_factory=list)
    groq_api_key: str = ""
    groq_api_keys: list[str] = field(default_factory=list)
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_models: list[str] = field(default_factory=list)

    @property
    def has_discord_token(self) -> bool:
        return bool(self.discord_bot_token and self.discord_bot_token not in {"", "your_discord_bot_token_here"})

    @property
    def has_football_api_key(self) -> bool:
        return bool(self.football_api_key and self.football_api_key not in {"", "your_api_football_key_here"})

    def __post_init__(self) -> None:
        # Normalize key sources for direct Settings(...) construction in tests/callers.
        raw_keys = [str(k or "").strip() for k in (self.llm_api_keys or [])]
        keys = [k for k in raw_keys if k and k not in {"", "your_llm_api_key_here"}]
        single = str(self.llm_api_key or "").strip()
        if single in {"", "your_llm_api_key_here"}:
            single = ""
        if not keys and single:
            keys = [single]
        # de-dupe preserving order
        deduped: list[str] = []
        for k in keys:
            if k not in deduped:
                deduped.append(k)
        self.llm_api_keys = deduped
        self.llm_api_key = deduped[0] if deduped else ""

    @property
    def has_llm(self) -> bool:
        return bool(self.llm_api_keys or self.openrouter_api_keys or self.groq_api_keys)


def load_settings() -> Settings:
    guild_id_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
    guild_id = int(guild_id_raw) if guild_id_raw.isdigit() else None
    db_path = os.getenv("DATABASE_PATH", "./football_bot.sqlite3").strip() or "./football_bot.sqlite3"

    source = os.getenv("FOOTBALL_DATA_SOURCE", "bundled").strip().lower() or "bundled"
    if source not in {"bundled", "api", "auto"}:
        source = "bundled"

    _llm_keys_raw = os.getenv("LLM_API_KEYS", "").replace(";", ",").split(",")
    llm_api_keys = [k.strip() for k in _llm_keys_raw if k.strip() and k.strip() not in {"", "your_llm_api_key_here"}]
    if not llm_api_keys:
        _single = os.getenv("LLM_API_KEY", "").strip()
        if _single and _single not in {"", "your_llm_api_key_here"}:
            llm_api_keys = [_single]
    llm_api_key = llm_api_keys[0] if llm_api_keys else ""

    _llm_models_raw = os.getenv("LLM_MODELS", "") or os.getenv("LLM_MODEL_FALLBACKS", "")
    llm_models = [m.strip() for m in _llm_models_raw.replace(";", ",").split(",") if m.strip()]

    def _parse_keys(*env_names: str) -> list[str]:
        out: list[str] = []
        for name in env_names:
            raw = os.getenv(name, "").replace(";", ",")
            for k in raw.split(","):
                k = k.strip()
                if k and k not in {"", "your_llm_api_key_here", "your_openrouter_api_key_here", "your_groq_api_key_here"} and k not in out:
                    out.append(k)
        return out

    def _parse_models(env_name: str) -> list[str]:
        raw = os.getenv(env_name, "").replace(";", ",")
        return [m.strip() for m in raw.split(",") if m.strip()]

    openrouter_api_keys = _parse_keys("OPENROUTER_API_KEYS", "OPENROUTER_API_KEY")
    groq_api_keys = _parse_keys("GROQ_API_KEYS", "GROQ_API_KEY")
    openrouter_models = _parse_models("OPENROUTER_MODELS") or _parse_models("OPENROUTER_MODEL")
    groq_models = _parse_models("GROQ_MODELS") or _parse_models("GROQ_MODEL")

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
        llm_api_key=llm_api_key,
        llm_api_keys=llm_api_keys,
        llm_base_url=os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").strip().rstrip("/"),
        llm_model=os.getenv("LLM_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash",
        llm_timeout=int(os.getenv("LLM_TIMEOUT", "45")),
        llm_models=llm_models,
        openrouter_api_key=openrouter_api_keys[0] if openrouter_api_keys else "",
        openrouter_api_keys=openrouter_api_keys,
        openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip().rstrip("/") or "https://openrouter.ai/api/v1",
        openrouter_models=openrouter_models,
        groq_api_key=groq_api_keys[0] if groq_api_keys else "",
        groq_api_keys=groq_api_keys,
        groq_base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").strip().rstrip("/") or "https://api.groq.com/openai/v1",
        groq_models=groq_models,
    )
