from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import Settings
from data.bundled import get_team, get_team_players, list_all_teams, list_league_keys, resolve_league_key, search_teams, LEAGUE_DISPLAY_NAMES
from db.database import Database

logger = logging.getLogger(__name__)

# Approx mapping for optional live API (API-Football league IDs)
LEAGUE_API_IDS = {
    "premier_league": 39,
    "la_liga": 140,
    "bundesliga": 78,
    "serie_a": 135,
    "ligue_1": 61,
}


def _derive_attributes(player: dict[str, Any]) -> dict[str, float]:
    # Deterministic-ish attributes from player id/name for consistent simulation
    seed = int(player.get("api_id") or 0) or sum(ord(c) for c in (player.get("name") or "x"))
    rng = random.Random(seed)
    pos = (player.get("position") or "MID").upper()
    def r(base, spread=12):
        return max(35.0, min(95.0, base + rng.uniform(-spread, spread)))
    # role-tuned baselines
    if pos == "GK":
        base = {"goalkeeping": r(78,10), "positioning": r(72,10), "decision_making": r(70,10), "composure": r(70,10), "passing": r(60,12), "strength": r(70,10)}
        for k in ["shooting","finishing","dribbling","pace","acceleration","tackling","interceptions","defensive_awareness","creativity","crossing","heading","stamina"]:
            base.setdefault(k, r(35,8) if k in {"shooting","finishing","dribbling","crossing","creativity"} else r(55,10))
    elif pos == "DEF":
        base = {"tackling": r(74,10), "interceptions": r(73,10), "defensive_awareness": r(75,10), "strength": r(74,10), "positioning": r(72,10), "heading": r(72,10), "pace": r(68,12), "passing": r(66,12)}
        for k in ["shooting","finishing","dribbling","acceleration","stamina","creativity","crossing","goalkeeping","decision_making","composure"]:
            base.setdefault(k, r(50,12) if k not in {"goalkeeping"} else r(12,5))
    elif pos == "FWD":
        base = {"finishing": r(76,10), "shooting": r(75,10), "dribbling": r(73,10), "pace": r(74,12), "acceleration": r(74,12), "positioning": r(74,10), "composure": r(72,10), "heading": r(68,12)}
        for k in ["passing","strength","stamina","tackling","interceptions","defensive_awareness","creativity","crossing","goalkeeping","decision_making"]:
            base.setdefault(k, r(55,12) if k not in {"goalkeeping"} else r(10,4))
    else:  # MID
        base = {"passing": r(75,10), "decision_making": r(73,10), "positioning": r(72,10), "creativity": r(72,12), "dribbling": r(71,10), "stamina": r(74,10), "tackling": r(66,12), "shooting": r(66,12)}
        for k in ["finishing","pace","acceleration","strength","interceptions","defensive_awareness","crossing","heading","goalkeeping","composure"]:
            base.setdefault(k, r(60,12) if k not in {"goalkeeping"} else r(10,4))
    base["stamina"] = max(base.get("stamina", 70), r(72,8))
    base["decision_making"] = max(base.get("decision_making", 65), r(68,8))
    base["composure"] = max(base.get("composure", 65), r(68,8))
    # keep within 40-95
    for k, v in list(base.items()):
        base[k] = float(max(40.0, min(95.0, v))) if k != "goalkeeping" or pos == "GK" else float(max(5.0, min(20.0, v)))
    if pos == "GK":
        base["goalkeeping"] = float(max(65.0, min(95.0, base.get("goalkeeping", 78))))
    return base


def normalize_player(player: dict[str, Any], team_api_id: int) -> dict[str, Any]:
    p = dict(player)
    p["api_id"] = int(p.get("api_id") or p.get("id") or 0)
    p["team_api_id"] = int(team_api_id)
    p["name"] = p.get("name") or "Unknown"
    p["position"] = (p.get("position") or "MID").upper()
    if p["position"] not in {"GK", "DEF", "MID", "FWD"}:
        # try infer
        pos = (p.get("position") or "").lower()
        if "goal" in pos or pos == "g": p["position"] = "GK"
        elif "def" in pos or "back" in pos: p["position"] = "DEF"
        elif "att" in pos or "forw" in pos or "strik" in pos or "wing" in pos: p["position"] = "FWD"
        else: p["position"] = "MID"
    p["attributes"] = p.get("attributes") or _derive_attributes(p)
    return p


@dataclass
class FootballDataService:
    settings: Settings
    db: Database

    @property
    def season(self) -> int:
        return int(self.settings.football_api_season)

    async def ensure_seeded(self) -> None:
        # Seed bundled teams/players into DB for fast lookup and persistence
        seed_key = "bundled_seed_v4"
        meta = await self.db.get_cache_meta(seed_key)
        if meta:
            return
        for league_key in list_league_keys():
            teams = list_all_teams(league_key)
            await self.db.upsert_teams(teams, self.season)
            for t in teams:
                players = [normalize_player(p, t["api_id"]) for p in get_team_players(t["api_id"], league_key)]
                await self.db.upsert_players(players, t["api_id"], self.season)
        await self.db.set_cache_meta(seed_key, {"ok": True, "season": self.season}, None)

    async def list_teams(self, query: str | None = None, league: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        league_key = resolve_league_key(league) if league else None
        if league and not league_key:
            # treat as text query fallback
            query = query or league
            league_key = None
        await self.ensure_seeded()
        if query:
            rows = await self.db.get_teams(self.season, query=query, limit=limit)
            if rows:
                return [dict(r) for r in rows]
            return search_teams(query, league_key, limit)
        if league_key:
            return list_all_teams(league_key)[:limit]
        rows = await self.db.get_teams(self.season, query=None, limit=limit)
        if rows:
            return [dict(r) for r in rows]
        return list_all_teams(None)[:limit]

    async def get_team(self, team_api_id: int) -> dict[str, Any] | None:
        await self.ensure_seeded()
        row = await self.db.get_team(team_api_id, self.season)
        if row:
            return dict(row)
        t = get_team(team_api_id)
        if not t:
            return None
        return {
            "api_id": int(t["api_id"]), "name": t.get("name"), "short_name": t.get("short_name"), "code": t.get("short_name"),
            "logo": t.get("logo"), "league_id": t.get("league_id"), "league_name": t.get("league_name"), "country": t.get("country"),
        }

    async def get_squad(self, team_api_id: int) -> list[dict[str, Any]]:
        await self.ensure_seeded()
        rows = await self.db.get_players_for_team(team_api_id, self.season)
        if rows:
            out = []
            for r in rows:
                d = dict(r)
                d["attributes"] = self.db.loads(d.get("attributes_json")) or {}
                out.append(d)
            return out
        players = [normalize_player(p, team_api_id) for p in get_team_players(team_api_id)]
        return players

    def list_leagues(self) -> list[dict[str, str]]:
        return [{"key": k, "name": LEAGUE_DISPLAY_NAMES.get(k, k)} for k in list_league_keys()]
