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
    dob TEXT,
    nationality TEXT,
    photo TEXT,
    position TEXT,
    detailed_position TEXT,
    number INTEGER,
    injured INTEGER NOT NULL DEFAULT 0,
    height_cm INTEGER,
    weight_kg INTEGER,
    preferred_foot TEXT,
    overall REAL,
    potential REAL,
    status TEXT DEFAULT 'active',
    attributes_json TEXT,
    statistics_json TEXT,
    raw_json TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (api_id, team_api_id, season)
);
CREATE INDEX IF NOT EXISTS idx_players_team_season ON players(team_api_id, season);
CREATE INDEX IF NOT EXISTS idx_players_api_season ON players(api_id, season);

CREATE TABLE IF NOT EXISTS player_career_stats (
    player_api_id INTEGER PRIMARY KEY,
    name TEXT,
    current_team_api_id INTEGER,
    matches_played INTEGER NOT NULL DEFAULT 0,
    starts INTEGER NOT NULL DEFAULT 0,
    minutes_played INTEGER NOT NULL DEFAULT 0,
    goals INTEGER NOT NULL DEFAULT 0,
    assists INTEGER NOT NULL DEFAULT 0,
    yellow_cards INTEGER NOT NULL DEFAULT 0,
    red_cards INTEGER NOT NULL DEFAULT 0,
    clean_sheets INTEGER NOT NULL DEFAULT 0,
    saves INTEGER NOT NULL DEFAULT 0,
    rating_sum REAL NOT NULL DEFAULT 0,
    rating_count INTEGER NOT NULL DEFAULT 0,
    avg_rating REAL NOT NULL DEFAULT 0,
    potm_awards INTEGER NOT NULL DEFAULT 0,
    trophies INTEGER NOT NULL DEFAULT 0,
    current_ovr REAL,
    current_pot REAL,
    previous_ovr REAL,
    rating_progression_json TEXT,
    career_history_json TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS player_match_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_api_id INTEGER NOT NULL,
    match_id TEXT NOT NULL,
    team_api_id INTEGER,
    opponent_api_id INTEGER,
    season INTEGER,
    competition TEXT,
    started INTEGER NOT NULL DEFAULT 0,
    minutes_played INTEGER NOT NULL DEFAULT 0,
    goals INTEGER NOT NULL DEFAULT 0,
    assists INTEGER NOT NULL DEFAULT 0,
    yellow_cards INTEGER NOT NULL DEFAULT 0,
    red_cards INTEGER NOT NULL DEFAULT 0,
    clean_sheets INTEGER NOT NULL DEFAULT 0,
    saves INTEGER NOT NULL DEFAULT 0,
    rating REAL,
    motm INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(player_api_id, match_id)
);
CREATE INDEX IF NOT EXISTS idx_pmh_player ON player_match_history(player_api_id);
CREATE INDEX IF NOT EXISTS idx_pmh_match ON player_match_history(match_id);

CREATE TABLE IF NOT EXISTS player_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_api_id INTEGER NOT NULL,
    from_team_api_id INTEGER,
    to_team_api_id INTEGER,
    season INTEGER,
    transfer_date TEXT,
    transfer_type TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pt_player ON player_transfers(player_api_id);

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

