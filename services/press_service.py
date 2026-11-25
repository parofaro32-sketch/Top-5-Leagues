from __future__ import annotations

import json
import logging
import random
from typing import Any

from db.database import Database, utc_now_iso

logger = logging.getLogger(__name__)


def _score(state: Any) -> tuple[int, int]:
    try:
        return int(state.home.score), int(state.away.score)
    except Exception:
        return 0, 0


def generate_press_question(state: Any, report: dict[str, Any] | None = None) -> dict[str, Any]:
    report = report or {}
    home = getattr(state.home.team, "name", "Home")
    away = getattr(state.away.team, "name", "Away")
    hs, aws = _score(state)
    total = hs + aws
    diff = abs(hs - aws)
    winner = home if hs > aws else (away if aws > hs else None)
    loser = away if hs > aws else (home if aws > hs else None)
    events = list(getattr(state, "events", []) or [])
    red_cards = [e for e in events if getattr(e, "event_type", "") in ("red_card", "second_yellow")]
    goals = [e for e in events if getattr(e, "event_type", "") in ("goal", "penalty_scored")]
    pens = [e for e in events if getattr(e, "event_type", "") in ("penalty_scored", "penalty_missed", "penalty_saved")]
    late_goal = any(int(getattr(e, "minute", 0) or 0) >= 85 for e in goals)
    clean_sheet = (hs == 0 or aws == 0)

    rng = random.Random(str(getattr(state, "id", "")) + f"{hs}-{aws}")
    prompts = []
    if winner and diff >= 3:
        prompts.append(f"{winner} won convincingly {hs}-{aws}. What pleased you most about the performance, and what still needs work?")
    if winner and diff == 1:
        prompts.append(f"It finished {home} {hs}-{aws} {away}. How important was that narrow win, and what was the key moment?")
    if not winner:
        prompts.append(f"It ended level at {hs}-{aws}. Did you feel your side deserved more, or was a draw fair?")
    if late_goal and winner:
        prompts.append(f"There was late drama in {home} {hs}-{aws} {away}. How do you assess the team's mentality in the decisive moments?")
    if red_cards:
        prompts.append(f"Cards played a role in {home} {hs}-{aws} {away}. How did the red card incident change your game plan?")
    if pens:
        prompts.append(f"Penalties were a talking point in {home} {hs}-{aws} {away}. What is your view on the penalty moments and the team's response?")
    if clean_sheet and winner:
        prompts.append(f"{winner} kept a clean sheet in a {hs}-{aws} result. How satisfied are you with the defensive organization?")
    if total == 0:
        prompts.append(f"A goalless draw between {home} and {away}. Was it a lack of quality in attack or strong defending?")
    if not prompts:
        prompts.append(f"After {home} {hs}-{aws} {away}, what is your main takeaway from the match and what do you tell the squad next?")

    question = rng.choice(prompts)
    return {
        "question": question,
        "home_team": home,
        "away_team": away,
        "home_score": hs,
        "away_score": aws,
        "match_id": str(getattr(state, "id", "")),
    }


class PressService:
    def __init__(self, db: Database):
        self.db = db

    async def create_for_match(self, *, state: Any, report: dict[str, Any] | None, channel_id: int | None, guild_id: int | None) -> dict[str, Any] | None:
        try:
            q = generate_press_question(state, report)
            season_id = getattr(state, "season_id", None)
            league_key = getattr(state, "league_key", None)
            # target home manager if claimed, else away manager if claimed, else none
            from services.season_service import SeasonService
            # can't easily get here; instead look up from DB season_teams
            manager_user_id = None
            team_api_id = None
            team_name = None
            if season_id:
                for side in ("home", "away"):
                    t = state.home if side == "home" else state.away
                    tid = int(getattr(t.team, "api_id", 0) or 0)
                    row = await self.db.fetchone(
                        "SELECT manager_user_id, team_name FROM season_teams WHERE season_id=? AND team_api_id=?",
                        (str(season_id), tid),
                    )
                    if row:
                        d = dict(row)
                        if d.get("manager_user_id") is not None:
                            manager_user_id = int(d.get("manager_user_id"))
                            team_api_id = tid
                            team_name = str(d.get("team_name") or getattr(t.team, "name", "Team"))
                            break
            # if no season manager, leave open to anyone? better target no one specific
            cur = await self.db.execute(
                """INSERT INTO press_conferences(match_id,season_id,guild_id,channel_id,league_key,team_api_id,team_name,manager_user_id,question,context_json,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?, 'open', ?,?)""",
                (q["match_id"], str(season_id) if season_id else None, guild_id, channel_id, league_key, team_api_id, team_name, manager_user_id, q["question"], json.dumps(q), utc_now_iso(), utc_now_iso()),
            )
            pid = cur.lastrowid
            row = await self.db.fetchone("SELECT * FROM press_conferences WHERE id=?", (int(pid),))
            return dict(row) if row else {"id": pid, **q}
        except Exception:
            logger.exception("Failed to create press conference")
            return None
