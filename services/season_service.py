from __future__ import annotations

import json
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any

from db.database import Database, utc_now_iso
from engine.match_engine import MatchEngine
from engine.models import Team, default_tactics, new_match_id
from engine.ai_manager import generate_ai_tactics
from services.lineup_service import auto_build_lineup

logger = logging.getLogger(__name__)

LEAGUE_KEYS = ["premier_league", "la_liga", "bundesliga", "serie_a", "ligue_1"]
EUROPEAN_COMPETITION_KEYS = ["champions_league", "europa_league", "conference_league"]
ALL_COMPETITION_KEYS = LEAGUE_KEYS + EUROPEAN_COMPETITION_KEYS
LEAGUE_DISPLAY = {
    "premier_league": "Premier League",
    "la_liga": "La Liga",
    "bundesliga": "Bundesliga",
    "serie_a": "Serie A",
    "ligue_1": "Ligue 1",
    "champions_league": "Champions League",
    "europa_league": "Europa League",
    "conference_league": "Conference League",
}

EUROPEAN_COMPETITION_CONFIG: dict[str, dict[str, Any]] = {
    # Traditional 32-team group stage: 8 groups of 4, 4 pots of 8, double round robin.
    # Same-association (domestic league) clashes avoided in group draw when possible.
    "champions_league": {
        "key": "champions_league",
        "name": "UEFA Champions League",
        "short_name": "UCL",
        "team_count": 32,
        "group_count": 8,
        "teams_per_group": 4,
        "pot_count": 4,
        "pots_size": 8,
        "double_round_robin": True,
    },
    "europa_league": {
        "key": "europa_league",
        "name": "UEFA Europa League",
        "short_name": "UEL",
        "team_count": 32,
        "group_count": 8,
        "teams_per_group": 4,
        "pot_count": 4,
        "pots_size": 8,
        "double_round_robin": True,
    },
    "conference_league": {
        "key": "conference_league",
        "name": "UEFA Conference League",
        "short_name": "UECL",
        "team_count": 32,
        "group_count": 8,
        "teams_per_group": 4,
        "pot_count": 4,
        "pots_size": 8,
        "double_round_robin": True,
    },
}

# Fair, varied per-team budgets (no two teams identical, no overpowered budgets).
TEAM_BUDGETS: dict[str, dict[str, int]] = {
    "Manchester City": {"transfer_budget": 155000000, "wage_budget": 4200000},
    "Arsenal": {"transfer_budget": 145000000, "wage_budget": 3900000},
    "Liverpool": {"transfer_budget": 140000000, "wage_budget": 3800000},
    "Manchester United": {"transfer_budget": 135000000, "wage_budget": 4000000},
    "Chelsea": {"transfer_budget": 130000000, "wage_budget": 3700000},
    "Tottenham Hotspur": {"transfer_budget": 115000000, "wage_budget": 3200000},
    "Newcastle United": {"transfer_budget": 110000000, "wage_budget": 3100000},
    "Aston Villa": {"transfer_budget": 95000000, "wage_budget": 2800000},
    "Brighton & Hove Albion": {"transfer_budget": 85000000, "wage_budget": 2400000},
    "West Ham United": {"transfer_budget": 90000000, "wage_budget": 2600000},
    "Crystal Palace": {"transfer_budget": 70000000, "wage_budget": 2100000},
    "Fulham": {"transfer_budget": 75000000, "wage_budget": 2200000},
    "Brentford": {"transfer_budget": 65000000, "wage_budget": 1900000},
    "Everton": {"transfer_budget": 60000000, "wage_budget": 2000000},
    "Nottingham Forest": {"transfer_budget": 72000000, "wage_budget": 2300000},
    "AFC Bournemouth": {"transfer_budget": 58000000, "wage_budget": 1800000},
    "Leeds United": {"transfer_budget": 68000000, "wage_budget": 2050000},
    "Sunderland": {"transfer_budget": 55000000, "wage_budget": 1700000},
    "Ipswich Town": {"transfer_budget": 48000000, "wage_budget": 1500000},
    "Coventry City": {"transfer_budget": 45000000, "wage_budget": 1400000},
    "Hull City": {"transfer_budget": 42000000, "wage_budget": 1350000},
    "Real Madrid": {"transfer_budget": 150000000, "wage_budget": 4100000},
    "FC Barcelona": {"transfer_budget": 85100000, "wage_budget": 2610000},
    "Atlético Madrid": {"transfer_budget": 120000000, "wage_budget": 3400000},
    "Athletic Bilbao": {"transfer_budget": 78000000, "wage_budget": 2310000},
    "Real Sociedad": {"transfer_budget": 72100000, "wage_budget": 2150000},
    "Villarreal CF": {"transfer_budget": 70100000, "wage_budget": 2110000},
    "Real Betis": {"transfer_budget": 65100000, "wage_budget": 1950000},
    "Sevilla FC": {"transfer_budget": 68100000, "wage_budget": 2010000},
    "Valencia CF": {"transfer_budget": 60100000, "wage_budget": 1850000},
    "Celta Vigo": {"transfer_budget": 52000000, "wage_budget": 1600000},
    "CA Osasuna": {"transfer_budget": 48100000, "wage_budget": 1510000},
    "Rayo Vallecano": {"transfer_budget": 50000000, "wage_budget": 1550000},
    "Getafe CF": {"transfer_budget": 46000000, "wage_budget": 1450000},
    "RCD Espanyol": {"transfer_budget": 44000000, "wage_budget": 1410000},
    "Deportivo Alavés": {"transfer_budget": 40000000, "wage_budget": 1300000},
    "Elche CF": {"transfer_budget": 38000000, "wage_budget": 1250000},
    "Levante UD": {"transfer_budget": 36000000, "wage_budget": 1200000},
    "Deportivo La Coruña": {"transfer_budget": 39000000, "wage_budget": 1280000},
    "Málaga CF": {"transfer_budget": 35000000, "wage_budget": 1180000},
    "Racing Santander": {"transfer_budget": 34000000, "wage_budget": 1150000},
    "Bayern Munich": {"transfer_budget": 145100000, "wage_budget": 4010000},
    "Borussia Dortmund": {"transfer_budget": 115100000, "wage_budget": 3300000},
    "Bayer Leverkusen": {"transfer_budget": 105000000, "wage_budget": 3000000},
    "RB Leipzig": {"transfer_budget": 100000000, "wage_budget": 2900000},
    "Eintracht Frankfurt": {"transfer_budget": 82000000, "wage_budget": 2500000},
    "VfB Stuttgart": {"transfer_budget": 80000000, "wage_budget": 2450000},
    "SC Freiburg": {"transfer_budget": 62000000, "wage_budget": 1910000},
    "TSG Hoffenheim": {"transfer_budget": 64000000, "wage_budget": 1960000},
    "Werder Bremen": {"transfer_budget": 56000000, "wage_budget": 1750000},
    "Borussia Mönchengladbach": {"transfer_budget": 58100000, "wage_budget": 1810000},
    "1. FC Köln": {"transfer_budget": 54000000, "wage_budget": 1710000},
    "Union Berlin": {"transfer_budget": 52100000, "wage_budget": 1650000},
    "Mainz 05": {"transfer_budget": 50100000, "wage_budget": 1610000},
    "FC Augsburg": {"transfer_budget": 46100000, "wage_budget": 1460000},
    "Hamburger SV": {"transfer_budget": 60200000, "wage_budget": 1860000},
    "Schalke 04": {"transfer_budget": 57000000, "wage_budget": 1780000},
    "SC Paderborn": {"transfer_budget": 42100000, "wage_budget": 1310000},
    "SV Elversberg": {"transfer_budget": 38100000, "wage_budget": 1210000},
    "Inter Milan": {"transfer_budget": 125000000, "wage_budget": 3500000},
    "Juventus": {"transfer_budget": 120100000, "wage_budget": 3410000},
    "AC Milan": {"transfer_budget": 115200000, "wage_budget": 3310000},
    "Napoli": {"transfer_budget": 110100000, "wage_budget": 3210000},
    "Roma": {"transfer_budget": 95100000, "wage_budget": 2810000},
    "Atalanta": {"transfer_budget": 88000000, "wage_budget": 2620000},
    "Lazio": {"transfer_budget": 78100000, "wage_budget": 2350000},
    "Fiorentina": {"transfer_budget": 72200000, "wage_budget": 2210000},
    "Bologna": {"transfer_budget": 70200000, "wage_budget": 2160000},
    "Torino": {"transfer_budget": 58200000, "wage_budget": 1820000},
    "Udinese": {"transfer_budget": 52200000, "wage_budget": 1660000},
    "Genoa": {"transfer_budget": 54100000, "wage_budget": 1720000},
    "Como": {"transfer_budget": 62100000, "wage_budget": 1920000},
    "Cagliari": {"transfer_budget": 48200000, "wage_budget": 1520000},
    "Parma": {"transfer_budget": 50200000, "wage_budget": 1560000},
    "Lecce": {"transfer_budget": 44100000, "wage_budget": 1420000},
    "Sassuolo": {"transfer_budget": 56100000, "wage_budget": 1760000},
    "Monza": {"transfer_budget": 46200000, "wage_budget": 1470000},
    "Venezia": {"transfer_budget": 40100000, "wage_budget": 1290000},
    "Frosinone": {"transfer_budget": 42200000, "wage_budget": 1320000},
    "Paris Saint-Germain": {"transfer_budget": 160000000, "wage_budget": 4500000},
    "Olympique Marseille": {"transfer_budget": 95200000, "wage_budget": 2700000},
    "AS Monaco": {"transfer_budget": 90100000, "wage_budget": 2630000},
    "Olympique Lyonnais": {"transfer_budget": 85200000, "wage_budget": 2510000},
    "Lille OSC": {"transfer_budget": 78200000, "wage_budget": 2320000},
    "RC Lens": {"transfer_budget": 72300000, "wage_budget": 2170000},
    "Stade Rennais FC": {"transfer_budget": 68200000, "wage_budget": 2060000},
    "OGC Nice": {"transfer_budget": 70300000, "wage_budget": 2120000},
    "RC Strasbourg Alsace": {"transfer_budget": 55100000, "wage_budget": 1730000},
    "Toulouse FC": {"transfer_budget": 52300000, "wage_budget": 1620000},
    "Stade Brestois 29": {"transfer_budget": 50300000, "wage_budget": 1570000},
    "FC Lorient": {"transfer_budget": 48300000, "wage_budget": 1530000},
    "Le Havre AC": {"transfer_budget": 45100000, "wage_budget": 1430000},
    "Angers SCO": {"transfer_budget": 43000000, "wage_budget": 1360000},
    "AJ Auxerre": {"transfer_budget": 44200000, "wage_budget": 1380000},
    "Paris FC": {"transfer_budget": 46300000, "wage_budget": 1480000},
    "Le Mans FC": {"transfer_budget": 40200000, "wage_budget": 1260000},
    "ES Troyes AC": {"transfer_budget": 41000000, "wage_budget": 1330000},
}

LEAGUE_BUDGET_BASELINES: dict[str, dict[str, int]] = {
    "premier_league": {"transfer_budget": 70000000, "wage_budget": 2100000},
    "la_liga": {"transfer_budget": 60000000, "wage_budget": 1900000},
    "bundesliga": {"transfer_budget": 65000000, "wage_budget": 2000000},
    "serie_a": {"transfer_budget": 62000000, "wage_budget": 1950000},
    "ligue_1": {"transfer_budget": 58000000, "wage_budget": 1850000},
}

def _budget_unique_nudge(team_name: str, league_key: str | None) -> tuple[int, int]:
    # Deterministic small nudge so teams without explicit entry still differ.
    base = f"{league_key}:{team_name}"
    h = sum((i + 1) * ord(c) for i, c in enumerate(base))
    return (h % 37) * 100000, (h % 29) * 10000

def default_season_budgets(league_key: str | None, team_name: str | None = None) -> dict[str, int]:
    if team_name and team_name in TEAM_BUDGETS:
        b = TEAM_BUDGETS[team_name]
        return {"transfer_budget": int(b["transfer_budget"]), "wage_budget": int(b["wage_budget"])}
    base = LEAGUE_BUDGET_BASELINES.get(str(league_key or ""), {"transfer_budget": 55000000, "wage_budget": 1700000})
    t_nudge, w_nudge = _budget_unique_nudge(team_name or "Unknown", league_key)
    return {"transfer_budget": int(base["transfer_budget"]) + t_nudge, "wage_budget": int(base["wage_budget"]) + w_nudge}

def _season_id() -> str:
    return f"sea_{uuid.uuid4().hex[:12]}"


def round_robin_pairs(team_ids: list[int]) -> list[list[tuple[int, int]]]:
    """Double round-robin using circle method. Returns list of matchdays, each a list of (home, away) pairs."""
    ids = list(team_ids)
    if len(ids) < 2:
        return []
    # If odd, add a bye
    bye = None
    if len(ids) % 2 == 1:
        ids.append(-1)
        bye = -1
    n = len(ids)
    half = n // 2
    arr = ids[:]
    rounds = []
    for _ in range(n - 1):
        left = arr[:half]
        right = list(reversed(arr[half:]))
        pairs = []
        for a, b in zip(left, right):
            if a == bye or b == bye:
                continue
            pairs.append((a, b))
        rounds.append(pairs)
        # rotate: keep first fixed
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]
    # Second leg: swap home/away
    second = [[(a, b) for (b, a) in rnd] for rnd in rounds]
    return rounds + second


def _standings_sort_key(r: dict[str, Any]) -> tuple:
    # points desc, gd desc, gf desc, name asc
    return (-int(r.get("points", 0)), -int(r.get("goal_difference", 0)), -int(r.get("goals_for", 0)), str(r.get("team_name", "")).lower())


