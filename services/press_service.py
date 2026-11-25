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


def _event_player(e: Any) -> str | None:
    p = getattr(e, "payload", None) or {}
    for k in ("player_name", "name", "scorer_name", "taker_name", "off_name", "on_name"):
        v = p.get(k)
        if v:
            return str(v)
    return None


def generate_press_questions(state: Any, report: dict[str, Any] | None = None) -> dict[str, Any]:
    report = report or {}
    home = getattr(state.home.team, "name", "Home")
    away = getattr(state.away.team, "name", "Away")
    hs, aws = _score(state)
    total = hs + aws
    diff = abs(hs - aws)
    winner = home if hs > aws else (away if aws > hs else None)
    loser = away if hs > aws else (home if aws > hs else None)
    events = list(getattr(state, "events", []) or [])
    goals = [e for e in events if getattr(e, "event_type", "") in ("goal", "penalty_scored")]
    reds = [e for e in events if getattr(e, "event_type", "") in ("red_card", "second_yellow")]
    yellows = [e for e in events if getattr(e, "event_type", "") == "yellow_card"]
    pens = [e for e in events if getattr(e, "event_type", "") in ("penalty_scored", "penalty_missed", "penalty_saved")]
    injuries = [e for e in events if getattr(e, "event_type", "") in ("injury", "injury_return")]
    late_goal = next((e for e in goals if int(getattr(e, "minute", 0) or 0) >= 85), None)
    first_goal = goals[0] if goals else None
    clean_sheet = (hs == 0 or aws == 0)
    stats = (report or {}).get("stats") or {}
    home_poss = stats.get("possession_home")
    away_poss = stats.get("possession_away")
    home_shots = stats.get("shots_home")
    away_shots = stats.get("shots_away")

    rng = random.Random(str(getattr(state, "id", "")) + f"{hs}-{aws}-press")
    match_id = str(getattr(state, "id", ""))

    # Build a rich pool and pick 3 distinct questions
    pool: list[str] = []
    if winner and diff >= 3:
        pool.append(
            f"{winner} secured a dominant {hs}-{aws} win over {loser}. Looking beyond the scoreline, which tactical decisions or individual performances do you think set the tone, and how do you keep the squad grounded after such a convincing result?"
        )
    if winner and diff == 1:
        pool.append(
            f"It finished {home} {hs}-{aws} {away} in a tight contest. From your perspective, what was the decisive moment that swung the match, and how do you assess your team's game management in the final stages under pressure?"
        )
    if not winner:
        pool.append(
            f"The match ended {home} {hs}-{aws} {away}. Considering the balance of play, do you feel your side created enough clear chances to win, and what specific adjustments will you make before the next fixture?"
        )
    if late_goal:
        p = _event_player(late_goal) or "a late goal"
        m = int(getattr(late_goal, "minute", 0) or 0)
        pool.append(
            f"There was late drama at {m}' with {p} involved in a decisive moment in the {home} {hs}-{aws} {away} result. How did you see the team's mentality in that period, and what instructions did you give before that final phase?"
        )
    if reds:
        p = _event_player(reds[0])
        m = int(getattr(reds[0], "minute", 0) or 0)
        who = f" involving {p}" if p else ""
        pool.append(
            f"A red card incident{who} around {m}' had a major influence on {home} {hs}-{aws} {away}. How did that change your tactical plan, and do you think your team adapted well to the numerical shift?"
        )
    if pens:
        pool.append(
            f"Penalty incidents were a major talking point in {home} {hs}-{aws} {away}. What is your assessment of the penalty decisions, your players' composure in those moments, and how you prepare the squad mentally for high-pressure situations like that?"
        )
    if clean_sheet and winner:
        pool.append(
            f"{winner} kept a clean sheet in the {hs}-{aws} result. How satisfied are you with the defensive structure and pressing coordination, and was that solidity planned specifically for {loser}'s threats?"
        )
    if total == 0:
        pool.append(
            f"It finished goalless between {home} and {away}. Was this mainly down to disciplined defending, a lack of cutting edge in the final third, or the match tempo not suiting your attacking players?"
        )
    if first_goal:
        p = _event_player(first_goal) or "the opener"
        m = int(getattr(first_goal, "minute", 0) or 0)
        pool.append(
            f"The opening goal ({p}, {m}') shaped the flow of {home} {hs}-{aws} {away}. How important was that moment in your game plan, and did the match unfold the way you expected after going ahead or falling behind?"
        )
    if home_poss is not None and away_poss is not None:
        pool.append(
            f"The possession battle finished around {home_poss}-{away_poss} in {home} {hs}-{aws} {away}. Do you feel your side controlled the right areas of the pitch, and what does this tell you about your midfield balance and build-up play?"
        )
    if home_shots is not None and away_shots is not None:
        pool.append(
            f"Shot count and chance quality were under scrutiny in {home} {hs}-{aws} {away} (shots {home_shots}-{away_shots}). Are you satisfied with the chances created, or do you think your team needs to be more ruthless and creative in the final third?"
        )
    if injuries:
        pool.append(
            f"Injuries/fitness concerns appeared during {home} {hs}-{aws} {away}. What is the latest update on the affected players, and how will that influence your rotation and tactical choices in the next match?"
        )
    if yellows:
        pool.append(
            f"Discipline was a factor with {len(yellows)} yellow card(s) shown in {home} {hs}-{aws} {away}. Are you concerned about your team's aggression and decision-making in duels, and how will you manage suspensions or caution risks going forward?"
        )
    # Always-available detailed fallbacks
    pool.append(
        f"After {home} {hs}-{aws} {away}, what was the most important tactical lesson you take from this match, and which specific area do you want to improve immediately in training?"
    )
    pool.append(
        f"Looking at the overall performance in {home} {hs}-{aws} {away}, which player or unit impressed you most, and where did you feel the team fell short of your standards?"
    )
    pool.append(
        f"With this {home} {hs}-{aws} {away} result in mind, how does this affect your short-term objectives and dressing-room message for the next fixture?"
    )

    # Deduplicate preserving order
    seen = set()
    uniq = []
    for q in pool:
        k = q.strip().lower()
        if k and k not in seen:
            seen.add(k)
            uniq.append(q.strip())

    rng.shuffle(uniq)
    questions = uniq[:3]
    while len(questions) < 3:
        questions.append(f"After {home} {hs}-{aws} {away}, what is your message to the supporters and what must improve next?")

    return {
        "questions": questions,
        "question": questions[0],
        "home_team": home,
        "away_team": away,
        "home_score": hs,
        "away_score": aws,
        "match_id": match_id,
    }


class PressService:
    def __init__(self, db: Database):
        self.db = db

    async def create_for_match(self, *, state: Any, report: dict[str, Any] | None, channel_id: int | None, guild_id: int | None) -> dict[str, Any] | None:
        try:
            q = generate_press_questions(state, report)
            season_id = getattr(state, "season_id", None)
            league_key = getattr(state, "league_key", None)
            # target home manager if claimed, else away manager if claimed, else none
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
            questions = q.get("questions") or [q.get("question") or "What are your thoughts on the match?"]
            cur = await self.db.execute(
                """INSERT INTO press_conferences(match_id,season_id,guild_id,channel_id,league_key,team_api_id,team_name,manager_user_id,question,context_json,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?, 'open', ?,?)""",
                (q["match_id"], str(season_id) if season_id else None, guild_id, channel_id, league_key, team_api_id, team_name, manager_user_id, json.dumps(questions), json.dumps(q), utc_now_iso(), utc_now_iso()),
            )
            pid = cur.lastrowid
            row = await self.db.fetchone("SELECT * FROM press_conferences WHERE id=?", (int(pid),))
            return dict(row) if row else {"id": pid, **q, "questions": questions}
        except Exception:
            logger.exception("Failed to create press conference")
            return None
