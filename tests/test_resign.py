import pytest

from db.database import Database
from services.season_service import SeasonService


async def _setup_db():
    db = Database(":memory:")
    await db.connect()
    await db.initialize()
    return db


@pytest.mark.asyncio
async def test_resign_clears_claim_and_vacates_team():
    db = await _setup_db()
    try:
        ss = SeasonService(db)
        # Seed a minimal team and claim
        await db.execute(
            "INSERT INTO teams(api_id,season,league_id,league_name,country,name,short_name,code,logo,venue_name,raw_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (100, 2026, 1, "Premier League", "England", "Test FC", "TFC", "TFC", None, None, None),
        )
        res = await ss.claim_team(guild_id=1, user_id=42, team_query="Test FC", league_key="premier_league")
        assert res.get("ok") is True

        claim = await ss._get_user_team_claim(1, 42, league_key="premier_league")
        assert claim is not None
        assert int(claim.get("team_api_id") or 0) == 100

        resign = await ss.resign_manager(guild_id=1, user_id=42)
        assert resign.get("ok") is True
        released = resign.get("released") or []
        assert any(int(r.get("team_api_id") or 0) == 100 for r in released)

        claim_after = await ss._get_user_team_claim(1, 42, league_key="premier_league")
        assert claim_after is None

        # Team should be claimable again
        res2 = await ss.claim_team(guild_id=1, user_id=99, team_query="Test FC", league_key="premier_league")
        assert res2.get("ok") is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_resign_no_claimed_team():
    db = await _setup_db()
    try:
        ss = SeasonService(db)
        res = await ss.resign_manager(guild_id=1, user_id=42)
        assert res.get("ok") is False
        assert res.get("reason") == "no_claimed_team"
    finally:
        await db.close()
