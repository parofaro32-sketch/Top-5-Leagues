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
LEAGUE_DISPLAY = {
    "premier_league": "Premier League",
    "la_liga": "La Liga",
    "bundesliga": "Bundesliga",
    "serie_a": "Serie A",
    "ligue_1": "Ligue 1",
}


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

    async def create_season(self, *, guild_id: int | None, league_key: str, season_year: int, name: str | None = None, auto_advance: bool = False) -> dict[str, Any]:
        if league_key not in LEAGUE_KEYS:
            raise ValueError(f"Unsupported league_key: {league_key}")
        # Prevent duplicate active season for same guild+league
        existing = await self.db.fetchone(
            "SELECT id FROM seasons WHERE guild_id IS ? AND league_key=? AND status='active'",
            (guild_id, league_key),
        )
        if existing:
            raise ValueError(f"An active season already exists for {LEAGUE_DISPLAY.get(league_key, league_key)} in this guild. Use it or archive it first.")

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
            await self.db.execute(
                """INSERT OR REPLACE INTO season_teams(season_id,team_api_id,team_name,league_key,manager_user_id,is_user_controlled,short_name,logo,created_at,updated_at)
                   VALUES(?,?,?,?,NULL,0,?,?,?,?)""",
                (season_id, tid, tname, league_key, short, logo, now, now),
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

    async def create_pending_transfer(self, *, season_id: str | None, guild_id: int | None, player_api_id: int, to_team_api_id: int, requested_by: int | None = None, note: str | None = None, transfer_type: str = "manual") -> dict[str, Any]:
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
        cur = await self.db.execute(
            """INSERT INTO season_transfers(season_id,guild_id,player_api_id,player_name,from_team_api_id,from_team_name,to_team_api_id,to_team_name,status,requested_by,transfer_type,note,meta_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?, 'pending', ?,?,?,?, ?,?)""",
            (season_id, guild_id, int(player_api_id), player_name, from_team_api_id, from_team_name, int(to_team_api_id), to_team_name, requested_by, transfer_type, note, json.dumps({"season_year": season_year}), now, now),
        )
        # fetch id
        row = await self.db.fetchone("SELECT * FROM season_transfers WHERE rowid=?", (cur.lastrowid,))
        return {"ok": True, "transfer": dict(row) if row else {"id": cur.lastrowid, "player_name": player_name, "from_team_name": from_team_name, "to_team_name": to_team_name, "status": "pending"}}

    async def confirm_transfer(self, transfer_id: int, *, confirmed_by: int | None = None) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM season_transfers WHERE id=?", (int(transfer_id),))
        if not row:
            return {"ok": False, "reason": "transfer_not_found"}
        tr = dict(row)
        if tr.get("status") == "confirmed":
            return {"ok": True, "already_confirmed": True, **tr}
        if tr.get("status") not in ("pending", "failed"):
            return {"ok": False, "reason": f"transfer_status_{tr.get('status')}"}

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

        # Conservative safety: block if active match involves from/to team
        active = await self.db.fetchall(
            "SELECT id, home_team_api_id, away_team_api_id, status FROM matches WHERE status IN ('running','break','halftime','setup','pending_break')",
        )
        for m in active:
            md = dict(m)
            hid = int(md.get("home_team_api_id") or -1)
            aid = int(md.get("away_team_api_id") or -1)
            if from_team_api_id in (hid, aid) or to_team_api_id in (hid, aid):
                return {"ok": False, "reason": f"active_match_blocks_transfer:{md.get('id')}"}

        # Move player in players table (PK includes team_api_id)
        player_rows = await self.db.fetchall(
            "SELECT * FROM players WHERE api_id=? AND season=?",
            (player_api_id, season_year),
        )
        moved_any = False
        for pr in player_rows:
            pd = dict(pr)
            old_team = int(pd.get("team_api_id") or -1)
            if from_team_api_id is not None and old_team != from_team_api_id:
                continue
            # remove old row
            await self.db.execute("DELETE FROM players WHERE api_id=? AND team_api_id=? AND season=?", (player_api_id, old_team, season_year))
            # insert at destination (preserve data)
            pd["team_api_id"] = to_team_api_id
            await self.db.upsert_players([pd], to_team_api_id, season_year)
            moved_any = True
            if from_team_api_id is None:
                from_team_api_id = old_team
        if not moved_any:
            # try to create minimal record at destination if player exists in any season
            any_player = await self.db.fetchone("SELECT * FROM players WHERE api_id=? ORDER BY season DESC, updated_at DESC LIMIT 1", (player_api_id,))
            if any_player:
                pd = dict(any_player)
                pd["team_api_id"] = to_team_api_id
                await self.db.upsert_players([pd], to_team_api_id, season_year)
                moved_any = True

        # career stats current team
        try:
            await self.db.execute(
                "UPDATE player_career_stats SET current_team_api_id=?, updated_at=? WHERE player_api_id=?",
                (to_team_api_id, utc_now_iso(), player_api_id),
            )
        except Exception:
            pass

        # season_player_stats - move rows for this season across team pk
        season_id = tr.get("season_id")
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
                     int(sd.get("motm") or 0), utc_now_iso()),
                )

        # record in player_transfers
        try:
            await self.db.execute(
                """INSERT INTO player_transfers(player_api_id,from_team_api_id,to_team_api_id,season,transfer_date,transfer_type,note,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (player_api_id, from_team_api_id, to_team_api_id, season_year, utc_now_iso()[:10], tr.get("transfer_type") or "manual", tr.get("note"), utc_now_iso()),
            )
        except Exception:
            pass

        now = utc_now_iso()
        await self.db.execute(
            "UPDATE season_transfers SET status='confirmed', confirmed_by=?, confirmed_at=?, updated_at=?, from_team_api_id=? WHERE id=?",
            (confirmed_by, now, now, from_team_api_id, int(transfer_id)),
        )
        tr2 = await self.db.fetchone("SELECT * FROM season_transfers WHERE id=?", (int(transfer_id),))
        out = dict(tr2) if tr2 else tr
        out["ok"] = True
        out["moved"] = moved_any
        return out

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
        """Find season_teams matching name in active seasons for a guild. Returns candidates with season info."""
        q = (team_query or "").strip().lower()
        if not q:
            return []
        seasons = await self.list_seasons(guild_id=guild_id, status="active")
        if league_key:
            seasons = [s for s in seasons if str(s.get("league_key")) == str(league_key)]
        # fallback: include global active seasons if none for guild
        if not seasons and guild_id is not None:
            seasons = await self.list_seasons(guild_id=None, status="active")
            if league_key:
                seasons = [s for s in seasons if str(s.get("league_key")) == str(league_key)]
        out: list[dict[str, Any]] = []
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
                    rec["_match_score"] = 3 if (q == name or q == short) else (2 if name.startswith(q) else 1)
                    out.append(rec)
        out.sort(key=lambda r: (-int(r.get("_match_score", 0)), str(r.get("team_name") or "")))
        # dedupe by (season_id, team_api_id)
        seen = set()
        deduped = []
        for r in out:
            k = (str(r.get("season_id")), int(r.get("team_api_id") or 0))
            if k in seen:
                continue
            seen.add(k)
            deduped.append(r)
        return deduped

    async def claim_team(self, *, guild_id: int | None, user_id: int, team_query: str, league_key: str | None = None) -> dict[str, Any]:
        candidates = await self.find_team_in_active_seasons(guild_id, team_query, league_key=league_key)
        if not candidates:
            return {"ok": False, "reason": "team_not_found"}
        # prefer exact match
        exact = [c for c in candidates if int(c.get("_match_score", 0)) == 3]
        pick = exact[0] if len(exact) == 1 else (candidates[0] if len(candidates) == 1 else None)
        if pick is None:
            # ambiguous
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
            return {"ok": True, "already_claimed_by_you": True, "team": existing, "season_id": season_id}
        if owner is not None and int(owner) != int(user_id):
            return {"ok": False, "reason": "already_claimed", "team": existing, "season_id": season_id, "owner_user_id": int(owner)}

        # Optional: prevent claiming multiple teams in same season
        already = await self.db.fetchone(
            "SELECT * FROM season_teams WHERE season_id=? AND manager_user_id=?",
            (season_id, int(user_id)),
        )
        if already:
            a = dict(already)
            return {"ok": False, "reason": "user_already_has_team", "team": existing, "season_id": season_id, "current_team": a}

        now = utc_now_iso()
        await self.db.execute(
            "UPDATE season_teams SET manager_user_id=?, is_user_controlled=1, updated_at=? WHERE season_id=? AND team_api_id=?",
            (int(user_id), now, season_id, team_api_id),
        )
        updated = await self.get_season_team(season_id, team_api_id)
        season = await self.get_season(season_id)
        return {"ok": True, "already_claimed_by_you": False, "team": updated, "season_id": season_id, "season": season}

    async def release_team(self, *, guild_id: int | None, user_id: int, team_query: str, league_key: str | None = None) -> dict[str, Any]:
        candidates = await self.find_team_in_active_seasons(guild_id, team_query, league_key=league_key)
        if not candidates:
            return {"ok": False, "reason": "team_not_found"}
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
        updated = await self.get_season_team(season_id, team_api_id)
        return {"ok": True, "team": updated, "season_id": season_id}

    async def list_claimed_teams(self, guild_id: int | None, league_key: str | None = None) -> list[dict[str, Any]]:
        seasons = await self.list_seasons(guild_id=guild_id, status="active")
        if league_key:
            seasons = [s for s in seasons if str(s.get("league_key")) == str(league_key)]
        if not seasons and guild_id is not None:
            seasons = await self.list_seasons(guild_id=None, status="active")
            if league_key:
                seasons = [s for s in seasons if str(s.get("league_key")) == str(league_key)]
        out = []
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
                out.append(d)
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
