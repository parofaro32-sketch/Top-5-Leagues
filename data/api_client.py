from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class FootballAPIClient:
    """Optional live client for API-Football (api-sports.io). Used only when configured."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 20, max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("FOOTBALL_API_KEY not configured")
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {"x-apisports-key": self.api_key}
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                    async with session.get(url, headers=headers, params=params or {}) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(min(2 ** attempt, 8))
                            continue
                        resp.raise_for_status()
                        return await resp.json()
            except Exception as e:
                last_err = e
                logger.warning("API request failed (attempt %s/%s): %s", attempt, self.max_retries, e)
                if attempt < self.max_retries:
                    await asyncio.sleep(0.5 * attempt)
        raise RuntimeError(f"API request failed after {self.max_retries} attempts: {last_err}")

    async def teams_by_league(self, league_id: int, season: int) -> list[dict[str, Any]]:
        data = await self._get("teams", {"league": league_id, "season": season})
        return data.get("response") or []

    async def players_by_team(self, team_id: int, season: int, page: int = 1) -> dict[str, Any]:
        return await self._get("players", {"team": team_id, "season": season, "page": page})
