import pytest

from db.database import Database
from services.season_service import SeasonService


async def _setup_db():
    db = Database(":memory:")
    await db.connect()
    await db.initialize()
    return db


async def _seed_teams(db: Database, season_year: int = 2026):
    # Seed 20 teams per league (enough for 32-team competition)
    leagues = {
        "premier_league": "Premier League",
        "la_liga": "La Liga",
        "bundesliga": "Bundesliga",
        "serie_a": "Serie A",
        "ligue_1": "Ligue 1",
    }
    tid = 1000
    for lk, lname in leagues.items():
        for i in range(20):
            tid += 1
            name = f"{lk.upper()} Team {i+1}"
            await db.execute(
                "INSERT OR REPLACE INTO teams(api_id,season,league_id,league_name,country,name,short_name,code,logo,venue_name,raw_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                (tid, season_year, 1, lname, "X", name, name[:3].upper(), name[:3].upper(), None, None, None),
            )
            # seed players so seed rating has signal
            for p in range(16):
                pid = tid * 100 + p
                overall = 85 - i + (p % 3)
                await db.execute(
                    """INSERT OR REPLACE INTO players(api_id,season,team_api_id,name,firstname,lastname,age,dob,nationality,photo,position,detailed_position,overall,potential,market_value,wage,height_cm,weight_kg,preferred_foot,shirt_number,status,raw_json,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
                    (pid, season_year, tid, f"P{pid}", "P", str(pid), 25, None, "X", None, "MID", "CM", float(overall), float(overall + 2), 10_000_000, 100_000, 180, 75, "Right", p + 1, "active", None),
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("competition_key", ["champions_league", "europa_league", "conference_league"])
async def test_european_pots_and_fixtures(competition_key):
    db = await _setup_db()
    try:
        await _seed_teams(db)
        ss = SeasonService(db)

        season = await ss.create_season(guild_id=1, league_key=competition_key, season_year=2026)
        sid = season["id"]

        pots_res = await ss.generate_european_pots(season_id=sid, competition_key=competition_key, season_year=2026)
        assert pots_res.get("ok") is True
        pots = pots_res.get("pots") or []
        cfg = ss.get_european_competition_config(competition_key)
        assert len(pots) == cfg["pot_count"]
        all_teams = [t for p in pots for t in p]
        assert len(all_teams) == cfg["team_count"]
        # no duplicates
        tids = [int(t["team_api_id"]) for t in all_teams]
        assert len(tids) == len(set(tids))
        # pot sizes
        for p in pots:
            assert len(p) == cfg["pots_size"]

        draw_res = await ss.draw_european_groups(season_id=sid, competition_key=competition_key, seed=42)
        assert draw_res.get("ok") is True
        groups = draw_res.get("groups") or []
        assert len(groups) == cfg["group_count"]
        for g in groups:
            teams = g.get("teams") or []
            assert len(teams) == cfg["teams_per_group"]
            # same-association constraint
            doms = [str(t.get("domestic_league_key") or "") for t in teams]
            doms = [d for d in doms if d]
            assert len(doms) == len(set(doms))
            # one per pot
            pots_seen = sorted({int(t.get("pot") or 0) for t in teams})
            assert pots_seen == list(range(1, cfg["pot_count"] + 1))

        fx_res = await ss.generate_european_group_stage_fixtures(season_id=sid, competition_key=competition_key)
        assert fx_res.get("ok") is True
        assert fx_res.get("fixtures_created") == cfg["group_count"] * 6 * 2  # 8 groups * 6 md * 2 matches? 4-team group: C(4,2)*2=12 fixtures per group
        # 4-team double round robin = 12 matches per group, 8 groups = 96
        assert fx_res.get("fixtures_created") == 96
        assert fx_res.get("matchdays") == 6

        fixtures = await db.fetchall("SELECT * FROM fixtures WHERE season_id=? AND league_key=?", (sid, competition_key))
        fixtures = [dict(r) for r in fixtures]
        assert len(fixtures) == 96
        # no duplicate pair per matchday
        seen = set()
        for f in fixtures:
            k = (int(f["matchday"]), int(f["home_team_api_id"]), int(f["away_team_api_id"]))
            assert k not in seen
            seen.add(k)
        # per-team fixture count
        for g in groups:
            for t in g.get("teams") or []:
                tid = int(t["team_api_id"])
                c = sum(1 for f in fixtures if int(f["home_team_api_id"]) == tid or int(f["away_team_api_id"]) == tid)
                assert c == 6

        val = await ss.validate_european_competition(sid, competition_key)
        assert val.get("ok") is True
        assert val.get("issues") == []
    finally:
        await db.close()