class SeasonService:
    def __init__(self, db: Database):
        self.db = db

    async def get_season(self, season_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM seasons WHERE id=?", (season_id,))
        return dict(row) if row else None

    async def ensure_season_budgets(self, season_id: str) -> None:
        season = await self.get_season(str(season_id))
        if not season:
            return
        league_key = str(season.get("league_key") or "")
        now = utc_now_iso()
        old_uniform = {
            "premier_league": (120_000_000, 3_000_000),
            "la_liga": (110_000_000, 2_800_000),
            "bundesliga": (105_000_000, 2_700_000),
            "serie_a": (105_000_000, 2_700_000),
            "ligue_1": (100_000_000, 2_500_000),
        }
        old_t, old_w = old_uniform.get(league_key, (0, 0))
        rows = await self.db.fetchall(
            "SELECT team_api_id, team_name, transfer_budget, wage_budget FROM season_teams WHERE season_id=?",
            (str(season_id),),
        )
        for r in rows:
            d = dict(r)
            tb = int(d.get("transfer_budget") or 0)
            wb = int(d.get("wage_budget") or 0)
            name = str(d.get("team_name") or "")
            per_team = default_season_budgets(league_key, name)
            should_set = (tb == 0 and wb == 0) or (old_t and old_w and tb == old_t and wb == old_w)
            # Reset FC Barcelona from the known bad uniform/default state to its intended budget.
            if name == "FC Barcelona" and old_t and old_w and tb == old_t and wb == old_w:
                should_set = True
            if should_set:
                await self.db.execute(
                    "UPDATE season_teams SET transfer_budget=?, wage_budget=?, updated_at=? WHERE season_id=? AND team_api_id=?",
                    (int(per_team["transfer_budget"]), int(per_team["wage_budget"]), now, str(season_id), int(d.get("team_api_id") or 0)),
                )

        # Reconcile confirmed transfers to ensure players are on the correct teams.
        try:
            confirmed = await self.db.fetchall(
                "SELECT id, player_api_id, to_team_api_id, meta_json FROM season_transfers WHERE season_id=? AND status='confirmed'",
                (str(season_id),),
            )
            for tr in confirmed:
                td = dict(tr)
                try:
                    meta = json.loads(td.get("meta_json") or "{}")
                except Exception:
                    meta = {}
                syear = int(meta.get("season_year") or season.get("season_year") or 2026)
                pid = int(td.get("player_api_id") or 0)
                to_tid = int(td.get("to_team_api_id") or 0)
                if not pid or not to_tid:
                    continue
                dest = await self.db.fetchone(
                    "SELECT 1 FROM players WHERE api_id=? AND team_api_id=? AND season=? LIMIT 1",
                    (pid, to_tid, syear),
                )
                if not dest:
                    try:
                        await self.confirm_transfer(int(td.get("id") or 0))
                    except Exception:
                        logger.exception("Failed to reconcile confirmed transfer %s", td.get("id"))
        except Exception:
            logger.exception("Failed to reconcile confirmed transfers for season %s", season_id)

    async def get_active_season(self, guild_id: int | None, league_key: str | None = None) -> dict[str, Any] | None:
        if league_key:
            row = await self.db.fetchone(
                "SELECT * FROM seasons WHERE guild_id IS ? AND league_key=? AND status='active' ORDER BY created_at DESC LIMIT 1",
                (guild_id, league_key),
            )
        else:
            row = await self.db.fetchone(
                "SELECT * FROM seasons WHERE guild_id IS ? AND status='active' ORDER BY created_at DESC LIMIT 1",
                (guild_id,),
            )
        if row is None and guild_id is not None:
            # fallback: any guild season
            if league_key:
                row = await self.db.fetchone(
                    "SELECT * FROM seasons WHERE league_key=? AND status='active' ORDER BY created_at DESC LIMIT 1",
                    (league_key,),
                )
            else:
                row = await self.db.fetchone(
                    "SELECT * FROM seasons WHERE status='active' ORDER BY created_at DESC LIMIT 1",
                )
        if row:
            try:
                await self.ensure_season_budgets(str(dict(row).get("id")))
            except Exception:
                pass
        return dict(row) if row else None

    async def list_seasons(self, guild_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM seasons WHERE 1=1"
        params: list[Any] = []
        if guild_id is not None:
            q += " AND guild_id=?"
            params.append(guild_id)
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY created_at DESC LIMIT 50"
        rows = await self.db.fetchall(q, tuple(params))
        return [dict(r) for r in rows]

    async def archive_season(self, season_id: str) -> dict[str, Any] | None:
        season = await self.get_season(str(season_id))
        if not season:
            return None
        if str(season.get("status") or "") == "archived":
            return season
        now = utc_now_iso()
        await self.db.execute(
            "UPDATE seasons SET status='archived', updated_at=? WHERE id=?",
            (now, str(season_id)),
        )
        return await self.get_season(str(season_id))

    async def create_season(self, *, guild_id: int | None, league_key: str, season_year: int, name: str | None = None, auto_advance: bool = False) -> dict[str, Any]:
        if league_key not in ALL_COMPETITION_KEYS:
            raise ValueError(f"Unsupported league_key: {league_key}")
        # Prevent duplicate active season for same guild+league
        existing = await self.db.fetchone(
            "SELECT id FROM seasons WHERE guild_id IS ? AND league_key=? AND status='active'",
            (guild_id, league_key),
        )
        if existing:
            raise ValueError(f"An active season already exists for {LEAGUE_DISPLAY.get(league_key, league_key)} in this guild. Use it or archive it first.")

        # European competitions: create season shell; pots/fixtures generated separately.
        if league_key in EUROPEAN_COMPETITION_KEYS:
            season_id = _season_id()
            now = utc_now_iso()
            cfg = self.get_european_competition_config(league_key)
            display = name or f"{cfg.get('name', LEAGUE_DISPLAY.get(league_key, league_key))} {season_year}/{str(season_year+1)[-2:]}"
            rules = {"competition_type": "european_group_stage", "european": cfg}
            await self.db.execute(
                """INSERT INTO seasons(id,guild_id,league_key,season_year,name,status,current_matchday,total_matchdays,auto_advance,rules_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,'active',1,0,?,?, ?,?)""",
                (season_id, guild_id, league_key, int(season_year), display, 1 if auto_advance else 0, json.dumps(rules), now, now),
            )
            return await self.get_season(season_id)  # type: ignore

        from data.football_data import list_all_teams
        teams = list_all_teams(league_key)
        if len(teams) < 2:
            raise ValueError(f"Not enough teams found for {league_key}")

        team_ids = [int(t["api_id"]) for t in teams]
        matchdays = round_robin_pairs(team_ids)
        season_id = _season_id()
        now = utc_now_iso()
        display = name or f"{LEAGUE_DISPLAY.get(league_key, league_key)} {season_year}/{str(season_year+1)[-2:]}"
        rules = {"points_win": 3, "points_draw": 1, "points_loss": 0, "tiebreakers": ["points", "gd", "gf", "name"]}

        await self.db.execute(
            """INSERT INTO seasons(id,guild_id,league_key,season_year,name,status,current_matchday,total_matchdays,auto_advance,rules_json,created_at,updated_at)
               VALUES(?,?,?,?,?,'active',1,?,?,?, ?,?)""",
            (season_id, guild_id, league_key, int(season_year), display, len(matchdays), 1 if auto_advance else 0, json.dumps(rules), now, now),
        )

        # season teams + standings
        for t in teams:
            tid = int(t["api_id"])
            tname = str(t.get("name") or tid)
            short = t.get("short_name") or t.get("code")
            logo = t.get("logo")
            budgets = default_season_budgets(league_key, tname)
            await self.db.execute(
                """INSERT OR REPLACE INTO season_teams(season_id,team_api_id,team_name,league_key,manager_user_id,is_user_controlled,short_name,logo,transfer_budget,wage_budget,created_at,updated_at)
                   VALUES(?,?,?,?,NULL,0,?,?,?,?,?,?)""",
                (season_id, tid, tname, league_key, short, logo, int(budgets["transfer_budget"]), int(budgets["wage_budget"]), now, now),
            )
            await self.db.execute(
                """INSERT OR REPLACE INTO standings(season_id,league_key,team_api_id,team_name,played,won,drawn,lost,goals_for,goals_against,goal_difference,points,home_played,home_won,home_drawn,home_lost,home_gf,home_ga,away_played,away_won,away_drawn,away_lost,away_gf,away_ga,form,updated_at)
                   VALUES(?,?,?,?,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'',?)""",
                (season_id, league_key, tid, tname, now),
            )

        # fixtures
        team_by_id = {int(t["api_id"]): t for t in teams}
        for md_idx, pairs in enumerate(matchdays, start=1):
            for home_id, away_id in pairs:
                ht = team_by_id.get(int(home_id))
                at = team_by_id.get(int(away_id))
                if not ht or not at:
                    continue
                await self.db.execute(
                    """INSERT INTO fixtures(season_id,league_key,matchday,home_team_api_id,away_team_api_id,home_team_name,away_team_name,home_score,away_score,status,match_id,kickoff_at,finalized_at,finalized,meta_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,NULL,NULL,'pending',NULL,NULL,NULL,0,NULL,?,?)""",
                    (season_id, league_key, md_idx, int(home_id), int(away_id), str(ht.get("name")), str(at.get("name")), now, now),
                )

        try:
            await self._sync_team_claims_to_season(season_id)
        except Exception:
            logger.exception("Failed to sync team claims to season %s", season_id)

        return await self.get_season(season_id)  # type: ignore

    async def get_standings(self, season_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM standings WHERE season_id=?", (season_id,))
        items = [dict(r) for r in rows]
        items.sort(key=_standings_sort_key)
        for i, r in enumerate(items, start=1):
            r["rank"] = i
        return items

    async def get_fixtures(self, season_id: str, matchday: int | None = None, status: str | None = None, team_api_id: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM fixtures WHERE season_id=?"
        params: list[Any] = [season_id]
        if matchday is not None:
            q += " AND matchday=?"
            params.append(int(matchday))
        if status:
            q += " AND status=?"
            params.append(status)
        if team_api_id is not None:
            q += " AND (home_team_api_id=? OR away_team_api_id=?)"
            params.extend([int(team_api_id), int(team_api_id)])
        q += " ORDER BY matchday ASC, id ASC"
        rows = await self.db.fetchall(q, tuple(params))
        return [dict(r) for r in rows]

    async def get_fixture(self, fixture_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM fixtures WHERE id=?", (int(fixture_id),))
        return dict(row) if row else None

    async def get_current_matchday_fixtures(self, season_id: str) -> list[dict[str, Any]]:
        season = await self.get_season(season_id)
        if not season:
            return []
        return await self.get_fixtures(season_id, matchday=int(season.get("current_matchday", 1)))

    async def set_fixture_match(self, fixture_id: int, match_id: str, status: str = "live") -> None:
        now = utc_now_iso()
        await self.db.execute(
            "UPDATE fixtures SET match_id=?, status=?, updated_at=? WHERE id=?",
            (match_id, status, now, int(fixture_id)),
        )

    async def mark_fixture_live(self, fixture_id: int, match_id: str) -> None:
        await self.set_fixture_match(fixture_id, match_id, status="live")

    async def is_matchday_complete(self, season_id: str, matchday: int) -> bool:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS c FROM fixtures WHERE season_id=? AND matchday=? AND status NOT IN ('played','cancelled')",
            (season_id, int(matchday)),
        )
        return int(row["c"] if row else 0) == 0

    async def advance_matchday_if_complete(self, season_id: str) -> dict[str, Any]:
        season = await self.get_season(season_id)
        if not season:
            return {"advanced": False, "reason": "season_not_found"}
        if season.get("status") != "active":
            return {"advanced": False, "reason": "season_not_active", "season": season}
        md = int(season.get("current_matchday", 1))
        total = int(season.get("total_matchdays", 0))
        if not await self.is_matchday_complete(season_id, md):
            return {"advanced": False, "reason": "matchday_incomplete", "current_matchday": md, "season": season}
        if md >= total:
            await self.db.execute("UPDATE seasons SET status='completed', updated_at=? WHERE id=?", (utc_now_iso(), season_id))
            season = await self.get_season(season_id)
            return {"advanced": False, "reason": "season_completed", "current_matchday": md, "season": season}
        new_md = md + 1
        await self.db.execute("UPDATE seasons SET current_matchday=?, updated_at=? WHERE id=?", (new_md, utc_now_iso(), season_id))
        season = await self.get_season(season_id)
        return {"advanced": True, "from_matchday": md, "to_matchday": new_md, "season": season}

    async def _apply_standings_for_fixture(self, season_id: str, fixture: dict[str, Any], *, reverse: bool = False) -> None:
        """Apply (or reverse) a fixture result to standings. reverse=True removes the result (for repair/recalc)."""
        hs = fixture.get("home_score")
        as_ = fixture.get("away_score")
        if hs is None or as_ is None:
            return
        hs = int(hs); as_ = int(as_)
        home_id = int(fixture["home_team_api_id"])
        away_id = int(fixture["away_team_api_id"])
        sign = -1 if reverse else 1

        def outcome_for(home: bool) -> str:
            if hs == as_:
                return "D"
            if home:
                return "W" if hs > as_ else "L"
            return "W" if as_ > hs else "L"

        for team_id, is_home in ((home_id, True), (away_id, False)):
            row = await self.db.fetchone("SELECT * FROM standings WHERE season_id=? AND team_api_id=?", (season_id, team_id))
            if not row:
                continue
            s = dict(row)
            gf = hs if is_home else as_
            ga = as_ if is_home else hs
            out = outcome_for(is_home)
            pts = 3 if out == "W" else (1 if out == "D" else 0)
            s["played"] = int(s.get("played", 0)) + sign * 1
            s["won"] = int(s.get("won", 0)) + sign * (1 if out == "W" else 0)
            s["drawn"] = int(s.get("drawn", 0)) + sign * (1 if out == "D" else 0)
            s["lost"] = int(s.get("lost", 0)) + sign * (1 if out == "L" else 0)
            s["goals_for"] = int(s.get("goals_for", 0)) + sign * gf
            s["goals_against"] = int(s.get("goals_against", 0)) + sign * ga
            s["goal_difference"] = int(s["goals_for"]) - int(s["goals_against"])
            s["points"] = int(s.get("points", 0)) + sign * pts
            prefix = "home_" if is_home else "away_"
            s[prefix + "played"] = int(s.get(prefix + "played", 0)) + sign * 1
            s[prefix + "won"] = int(s.get(prefix + "won", 0)) + sign * (1 if out == "W" else 0)
            s[prefix + "drawn"] = int(s.get(prefix + "drawn", 0)) + sign * (1 if out == "D" else 0)
            s[prefix + "lost"] = int(s.get(prefix + "lost", 0)) + sign * (1 if out == "L" else 0)
            s[prefix + "gf"] = int(s.get(prefix + "gf", 0)) + sign * gf
            s[prefix + "ga"] = int(s.get(prefix + "ga", 0)) + sign * ga
            # form: last 5, most recent at right
            form = str(s.get("form") or "")
            if reverse:
                # remove last occurrence of out from right
                if form.endswith(out):
                    form = form[:-1]
                else:
                    idx = form.rfind(out)
                    if idx != -1:
                        form = form[:idx] + form[idx+1:]
            else:
                form = (form + out)[-5:]
            s["form"] = form
            await self.db.execute(
                """UPDATE standings SET played=?,won=?,drawn=?,lost=?,goals_for=?,goals_against=?,goal_difference=?,points=?,
                   home_played=?,home_won=?,home_drawn=?,home_lost=?,home_gf=?,home_ga=?,
                   away_played=?,away_won=?,away_drawn=?,away_lost=?,away_gf=?,away_ga=?,
                   form=?,updated_at=? WHERE season_id=? AND team_api_id=?""",
                (int(s["played"]), int(s["won"]), int(s["drawn"]), int(s["lost"]), int(s["goals_for"]), int(s["goals_against"]), int(s["goal_difference"]), int(s["points"]),
                 int(s["home_played"]), int(s["home_won"]), int(s["home_drawn"]), int(s["home_lost"]), int(s["home_gf"]), int(s["home_ga"]),
                 int(s["away_played"]), int(s["away_won"]), int(s["away_drawn"]), int(s["away_lost"]), int(s["away_gf"]), int(s["away_ga"]),
                 s["form"], utc_now_iso(), season_id, team_id),
            )

    async def _upsert_season_player_stats(self, season_id: str, team_api_id: int, player_api_id: int, player_name: str, patch: dict[str, Any]) -> None:
        row = await self.db.fetchone(
            "SELECT * FROM season_player_stats WHERE season_id=? AND team_api_id=? AND player_api_id=?",
            (season_id, int(team_api_id), int(player_api_id)),
        )
        now = utc_now_iso()
        if not row:
            apps = int(patch.get("appearances", 0))
            rs = float(patch.get("rating_sum", 0.0))
            rc = int(patch.get("rating_count", 0))
            avg = (rs / rc) if rc else 0.0
            await self.db.execute(
                """INSERT INTO season_player_stats(season_id,team_api_id,player_api_id,player_name,appearances,starts,minutes_played,goals,assists,yellow_cards,red_cards,clean_sheets,saves,rating_sum,rating_count,avg_rating,motm,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (season_id, int(team_api_id), int(player_api_id), player_name,
                 apps, int(patch.get("starts", 0)), int(patch.get("minutes_played", 0)), int(patch.get("goals", 0)), int(patch.get("assists", 0)),
                 int(patch.get("yellow_cards", 0)), int(patch.get("red_cards", 0)), int(patch.get("clean_sheets", 0)), int(patch.get("saves", 0)),
                 rs, rc, avg, int(patch.get("motm", 0)), now),
            )
            return
        cur = dict(row)
        for k in ["appearances","starts","minutes_played","goals","assists","yellow_cards","red_cards","clean_sheets","saves","rating_count","motm"]:
            if k in patch:
                cur[k] = int(cur.get(k) or 0) + int(patch.get(k) or 0)
        if "rating_sum" in patch:
            cur["rating_sum"] = float(cur.get("rating_sum") or 0.0) + float(patch.get("rating_sum") or 0.0)
        rc = int(cur.get("rating_count") or 0)
        cur["avg_rating"] = (float(cur.get("rating_sum") or 0.0) / rc) if rc else 0.0
        if player_name:
            cur["player_name"] = player_name
        await self.db.execute(
            """UPDATE season_player_stats SET player_name=?,appearances=?,starts=?,minutes_played=?,goals=?,assists=?,yellow_cards=?,red_cards=?,clean_sheets=?,saves=?,rating_sum=?,rating_count=?,avg_rating=?,motm=?,updated_at=?
               WHERE season_id=? AND team_api_id=? AND player_api_id=?""",
            (cur.get("player_name"), int(cur.get("appearances") or 0), int(cur.get("starts") or 0), int(cur.get("minutes_played") or 0),
             int(cur.get("goals") or 0), int(cur.get("assists") or 0), int(cur.get("yellow_cards") or 0), int(cur.get("red_cards") or 0),
             int(cur.get("clean_sheets") or 0), int(cur.get("saves") or 0), float(cur.get("rating_sum") or 0.0), int(cur.get("rating_count") or 0), float(cur.get("avg_rating") or 0.0),
             int(cur.get("motm") or 0), now, season_id, int(team_api_id), int(player_api_id)),
        )

    async def finalize_fixture_for_match(self, *, season_id: str, fixture_id: int, match_state: Any) -> dict[str, Any]:
        """Idempotent: apply fixture result + standings + season player stats once per fixture."""
        fixture = await self.get_fixture(fixture_id)
        if not fixture:
            return {"ok": False, "reason": "fixture_not_found"}
        if str(fixture.get("season_id")) != str(season_id):
            return {"ok": False, "reason": "season_mismatch"}
        if int(fixture.get("finalized") or 0) == 1:
            return {"ok": True, "already_finalized": True, "fixture": fixture}

        st = match_state
        status = getattr(st, "status", None)
        if status != "finished":
            return {"ok": False, "reason": "match_not_finished"}

        home_score = int(getattr(st.home, "score", 0))
        away_score = int(getattr(st.away, "score", 0))
        now = utc_now_iso()

        # Mark played and set score
        await self.db.execute(
            """UPDATE fixtures SET home_score=?, away_score=?, status='played', match_id=?, finalized=1, finalized_at=?, updated_at=?
               WHERE id=? AND finalized=0""",
            (home_score, away_score, str(getattr(st, "id", "")), now, now, int(fixture_id)),
        )
        # Re-read to confirm we won the race
        fixture2 = await self.get_fixture(fixture_id)
        if not fixture2:
            return {"ok": False, "reason": "fixture_missing_after_update"}
        if int(fixture2.get("finalized") or 0) == 1 and str(fixture2.get("match_id") or "") != str(getattr(st, "id", "")):
            # Another process finalized it with a different match - do not apply stats again
            return {"ok": True, "already_finalized": True, "fixture": fixture2}

        # Apply standings
        fixture2["home_score"] = home_score
        fixture2["away_score"] = away_score
        await self._apply_standings_for_fixture(season_id, fixture2, reverse=False)

        # Season player stats
        try:
            from engine.report import man_of_the_match
            motm = man_of_the_match(st) or {}
            motm_name = motm.get("name")
        except Exception:
            motm_name = None

        for side in ("home", "away"):
            team = st.home if side == "home" else st.away
            opp = st.away if side == "home" else st.home
            team_goals = int(team.score)
            opp_goals = int(opp.score)
            clean_sheet = 1 if opp_goals == 0 else 0
            for ps in st.player_states.values():
                if ps.team_side != side:
                    continue
                if not (ps.on_pitch or ps.minutes_played > 0 or ps.substituted_in_minute is not None or ps.is_starter):
                    continue
                minutes = int(ps.minutes_played or 0)
                is_motm = 1 if motm_name and ps.name == motm_name else 0
                cs = 1 if (clean_sheet and ps.position == "GK" and minutes > 0) else 0
                patch = {
                    "appearances": 1 if (minutes > 0 or ps.is_starter or ps.substituted_in_minute is not None) else 0,
                    "starts": 1 if ps.is_starter else 0,
                    "minutes_played": minutes,
                    "goals": int(ps.goals or 0),
                    "assists": int(ps.assists or 0),
                    "yellow_cards": int(ps.yellow_cards or 0),
                    "red_cards": 1 if ps.red_card else 0,
                    "clean_sheets": cs,
                    "saves": int(ps.saves or 0),
                    "rating_sum": float(ps.rating or 0.0),
                    "rating_count": 1 if (minutes > 0 or ps.is_starter) else 0,
                    "motm": is_motm,
                }
                if patch["appearances"] or patch["rating_count"]:
                    try:
                        await self._upsert_season_player_stats(season_id, int(team.team.api_id), int(ps.player_api_id), str(ps.name), patch)
                    except Exception:
                        logger.exception("Failed to upsert season player stats season=%s player=%s", season_id, ps.player_api_id)

        fixture_final = await self.get_fixture(fixture_id)
        return {"ok": True, "already_finalized": False, "fixture": fixture_final, "home_score": home_score, "away_score": away_score}

    async def quick_sim_fixture(self, *, season_id: str, fixture_id: int, guild_id: int | None = None, channel_id: int | None = None) -> dict[str, Any]:
        """Simulate a pending fixture headlessly to completion and finalize it. Idempotent if already played."""
        fixture = await self.get_fixture(fixture_id)
        if not fixture:
            return {"ok": False, "reason": "fixture_not_found"}
        if str(fixture.get("season_id")) != str(season_id):
            return {"ok": False, "reason": "season_mismatch"}
        if fixture.get("status") == "played" or int(fixture.get("finalized") or 0) == 1:
            return {"ok": True, "already_played": True, "fixture": fixture}
        if fixture.get("status") == "live" and fixture.get("match_id"):
            # Do not quick-sim a live/user match
            return {"ok": False, "reason": "fixture_live", "fixture": fixture}

        from data.football_data import get_team, get_team_players
        home = get_team(int(fixture["home_team_api_id"]), fixture.get("league_key"))
        away = get_team(int(fixture["away_team_api_id"]), fixture.get("league_key"))
        if not home or not away:
            return {"ok": False, "reason": "team_not_found"}
        home_squad = get_team_players(int(fixture["home_team_api_id"]), fixture.get("league_key"))
        away_squad = get_team_players(int(fixture["away_team_api_id"]), fixture.get("league_key"))
        if not home_squad or not away_squad:
            return {"ok": False, "reason": "squad_not_found"}

        rng = random.Random()
        ht = Team(api_id=int(home["api_id"]), name=home.get("name", "Home"), league_id=home.get("league_id"), league_name=home.get("league_name"), country=home.get("country"), logo=home.get("logo"), short_name=home.get("short_name") or home.get("code"))
        at = Team(api_id=int(away["api_id"]), name=away.get("name", "Away"), league_id=away.get("league_id"), league_name=away.get("league_name"), country=away.get("country"), logo=away.get("logo"), short_name=away.get("short_name") or away.get("code"))
        hl = auto_build_lineup(ht.api_id, ht.name, home_squad, "4-3-3")
        al = auto_build_lineup(at.api_id, at.name, away_squad, "4-3-3")
        htac = generate_ai_tactics(rng)
        atac = generate_ai_tactics(rng)

        # Build Player objects
        from engine.models import Player, PlayerAttributes
        def to_players(squad, team_api_id):
            out = []
            for p in squad:
                attrs = p.get("attributes") or {}
                out.append(Player(api_id=int(p["api_id"]), name=p.get("name", "Unknown"), team_api_id=int(team_api_id),
                                  position=p.get("position","MID"), detailed_position=p.get("detailed_position"), number=p.get("number"),
                                  age=p.get("age"), nationality=p.get("nationality"), injured=bool(p.get("injured")),
                                  photo=p.get("photo"), height_cm=p.get("height_cm"), weight_kg=p.get("weight_kg"), preferred_foot=p.get("preferred_foot"),
                                  overall=float(p.get("overall") or 65), potential=float(p.get("potential") or p.get("overall") or 70),
                                  attributes=PlayerAttributes.from_dict(attrs), raw=p))
            return out

        home_players = to_players(home_squad, ht.api_id)
        away_players = to_players(away_squad, at.api_id)

        engine = MatchEngine.initialize(
            match_id=new_match_id(), guild_id=guild_id, channel_id=channel_id, mode="bot_vs_bot",
            home_team=ht, away_team=at, home_lineup=hl, away_lineup=al,
            home_tactics=htac, away_tactics=atac,
            home_players=home_players, away_players=away_players,
        )
        st = engine.state
        st.season_id = season_id
        st.fixture_id = int(fixture_id)
        st.league_key = fixture.get("league_key")
        st.competition_type = "league"
        st.submission["competition"] = LEAGUE_DISPLAY.get(fixture.get("league_key"), "League")
        st.submission["match_type"] = "League"
        st.status = "running"
        st.phase = "first_half"
        # mark fixture live
        await self.mark_fixture_live(int(fixture_id), st.id)

        # Run to completion headlessly, auto-resolving breaks
        max_loops = 400
        loops = 0
        while st.status not in ("finished", "cancelled") and loops < max_loops:
            loops += 1
            try:
                step = engine.simulate_until_break_or_end()
            except Exception:
                logger.exception("Quick sim step failed fixture=%s", fixture_id)
                break
            if st.status == "finished":
                break
            # auto-resolve any break
            if st.pending_break:
                try:
                    # simple AI decisions: no subs/tactics change, just mark done
                    engine.mark_break_decision("home", True)
                    engine.mark_break_decision("away", True)
                except Exception:
                    pass
                try:
                    # resume
                    engine.resume_after_break()
                except Exception:
                    # if resume fails, try to force continue by clearing pending_break
                    try:
                        st.pending_break = None
                        st.home_break_decision_done = False
                        st.away_break_decision_done = False
                        if st.status not in ("finished", "cancelled"):
                            st.status = "running"
                    except Exception:
                        pass
            else:
                # no break, but not finished - avoid tight loop
                if not step.events:
                    # nudge by trying to continue
                    try:
                        step2 = engine.simulate_until_break_or_end()
                        if not step2.events and st.status not in ("finished", "cancelled"):
                            break
                    except Exception:
                        break

        if st.status != "finished":
            # force-finish as draw with current score to avoid stuck fixture
            logger.warning("Quick sim did not finish cleanly fixture=%s status=%s minute=%s loops=%s", fixture_id, st.status, st.minute, loops)
            st.status = "finished"
            st.phase = "finished"

        # Persist match minimal record (state_json) so we have a record
        try:
            await self.db.save_match({
                "id": st.id, "guild_id": st.guild_id, "channel_id": st.channel_id, "message_id": st.message_id,
                "home_team_api_id": st.home.team.api_id, "away_team_api_id": st.away.team.api_id,
                "home_team_name": st.home.team.name, "away_team_name": st.away.team.name,
                "mode": st.mode, "status": st.status, "current_minute": st.minute, "phase": st.phase,
                "state": st.to_dict(), "season_id": season_id, "fixture_id": int(fixture_id),
                "league_key": st.league_key, "competition_type": "league",
            })
        except Exception:
            logger.exception("Failed to save quick-sim match fixture=%s", fixture_id)

        # Persist player career stats for this match too
        try:
            # build a lightweight session-like wrapper for persist_player_stats? easier to do it directly
            await self._persist_career_stats_for_state(st)
        except Exception:
            logger.exception("Failed to persist career stats for quick-sim fixture=%s", fixture_id)

        fin = await self.finalize_fixture_for_match(season_id=season_id, fixture_id=int(fixture_id), match_state=st)
        return {"ok": True, "already_played": False, "fixture": fin.get("fixture"), "home_score": st.home.score, "away_score": st.away.score, "match_id": st.id}

    async def _persist_career_stats_for_state(self, st: Any) -> bool:
        sub = getattr(st, "submission", {}) or {}
        if sub.get("stats_persisted"):
            return False
        if getattr(st, "status", None) != "finished":
            return False
        try:
            from engine.report import man_of_the_match
            motm = man_of_the_match(st) or {}
            motm_name = motm.get("name")
        except Exception:
            motm_name = None
        comp = sub.get("competition") or getattr(getattr(st.home.team, "league_name", None), "strip", lambda: None)() or "Friendly"
        for side in ("home", "away"):
            team = st.home if side == "home" else st.away
            opp = st.away if side == "home" else st.home
            team_goals = int(team.score)
            opp_goals = int(opp.score)
            result = "D"
            if team_goals > opp_goals: result = "W"
            elif team_goals < opp_goals: result = "L"
            clean_sheet = 1 if opp_goals == 0 else 0
            for ps in st.player_states.values():
                if ps.team_side != side:
                    continue
                if not (ps.on_pitch or ps.minutes_played > 0 or ps.substituted_in_minute is not None or ps.is_starter):
                    continue
                started = 1 if ps.is_starter else 0
                minutes = int(ps.minutes_played or 0)
                is_motm = 1 if motm_name and ps.name == motm_name else 0
                cs = 1 if (clean_sheet and ps.position == "GK" and minutes > 0) else 0
                row = {
                    "player_api_id": int(ps.player_api_id),
                    "match_id": str(st.id),
                    "team_api_id": int(team.team.api_id),
                    "opponent_api_id": int(opp.team.api_id),
                    "season": None,
                    "competition": comp,
                    "started": started,
                    "minutes_played": minutes,
                    "goals": int(ps.goals or 0),
                    "assists": int(ps.assists or 0),
                    "yellow_cards": int(ps.yellow_cards or 0),
                    "red_cards": 1 if ps.red_card else 0,
                    "clean_sheets": cs,
                    "saves": int(ps.saves or 0),
                    "rating": float(ps.rating or 0.0),
                    "motm": is_motm,
                    "result": result,
                }
                try:
                    await self.db.record_player_match_history(row)
                except Exception:
                    logger.exception("Failed to record player match history player=%s match=%s", ps.player_api_id, st.id)
                try:
                    patch = {
                        "name": ps.name,
                        "current_team_api_id": int(team.team.api_id),
                        "matches_played": 1 if (minutes > 0 or ps.on_pitch or ps.substituted_in_minute is not None or ps.is_starter) else 0,
                        "starts": started,
                        "minutes_played": minutes,
                        "goals": int(ps.goals or 0),
                        "assists": int(ps.assists or 0),
                        "yellow_cards": int(ps.yellow_cards or 0),
                        "red_cards": 1 if ps.red_card else 0,
                        "clean_sheets": cs,
                        "saves": int(ps.saves or 0),
                        "rating_sum": float(ps.rating or 0.0),
                        "rating_count": 1 if (minutes > 0 or ps.is_starter) else 0,
                        "potm_awards": is_motm,
                        "rating_progression_entry": {"match_id": str(st.id), "rating": float(ps.rating or 0.0), "date": (st.public_stats or {}).get("finished_at")},
                    }
                    if patch["matches_played"] or patch["rating_count"]:
                        await self.db.upsert_player_career_stats(int(ps.player_api_id), patch)
                except Exception:
                    logger.exception("Failed to upsert career stats player=%s match=%s", ps.player_api_id, st.id)
        sub["stats_persisted"] = True
        st.submission = sub
        return True

    async def finalize_match_if_season_fixture(self, match_state: Any) -> dict[str, Any]:
        """Call after a match finishes. If it's a season fixture, finalize fixture+standings+season stats. Idempotent."""
        st = match_state
        season_id = getattr(st, "season_id", None)
        fixture_id = getattr(st, "fixture_id", None)
        if not season_id or not fixture_id:
            return {"ok": True, "season_fixture": False}
        return await self.finalize_fixture_for_match(season_id=str(season_id), fixture_id=int(fixture_id), match_state=st)

    async def simulate_matchday_remaining(self, season_id: str, matchday: int, *, guild_id: int | None = None) -> list[dict[str, Any]]:
        fixtures = await self.get_fixtures(season_id, matchday=matchday)
        results = []
        for fx in fixtures:
            if fx.get("status") == "played" or int(fx.get("finalized") or 0) == 1:
                continue
            if fx.get("status") == "live":
                results.append({"fixture_id": fx["id"], "skipped": "live"})
                continue
            res = await self.quick_sim_fixture(season_id=season_id, fixture_id=int(fx["id"]), guild_id=guild_id)
            results.append({"fixture_id": fx["id"], **res})
        return results

    async def get_next_user_fixture(self, season_id: str, user_id: int) -> dict[str, Any] | None:
        # find team managed by user in this season
        row = await self.db.fetchone(
            "SELECT * FROM season_teams WHERE season_id=? AND manager_user_id=? LIMIT 1",
            (season_id, int(user_id)),
        )
        if not row:
            return None
        team_api_id = int(row["team_api_id"])
        season = await self.get_season(season_id)
        if not season:
            return None
        md = int(season.get("current_matchday", 1))
        # look from current matchday onward
        rows = await self.db.fetchall(
            """SELECT * FROM fixtures WHERE season_id=? AND matchday>=? AND status IN ('pending','live')
               AND (home_team_api_id=? OR away_team_api_id=?) ORDER BY matchday ASC, id ASC LIMIT 1""",
            (season_id, md, team_api_id, team_api_id),
        )
        return dict(rows[0]) if rows else None

    async def get_top_scorers(self, season_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """SELECT sps.*, st.team_name as team_name
               FROM season_player_stats sps
               LEFT JOIN season_teams st ON st.season_id=sps.season_id AND st.team_api_id=sps.team_api_id
               WHERE sps.season_id=? AND sps.goals>0
               ORDER BY sps.goals DESC, sps.assists DESC, sps.avg_rating DESC, sps.minutes_played ASC
               LIMIT ?""",
            (season_id, int(limit)),
        )
        return [dict(r) for r in rows]

    async def get_top_assisters(self, season_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """SELECT sps.*, st.team_name as team_name
               FROM season_player_stats sps
               LEFT JOIN season_teams st ON st.season_id=sps.season_id AND st.team_api_id=sps.team_api_id
               WHERE sps.season_id=? AND sps.assists>0
               ORDER BY sps.assists DESC, sps.goals DESC, sps.avg_rating DESC, sps.minutes_played ASC
               LIMIT ?""",
            (season_id, int(limit)),
        )
        return [dict(r) for r in rows]

    async def rebuild_standings(self, season_id: str) -> list[dict[str, Any]]:
        season = await self.get_season(season_id)
        if not season:
            return []
        # reset
        now = utc_now_iso()
        await self.db.execute(
            """UPDATE standings SET played=0,won=0,drawn=0,lost=0,goals_for=0,goals_against=0,goal_difference=0,points=0,
               home_played=0,home_won=0,home_drawn=0,home_lost=0,home_gf=0,home_ga=0,
               away_played=0,away_won=0,away_drawn=0,away_lost=0,away_gf=0,away_ga=0,form='',updated_at=?
               WHERE season_id=?""",
            (now, season_id),
        )
        fixtures = await self.db.fetchall(
            "SELECT * FROM fixtures WHERE season_id=? AND status='played' AND finalized=1 ORDER BY matchday ASC, id ASC",
            (season_id,),
        )
        for fx in fixtures:
            await self._apply_standings_for_fixture(season_id, dict(fx), reverse=False)
        return await self.get_standings(season_id)

    async def create_pending_transfer(self, *, season_id: str | None, guild_id: int | None, player_api_id: int, to_team_api_id: int, requested_by: int | None = None, note: str | None = None, transfer_type: str = "manual", fee: int = 0, wage: int = 0, signing_bonus: int = 0, contract_years: int | None = None, role: str | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        season_year = 2026
        if season_id:
            s = await self.get_season(season_id)
            if s:
                season_year = int(s.get("season_year") or 2026)
        # find player
        player = await self.db.fetchone("SELECT * FROM players WHERE api_id=? AND season=? ORDER BY updated_at DESC LIMIT 1", (int(player_api_id), season_year))
        if not player:
            return {"ok": False, "reason": "player_not_found"}
        p = dict(player)
        from_team_api_id = int(p.get("team_api_id")) if p.get("team_api_id") is not None else None
        player_name = p.get("name") or str(player_api_id)
        from_team = await self.db.fetchone("SELECT name FROM teams WHERE api_id=? AND season=?", (from_team_api_id, season_year)) if from_team_api_id else None
        to_team = await self.db.fetchone("SELECT name FROM teams WHERE api_id=? AND season=?", (int(to_team_api_id), season_year))
        from_team_name = (dict(from_team).get("name") if from_team else None) or str(from_team_api_id)
        to_team_name = (dict(to_team).get("name") if to_team else None) or str(to_team_api_id)
        if from_team_api_id == int(to_team_api_id):
            return {"ok": False, "reason": "player_already_in_team"}
        now = utc_now_iso()
        meta_obj = {"season_year": season_year}
        if isinstance(meta, dict):
            meta_obj.update(meta)
        cur = await self.db.execute(
            """INSERT INTO season_transfers(season_id,guild_id,player_api_id,player_name,from_team_api_id,from_team_name,to_team_api_id,to_team_name,status,requested_by,transfer_type,note,fee,wage,signing_bonus,contract_years,role,meta_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?, 'pending', ?,?,?,?,?,?,?,?, ?,?,?)""",
            (season_id, guild_id, int(player_api_id), player_name, from_team_api_id, from_team_name, int(to_team_api_id), to_team_name, requested_by, transfer_type, note, int(fee or 0), int(wage or 0), int(signing_bonus or 0), int(contract_years) if contract_years is not None else None, role, json.dumps(meta_obj), now, now),
        )
        # fetch id
        row = await self.db.fetchone("SELECT * FROM season_transfers WHERE rowid=?", (cur.lastrowid,))
        return {"ok": True, "transfer": dict(row) if row else {"id": cur.lastrowid, "player_name": player_name, "from_team_name": from_team_name, "to_team_name": to_team_name, "status": "pending"}}

    async def confirm_transfer(self, transfer_id: int, *, confirmed_by: int | None = None) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM season_transfers WHERE id=?", (int(transfer_id),))
        if not row:
            return {"ok": False, "reason": "transfer_not_found"}
        tr = dict(row)
        meta = {}
        try:
            meta = json.loads(tr.get("meta_json") or "{}")
        except Exception:
            meta = {}
        season_year = int(meta.get("season_year") or 2026)
        player_api_id = int(tr["player_api_id"])
        from_team_api_id = tr.get("from_team_api_id")
        to_team_api_id = int(tr["to_team_api_id"])
        if from_team_api_id is not None:
            from_team_api_id = int(from_team_api_id)

        fee = int(tr.get("fee") or 0)
        wage = int(tr.get("wage") or 0)
        signing_bonus = int(tr.get("signing_bonus") or 0)

        already_confirmed = str(tr.get("status") or "") == "confirmed"
        if already_confirmed:
            dest = await self.db.fetchone(
                "SELECT 1 FROM players WHERE api_id=? AND team_api_id=? AND season=? LIMIT 1",
                (player_api_id, to_team_api_id, season_year),
            )
            if dest:
                return {"ok": True, "already_confirmed": True, **tr}
            # fall through to re-apply move only (budgets/records guarded below)
        elif tr.get("status") not in ("pending", "failed"):
            return {"ok": False, "reason": f"transfer_status_{tr.get('status')}"}

        # Active matches must not block agreed transfers/finalization. Financial + roster updates are atomic below.

        season_id = tr.get("season_id")
        now = utc_now_iso()
        moved_any = False
        budget_applied = False

        async with self.db.transaction():
            # Re-read transfer status inside transaction
            row2 = await self.db.fetchone("SELECT * FROM season_transfers WHERE id=?", (int(transfer_id),))
            if not row2:
                return {"ok": False, "reason": "transfer_not_found"}
            tr2_cur = dict(row2)
            if str(tr2_cur.get("status") or "") == "confirmed":
                dest = await self.db.fetchone(
                    "SELECT 1 FROM players WHERE api_id=? AND team_api_id=? AND season=? LIMIT 1",
                    (player_api_id, to_team_api_id, season_year),
                )
                if dest:
                    return {"ok": True, "already_confirmed": True, **tr2_cur}
            elif str(tr2_cur.get("status") or "") not in ("pending", "failed"):
                return {"ok": False, "reason": f"transfer_status_{tr2_cur.get('status')}"}

            # Do not block finalization due to active matches.

            # Move player in players table (PK includes team_api_id)
            player_rows = await self.db.fetchall(
                "SELECT * FROM players WHERE api_id=? AND season=?",
                (player_api_id, season_year),
            )
            for pr in player_rows:
                pd = dict(pr)
                old_team = int(pd.get("team_api_id") or -1)
                if from_team_api_id is not None and old_team != from_team_api_id:
                    continue
                await self.db.execute("DELETE FROM players WHERE api_id=? AND team_api_id=? AND season=?", (player_api_id, old_team, season_year))
                pd["team_api_id"] = to_team_api_id
                if wage > 0:
                    pd["wage"] = wage
                await self.db.upsert_players([pd], to_team_api_id, season_year)
                moved_any = True
                if from_team_api_id is None:
                    from_team_api_id = old_team

            if not moved_any:
                any_player = await self.db.fetchone("SELECT * FROM players WHERE api_id=? ORDER BY season DESC, updated_at DESC LIMIT 1", (player_api_id,))
                if any_player:
                    pd = dict(any_player)
                    pd["team_api_id"] = to_team_api_id
                    if wage > 0:
                        pd["wage"] = wage
                    await self.db.upsert_players([pd], to_team_api_id, season_year)
                    moved_any = True

            # career stats current team
            try:
                await self.db.execute(
                    "UPDATE player_career_stats SET current_team_api_id=?, updated_at=? WHERE player_api_id=?",
                    (to_team_api_id, now, player_api_id),
                )
            except Exception:
                pass

            # season_player_stats - move rows for this season across team pk
            if season_id:
                sps_rows = await self.db.fetchall(
                    "SELECT * FROM season_player_stats WHERE season_id=? AND player_api_id=?",
                    (str(season_id), player_api_id),
                )
                for sr in sps_rows:
                    sd = dict(sr)
                    old_tid = int(sd.get("team_api_id") or -1)
                    if from_team_api_id is not None and old_tid != from_team_api_id:
                        continue
                    await self.db.execute(
                        "DELETE FROM season_player_stats WHERE season_id=? AND team_api_id=? AND player_api_id=?",
                        (str(season_id), old_tid, player_api_id),
                    )
                    sd["team_api_id"] = to_team_api_id
                    await self.db.execute(
                        """INSERT INTO season_player_stats(season_id,team_api_id,player_api_id,player_name,appearances,starts,minutes_played,goals,assists,yellow_cards,red_cards,clean_sheets,saves,rating_sum,rating_count,avg_rating,motm,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(season_id,team_api_id,player_api_id) DO UPDATE SET
                           player_name=excluded.player_name,appearances=excluded.appearances,starts=excluded.starts,minutes_played=excluded.minutes_played,
                           goals=excluded.goals,assists=excluded.assists,yellow_cards=excluded.yellow_cards,red_cards=excluded.red_cards,
                           clean_sheets=excluded.clean_sheets,saves=excluded.saves,rating_sum=excluded.rating_sum,rating_count=excluded.rating_count,
                           avg_rating=excluded.avg_rating,motm=excluded.motm,updated_at=excluded.updated_at""",
                        (str(season_id), to_team_api_id, player_api_id, sd.get("player_name"), int(sd.get("appearances") or 0), int(sd.get("starts") or 0), int(sd.get("minutes_played") or 0),
                         int(sd.get("goals") or 0), int(sd.get("assists") or 0), int(sd.get("yellow_cards") or 0), int(sd.get("red_cards") or 0),
                         int(sd.get("clean_sheets") or 0), int(sd.get("saves") or 0), float(sd.get("rating_sum") or 0.0), int(sd.get("rating_count") or 0), float(sd.get("avg_rating") or 0.0),
                         int(sd.get("motm") or 0), now),
                    )

            # record in player_transfers (skip on reconciliation to avoid duplicates)
            if not already_confirmed:
                try:
                    await self.db.execute(
                        """INSERT INTO player_transfers(player_api_id,from_team_api_id,to_team_api_id,season,transfer_date,transfer_type,note,created_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (player_api_id, from_team_api_id, to_team_api_id, season_year, now[:10], tr.get("transfer_type") or "manual", tr.get("note"), now),
                    )
                except Exception:
                    pass

            # Apply budgets (fee+signing bonus from buying transfer budget, wage from wage budget; credit selling transfer budget)
            if season_id and not already_confirmed:
                total_transfer_cost = max(0, fee) + max(0, signing_bonus)
                wage_cost = max(0, wage)
                try:
                    await self.ensure_season_budgets(str(season_id))
                except Exception:
                    pass
                bt_row = await self.db.fetchone(
                    "SELECT transfer_budget, wage_budget, team_name FROM season_teams WHERE season_id=? AND team_api_id=?",
                    (str(season_id), int(to_team_api_id)),
                )
                bt = dict(bt_row) if bt_row else {}
                tb = int(bt.get("transfer_budget") or 0)
                wb = int(bt.get("wage_budget") or 0)
                if not bt:
                    # No season team row - cannot safely deduct
                    if total_transfer_cost > 0 or wage_cost > 0:
                        raise RuntimeError("buying_team_not_in_season")
                if total_transfer_cost > tb:
                    raise RuntimeError(f"insufficient_transfer_budget:{tb}:{total_transfer_cost}")
                if wage_cost > wb:
                    raise RuntimeError(f"insufficient_wage_budget:{wb}:{wage_cost}")
                await self.db.execute(
                    """UPDATE season_teams
                       SET transfer_budget = MAX(0, COALESCE(transfer_budget,0) - ?),
                           wage_budget = MAX(0, COALESCE(wage_budget,0) - ?),
                           updated_at=?
                       WHERE season_id=? AND team_api_id=?""",
                    (total_transfer_cost, wage_cost, now, str(season_id), int(to_team_api_id)),
                )
                if from_team_api_id is not None and fee > 0:
                    await self.db.execute(
                        """UPDATE season_teams
                           SET transfer_budget = COALESCE(transfer_budget,0) + ?,
                               updated_at=?
                           WHERE season_id=? AND team_api_id=?""",
                        (max(0, fee), now, str(season_id), int(from_team_api_id)),
                    )
                budget_applied = bool(total_transfer_cost > 0 or wage_cost > 0 or (from_team_api_id is not None and fee > 0))

            await self.db.execute(
                "UPDATE season_transfers SET status='confirmed', confirmed_by=?, confirmed_at=?, updated_at=?, from_team_api_id=? WHERE id=?",
                (confirmed_by, now, now, from_team_api_id, int(transfer_id)),
            )

        tr2 = await self.db.fetchone("SELECT * FROM season_transfers WHERE id=?", (int(transfer_id),))
        out = dict(tr2) if tr2 else tr
        out["ok"] = True
        out["moved"] = moved_any
        out["budget_applied"] = budget_applied
        out["fee"] = fee
        out["wage"] = wage
        out["signing_bonus"] = signing_bonus
        return out

    async def retry_pending_transfers_for_teams(self, team_api_ids: list[int]) -> list[dict[str, Any]]:
        """Try to confirm pending transfers involving the given teams when no active match blocks them."""
        ids = {int(x) for x in (team_api_ids or []) if x is not None}
        if not ids:
            return []
        rows = await self.db.fetchall(
            "SELECT id, from_team_api_id, to_team_api_id FROM season_transfers WHERE status='pending' ORDER BY id ASC LIMIT 50"
        )
        results: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            from_id = int(d.get("from_team_api_id") or 0)
            to_id = int(d.get("to_team_api_id") or 0)
            if from_id not in ids and to_id not in ids:
                continue
            tid = int(d.get("id"))
            try:
                conf = await self.confirm_transfer(tid)
            except Exception as e:
                logger.exception("retry_pending_transfers_for_teams failed transfer_id=%s", tid)
                results.append({"id": tid, "ok": False, "reason": f"exception:{e.__class__.__name__}"})
                continue
            results.append({"id": tid, **conf})
        return results

    async def get_season_team(self, season_id: str, team_api_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM season_teams WHERE season_id=? AND team_api_id=?",
            (str(season_id), int(team_api_id)),
        )
        return dict(row) if row else None

    async def list_season_teams(self, season_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM season_teams WHERE season_id=? ORDER BY team_name ASC",
            (str(season_id),),
        )
        return [dict(r) for r in rows]

    async def find_team_in_active_seasons(self, guild_id: int | None, team_query: str, league_key: str | None = None) -> list[dict[str, Any]]:
        """Find season_teams matching name in seasons for a guild. Prefers active seasons, falls back to any status."""
        q = (team_query or "").strip().lower()
        if not q:
            return []

        async def _collect(status: str | None) -> list[dict[str, Any]]:
            seasons = await self.list_seasons(guild_id=guild_id, status=status)
            if league_key:
                seasons = [s for s in seasons if str(s.get("league_key")) == str(league_key)]
            if not seasons and guild_id is not None:
                seasons = await self.list_seasons(guild_id=None, status=status)
                if league_key:
                    seasons = [s for s in seasons if str(s.get("league_key")) == str(league_key)]
            # prefer active first when status is None
            seasons.sort(key=lambda s: 0 if str(s.get("status")) == "active" else 1)
            found: list[dict[str, Any]] = []
            for s in seasons:
                sid = str(s.get("id"))
                teams = await self.list_season_teams(sid)
                for t in teams:
                    name = str(t.get("team_name") or "").lower()
                    short = str(t.get("short_name") or "").lower()
                    if q == name or q == short or q in name or name.startswith(q):
                        rec = dict(t)
                        rec["season_name"] = s.get("name")
                        rec["season_league_key"] = s.get("league_key")
                        rec["season_guild_id"] = s.get("guild_id")
                        rec["season_status"] = s.get("status")
                        rec["_match_score"] = 3 if (q == name or q == short) else (2 if name.startswith(q) else 1)
                        found.append(rec)
            return found

        out = await _collect("active")
        if not out:
            out = await _collect(None)
        out.sort(key=lambda r: (-int(r.get("_match_score", 0)), 0 if str(r.get("season_status")) == "active" else 1, str(r.get("team_name") or "")))
        seen = set()
        deduped = []
        for r in out:
            k = (str(r.get("season_id")), int(r.get("team_api_id") or 0))
            if k in seen:
                continue
            seen.add(k)
            deduped.append(r)
        return deduped

    async def _resolve_team_for_claim(self, *, guild_id: int | None, team_query: str, league_key: str | None = None) -> dict[str, Any] | None:
        """Resolve a team for claiming from DB/bundled data (no season auto-create). Returns team dict with league_key."""
        q = (team_query or "").strip().lower()
        if not q:
            return None
        season_year = 2026
        rows = await self.db.fetchall(
            "SELECT * FROM teams WHERE season=?",
            (season_year,),
        )
        if not rows:
            rows = await self.db.fetchall("SELECT * FROM teams ORDER BY season DESC LIMIT 500")
        inv_league = {str(v).lower(): k for k, v in LEAGUE_DISPLAY.items()}
        inv_league.update({
            "english premier league": "premier_league",
            "premier league": "premier_league",
            "la liga": "la_liga",
            "bundesliga": "bundesliga",
            "serie a": "serie_a",
            "ligue 1": "ligue_1",
        })

        def _score_team(t: dict):
            name = str(t.get("name") or "").lower()
            short = str(t.get("short_name") or t.get("code") or "").lower()
            score = 0
            if q == name or q == short:
                score = 3
            elif name.startswith(q) or q in name:
                score = 2
            elif short and q in short:
                score = 1
            return score

        candidates = []
        for r in rows:
            t = dict(r)
            score = _score_team(t)
            if score:
                candidates.append((score, t))
        if not candidates:
            try:
                from data.bundled import list_league_keys as _lk2, list_all_teams as _lat2
                for lk in list(_lk2()):
                    for tt in (_lat2(lk) or []):
                        t = dict(tt)
                        t.setdefault("league_name", LEAGUE_DISPLAY.get(lk, lk))
                        score = _score_team(t)
                        if score:
                            t["_league_key"] = lk
                            candidates.append((score, t))
            except Exception:
                pass
        if not candidates:
            return None
        candidates.sort(key=lambda x: (-x[0], str(x[1].get("name") or "")))
        top_score = candidates[0][0]
        top = [t for s, t in candidates if s == top_score]
        if len(top) != 1:
            return {"ambiguous": True, "candidates": top[:10]}
        team = top[0]
        team_league_key = league_key or team.get("_league_key")
        if not team_league_key:
            lname = str(team.get("league_name") or "").lower().strip()
            team_league_key = inv_league.get(lname)
        if not team_league_key:
            try:
                from data.bundled import list_league_keys as _lk, list_all_teams as _lat
                for lk in (league_key and [league_key]) or list(_lk()):
                    for tt in (_lat(lk) or []):
                        if int(tt.get("api_id") or 0) == int(team.get("api_id") or -1):
                            team_league_key = lk
                            break
                    if team_league_key:
                        break
            except Exception:
                pass
        if not team_league_key:
            return None
        team["league_key"] = team_league_key
        return team

    async def _upsert_team_claim(self, *, guild_id: int | None, league_key: str, team_api_id: int, team_name: str, short_name: str | None, logo: str | None, user_id: int) -> dict[str, Any]:
        now = utc_now_iso()
        existing = await self.db.fetchone(
            "SELECT * FROM team_claims WHERE COALESCE(guild_id,-1)=COALESCE(?,-1) AND league_key=? AND team_api_id=?",
            (guild_id, str(league_key), int(team_api_id)),
        )
        if existing:
            await self.db.execute(
                "UPDATE team_claims SET team_name=?, short_name=?, logo=?, user_id=?, updated_at=? WHERE id=?",
                (team_name, short_name, logo, int(user_id), now, int(dict(existing)["id"])),
            )
            row = await self.db.fetchone("SELECT * FROM team_claims WHERE id=?", (int(dict(existing)["id"]),))
        else:
            cur = await self.db.execute(
                """INSERT INTO team_claims(guild_id,league_key,team_api_id,team_name,short_name,logo,user_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (guild_id, str(league_key), int(team_api_id), team_name, short_name, logo, int(user_id), now, now),
            )
            row = await self.db.fetchone("SELECT * FROM team_claims WHERE id=?", (cur.lastrowid,))
        return dict(row) if row else {"guild_id": guild_id, "league_key": league_key, "team_api_id": team_api_id, "team_name": team_name, "user_id": user_id}

    async def _get_team_claim(self, guild_id: int | None, league_key: str, team_api_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM team_claims WHERE COALESCE(guild_id,-1)=COALESCE(?,-1) AND league_key=? AND team_api_id=?",
            (guild_id, str(league_key), int(team_api_id)),
        )
        return dict(row) if row else None

    async def _get_user_team_claim(self, guild_id: int | None, user_id: int, league_key: str | None = None) -> dict[str, Any] | None:
        q = "SELECT * FROM team_claims WHERE COALESCE(guild_id,-1)=COALESCE(?,-1) AND user_id=?"
        params: list[Any] = [guild_id, int(user_id)]
        if league_key:
            q += " AND league_key=?"
            params.append(str(league_key))
        q += " ORDER BY updated_at DESC LIMIT 1"
        row = await self.db.fetchone(q, tuple(params))
        if not row and guild_id is not None:
            # fallback any guild
            q2 = "SELECT * FROM team_claims WHERE user_id=?"
            p2: list[Any] = [int(user_id)]
            if league_key:
                q2 += " AND league_key=?"
                p2.append(str(league_key))
            q2 += " ORDER BY updated_at DESC LIMIT 1"
            row = await self.db.fetchone(q2, tuple(p2))
        return dict(row) if row else None

    async def _delete_team_claim(self, guild_id: int | None, league_key: str, team_api_id: int) -> None:
        await self.db.execute(
            "DELETE FROM team_claims WHERE COALESCE(guild_id,-1)=COALESCE(?,-1) AND league_key=? AND team_api_id=?",
            (guild_id, str(league_key), int(team_api_id)),
        )

    async def _sync_team_claims_to_season(self, season_id: str) -> None:
        season = await self.get_season(str(season_id))
        if not season:
            return
        league_key = str(season.get("league_key") or "")
        guild_id = season.get("guild_id")
        claims = await self.db.fetchall(
            "SELECT * FROM team_claims WHERE league_key=? AND (guild_id IS ? OR guild_id IS NULL)",
            (league_key, guild_id),
        )
        now = utc_now_iso()
        for r in claims:
            c = dict(r)
            tid = int(c.get("team_api_id") or 0)
            uid = c.get("user_id")
            if not tid or uid is None:
                continue
            st = await self.get_season_team(str(season_id), tid)
            if not st:
                continue
            if st.get("manager_user_id") is not None:
                continue
            await self.db.execute(
                "UPDATE season_teams SET manager_user_id=?, is_user_controlled=1, updated_at=? WHERE season_id=? AND team_api_id=?",
                (int(uid), now, str(season_id), tid),
            )

    async def claim_team(self, *, guild_id: int | None, user_id: int, team_query: str, league_key: str | None = None) -> dict[str, Any]:
        candidates = await self.find_team_in_active_seasons(guild_id, team_query, league_key=league_key)
        if not candidates:
            # No season candidates - fall back to season-independent claim (no auto-create)
            resolved = await self._resolve_team_for_claim(guild_id=guild_id, team_query=team_query, league_key=league_key)
            if not resolved:
                return {"ok": False, "reason": "team_not_found"}
            if resolved.get("ambiguous"):
                cands = resolved.get("candidates") or []
                return {
                    "ok": False,
                    "reason": "ambiguous_team",
                    "candidates": [{"team_name": c.get("name"), "season_name": LEAGUE_DISPLAY.get(c.get("_league_key") or c.get("league_key"), c.get("league_key")), "league_key": c.get("_league_key") or c.get("league_key"), "team_api_id": c.get("api_id"), "season_id": None} for c in cands[:10]],
                }
            lk = str(resolved.get("league_key") or league_key or "")
            tid = int(resolved.get("api_id") or resolved.get("team_api_id") or 0)
            tname = str(resolved.get("name") or team_query)
            if not lk or not tid:
                return {"ok": False, "reason": "team_not_found"}
            existing_claim = await self._get_team_claim(guild_id, lk, tid)
            if existing_claim and int(existing_claim.get("user_id") or 0) == int(user_id):
                return {"ok": True, "already_claimed_by_you": True, "team": {"team_name": tname, "team_api_id": tid, "league_key": lk, "manager_user_id": user_id}, "season_id": None, "season": None, "claim": existing_claim}
            if existing_claim and existing_claim.get("user_id") is not None:
                return {"ok": False, "reason": "already_claimed", "team": {"team_name": tname, "team_api_id": tid, "league_key": lk}, "season_id": None, "owner_user_id": int(existing_claim.get("user_id"))}
            # prevent claiming multiple teams in same guild+league (independent of season)
            my_claim = await self._get_user_team_claim(guild_id, user_id, league_key=lk)
            if my_claim and int(my_claim.get("team_api_id") or 0) != tid:
                return {"ok": False, "reason": "user_already_has_team", "team": {"team_name": tname, "team_api_id": tid, "league_key": lk}, "season_id": None, "current_team": {"team_name": my_claim.get("team_name"), "team_api_id": my_claim.get("team_api_id"), "league_key": my_claim.get("league_key")}}
            claim = await self._upsert_team_claim(guild_id=guild_id, league_key=lk, team_api_id=tid, team_name=tname, short_name=resolved.get("short_name") or resolved.get("code"), logo=resolved.get("logo"), user_id=user_id)
            team_obj = {"team_name": tname, "team_api_id": tid, "league_key": lk, "manager_user_id": user_id, "short_name": resolved.get("short_name") or resolved.get("code"), "logo": resolved.get("logo")}
            return {"ok": True, "already_claimed_by_you": False, "team": team_obj, "season_id": None, "season": None, "claim": claim}

        # prefer exact match
        exact = [c for c in candidates if int(c.get("_match_score", 0)) == 3]
        pick = exact[0] if len(exact) == 1 else (candidates[0] if len(candidates) == 1 else None)
        if pick is None:
            return {
                "ok": False,
                "reason": "ambiguous_team",
                "candidates": [{"team_name": c.get("team_name"), "season_name": c.get("season_name"), "league_key": c.get("season_league_key"), "team_api_id": c.get("team_api_id"), "season_id": c.get("season_id")} for c in candidates[:10]],
            }
        season_id = str(pick.get("season_id"))
        team_api_id = int(pick.get("team_api_id"))
        existing = await self.get_season_team(season_id, team_api_id)
        if not existing:
            return {"ok": False, "reason": "team_not_found"}
        owner = existing.get("manager_user_id")
        if owner is not None and int(owner) == int(user_id):
            # ensure team_claims record exists too
            try:
                await self._upsert_team_claim(guild_id=guild_id, league_key=str(pick.get("season_league_key") or ""), team_api_id=team_api_id, team_name=str(existing.get("team_name") or ""), short_name=existing.get("short_name"), logo=existing.get("logo"), user_id=user_id)
            except Exception:
                pass
            return {"ok": True, "already_claimed_by_you": True, "team": existing, "season_id": season_id}
        if owner is not None and int(owner) != int(user_id):
            return {"ok": False, "reason": "already_claimed", "team": existing, "season_id": season_id, "owner_user_id": int(owner)}

        # prevent claiming multiple teams in same season
        already = await self.db.fetchone(
            "SELECT * FROM season_teams WHERE season_id=? AND manager_user_id=?",
            (season_id, int(user_id)),
        )
        if already:
            a = dict(already)
            return {"ok": False, "reason": "user_already_has_team", "team": existing, "season_id": season_id, "current_team": a}
        # also prevent multiple claims in same league (season-independent)
        lk_check = str(pick.get("season_league_key") or league_key or "")
        if lk_check:
            my_claim = await self._get_user_team_claim(guild_id, int(user_id), league_key=lk_check)
            if my_claim and int(my_claim.get("team_api_id") or 0) != team_api_id:
                return {"ok": False, "reason": "user_already_has_team", "team": existing, "season_id": season_id, "current_team": {"team_name": my_claim.get("team_name"), "team_api_id": my_claim.get("team_api_id"), "league_key": my_claim.get("league_key")}}

        now = utc_now_iso()
        await self.db.execute(
            "UPDATE season_teams SET manager_user_id=?, is_user_controlled=1, updated_at=? WHERE season_id=? AND team_api_id=?",
            (int(user_id), now, season_id, team_api_id),
        )
        updated = await self.get_season_team(season_id, team_api_id)
        season = await self.get_season(season_id)
        # also persist in team_claims (season-independent)
        try:
            await self._upsert_team_claim(guild_id=guild_id, league_key=str(pick.get("season_league_key") or (season or {}).get("league_key") or ""), team_api_id=team_api_id, team_name=str((updated or existing or {}).get("team_name") or ""), short_name=(updated or existing or {}).get("short_name"), logo=(updated or existing or {}).get("logo"), user_id=user_id)
        except Exception:
            pass
        return {"ok": True, "already_claimed_by_you": False, "team": updated, "season_id": season_id, "season": season}

    async def release_team(self, *, guild_id: int | None, user_id: int, team_query: str, league_key: str | None = None) -> dict[str, Any]:
        candidates = await self.find_team_in_active_seasons(guild_id, team_query, league_key=league_key)
        if not candidates:
            # try claim-only release
            resolved = await self._resolve_team_for_claim(guild_id=guild_id, team_query=team_query, league_key=league_key)
            if not resolved or resolved.get("ambiguous"):
                if resolved and resolved.get("ambiguous"):
                    return {"ok": False, "reason": "ambiguous_team", "candidates": resolved.get("candidates", [])[:10]}
                return {"ok": False, "reason": "team_not_found"}
            lk = str(resolved.get("league_key") or league_key or "")
            tid = int(resolved.get("api_id") or 0)
            claim = await self._get_team_claim(guild_id, lk, tid)
            if not claim:
                return {"ok": False, "reason": "not_claimed", "team": {"team_name": resolved.get("name"), "team_api_id": tid, "league_key": lk}}
            if int(claim.get("user_id") or 0) != int(user_id):
                return {"ok": False, "reason": "not_owner", "team": {"team_name": resolved.get("name"), "team_api_id": tid, "league_key": lk}, "owner_user_id": int(claim.get("user_id") or 0)}
            await self._delete_team_claim(guild_id, lk, tid)
            return {"ok": True, "team": {"team_name": resolved.get("name"), "team_api_id": tid, "league_key": lk, "manager_user_id": None}, "season_id": None}
        exact = [c for c in candidates if int(c.get("_match_score", 0)) == 3]
        pick = exact[0] if len(exact) == 1 else (candidates[0] if len(candidates) == 1 else None)
        if pick is None:
            return {"ok": False, "reason": "ambiguous_team", "candidates": candidates[:10]}
        season_id = str(pick.get("season_id"))
        team_api_id = int(pick.get("team_api_id"))
        existing = await self.get_season_team(season_id, team_api_id)
        if not existing:
            return {"ok": False, "reason": "team_not_found"}
        owner = existing.get("manager_user_id")
        if owner is None:
            return {"ok": False, "reason": "not_claimed", "team": existing}
        if int(owner) != int(user_id):
            return {"ok": False, "reason": "not_owner", "team": existing, "owner_user_id": int(owner)}
        now = utc_now_iso()
        await self.db.execute(
            "UPDATE season_teams SET manager_user_id=NULL, is_user_controlled=0, updated_at=? WHERE season_id=? AND team_api_id=?",
            (now, season_id, team_api_id),
        )
        # also remove season-independent claim
        try:
            await self._delete_team_claim(guild_id, str(pick.get("season_league_key") or ""), team_api_id)
        except Exception:
            pass
        updated = await self.get_season_team(season_id, team_api_id)
        return {"ok": True, "team": updated, "season_id": season_id}

    async def resign_manager(self, *, guild_id: int | None, user_id: int) -> dict[str, Any]:
        """Resign the user from all claimed teams in the given guild context.
        Clears team_claims and season_teams.manager_user_id. Safe if no claims exist.
        """
        now = utc_now_iso()
        uid = int(user_id)

        # Collect team_claims for this guild (and also guild-less claims if guild is None)
        if guild_id is None:
            claim_rows = await self.db.fetchall("SELECT * FROM team_claims WHERE user_id=?", (uid,))
        else:
            claim_rows = await self.db.fetchall(
                "SELECT * FROM team_claims WHERE COALESCE(guild_id,-1)=COALESCE(?,-1) AND user_id=?",
                (guild_id, uid),
            )
        claims = [dict(r) for r in (claim_rows or [])]

        # Also find season_teams managed by this user in seasons for this guild
        if guild_id is None:
            st_rows = await self.db.fetchall(
                """SELECT st.*, s.league_key AS season_league_key, s.name AS season_name, s.guild_id AS season_guild_id
                   FROM season_teams st
                   JOIN seasons s ON s.id = st.season_id
                   WHERE st.manager_user_id=?""",
                (uid,),
            )
        else:
            st_rows = await self.db.fetchall(
                """SELECT st.*, s.league_key AS season_league_key, s.name AS season_name, s.guild_id AS season_guild_id
                   FROM season_teams st
                   JOIN seasons s ON s.id = st.season_id
                   WHERE st.manager_user_id=? AND (s.guild_id IS ? OR s.guild_id IS NULL)""",
                (uid, guild_id),
            )
        season_teams = [dict(r) for r in (st_rows or [])]

        if not claims and not season_teams:
            return {"ok": False, "reason": "no_claimed_team", "released": []}

        released: list[dict[str, Any]] = []
        seen: set[tuple[str | None, int]] = set()

        # Clear season team manager assignments
        for st in season_teams:
            sid = str(st.get("season_id") or "")
            tid = int(st.get("team_api_id") or 0)
            key = (sid, tid)
            if not sid or not tid or key in seen:
                continue
            seen.add(key)
            await self.db.execute(
                "UPDATE season_teams SET manager_user_id=NULL, is_user_controlled=0, updated_at=? WHERE season_id=? AND team_api_id=? AND manager_user_id=?",
                (now, sid, tid, uid),
            )
            released.append({
                "team_api_id": tid,
                "team_name": st.get("team_name"),
                "league_key": st.get("league_key") or st.get("season_league_key"),
                "season_id": sid,
                "season_name": st.get("season_name"),
                "source": "season_team",
            })

        # Delete team_claims
        for c in claims:
            lk = str(c.get("league_key") or "")
            tid = int(c.get("team_api_id") or 0)
            if not lk or not tid:
                continue
            await self.db.execute(
                "DELETE FROM team_claims WHERE id=? AND user_id=?",
                (int(c.get("id") or 0), uid),
            )
            # avoid duplicate in released list
            if not any(int(r.get("team_api_id") or 0) == tid and str(r.get("league_key") or "") == lk for r in released):
                released.append({
                    "team_api_id": tid,
                    "team_name": c.get("team_name"),
                    "league_key": lk,
                    "season_id": None,
                    "season_name": None,
                    "source": "team_claim",
                })

        # Also clear any season_teams in same guild that match released team_api_ids but weren't caught (defensive)
        released_tids = {int(r.get("team_api_id") or 0) for r in released if int(r.get("team_api_id") or 0)}
        if released_tids and guild_id is not None:
            for tid in released_tids:
                await self.db.execute(
                    """UPDATE season_teams SET manager_user_id=NULL, is_user_controlled=0, updated_at=?
                       WHERE team_api_id=? AND manager_user_id=? AND season_id IN (SELECT id FROM seasons WHERE guild_id IS ? OR guild_id IS NULL)""",
                    (now, int(tid), uid, guild_id),
                )
        elif released_tids and guild_id is None:
            for tid in released_tids:
                await self.db.execute(
                    "UPDATE season_teams SET manager_user_id=NULL, is_user_controlled=0, updated_at=? WHERE team_api_id=? AND manager_user_id=?",
                    (now, int(tid), uid),
                )

        return {"ok": True, "released": released}

    async def list_claimed_teams(self, guild_id: int | None, league_key: str | None = None) -> list[dict[str, Any]]:
        async def _collect(status: str | None):
            seasons = await self.list_seasons(guild_id=guild_id, status=status)
            if league_key:
                seasons = [s for s in seasons if str(s.get("league_key")) == str(league_key)]
            if not seasons and guild_id is not None:
                seasons = await self.list_seasons(guild_id=None, status=status)
                if league_key:
                    seasons = [s for s in seasons if str(s.get("league_key")) == str(league_key)]
            return seasons

        seasons = await _collect("active")
        out = []
        seen_team = set()
        for s in seasons:
            sid = str(s.get("id"))
            rows = await self.db.fetchall(
                "SELECT * FROM season_teams WHERE season_id=? AND manager_user_id IS NOT NULL ORDER BY team_name ASC",
                (sid,),
            )
            for r in rows:
                d = dict(r)
                d["season_name"] = s.get("name")
                d["season_league_key"] = s.get("league_key")
                k = (str(d.get("season_league_key") or ""), int(d.get("team_api_id") or 0))
                if k in seen_team:
                    continue
                seen_team.add(k)
                out.append(d)
        if not out:
            for s in await _collect(None):
                sid = str(s.get("id"))
                rows = await self.db.fetchall(
                    "SELECT * FROM season_teams WHERE season_id=? AND manager_user_id IS NOT NULL ORDER BY team_name ASC",
                    (sid,),
                )
                for r in rows:
                    d = dict(r)
                    d["season_name"] = s.get("name")
                    d["season_league_key"] = s.get("league_key")
                    k = (str(d.get("season_league_key") or ""), int(d.get("team_api_id") or 0))
                    if k in seen_team:
                        continue
                    seen_team.add(k)
                    out.append(d)
        # Include season-independent claims (no season set)
        try:
            q = "SELECT * FROM team_claims WHERE 1=1"
            params: list[Any] = []
            if guild_id is not None:
                q += " AND (guild_id IS ? OR guild_id IS NULL)"
                params.append(guild_id)
            if league_key:
                q += " AND league_key=?"
                params.append(str(league_key))
            q += " ORDER BY updated_at DESC LIMIT 200"
            claim_rows = await self.db.fetchall(q, tuple(params))
            for r in claim_rows:
                c = dict(r)
                lk = str(c.get("league_key") or "")
                tid = int(c.get("team_api_id") or 0)
                k = (lk, tid)
                if k in seen_team:
                    continue
                seen_team.add(k)
                out.append({
                    "season_id": None,
                    "team_api_id": tid,
                    "team_name": c.get("team_name"),
                    "league_key": lk,
                    "season_league_key": lk,
                    "manager_user_id": c.get("user_id"),
                    "is_user_controlled": 1,
                    "short_name": c.get("short_name"),
                    "logo": c.get("logo"),
                    "season_name": None,
                    "claim_only": True,
                })
        except Exception:
            pass
        return out

    async def get_fixture_managers(self, season_id: str, fixture: dict[str, Any]) -> dict[str, int | None]:
        """Return manager user IDs for home/away if claimed in this season."""
        home_id = int(fixture.get("home_team_api_id") or 0)
        away_id = int(fixture.get("away_team_api_id") or 0)
        home_row = await self.get_season_team(season_id, home_id) if home_id else None
        away_row = await self.get_season_team(season_id, away_id) if away_id else None
        home_mgr = home_row.get("manager_user_id") if home_row else None
        away_mgr = away_row.get("manager_user_id") if away_row else None
        return {
            "home_manager_id": int(home_mgr) if home_mgr is not None else None,
            "away_manager_id": int(away_mgr) if away_mgr is not None else None,
            "home_team": home_row,
            "away_team": away_row,
        }

    async def get_user_team_in_active_season(self, guild_id: int | None, user_id: int, league_key: str | None = None) -> dict[str, Any] | None:
        async def _lookup(lk: str | None, status: str | None = "active"):
            seasons = await self.list_seasons(guild_id=guild_id, status=status)
            if lk:
                seasons = [s for s in seasons if str(s.get("league_key")) == str(lk)]
            if not seasons and guild_id is not None:
                seasons = await self.list_seasons(guild_id=None, status=status)
                if lk:
                    seasons = [s for s in seasons if str(s.get("league_key")) == str(lk)]
            # prefer active, then most recent
            seasons.sort(key=lambda s: (0 if str(s.get("status")) == "active" else 1, str(s.get("created_at") or "")), reverse=False)
            for s in seasons:
                sid = str(s.get("id"))
                row = await self.db.fetchone(
                    "SELECT * FROM season_teams WHERE season_id=? AND manager_user_id=? LIMIT 1",
                    (sid, int(user_id)),
                )
                if row:
                    d = dict(row)
                    # Ensure budgets are backfilled for migrated/zero rows
                    try:
                        if int(d.get("transfer_budget") or 0) == 0 and int(d.get("wage_budget") or 0) == 0:
                            await self.ensure_season_budgets(sid)
                            refreshed = await self.get_season_team(sid, int(d.get("team_api_id") or 0))
                            if refreshed:
                                d = refreshed
                    except Exception:
                        pass
                    d["season_name"] = s.get("name")
                    d["season_league_key"] = s.get("league_key")
                    d["season_year"] = s.get("season_year")
                    d["_season"] = dict(s)
                    return d
            return None

        # league-specific active -> league-specific any -> any active -> any
        for lk, st in ((league_key, "active"), (league_key, None), (None, "active"), (None, None)):
            found = await _lookup(lk, st)
            if found:
                return found
        # Fallback: season-independent team claim (no season set)
        claim = await self._get_user_team_claim(guild_id, int(user_id), league_key=league_key)
        if not claim and league_key:
            claim = await self._get_user_team_claim(guild_id, int(user_id), league_key=None)
        if claim:
            lk = str(claim.get("league_key") or "")
            d = {
                "season_id": None,
                "team_api_id": int(claim.get("team_api_id") or 0),
                "team_name": claim.get("team_name"),
                "league_key": lk,
                "manager_user_id": int(user_id),
                "is_user_controlled": 1,
                "short_name": claim.get("short_name"),
                "logo": claim.get("logo"),
                "season_name": None,
                "season_league_key": lk,
                "season_year": 2026,
                "_season": None,
                "claim_only": True,
            }
            return d
        return None

    # ------------------------------------------------------------------
    # European competitions (UCL / UEL / UECL)
    # ------------------------------------------------------------------

    def get_european_competition_config(self, competition_key: str) -> dict[str, Any]:
        cfg = EUROPEAN_COMPETITION_CONFIG.get(str(competition_key))
        if not cfg:
            raise ValueError(f"Unsupported European competition: {competition_key}")
        return dict(cfg)

    async def _team_seed_rating(self, team_api_id: int, season_year: int = 2026) -> float:
        """Deterministic seed rating fallback (higher = stronger).
        Uses average overall of top 16 players in DB; falls back to 70.0.
        """
        rows = await self.db.fetchall(
            "SELECT overall, potential, market_value FROM players WHERE team_api_id=? AND season=?",
            (int(team_api_id), int(season_year)),
        )
        vals: list[float] = []
        mv = 0.0
        for r in rows or []:
            d = dict(r)
            try:
                ov = float(d.get("overall") or 0)
            except Exception:
                ov = 0.0
            if ov > 0:
                vals.append(ov)
            try:
                mv += float(d.get("market_value") or 0)
            except Exception:
                pass
        vals.sort(reverse=True)
        top = vals[:16] if vals else []
        base = (sum(top) / len(top)) if top else 70.0
        # small market value bonus, capped so it doesn't dominate
        mv_bonus = min(5.0, (mv / 500_000_000.0) * 5.0) if mv > 0 else 0.0
        return round(base + mv_bonus, 4)

    async def _get_domestic_league_key_for_team(self, team_api_id: int, season_year: int = 2026) -> str | None:
        row = await self.db.fetchone(
            "SELECT league_name FROM teams WHERE api_id=? AND season=? ORDER BY updated_at DESC LIMIT 1",
            (int(team_api_id), int(season_year)),
        )
        if not row:
            row = await self.db.fetchone(
                "SELECT league_name FROM teams WHERE api_id=? ORDER BY season DESC, updated_at DESC LIMIT 1",
                (int(team_api_id),),
            )
        if not row:
            return None
        lname = str(dict(row).get("league_name") or "").strip().lower()
        inv = {str(v).lower(): k for k, v in LEAGUE_DISPLAY.items()}
        inv.update({
            "english premier league": "premier_league",
            "premier league": "premier_league",
            "la liga": "la_liga",
            "bundesliga": "bundesliga",
            "serie a": "serie_a",
            "ligue 1": "ligue_1",
        })
        return inv.get(lname)

    async def get_eligible_european_teams(
        self,
        competition_key: str,
        *,
        season_year: int = 2026,
        guild_id: int | None = None,
        max_per_league: int = 20,
    ) -> list[dict[str, Any]]:
        """Return eligible European participants from Top 5 league teams in the DB.
        Robust matching: tries requested season_year, then falls back to other seasons and bundled data.
        max_per_league is a soft spread preference, not a hard blocker when it would cause insufficient teams.
        """
        cfg = self.get_european_competition_config(competition_key)
        team_count = int(cfg["team_count"])

        def _league_key_from_name(league_name: str | None) -> str | None:
            return self._norm_league_to_key(str(league_name or ""))

        def _add_team(pool: dict[str, dict[str, Any]], row: dict[str, Any], lk: str) -> None:
            tid = int(row.get("api_id") or row.get("team_api_id") or 0)
            if not tid or lk not in LEAGUE_KEYS:
                return
            cur = pool.get(str(tid))
            cand = {
                "team_api_id": tid,
                "team_name": str(row.get("name") or row.get("team_name") or tid),
                "short_name": row.get("short_name") or row.get("code"),
                "logo": row.get("logo"),
                "domestic_league_key": lk,
                "_season": int(row.get("season") or 0),
            }
            if cur is None:
                pool[str(tid)] = cand
                return
            # Prefer exact season_year, then most recent season
            cur_season = int(cur.get("_season") or 0)
            cand_season = int(cand.get("_season") or 0)
            cur_is_exact = cur_season == int(season_year)
            cand_is_exact = cand_season == int(season_year)
            if cand_is_exact and not cur_is_exact:
                pool[str(tid)] = cand
            elif cand_is_exact == cur_is_exact and cand_season > cur_season:
                pool[str(tid)] = cand

        # 1) Gather from DB (prefer requested season, but include all top-5-league teams)
        team_pool: dict[str, dict[str, Any]] = {}
        try:
            rows_exact = await self.db.fetchall(
                "SELECT api_id, name, short_name, code, logo, league_name, season FROM teams WHERE season=?",
                (int(season_year),),
            )
        except Exception:
            rows_exact = []
        try:
            rows_all = await self.db.fetchall(
                "SELECT api_id, name, short_name, code, logo, league_name, season FROM teams WHERE league_name IS NOT NULL AND league_name != ''",
            )
        except Exception:
            rows_all = []

        for rr in (rows_exact or []):
            d = dict(rr)
            lk = _league_key_from_name(d.get("league_name"))
            if lk:
                _add_team(team_pool, d, lk)
        # Fill from all seasons if needed or to be robust
        if len(team_pool) < team_count or not any(True for _ in team_pool.values()):
            for rr in (rows_all or []):
                d = dict(rr)
                lk = _league_key_from_name(d.get("league_name"))
                if lk:
                    _add_team(team_pool, d, lk)

        # 2) Bundled fallback/supplement (only for missing leagues/teams)
        try:
            from data.football_data import list_all_teams
            for lk in LEAGUE_KEYS:
                try:
                    bundled = list_all_teams(lk) or []
                except Exception:
                    bundled = []
                for t in bundled:
                    d = dict(t)
                    d.setdefault("season", int(season_year))
                    _add_team(team_pool, d, lk)
        except Exception:
            pass

        # 3) Score all candidates
        all_scored: list[dict[str, Any]] = []
        for t in team_pool.values():
            lk = str(t.get("domestic_league_key") or "")
            if lk not in LEAGUE_KEYS:
                continue
            rating = 70.0
            try:
                rating = float(await self._team_seed_rating(int(t["team_api_id"]), season_year=season_year))
            except Exception:
                rating = 70.0
            item = dict(t)
            item.pop("_season", None)
            item["seed_rating"] = float(rating)
            item["domestic_league_key"] = lk
            all_scored.append(item)

        # Dedupe by team_api_id (defensive)
        dedup: dict[int, dict[str, Any]] = {}
        for x in all_scored:
            tid = int(x.get("team_api_id") or 0)
            if not tid:
                continue
            prev = dedup.get(tid)
            if prev is None or float(x.get("seed_rating") or 0) > float(prev.get("seed_rating") or 0):
                dedup[tid] = x
        all_scored = list(dedup.values())

        # 4) Build selection with soft league spread, but never block filling
        per_league_count: dict[str, int] = {k: 0 for k in LEAGUE_KEYS}
        selected: list[dict[str, Any]] = []
        selected_ids: set[int] = set()

        # Soft target allocation by league strength
        league_strength = []
        for lk in LEAGUE_KEYS:
            vals = sorted([float(x.get("seed_rating") or 0) for x in all_scored if x.get("domestic_league_key") == lk], reverse=True)
            topn = vals[:6]
            league_strength.append((sum(topn) / len(topn) if topn else 0.0, lk))
        league_strength.sort(reverse=True)
        base = team_count // len(LEAGUE_KEYS)
        rem = team_count % len(LEAGUE_KEYS)
        soft_target = {lk: base for lk in LEAGUE_KEYS}
        for i in range(rem):
            soft_target[league_strength[i % len(league_strength)][1]] += 1

        # Pass 1: respect soft target and soft max_per_league
        soft_cap = max(1, int(max_per_league)) if max_per_league is not None else team_count
        # If soft cap is too small to fill, relax it
        if soft_cap * len(LEAGUE_KEYS) < team_count:
            soft_cap = team_count
        for lk in LEAGUE_KEYS:
            pool = [x for x in all_scored if x.get("domestic_league_key") == lk]
            pool.sort(key=lambda x: (-float(x.get("seed_rating") or 0), str(x.get("team_name") or "")))
            take = min(len(pool), int(soft_target.get(lk, 0)), soft_cap)
            for x in pool[:take]:
                tid = int(x["team_api_id"])
                if tid in selected_ids:
                    continue
                selected.append(x)
                selected_ids.add(tid)
                per_league_count[lk] = per_league_count.get(lk, 0) + 1

        # Pass 2: fill remaining slots by global rating, still preferring spread but not hard-blocking
        if len(selected) < team_count:
            rest = [x for x in all_scored if int(x["team_api_id"]) not in selected_ids]
            rest.sort(key=lambda x: (per_league_count.get(str(x.get("domestic_league_key")), 0), -float(x.get("seed_rating") or 0), str(x.get("team_name") or "")))
            for x in rest:
                if len(selected) >= team_count:
                    break
                tid = int(x["team_api_id"])
                if tid in selected_ids:
                    continue
                lk = str(x.get("domestic_league_key") or "")
                selected.append(x)
                selected_ids.add(tid)
                per_league_count[lk] = per_league_count.get(lk, 0) + 1

        # Final deterministic ordering for seeding
        selected.sort(key=lambda x: (-float(x.get("seed_rating") or 0), str(x.get("team_name") or "")))
        out = selected[:team_count]
        for i, t in enumerate(out, start=1):
            t["seed_rank"] = i
        return out

    def _norm_league_to_key(self, league_name: str) -> str | None:
        lname = str(league_name or "").strip().lower()
        inv = {str(v).lower(): k for k, v in LEAGUE_DISPLAY.items()}
        inv.update({
            "english premier league": "premier_league",
            "premier league": "premier_league",
            "la liga": "la_liga",
            "bundesliga": "bundesliga",
            "serie a": "serie_a",
            "ligue 1": "ligue_1",
        })
        return inv.get(lname)

    async def generate_european_pots(
        self,
        *,
        season_id: str,
        competition_key: str,
        season_year: int = 2026,
        guild_id: int | None = None,
        max_per_league: int = 20,
    ) -> dict[str, Any]:
        cfg = self.get_european_competition_config(competition_key)
        team_count = int(cfg["team_count"])
        pot_count = int(cfg["pot_count"])
        pot_size = int(cfg["pots_size"])
        if pot_count * pot_size != team_count:
            raise ValueError("Invalid European competition config: pot_count * pots_size != team_count")

        # Resolve guild/season context for cross-competition dedupe
        season = await self.get_season(str(season_id))
        if season:
            try:
                season_year = int(season.get("season_year") or season_year)
            except Exception:
                pass
            if guild_id is None and season.get("guild_id") is not None:
                try:
                    guild_id = int(season.get("guild_id"))
                except Exception:
                    pass

        teams = await self.get_eligible_european_teams(
            competition_key, season_year=season_year, guild_id=guild_id, max_per_league=max_per_league
        )

        # Prefer excluding teams already used in other European competitions (same guild+season_year)
        used_elsewhere: set[int] = set()
        try:
            other_seasons = await self.db.fetchall(
                "SELECT id, league_key FROM seasons WHERE league_key IN (?,?,?) AND season_year=?",
                ("champions_league", "europa_league", "conference_league", int(season_year)),
            )
            for rs in other_seasons or []:
                sd = dict(rs)
                sid2 = str(sd.get("id") or "")
                lk2 = str(sd.get("league_key") or "")
                if not sid2 or lk2 == str(competition_key):
                    continue
                if guild_id is not None and sd.get("guild_id") is not None:
                    try:
                        if int(sd.get("guild_id")) != int(guild_id):
                            continue
                    except Exception:
                        pass
                rows = await self.db.fetchall(
                    "SELECT team_api_id FROM competition_entries WHERE season_id=? AND competition_key=?",
                    (sid2, lk2),
                )
                for rr in rows or []:
                    try:
                        used_elsewhere.add(int(dict(rr).get("team_api_id") or 0))
                    except Exception:
                        pass
        except Exception:
            # Non-fatal: fall back to no cross-competition exclusion
            used_elsewhere = set()

        if used_elsewhere:
            preferred = [t for t in teams if int(t.get("team_api_id") or 0) not in used_elsewhere]
            if len(preferred) >= team_count:
                teams = preferred
            else:
                # Not enough unique teams across competitions; allow overlap but keep preference
                seen_ids = {int(t.get("team_api_id") or 0) for t in preferred}
                fallback = preferred + [t for t in teams if int(t.get("team_api_id") or 0) not in seen_ids]
                teams = fallback

        if len(teams) < team_count:
            return {
                "ok": False,
                "reason": "insufficient_teams",
                "needed": team_count,
                "found": len(teams),
                "teams": teams,
            }

        teams = teams[:team_count]
        # Final safety: ensure no duplicates within this competition
        uniq: list[dict[str, Any]] = []
        seen: set[int] = set()
        for t in teams:
            tid = int(t.get("team_api_id") or 0)
            if not tid or tid in seen:
                continue
            seen.add(tid)
            uniq.append(t)
        if len(uniq) < team_count:
            # Try to top up from all scored candidates
            all_teams = await self.get_eligible_european_teams(
                competition_key, season_year=season_year, guild_id=guild_id, max_per_league=max_per_league
            )
            for t in all_teams:
                tid = int(t.get("team_api_id") or 0)
                if not tid or tid in seen:
                    continue
                uniq.append(t)
                seen.add(tid)
                if len(uniq) >= team_count:
                    break
        teams = uniq[:team_count]
        if len(teams) < team_count:
            return {
                "ok": False,
                "reason": "insufficient_teams",
                "needed": team_count,
                "found": len(teams),
                "teams": teams,
            }

        pots: list[list[dict[str, Any]]] = []
        for p in range(pot_count):
            chunk = teams[p * pot_size:(p + 1) * pot_size]
            for t in chunk:
                t["pot"] = p + 1
            pots.append(chunk)

        # persist
        now = utc_now_iso()
        await self.db.execute(
            "DELETE FROM competition_entries WHERE season_id=? AND competition_key=?",
            (str(season_id), str(competition_key)),
        )
        for pot in pots:
            for t in pot:
                await self.db.execute(
                    """INSERT OR REPLACE INTO competition_entries(
                        season_id, competition_key, team_api_id, team_name, domestic_league_key, pot, group_name, seed_rating, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(season_id),
                        str(competition_key),
                        int(t["team_api_id"]),
                        str(t.get("team_name") or ""),
                        t.get("domestic_league_key"),
                        int(t.get("pot") or 0),
                        None,
                        float(t.get("seed_rating") or 0.0),
                        now,
                        now,
                    ),
                )
        return {"ok": True, "competition_key": competition_key, "season_id": str(season_id), "pots": pots, "team_count": len(teams)}

    async def get_european_pots(self, season_id: str, competition_key: str) -> dict[str, Any]:
        cfg = self.get_european_competition_config(competition_key)
        rows = await self.db.fetchall(
            "SELECT * FROM competition_entries WHERE season_id=? AND competition_key=? ORDER BY pot ASC, seed_rating DESC, team_name ASC",
            (str(season_id), str(competition_key)),
        )
        entries = [dict(r) for r in (rows or [])]
        pots: dict[int, list[dict[str, Any]]] = {}
        for e in entries:
            p = int(e.get("pot") or 0)
            if p <= 0:
                continue
            pots.setdefault(p, []).append(e)
        ordered_pots = [pots.get(i, []) for i in range(1, int(cfg["pot_count"]) + 1)]
        return {"ok": True, "competition_key": competition_key, "season_id": str(season_id), "config": cfg, "pots": ordered_pots, "entries": entries}

    async def draw_european_groups(
        self,
        *,
        season_id: str,
        competition_key: str,
        seed: int | None = None,
        max_attempts: int = 2000,
    ) -> dict[str, Any]:
        cfg = self.get_european_competition_config(competition_key)
        pot_data = await self.get_european_pots(season_id, competition_key)
        pots = pot_data.get("pots") or []
        if not pots or any(len(p) == 0 for p in pots):
            return {"ok": False, "reason": "pots_not_generated", "detail": "Generate pots before drawing groups."}

        group_count = int(cfg["group_count"])
        teams_per_group = int(cfg["teams_per_group"])
        pot_count = int(cfg["pot_count"])
        # Flatten validation
        all_teams = [t for pot in pots for t in pot]
        if len(all_teams) != group_count * teams_per_group:
            return {"ok": False, "reason": "invalid_pot_size", "found": len(all_teams), "expected": group_count * teams_per_group}
        team_ids = [int(t.get("team_api_id") or 0) for t in all_teams]
        if len(set(team_ids)) != len(team_ids):
            return {"ok": False, "reason": "duplicate_teams_in_pots"}

        rng = random.Random(seed)

        def _can_place(group: list[dict[str, Any]], team: dict[str, Any]) -> bool:
            # One per pot already enforced by construction; enforce same-association constraint
            dom = str(team.get("domestic_league_key") or "")
            if not dom:
                return True
            for g in group:
                if str(g.get("domestic_league_key") or "") == dom:
                    return False
            return True

        # Build pot lists (copy)
        pot_lists = [[dict(x) for x in pot] for pot in pots]
        for pl in pot_lists:
            rng.shuffle(pl)

        best_groups: list[list[dict[str, Any]]] | None = None
        best_conflicts = 10**9

        for _attempt in range(max(1, int(max_attempts))):
            groups: list[list[dict[str, Any]]] = [[] for _ in range(group_count)]
            ok = True
            # For each pot, assign one team to each group
            for pi in range(pot_count):
                candidates = list(pot_lists[pi])
                rng.shuffle(candidates)
                # order groups by fewest options first (heuristic)
                group_order = list(range(group_count))
                rng.shuffle(group_order)

                assignment: dict[int, dict[str, Any]] = {}
                used: set[int] = set()

                def _backtrack(g_idx_list: list[int], pos: int) -> bool:
                    if pos >= len(g_idx_list):
                        return True
                    gi = g_idx_list[pos]
                    # try candidates with fewest valid placements? simple: shuffle candidates
                    opts = [c for c in candidates if int(c.get("team_api_id") or 0) not in used and _can_place(groups[gi], c)]
                    rng.shuffle(opts)
                    for cand in opts:
                        tid = int(cand.get("team_api_id") or 0)
                        groups[gi].append(cand)
                        used.add(tid)
                        assignment[gi] = cand
                        if _backtrack(g_idx_list, pos + 1):
                            return True
                        used.remove(tid)
                        groups[gi].pop()
                        assignment.pop(gi, None)
                    return False

                # heuristic: groups with most constraints first
                g_order2 = sorted(group_order, key=lambda gi: sum(1 for c in candidates if _can_place(groups[gi], c)))
                if not _backtrack(g_order2, 0):
                    ok = False
                    break

            if ok:
                best_groups = groups
                best_conflicts = 0
                break
            # keep best (least conflicts) as fallback report; but we won't save invalid
            # compute conflicts
            conflicts = 0
            for g in groups:
                seen = {}
                for t in g:
                    dom = str(t.get("domestic_league_key") or "")
                    if not dom:
                        continue
                    seen[dom] = seen.get(dom, 0) + 1
                for v in seen.values():
                    if v > 1:
                        conflicts += (v - 1)
            if conflicts < best_conflicts:
                best_conflicts = conflicts

        if best_groups is None:
            return {
                "ok": False,
                "reason": "draw_infeasible",
                "detail": "Could not generate a valid draw respecting same-association constraints. Try again or adjust configuration.",
                "attempts": int(max_attempts),
            }

        # Persist group assignments
        now = utc_now_iso()
        group_names = [chr(ord("A") + i) for i in range(group_count)]
        await self.db.execute(
            "UPDATE competition_entries SET group_name=NULL, updated_at=? WHERE season_id=? AND competition_key=?",
            (now, str(season_id), str(competition_key)),
        )
        for gi, group in enumerate(best_groups):
            gname = group_names[gi]
            for t in group:
                await self.db.execute(
                    "UPDATE competition_entries SET group_name=?, updated_at=? WHERE season_id=? AND competition_key=? AND team_api_id=?",
                    (gname, now, str(season_id), str(competition_key), int(t.get("team_api_id") or 0)),
                )

        groups_out = []
        for gi, group in enumerate(best_groups):
            groups_out.append({
                "group_name": group_names[gi],
                "teams": group,
            })
        return {"ok": True, "season_id": str(season_id), "competition_key": str(competition_key), "groups": groups_out}

    async def get_european_groups(self, season_id: str, competition_key: str) -> dict[str, Any]:
        cfg = self.get_european_competition_config(competition_key)
        rows = await self.db.fetchall(
            "SELECT * FROM competition_entries WHERE season_id=? AND competition_key=? ORDER BY group_name ASC, pot ASC, seed_rating DESC",
            (str(season_id), str(competition_key)),
        )
        entries = [dict(r) for r in (rows or [])]
        groups: dict[str, list[dict[str, Any]]] = {}
        for e in entries:
            g = str(e.get("group_name") or "").strip()
            if not g:
                continue
            groups.setdefault(g, []).append(e)
        ordered = [{"group_name": k, "teams": v} for k, v in sorted(groups.items())]
        return {"ok": True, "season_id": str(season_id), "competition_key": str(competition_key), "config": cfg, "groups": ordered, "entries": entries}

    async def generate_european_group_stage_fixtures(
        self,
        *,
        season_id: str,
        competition_key: str,
        regenerate: bool = False,
    ) -> dict[str, Any]:
        cfg = self.get_european_competition_config(competition_key)
        groups_res = await self.get_european_groups(season_id, competition_key)
        groups = groups_res.get("groups") or []
        if not groups:
            return {"ok": False, "reason": "groups_not_drawn", "detail": "Draw groups before generating fixtures."}

        # Validate groups
        expected_groups = int(cfg["group_count"])
        expected_tpg = int(cfg["teams_per_group"])
        if len(groups) != expected_groups:
            return {"ok": False, "reason": "invalid_group_count", "found": len(groups), "expected": expected_groups}
        for g in groups:
            if len(g.get("teams") or []) != expected_tpg:
                return {"ok": False, "reason": "invalid_group_size", "group": g.get("group_name"), "found": len(g.get("teams") or []), "expected": expected_tpg}

        # Check existing fixtures
        existing = await self.db.fetchall(
            "SELECT id, matchday, home_team_api_id, away_team_api_id, status FROM fixtures WHERE season_id=? AND league_key=?",
            (str(season_id), str(competition_key)),
        )
        existing = [dict(r) for r in (existing or [])]
        if existing and not regenerate:
            return {"ok": False, "reason": "fixtures_already_exist", "existing_count": len(existing), "detail": "Fixtures already exist. Pass regenerate=True to replace them safely."}

        # Regenerate safely: delete only fixtures for this season+competition
        if existing and regenerate:
            # Do not delete played/live fixtures by default? Safer to refuse if any played/live
            locked = [f for f in existing if str(f.get("status") or "") in {"played", "live"} or int(f.get("finalized") or 0) == 1]
            if locked:
                return {"ok": False, "reason": "cannot_regenerate_played_fixtures", "locked_count": len(locked)}
            await self.db.execute(
                "DELETE FROM fixtures WHERE season_id=? AND league_key=?",
                (str(season_id), str(competition_key)),
            )

        now = utc_now_iso()
        all_fixtures: list[dict[str, Any]] = []
        # Generate per-group round robin and merge by matchday
        group_matchdays: list[list[dict[str, Any]]] = []
        max_md = 0
        for g in groups:
            gname = str(g.get("group_name"))
            teams = g.get("teams") or []
            team_ids = [int(t.get("team_api_id") or 0) for t in teams]
            by_id = {int(t.get("team_api_id") or 0): t for t in teams}
            pairs_by_md = round_robin_pairs(team_ids)
            if cfg.get("double_round_robin", True) is False:
                # single round robin - keep as is? round_robin_pairs already returns double (home+away) for even n
                # For single, take first half. But typical is double. Config says double_round_robin True.
                half = len(pairs_by_md) // 2
                pairs_by_md = pairs_by_md[:half]
            max_md = max(max_md, len(pairs_by_md))
            for md_idx, pairs in enumerate(pairs_by_md, start=1):
                for home_id, away_id in pairs:
                    ht = by_id.get(int(home_id), {})
                    at = by_id.get(int(away_id), {})
                    rec = {
                        "season_id": str(season_id),
                        "league_key": str(competition_key),
                        "matchday": int(md_idx),
                        "home_team_api_id": int(home_id),
                        "away_team_api_id": int(away_id),
                        "home_team_name": str(ht.get("team_name") or home_id),
                        "away_team_name": str(at.get("team_name") or away_id),
                        "group_name": gname,
                    }
                    all_fixtures.append(rec)

        # Insert fixtures
        for f in all_fixtures:
            meta = {"group": f.get("group_name"), "stage": "group_stage", "competition_key": str(competition_key)}
            await self.db.execute(
                """INSERT OR REPLACE INTO fixtures(
                    season_id,league_key,matchday,home_team_api_id,away_team_api_id,home_team_name,away_team_name,
                    home_score,away_score,status,match_id,kickoff_at,finalized_at,finalized,meta_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,NULL,NULL,'pending',NULL,NULL,NULL,0,?,?,?)""",
                (
                    str(f["season_id"]), str(f["league_key"]), int(f["matchday"]),
                    int(f["home_team_api_id"]), int(f["away_team_api_id"]),
                    str(f["home_team_name"]), str(f["away_team_name"]),
                    json.dumps(meta), now, now,
                ),
            )

        # Ensure season exists/updated
        season = await self.get_season(str(season_id))
        if season:
            try:
                await self.db.execute(
                    "UPDATE seasons SET total_matchdays=?, updated_at=? WHERE id=?",
                    (max(int(season.get("total_matchdays") or 0), max_md), now, str(season_id)),
                )
            except Exception:
                pass

        return {
            "ok": True,
            "season_id": str(season_id),
            "competition_key": str(competition_key),
            "fixtures_created": len(all_fixtures),
            "matchdays": max_md,
            "groups": len(groups),
        }

    async def validate_european_competition(self, season_id: str, competition_key: str) -> dict[str, Any]:
        cfg = self.get_european_competition_config(competition_key)
        pot_res = await self.get_european_pots(season_id, competition_key)
        groups_res = await self.get_european_groups(season_id, competition_key)
        entries = pot_res.get("entries") or []
        groups = groups_res.get("groups") or []
        fixtures = await self.db.fetchall(
            "SELECT * FROM fixtures WHERE season_id=? AND league_key=? ORDER BY matchday ASC, id ASC",
            (str(season_id), str(competition_key)),
        )
        fixtures = [dict(r) for r in (fixtures or [])]

        issues: list[str] = []
        warnings: list[str] = []

        team_count = int(cfg["team_count"])
        if len(entries) != team_count:
            issues.append(f"Expected {team_count} entries, found {len(entries)}.")
        # duplicates
        tids = [int(e.get("team_api_id") or 0) for e in entries]
        if len(set(tids)) != len(tids):
            issues.append("Duplicate teams found in competition entries.")
        # pot sizes
        pot_count = int(cfg["pot_count"])
        pot_size = int(cfg["pots_size"])
        for p in range(1, pot_count + 1):
            c = sum(1 for e in entries if int(e.get("pot") or 0) == p)
            if c != pot_size:
                issues.append(f"Pot {p} has {c} teams, expected {pot_size}.")
        # groups
        if len(groups) != int(cfg["group_count"]):
            issues.append(f"Expected {cfg['group_count']} groups, found {len(groups)}.")
        for g in groups:
            gteams = g.get("teams") or []
            if len(gteams) != int(cfg["teams_per_group"]):
                issues.append(f"Group {g.get('group_name')} has {len(gteams)} teams, expected {cfg['teams_per_group']}.")
            # same-association check
            doms = [str(t.get("domestic_league_key") or "") for t in gteams if t.get("domestic_league_key")]
            dup_doms = {d for d in doms if doms.count(d) > 1}
            if dup_doms:
                issues.append(f"Group {g.get('group_name')} violates same-association constraint: {', '.join(sorted(dup_doms))}.")
            # pot coverage
            pots_seen = sorted({int(t.get("pot") or 0) for t in gteams})
            if pots_seen != list(range(1, pot_count + 1)):
                warnings.append(f"Group {g.get('group_name')} pot coverage: {pots_seen}.")

        # fixtures validation
        group_team_ids: dict[str, set[int]] = {}
        for g in groups:
            group_team_ids[str(g.get("group_name"))] = {int(t.get("team_api_id") or 0) for t in (g.get("teams") or [])}

        seen_pairs = set()
        for f in fixtures:
            meta = {}
            try:
                meta = json.loads(f.get("meta_json") or "{}")
            except Exception:
                meta = {}
            gname = str(meta.get("group") or "")
            hid = int(f.get("home_team_api_id") or 0)
            aid = int(f.get("away_team_api_id") or 0)
            md = int(f.get("matchday") or 0)
            pair = (md, hid, aid)
            if pair in seen_pairs:
                issues.append(f"Duplicate fixture: MD{md} {hid} vs {aid}.")
            seen_pairs.add(pair)
            if hid == aid:
                issues.append(f"Self-match in fixture {f.get('id')}.")
            if gname and gname in group_team_ids:
                if hid not in group_team_ids[gname] or aid not in group_team_ids[gname]:
                    issues.append(f"Fixture {f.get('id')} has team outside group {gname}.")
            # same-association constraint in fixture (should already be enforced by group)
            hdom = None
            adom = None
            for e in entries:
                if int(e.get("team_api_id") or 0) == hid:
                    hdom = str(e.get("domestic_league_key") or "")
                if int(e.get("team_api_id") or 0) == aid:
                    adom = str(e.get("domestic_league_key") or "")
            if hdom and adom and hdom == adom:
                issues.append(f"Fixture {f.get('id')} violates same-association constraint ({hdom}).")

        # per-team fixture count
        for g in groups:
            gteams = g.get("teams") or []
            gname = str(g.get("group_name"))
            expected_per_team = (len(gteams) - 1) * (2 if cfg.get("double_round_robin", True) else 1)
            for t in gteams:
                tid = int(t.get("team_api_id") or 0)
                c = 0
                for f in fixtures:
                    meta = {}
                    try:
                        meta = json.loads(f.get("meta_json") or "{}")
                    except Exception:
                        meta = {}
                    if str(meta.get("group") or "") != gname:
                        continue
                    if int(f.get("home_team_api_id") or 0) == tid or int(f.get("away_team_api_id") or 0) == tid:
                        c += 1
                if c != expected_per_team and fixtures:
                    issues.append(f"{t.get('team_name')} in Group {gname} has {c} fixtures, expected {expected_per_team}.")

        return {
            "ok": len(issues) == 0,
            "competition_key": str(competition_key),
            "season_id": str(season_id),
            "issues": issues,
            "warnings": warnings,
            "counts": {
                "entries": len(entries),
                "groups": len(groups),
                "fixtures": len(fixtures),
            },
        }
