from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS teams (
    api_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    league_id INTEGER,
    league_name TEXT,
    country TEXT,
    name TEXT NOT NULL,
    short_name TEXT,
    code TEXT,
    logo TEXT,
    venue_name TEXT,
    raw_json TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (api_id, season)
);

CREATE TABLE IF NOT EXISTS players (
    api_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    team_api_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    firstname TEXT,
    lastname TEXT,
    age INTEGER,
    nationality TEXT,
    photo TEXT,
    position TEXT,
    detailed_position TEXT,
    number INTEGER,
    injured INTEGER NOT NULL DEFAULT 0,
    attributes_json TEXT,
    statistics_json TEXT,
    raw_json TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (api_id, team_api_id, season)
);
CREATE INDEX IF NOT EXISTS idx_players_team_season ON players(team_api_id, season);

CREATE TABLE IF NOT EXISTS lineup_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    match_id TEXT,
    guild_id INTEGER,
    channel_id INTEGER,
    user_id INTEGER,
    team_api_id INTEGER NOT NULL,
    team_name TEXT,
    formation TEXT NOT NULL,
    captain_player_id INTEGER,
    goalkeeper_player_id INTEGER,
    starters_json TEXT NOT NULL,
    bench_json TEXT NOT NULL,
    source TEXT,
    validated INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(scope_key)
);

CREATE TABLE IF NOT EXISTS tactics_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    match_id TEXT,
    guild_id INTEGER,
    channel_id INTEGER,
    user_id INTEGER,
    team_api_id INTEGER NOT NULL,
    team_name TEXT,
    tactics_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(scope_key)
);

