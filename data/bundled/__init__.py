from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

BUNDLED_DIR = Path(__file__).resolve().parent
LEAGUES_DIR = BUNDLED_DIR / "leagues"

LEAGUE_FILES = {
    "premier_league": LEAGUES_DIR / "premier_league.json",
    "la_liga": LEAGUES_DIR / "la_liga.json",
    "bundesliga": LEAGUES_DIR / "bundesliga.json",
    "serie_a": LEAGUES_DIR / "serie_a.json",
    "ligue_1": LEAGUES_DIR / "ligue_1.json",
}

LEAGUE_ALIASES = {
    "epl": "premier_league", "english premier league": "premier_league", "premier league": "premier_league", "england": "premier_league",
    "laliga": "la_liga", "la liga": "la_liga", "spanish la liga": "la_liga", "spain": "la_liga",
    "bundesliga": "bundesliga", "german bundesliga": "bundesliga", "germany": "bundesliga",
    "serie a": "serie_a", "seriea": "serie_a", "italian serie a": "serie_a", "italy": "serie_a",
    "ligue 1": "ligue_1", "ligue1": "ligue_1", "french ligue 1": "ligue_1", "france": "ligue_1",
}

LEAGUE_DISPLAY_NAMES = {
    "premier_league": "English Premier League",
    "la_liga": "Spanish La Liga",
    "bundesliga": "German Bundesliga",
    "serie_a": "Italian Serie A",
    "ligue_1": "French Ligue 1",
}


def resolve_league_key(name: str) -> str | None:
    if not name:
        return None
    k = name.strip().lower()
    if k in LEAGUE_FILES:
        return k
    return LEAGUE_ALIASES.get(k)


@lru_cache(maxsize=16)
def load_league(key: str) -> dict[str, Any] | None:
    path = LEAGUE_FILES.get(key)
    if not path or not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def list_league_keys() -> list[str]:
    return list(LEAGUE_FILES.keys())


def list_all_teams(league_key: str | None = None) -> list[dict[str, Any]]:
    keys = [league_key] if league_key else list_league_keys()
    out: list[dict[str, Any]] = []
    for k in keys:
        data = load_league(k)
        if not data:
            continue
        league_id = data.get("league_id")
        league_name = data.get("league_name")
        country = data.get("country")
        for t in data.get("teams", []):
            team = {
                "api_id": int(t["api_id"]),
                "name": t.get("name"),
                "short_name": t.get("short_name"),
                "code": t.get("short_name"),
                "logo": t.get("logo"),
                "league_id": league_id,
                "league_name": league_name,
                "country": country,
                "venue_name": t.get("venue_name"),
            }
            out.append(team)
    return out


def get_team(team_api_id: int, league_key: str | None = None) -> dict[str, Any] | None:
    keys = [league_key] if league_key else list_league_keys()
    for k in keys:
        data = load_league(k)
        if not data:
            continue
        for t in data.get("teams", []):
            if int(t.get("api_id", 0)) == int(team_api_id):
                team = dict(t)
                team["api_id"] = int(team["api_id"])
                team["league_id"] = data.get("league_id")
                team["league_name"] = data.get("league_name")
                team["country"] = data.get("country")
                return team
    return None


def get_team_players(team_api_id: int, league_key: str | None = None) -> list[dict[str, Any]]:
    team = get_team(team_api_id, league_key)
    if not team:
        return []
    players = []
    for p in team.get("players", []):
        players.append({
            "api_id": int(p.get("api_id") or p.get("id") or 0),
            "name": p.get("name"),
            "firstname": p.get("firstname"),
            "lastname": p.get("lastname"),
            "age": p.get("age"),
            "dob": p.get("dob"),
            "nationality": p.get("nationality"),
            "photo": p.get("photo"),
            "position": p.get("position") or "MID",
            "detailed_position": p.get("detailed_position"),
            "number": p.get("number"),
            "injured": bool(p.get("injured", False)),
            "height_cm": p.get("height_cm"),
            "weight_kg": p.get("weight_kg"),
            "preferred_foot": p.get("preferred_foot"),
            "overall": p.get("overall"),
            "potential": p.get("potential"),
            "status": p.get("status") or "active",
            "attributes": p.get("attributes") or {},
            "statistics": p.get("statistics") or {},
            "team_api_id": int(team_api_id),
        })
    return players


def search_teams(query: str, league_key: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
    q = (query or "").strip().lower()
    teams = list_all_teams(league_key)
    if not q:
        return teams[:limit]
    scored = []
    for t in teams:
        name = (t.get("name") or "").lower()
        short = (t.get("short_name") or "").lower()
        league = (t.get("league_name") or "").lower()
        s = 0
        if q == name or q == short:
            s = 100
        elif name.startswith(q) or short.startswith(q):
            s = 80
        elif q in name or q in short:
            s = 60
        elif q in league:
            s = 30
        if s:
            scored.append((s, t))
    scored.sort(key=lambda x: (-x[0], x[1].get("league_name", ""), x[1].get("name", "")))
    return [t for _, t in scored[:limit]]