CREATE TABLE IF NOT EXISTS seasons (
    id TEXT PRIMARY KEY,
    guild_id INTEGER,
    league_key TEXT NOT NULL,
    season_year INTEGER NOT NULL,
    name TEXT,
    status TEXT NOT NULL DEFAULT 'active', -- setup|active|completed|archived
    current_matchday INTEGER NOT NULL DEFAULT 1,
    total_matchdays INTEGER NOT NULL DEFAULT 0,
    auto_advance INTEGER NOT NULL DEFAULT 0,
    rules_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_seasons_guild_league ON seasons(guild_id, league_key, status);

CREATE TABLE IF NOT EXISTS season_teams (
    season_id TEXT NOT NULL,
    team_api_id INTEGER NOT NULL,
    team_name TEXT NOT NULL,
    league_key TEXT NOT NULL,
    manager_user_id INTEGER,
    is_user_controlled INTEGER NOT NULL DEFAULT 0,
    short_name TEXT,
    logo TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (season_id, team_api_id),
    FOREIGN KEY (season_id) REFERENCES seasons(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_season_teams_season ON season_teams(season_id);

CREATE TABLE IF NOT EXISTS fixtures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id TEXT NOT NULL,
    league_key TEXT NOT NULL,
    matchday INTEGER NOT NULL,
    home_team_api_id INTEGER NOT NULL,
    away_team_api_id INTEGER NOT NULL,
    home_team_name TEXT NOT NULL,
    away_team_name TEXT NOT NULL,
    home_score INTEGER,
    away_score INTEGER,
    status TEXT NOT NULL DEFAULT 'pending', -- pending|live|played|postponed|cancelled
    match_id TEXT,
    kickoff_at TEXT,
    finalized_at TEXT,
    finalized INTEGER NOT NULL DEFAULT 0,
    meta_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (season_id) REFERENCES seasons(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_fixtures_season_matchday ON fixtures(season_id, matchday);
CREATE INDEX IF NOT EXISTS idx_fixtures_season_status ON fixtures(season_id, status);
CREATE INDEX IF NOT EXISTS idx_fixtures_match_id ON fixtures(match_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fixtures_unique_pair ON fixtures(season_id, matchday, home_team_api_id, away_team_api_id);

CREATE TABLE IF NOT EXISTS standings (
    season_id TEXT NOT NULL,
    league_key TEXT NOT NULL,
    team_api_id INTEGER NOT NULL,
    team_name TEXT NOT NULL,
    played INTEGER NOT NULL DEFAULT 0,
    won INTEGER NOT NULL DEFAULT 0,
    drawn INTEGER NOT NULL DEFAULT 0,
    lost INTEGER NOT NULL DEFAULT 0,
    goals_for INTEGER NOT NULL DEFAULT 0,
    goals_against INTEGER NOT NULL DEFAULT 0,
    goal_difference INTEGER NOT NULL DEFAULT 0,
    points INTEGER NOT NULL DEFAULT 0,
    home_played INTEGER NOT NULL DEFAULT 0,
    home_won INTEGER NOT NULL DEFAULT 0,
    home_drawn INTEGER NOT NULL DEFAULT 0,
    home_lost INTEGER NOT NULL DEFAULT 0,
    home_gf INTEGER NOT NULL DEFAULT 0,
    home_ga INTEGER NOT NULL DEFAULT 0,
    away_played INTEGER NOT NULL DEFAULT 0,
    away_won INTEGER NOT NULL DEFAULT 0,
    away_drawn INTEGER NOT NULL DEFAULT 0,
    away_lost INTEGER NOT NULL DEFAULT 0,
    away_gf INTEGER NOT NULL DEFAULT 0,
    away_ga INTEGER NOT NULL DEFAULT 0,
    form TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (season_id, team_api_id),
    FOREIGN KEY (season_id) REFERENCES seasons(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_standings_season_rank ON standings(season_id, points DESC, goal_difference DESC, goals_for DESC);

CREATE TABLE IF NOT EXISTS season_player_stats (
    season_id TEXT NOT NULL,
    team_api_id INTEGER NOT NULL,
    player_api_id INTEGER NOT NULL,
    player_name TEXT,
    appearances INTEGER NOT NULL DEFAULT 0,
    starts INTEGER NOT NULL DEFAULT 0,
    minutes_played INTEGER NOT NULL DEFAULT 0,
    goals INTEGER NOT NULL DEFAULT 0,
    assists INTEGER NOT NULL DEFAULT 0,
    yellow_cards INTEGER NOT NULL DEFAULT 0,
    red_cards INTEGER NOT NULL DEFAULT 0,
    clean_sheets INTEGER NOT NULL DEFAULT 0,
    saves INTEGER NOT NULL DEFAULT 0,
    rating_sum REAL NOT NULL DEFAULT 0,
    rating_count INTEGER NOT NULL DEFAULT 0,
    avg_rating REAL NOT NULL DEFAULT 0,
    motm INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (season_id, team_api_id, player_api_id),
    FOREIGN KEY (season_id) REFERENCES seasons(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sps_season_goals ON season_player_stats(season_id, goals DESC);

CREATE TABLE IF NOT EXISTS season_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id TEXT,
    guild_id INTEGER,
    player_api_id INTEGER NOT NULL,
    player_name TEXT,
    from_team_api_id INTEGER,
    from_team_name TEXT,
    to_team_api_id INTEGER NOT NULL,
    to_team_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending', -- pending|confirmed|cancelled|failed
    requested_by INTEGER,
    confirmed_by INTEGER,
    transfer_type TEXT DEFAULT 'manual',
    note TEXT,
    meta_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_season_transfers_status ON season_transfers(status, season_id);
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

    async def _ensure_column(self, table: str, column: str, col_def: str) -> None:
        rows = await self.fetchall(f"PRAGMA table_info({table})")
        existing = {str(r["name"]) for r in rows}
        if column not in existing:
            await self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")

    async def initialize(self) -> None:
        await self.require_conn().executescript(SCHEMA)
        # lightweight migrations for existing DBs
        for col, defn in [
            ("dob", "TEXT"), ("height_cm", "INTEGER"), ("weight_kg", "INTEGER"),
            ("preferred_foot", "TEXT"), ("overall", "REAL"), ("potential", "REAL"),
            ("status", "TEXT DEFAULT 'active'"),
        ]:
            try:
                await self._ensure_column("players", col, defn)
            except Exception:
                pass
        for col, defn in [
            ("submission_deadline_at", "TEXT"), ("submission_state_json", "TEXT"),
            ("season_id", "TEXT"), ("fixture_id", "INTEGER"),
            ("league_key", "TEXT"), ("competition_type", "TEXT DEFAULT 'friendly'"),
        ]:
            try:
                await self._ensure_column("matches", col, defn)
            except Exception:
                pass
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
        rows = []
        for p in players:
            rows.append((
                int(p["api_id"]), season, team_api_id, p.get("name") or "", p.get("firstname"), p.get("lastname"),
                p.get("age"), p.get("dob"), p.get("nationality"), p.get("photo"), p.get("position"), p.get("detailed_position"),
                p.get("number"), 1 if p.get("injured") else 0, p.get("height_cm"), p.get("weight_kg"), p.get("preferred_foot"),
                p.get("overall"), p.get("potential"), p.get("status") or "active",
                self.dumps(p.get("attributes") or {}), self.dumps(p.get("statistics") or {}), self.dumps(p.get("raw") or p), utc_now_iso(),
            ))
        await self.executemany(
            """INSERT INTO players(api_id,season,team_api_id,name,firstname,lastname,age,dob,nationality,photo,position,detailed_position,number,injured,height_cm,weight_kg,preferred_foot,overall,potential,status,attributes_json,statistics_json,raw_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(api_id,team_api_id,season) DO UPDATE SET
               name=excluded.name,firstname=excluded.firstname,lastname=excluded.lastname,age=excluded.age,dob=excluded.dob,nationality=excluded.nationality,
               photo=excluded.photo,position=excluded.position,detailed_position=excluded.detailed_position,number=excluded.number,
               injured=excluded.injured,height_cm=excluded.height_cm,weight_kg=excluded.weight_kg,preferred_foot=excluded.preferred_foot,
               overall=excluded.overall,potential=excluded.potential,status=excluded.status,
               attributes_json=excluded.attributes_json,statistics_json=excluded.statistics_json,raw_json=excluded.raw_json,updated_at=excluded.updated_at""", rows)

    async def delete_stale_players_for_team(self, team_api_id: int, season: int, keep_api_ids: list[int]) -> None:
        if not keep_api_ids:
            await self.execute("DELETE FROM players WHERE team_api_id=? AND season=?", (team_api_id, season))
            return
        placeholders = ",".join("?" for _ in keep_api_ids)
        params = [team_api_id, season, *[int(x) for x in keep_api_ids]]
        await self.execute(f"DELETE FROM players WHERE team_api_id=? AND season=? AND api_id NOT IN ({placeholders})", params)

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
            """INSERT INTO matches(id,guild_id,channel_id,message_id,home_team_api_id,away_team_api_id,home_team_name,away_team_name,mode,status,current_minute,phase,state_json,updated_at,season_id,fixture_id,league_key,competition_type)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET guild_id=excluded.guild_id,channel_id=excluded.channel_id,message_id=excluded.message_id,
               home_team_api_id=excluded.home_team_api_id,away_team_api_id=excluded.away_team_api_id,home_team_name=excluded.home_team_name,away_team_name=excluded.away_team_name,
               mode=excluded.mode,status=excluded.status,current_minute=excluded.current_minute,phase=excluded.phase,state_json=excluded.state_json,updated_at=excluded.updated_at,
               season_id=excluded.season_id,fixture_id=excluded.fixture_id,league_key=excluded.league_key,competition_type=excluded.competition_type""",
            (match["id"], match.get("guild_id"), match.get("channel_id"), match.get("message_id"), match["home_team_api_id"], match["away_team_api_id"],
             match["home_team_name"], match["away_team_name"], match["mode"], match["status"], match.get("current_minute", 0), match.get("phase", "pre_kickoff"),
             self.dumps(match.get("state") or match), utc_now_iso(),
             match.get("season_id"), match.get("fixture_id"), match.get("league_key"), match.get("competition_type", "friendly")))

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

    async def get_match_participants(self, match_id: str) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM match_participants WHERE match_id=?", (match_id,))

    async def append_match_event(self, **kw: Any) -> None:
        await self.execute(
            """INSERT OR REPLACE INTO match_events(match_id,seq,minute,extra_minute,phase,event_type,team_side,player_api_id,payload_json,commentary,public_payload_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (kw["match_id"], kw["seq"], kw.get("minute", 0), kw.get("extra_minute", 0), kw.get("phase"), kw["event_type"], kw.get("team_side"),
             kw.get("player_api_id"), self.dumps(kw.get("payload") or {}), kw.get("commentary"), self.dumps(kw.get("public_payload") or kw.get("payload") or {})))

    async def get_match_events(self, match_id: str, after_seq: int = -1) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM match_events WHERE match_id=? AND seq>? ORDER BY seq ASC", (match_id, after_seq))

    async def search_players(self, query: str, season: int, limit: int = 25) -> list[aiosqlite.Row]:
        like = f"%{(query or '').strip()}%"
        return await self.fetchall(
            """SELECT p.*, t.name as team_name, t.league_name
               FROM players p LEFT JOIN teams t ON t.api_id=p.team_api_id AND t.season=p.season
               WHERE p.season=? AND (p.name LIKE ? OR p.firstname LIKE ? OR p.lastname LIKE ?)
               ORDER BY p.overall DESC NULLS LAST, p.name LIMIT ?""",
            (season, like, like, like, limit),
        )

    async def upsert_player_career_stats(self, player_api_id: int, patch: dict[str, Any]) -> None:
        row = await self.fetchone("SELECT * FROM player_career_stats WHERE player_api_id=?", (player_api_id,))
        if not row:
            await self.execute(
                """INSERT INTO player_career_stats(player_api_id,name,current_team_api_id,matches_played,starts,minutes_played,goals,assists,yellow_cards,red_cards,clean_sheets,saves,rating_sum,rating_count,avg_rating,potm_awards,trophies,current_ovr,current_pot,previous_ovr,rating_progression_json,career_history_json,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    player_api_id, patch.get("name"), patch.get("current_team_api_id"),
                    int(patch.get("matches_played", 0)), int(patch.get("starts", 0)), int(patch.get("minutes_played", 0)),
                    int(patch.get("goals", 0)), int(patch.get("assists", 0)), int(patch.get("yellow_cards", 0)), int(patch.get("red_cards", 0)),
                    int(patch.get("clean_sheets", 0)), int(patch.get("saves", 0)),
                    float(patch.get("rating_sum", 0.0)), int(patch.get("rating_count", 0)), float(patch.get("avg_rating", 0.0)),
                    int(patch.get("potm_awards", 0)), int(patch.get("trophies", 0)),
                    patch.get("current_ovr"), patch.get("current_pot"), patch.get("previous_ovr"),
                    self.dumps(patch.get("rating_progression") or []), self.dumps(patch.get("career_history") or []), utc_now_iso(),
                ),
            )
            return
        cur = dict(row)
        rating_progression = self.loads(cur.get("rating_progression_json")) or []
        career_history = self.loads(cur.get("career_history_json")) or []
        # apply increments
        for k in ["matches_played","starts","minutes_played","goals","assists","yellow_cards","red_cards","clean_sheets","saves","rating_count","potm_awards","trophies"]:
            if k in patch:
                cur[k] = int(cur.get(k) or 0) + int(patch.get(k) or 0)
        if "rating_sum" in patch:
            cur["rating_sum"] = float(cur.get("rating_sum") or 0.0) + float(patch.get("rating_sum") or 0.0)
        if cur.get("rating_count", 0) > 0:
            cur["avg_rating"] = float(cur.get("rating_sum", 0.0)) / float(cur["rating_count"])
        if patch.get("name"):
            cur["name"] = patch["name"]
        if patch.get("current_team_api_id") is not None:
            cur["current_team_api_id"] = patch["current_team_api_id"]
        if patch.get("current_ovr") is not None:
            cur["previous_ovr"] = cur.get("current_ovr")
            cur["current_ovr"] = patch["current_ovr"]
        if patch.get("current_pot") is not None:
            cur["current_pot"] = patch["current_pot"]
        if patch.get("rating_progression_entry"):
            rating_progression.append(patch["rating_progression_entry"])
            rating_progression = rating_progression[-200:]
        if patch.get("career_history_entry"):
            career_history.append(patch["career_history_entry"])
            career_history = career_history[-500:]
        await self.execute(
            """UPDATE player_career_stats SET name=?,current_team_api_id=?,matches_played=?,starts=?,minutes_played=?,goals=?,assists=?,yellow_cards=?,red_cards=?,clean_sheets=?,saves=?,rating_sum=?,rating_count=?,avg_rating=?,potm_awards=?,trophies=?,current_ovr=?,current_pot=?,previous_ovr=?,rating_progression_json=?,career_history_json=?,updated_at=?
               WHERE player_api_id=?""",
            (
                cur.get("name"), cur.get("current_team_api_id"), int(cur.get("matches_played") or 0), int(cur.get("starts") or 0), int(cur.get("minutes_played") or 0),
                int(cur.get("goals") or 0), int(cur.get("assists") or 0), int(cur.get("yellow_cards") or 0), int(cur.get("red_cards") or 0),
                int(cur.get("clean_sheets") or 0), int(cur.get("saves") or 0), float(cur.get("rating_sum") or 0.0), int(cur.get("rating_count") or 0), float(cur.get("avg_rating") or 0.0),
                int(cur.get("potm_awards") or 0), int(cur.get("trophies") or 0), cur.get("current_ovr"), cur.get("current_pot"), cur.get("previous_ovr"),
                self.dumps(rating_progression), self.dumps(career_history), utc_now_iso(), player_api_id,
            ),
        )

    async def record_player_match_history(self, row: dict[str, Any]) -> None:
        await self.execute(
            """INSERT OR REPLACE INTO player_match_history(player_api_id,match_id,team_api_id,opponent_api_id,season,competition,started,minutes_played,goals,assists,yellow_cards,red_cards,clean_sheets,saves,rating,motm,result)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(row["player_api_id"]), str(row["match_id"]), row.get("team_api_id"), row.get("opponent_api_id"), row.get("season"), row.get("competition"),
                1 if row.get("started") else 0, int(row.get("minutes_played") or 0), int(row.get("goals") or 0), int(row.get("assists") or 0),
                int(row.get("yellow_cards") or 0), int(row.get("red_cards") or 0), int(row.get("clean_sheets") or 0), int(row.get("saves") or 0),
                row.get("rating"), 1 if row.get("motm") else 0, row.get("result"),
            ),
        )

    async def get_player_career_stats(self, player_api_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM player_career_stats WHERE player_api_id=?", (player_api_id,))