CREATE TABLE IF NOT EXISTS matches (
    id TEXT PRIMARY KEY,
    guild_id INTEGER,
    channel_id INTEGER,
    message_id INTEGER,
    home_team_api_id INTEGER NOT NULL,
    away_team_api_id INTEGER NOT NULL,
    home_team_name TEXT NOT NULL,
    away_team_name TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    current_minute INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT 'pre_kickoff',
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS match_participants (
    match_id TEXT NOT NULL,
    side TEXT NOT NULL,
    team_api_id INTEGER NOT NULL,
    manager_user_id INTEGER,
    is_bot INTEGER NOT NULL DEFAULT 0,
    lineup_submission_id INTEGER,
    tactics_submission_id INTEGER,
    PRIMARY KEY (match_id, side),
    FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS match_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    minute INTEGER NOT NULL,
    extra_minute INTEGER NOT NULL DEFAULT 0,
    phase TEXT,
    event_type TEXT NOT NULL,
    team_side TEXT,
    player_api_id INTEGER,
    payload_json TEXT,
    commentary TEXT,
    public_payload_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
    UNIQUE(match_id, seq)
);

CREATE TABLE IF NOT EXISTS api_cache_meta (
    cache_key TEXT PRIMARY KEY,
    value_json TEXT,
    expires_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        parent = Path(self.path).parent
        if str(parent) not in {"", "."}:
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys=ON;")

    async def initialize(self) -> None:
        await self.require_conn().executescript(SCHEMA)
        await self.require_conn().commit()

    def require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> aiosqlite.Cursor:
        cur = await self.require_conn().execute(sql, params)
        await self.require_conn().commit()
        return cur

    async def executemany(self, sql: str, seq: list[tuple[Any, ...]] | list[list[Any]]) -> None:
        await self.require_conn().executemany(sql, seq)
        await self.require_conn().commit()

    async def fetchone(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> aiosqlite.Row | None:
        cur = await self.require_conn().execute(sql, params)
        return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[aiosqlite.Row]:
        cur = await self.require_conn().execute(sql, params)
        return await cur.fetchall()

    @staticmethod
    def dumps(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def loads(data: str | None) -> Any:
        if not data:
            return None
        return json.loads(data)

    async def upsert_teams(self, teams: list[dict[str, Any]], season: int) -> None:
        if not teams:
            return
        rows = [(
            int(t["api_id"]), season, t.get("league_id"), t.get("league_name"), t.get("country"),
            t.get("name"), t.get("short_name"), t.get("code"), t.get("logo"), t.get("venue_name"),
            self.dumps(t.get("raw") or t), utc_now_iso(),
        ) for t in teams]
        await self.executemany(
            """INSERT INTO teams(api_id,season,league_id,league_name,country,name,short_name,code,logo,venue_name,raw_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(api_id,season) DO UPDATE SET
               league_id=excluded.league_id,league_name=excluded.league_name,country=excluded.country,name=excluded.name,
               short_name=excluded.short_name,code=excluded.code,logo=excluded.logo,venue_name=excluded.venue_name,
               raw_json=excluded.raw_json,updated_at=excluded.updated_at""", rows)

    async def upsert_players(self, players: list[dict[str, Any]], team_api_id: int, season: int) -> None:
        if not players:
            return
        rows = [(
            int(p["api_id"]), season, team_api_id, p.get("name") or "", p.get("firstname"), p.get("lastname"),
            p.get("age"), p.get("nationality"), p.get("photo"), p.get("position"), p.get("detailed_position"),
            p.get("number"), 1 if p.get("injured") else 0, self.dumps(p.get("attributes") or {}),
            self.dumps(p.get("statistics") or {}), self.dumps(p.get("raw") or p), utc_now_iso(),
        ) for p in players]
        await self.executemany(
            """INSERT INTO players(api_id,season,team_api_id,name,firstname,lastname,age,nationality,photo,position,detailed_position,number,injured,attributes_json,statistics_json,raw_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(api_id,team_api_id,season) DO UPDATE SET
               name=excluded.name,firstname=excluded.firstname,lastname=excluded.lastname,age=excluded.age,nationality=excluded.nationality,
               photo=excluded.photo,position=excluded.position,detailed_position=excluded.detailed_position,number=excluded.number,
               injured=excluded.injured,attributes_json=excluded.attributes_json,statistics_json=excluded.statistics_json,raw_json=excluded.raw_json,updated_at=excluded.updated_at""", rows)

    async def get_teams(self, season: int, query: str | None = None, limit: int = 25) -> list[aiosqlite.Row]:
        if query:
            like = f"%{query.strip()}%"
            return await self.fetchall(
                "SELECT * FROM teams WHERE season=? AND (name LIKE ? OR short_name LIKE ? OR code LIKE ? OR league_name LIKE ?) ORDER BY name LIMIT ?",
                (season, like, like, like, like, limit))
        return await self.fetchall("SELECT * FROM teams WHERE season=? ORDER BY league_name,name LIMIT ?", (season, limit))

    async def get_team(self, team_api_id: int, season: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM teams WHERE api_id=? AND season=?", (team_api_id, season))

    async def get_players_for_team(self, team_api_id: int, season: int) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM players WHERE team_api_id=? AND season=? ORDER BY position,name", (team_api_id, season))

    async def get_cache_meta(self, key: str) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM api_cache_meta WHERE cache_key=?", (key,))

    async def set_cache_meta(self, key: str, value: Any, expires_at: str | None) -> None:
        await self.execute(
            """INSERT INTO api_cache_meta(cache_key,value_json,expires_at,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(cache_key) DO UPDATE SET value_json=excluded.value_json,expires_at=excluded.expires_at,updated_at=excluded.updated_at""",
            (key, self.dumps(value), expires_at, utc_now_iso()))

    async def upsert_lineup_submission(self, **kw: Any) -> int:
        await self.execute(
            """INSERT INTO lineup_submissions(scope_key,match_id,guild_id,channel_id,user_id,team_api_id,team_name,formation,captain_player_id,goalkeeper_player_id,starters_json,bench_json,source,validated,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(scope_key) DO UPDATE SET match_id=excluded.match_id,guild_id=excluded.guild_id,channel_id=excluded.channel_id,user_id=excluded.user_id,
               team_api_id=excluded.team_api_id,team_name=excluded.team_name,formation=excluded.formation,captain_player_id=excluded.captain_player_id,
               goalkeeper_player_id=excluded.goalkeeper_player_id,starters_json=excluded.starters_json,bench_json=excluded.bench_json,source=excluded.source,
               validated=excluded.validated,updated_at=excluded.updated_at""",
            (kw["scope_key"], kw.get("match_id"), kw.get("guild_id"), kw.get("channel_id"), kw.get("user_id"), kw["team_api_id"], kw.get("team_name"),
             kw["formation"], kw.get("captain_player_id"), kw.get("goalkeeper_player_id"), self.dumps(kw["starters"]), self.dumps(kw["bench"]),
             kw.get("source"), 1 if kw.get("validated", True) else 0, utc_now_iso()))
        row = await self.fetchone("SELECT id FROM lineup_submissions WHERE scope_key=?", (kw["scope_key"],))
        return int(row["id"]) if row else 0

    async def get_lineup_submission(self, scope_key: str) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM lineup_submissions WHERE scope_key=?", (scope_key,))

    async def upsert_tactics_submission(self, **kw: Any) -> int:
        await self.execute(
            """INSERT INTO tactics_submissions(scope_key,match_id,guild_id,channel_id,user_id,team_api_id,team_name,tactics_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(scope_key) DO UPDATE SET match_id=excluded.match_id,guild_id=excluded.guild_id,channel_id=excluded.channel_id,user_id=excluded.user_id,
               team_api_id=excluded.team_api_id,team_name=excluded.team_name,tactics_json=excluded.tactics_json,updated_at=excluded.updated_at""",
            (kw["scope_key"], kw.get("match_id"), kw.get("guild_id"), kw.get("channel_id"), kw.get("user_id"), kw["team_api_id"], kw.get("team_name"), self.dumps(kw["tactics"]), utc_now_iso()))
        row = await self.fetchone("SELECT id FROM tactics_submissions WHERE scope_key=?", (kw["scope_key"],))
        return int(row["id"]) if row else 0

    async def get_tactics_submission(self, scope_key: str) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM tactics_submissions WHERE scope_key=?", (scope_key,))

    async def save_match(self, match: dict[str, Any]) -> None:
        await self.execute(
            """INSERT INTO matches(id,guild_id,channel_id,message_id,home_team_api_id,away_team_api_id,home_team_name,away_team_name,mode,status,current_minute,phase,state_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET guild_id=excluded.guild_id,channel_id=excluded.channel_id,message_id=excluded.message_id,
               home_team_api_id=excluded.home_team_api_id,away_team_api_id=excluded.away_team_api_id,home_team_name=excluded.home_team_name,away_team_name=excluded.away_team_name,
               mode=excluded.mode,status=excluded.status,current_minute=excluded.current_minute,phase=excluded.phase,state_json=excluded.state_json,updated_at=excluded.updated_at""",
            (match["id"], match.get("guild_id"), match.get("channel_id"), match.get("message_id"), match["home_team_api_id"], match["away_team_api_id"],
             match["home_team_name"], match["away_team_name"], match["mode"], match["status"], match.get("current_minute", 0), match.get("phase", "pre_kickoff"),
             self.dumps(match.get("state") or match), utc_now_iso()))

    async def get_match(self, match_id: str) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM matches WHERE id=?", (match_id,))

    async def list_recoverable_matches(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM matches WHERE status IN ('running','break','halftime','setup') ORDER BY updated_at DESC")

    async def upsert_match_participant(self, **kw: Any) -> None:
        await self.execute(
            """INSERT INTO match_participants(match_id,side,team_api_id,manager_user_id,is_bot,lineup_submission_id,tactics_submission_id)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(match_id,side) DO UPDATE SET team_api_id=excluded.team_api_id,manager_user_id=excluded.manager_user_id,is_bot=excluded.is_bot,
               lineup_submission_id=excluded.lineup_submission_id,tactics_submission_id=excluded.tactics_submission_id""",
            (kw["match_id"], kw["side"], kw["team_api_id"], kw.get("manager_user_id"), 1 if kw.get("is_bot") else 0, kw.get("lineup_submission_id"), kw.get("tactics_submission_id")))

    async def append_match_event(self, **kw: Any) -> None:
        await self.execute(
            """INSERT OR REPLACE INTO match_events(match_id,seq,minute,extra_minute,phase,event_type,team_side,player_api_id,payload_json,commentary,public_payload_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (kw["match_id"], kw["seq"], kw.get("minute", 0), kw.get("extra_minute", 0), kw.get("phase"), kw["event_type"], kw.get("team_side"),
             kw.get("player_api_id"), self.dumps(kw.get("payload") or {}), kw.get("commentary"), self.dumps(kw.get("public_payload") or kw.get("payload") or {})))

    async def get_match_events(self, match_id: str, after_seq: int = -1) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM match_events WHERE match_id=? AND seq>? ORDER BY seq ASC", (match_id, after_seq))
