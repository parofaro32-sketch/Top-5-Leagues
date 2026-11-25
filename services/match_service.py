from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

from engine.ai_manager import decide_ai_break_actions, generate_ai_lineup, generate_ai_tactics
from engine.match_engine import MatchEngine
from engine.models import Lineup, MatchState, Player, Team, default_tactics, new_match_id
from engine.report import break_status, full_time_report
from db.database import Database


@dataclass
class ActiveMatchSession:
    state: MatchState
    engine: MatchEngine
    db: Database
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    home_manager_id: int | None = None
    away_manager_id: int | None = None
    commentary_channel_id: int | None = None
    status_message_id: int | None = None
    persist: bool = True

    async def save(self) -> None:
        if not self.persist:
            return
        try:
            await self.db.save_match({
                "id": self.state.id,
                "guild_id": self.state.guild_id,
                "channel_id": self.state.channel_id,
                "message_id": self.state.message_id,
                "home_team_api_id": self.state.home.team.api_id,
                "away_team_api_id": self.state.away.team.api_id,
                "home_team_name": self.state.home.team.name,
                "away_team_name": self.state.away.team.name,
                "mode": self.state.mode,
                "status": self.state.status,
                "current_minute": self.state.minute,
                "phase": self.state.phase,
                "state": self.state.to_dict(),
                "season_id": self.state.season_id,
                "fixture_id": self.state.fixture_id,
                "league_key": self.state.league_key,
                "competition_type": self.state.competition_type,
            })
            await self.db.upsert_match_participant(match_id=self.state.id, side="home", team_api_id=self.state.home.team.api_id, manager_user_id=self.home_manager_id, is_bot=self.home_manager_id is None)
            await self.db.upsert_match_participant(match_id=self.state.id, side="away", team_api_id=self.state.away.team.api_id, manager_user_id=self.away_manager_id, is_bot=self.away_manager_id is None)
        except Exception:
            logger.exception("MATCH_SERVICE save failed match_id=%s", self.state.id)

    async def append_events(self, events: list[dict[str, Any]]) -> None:
        if not self.persist:
            return
        for ev in events:
            try:
                await self.db.append_match_event(
                    match_id=self.state.id, seq=ev.get("seq"), minute=ev.get("minute", 0), extra_minute=ev.get("extra_minute", 0), phase=ev.get("phase"),
                    event_type=ev.get("event_type"), team_side=ev.get("team_side"), player_api_id=ev.get("player_api_id"),
                    payload=ev.get("payload") or {}, commentary=ev.get("commentary"), public_payload=ev.get("public_payload") or ev.get("payload") or {},
                )
            except Exception:
                logger.exception("MATCH_SERVICE append_event failed match_id=%s seq=%s", self.state.id, ev.get("seq"))

    async def start_if_setup(self) -> list[dict[str, Any]]:
        """Start the match (emit kickoff) if still in setup. Returns kickoff events."""
        async with self.lock:
            if self.state.status != "setup":
                return []
            if not self.is_setup_complete():
                missing = ", ".join(self.missing_submissions()) or "unknown"
                logger.info("MATCH_SERVICE start blocked - setup incomplete match_id=%s missing=%s", self.state.id, missing)
                return []
            out = self.engine.start()
            await self.append_events(out)
            await self.save()
            return out

    async def start_and_simulate_until_break(self, tick_callback: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None) -> list[dict[str, Any]]:
        async with self.lock:
            if not self.is_setup_complete():
                missing = ", ".join(self.missing_submissions()) or "unknown"
                logger.info("MATCH_SERVICE start blocked - setup incomplete match_id=%s missing=%s", self.state.id, missing)
                return []
            out = self.engine.start() if self.state.status == "setup" else []
            await self.append_events(out)
            if tick_callback and out:
                try:
                    await tick_callback(out)
                except Exception:
                    logger.exception("MATCH_SERVICE tick_callback failed (start) match_id=%s", self.state.id)
            try:
                loop = asyncio.get_running_loop()
                step = await loop.run_in_executor(None, self.engine.simulate_until_break_or_end)
                out.extend(step.events)
                await self.append_events(step.events)
                if tick_callback and step.events:
                    try:
                        await tick_callback(step.events)
                    except Exception:
                        logger.exception("MATCH_SERVICE tick_callback failed (step) match_id=%s", self.state.id)
            except Exception:
                logger.exception("MATCH_SERVICE simulate_until_break_or_end failed match_id=%s", self.state.id)
            await self.save()
            return out

    async def continue_after_break(self, tick_callback: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None) -> list[dict[str, Any]]:
        async with self.lock:
            try:
                evs = self.engine.resume_after_break()
            except Exception:
                logger.exception("MATCH_SERVICE resume_after_break failed match_id=%s", self.state.id)
                evs = []
            await self.append_events(evs)
            if tick_callback and evs:
                try:
                    await tick_callback(evs)
                except Exception:
                    logger.exception("MATCH_SERVICE tick_callback failed (resume) match_id=%s", self.state.id)
            try:
                loop = asyncio.get_running_loop()
                step = await loop.run_in_executor(None, self.engine.simulate_until_break_or_end)
                evs.extend(step.events)
                await self.append_events(step.events)
                if tick_callback and step.events:
                    try:
                        await tick_callback(step.events)
                    except Exception:
                        logger.exception("MATCH_SERVICE tick_callback failed (continue_step) match_id=%s", self.state.id)
            except Exception:
                logger.exception("MATCH_SERVICE continue simulate failed match_id=%s", self.state.id)
            await self.save()
            return evs

    async def apply_break_actions(self, side: str, substitutions: list[dict[str, Any]] | None = None, tactics_updates: dict[str, Any] | None = None, summary: str | None = None, mark_done: bool = True) -> list[dict[str, Any]]:
        async with self.lock:
            evs: list[dict[str, Any]] = []
            for sub in (substitutions or []):
                evs.append(self.engine.apply_substitution(side, int(sub["off_player_id"]), int(sub["on_player_id"]), reason=sub.get("reason")))
            if tactics_updates or summary:
                # Security: strictly normalize tactics and sanitize summary; drop outcome-manipulation keys
                try:
                    from services.tactics_service import normalize_tactics, sanitize_tactics_summary
                    tactics_updates = normalize_tactics(tactics_updates or {})
                    summary = sanitize_tactics_summary(summary)
                except Exception:
                    tactics_updates = {}
                    summary = None
                evs.append(self.engine.apply_tactical_change(side, tactics_updates or {}, summary=summary))
            if mark_done:
                self.engine.mark_break_decision(side, True)
            await self.append_events(evs)
            await self.save()
            return evs

    async def auto_resolve_ai_sides_if_ready(self) -> list[dict[str, Any]]:
        async with self.lock:
            evs: list[dict[str, Any]] = []
            if self.home_manager_id is None and not self.state.home_break_decision_done and self.state.pending_break:
                actions = decide_ai_break_actions(self.state, "home")
                for sub in actions.get("substitutions", []):
                    try:
                        evs.append(self.engine.apply_substitution("home", int(sub["off_player_id"]), int(sub["on_player_id"]), reason=sub.get("reason") or "tactical/freshness"))
                    except Exception:
                        pass
                if actions.get("tactics_updates") or actions.get("notes"):
                    try:
                        notes = actions.get("notes") or []
                        base = ", ".join(notes) if notes else "AI tactical adjustment"
                        if actions.get("tactics_updates"):
                            summary = f"AI tactical adjustment ({base})" if notes else "AI tactical adjustment"
                        else:
                            summary = f"AI assessment ({base})"
                        evs.append(self.engine.apply_tactical_change("home", actions.get("tactics_updates") or {}, summary=summary))
                    except Exception:
                        pass
                self.engine.mark_break_decision("home", True)
            if self.away_manager_id is None and not self.state.away_break_decision_done and self.state.pending_break:
                actions = decide_ai_break_actions(self.state, "away")
                for sub in actions.get("substitutions", []):
                    try:
                        evs.append(self.engine.apply_substitution("away", int(sub["off_player_id"]), int(sub["on_player_id"]), reason=sub.get("reason") or "tactical/freshness"))
                    except Exception:
                        pass
                if actions.get("tactics_updates") or actions.get("notes"):
                    try:
                        notes = actions.get("notes") or []
                        base = ", ".join(notes) if notes else "AI tactical adjustment"
                        if actions.get("tactics_updates"):
                            summary = f"AI tactical adjustment ({base})" if notes else "AI tactical adjustment"
                        else:
                            summary = f"AI assessment ({base})"
                        evs.append(self.engine.apply_tactical_change("away", actions.get("tactics_updates") or {}, summary=summary))
                    except Exception:
                        pass
                self.engine.mark_break_decision("away", True)
            if evs:
                await self.append_events(evs)
            await self.save()
            return evs

    def side_submission_complete(self, side: str) -> bool:
        sub = self.state.submission or {}
        return bool(sub.get(f"{side}_lineup_submitted")) and bool(sub.get(f"{side}_tactics_submitted"))

    def is_setup_complete(self) -> bool:
        # Bots are always complete; humans must submit lineup + tactics
        home_ok = self.home_manager_id is None or self.side_submission_complete("home")
        away_ok = self.away_manager_id is None or self.side_submission_complete("away")
        return home_ok and away_ok

    def missing_submissions(self) -> list[str]:
        missing = []
        for side, mgr in (("home", self.home_manager_id), ("away", self.away_manager_id)):
            if mgr is None:
                continue
            if not self.side_submission_complete(side):
                missing.append(side)
        return missing

    def is_ready_to_resume(self) -> bool:
        if self.state.pending_break is None:
            return False
        home_ok = self.state.home_break_decision_done or self.home_manager_id is None
        away_ok = self.state.away_break_decision_done or self.away_manager_id is None
        return home_ok and away_ok

    def break_status(self) -> dict[str, Any]:
        return break_status(self.state)

    async def persist_player_stats(self) -> bool:
        """Persist player match/career stats once. Returns True if persisted now."""
        sub = self.state.submission or {}
        if sub.get("stats_persisted"):
            return False
        if self.state.status != "finished":
            return False
        from engine.report import man_of_the_match
        motm = man_of_the_match(self.state) or {}
        motm_name = motm.get("name")
        comp = sub.get("competition") or self.state.home.team.league_name or "Friendly"
        for side in ("home", "away"):
            team = self.state.team(side)
            opp = self.state.team(self.state.opp_side(side))
            team_goals = team.score
            opp_goals = opp.score
            result = "D"
            if team_goals > opp_goals: result = "W"
            elif team_goals < opp_goals: result = "L"
            clean_sheet = 1 if opp_goals == 0 else 0
            for ps in self.state.player_states.values():
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
                    "match_id": self.state.id,
                    "team_api_id": team.team.api_id,
                    "opponent_api_id": opp.team.api_id,
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
                    logger.exception("Failed to record player match history player=%s match=%s", ps.player_api_id, self.state.id)
                try:
                    patch = {
                        "name": ps.name,
                        "current_team_api_id": team.team.api_id,
                        "matches_played": 1 if minutes > 0 or ps.on_pitch or ps.substituted_in_minute is not None or ps.is_starter else 0,
                        "starts": started,
                        "minutes_played": minutes,
                        "goals": int(ps.goals or 0),
                        "assists": int(ps.assists or 0),
                        "yellow_cards": int(ps.yellow_cards or 0),
                        "red_cards": 1 if ps.red_card else 0,
                        "clean_sheets": cs,
                        "saves": int(ps.saves or 0),
                        "rating_sum": float(ps.rating or 0.0),
                        "rating_count": 1 if minutes > 0 or ps.is_starter else 0,
                        "potm_awards": is_motm,
                        "rating_progression_entry": {"match_id": self.state.id, "rating": float(ps.rating or 0.0), "date": self.state.public_stats.get("finished_at")},
                    }
                    if patch["matches_played"] or patch["rating_count"]:
                        await self.db.upsert_player_career_stats(int(ps.player_api_id), patch)
                except Exception:
                    logger.exception("Failed to upsert career stats player=%s match=%s", ps.player_api_id, self.state.id)
        sub["stats_persisted"] = True
        await self.save()
        return True

    def report(self) -> dict[str, Any]:
        return full_time_report(self.state)


class MatchService:
    def __init__(self, db: Database):
        self.db = db
        self.sessions: dict[str, ActiveMatchSession] = {}

    def _players_from_squad(self, squad: list[dict[str, Any]], team_api_id: int) -> list[Player]:
        out = []
        from engine.models import PlayerAttributes
        for p in squad:
            attrs = p.get("attributes") or {}
            out.append(Player(
                api_id=int(p.get("api_id") or p.get("id") or 0), name=p.get("name") or "Unknown", team_api_id=team_api_id,
                position=(p.get("position") or "MID").upper(), detailed_position=p.get("detailed_position"), number=p.get("number"),
                nationality=p.get("nationality"), photo=p.get("photo"), injured=bool(p.get("injured", False)),
                attributes=PlayerAttributes.from_dict(attrs),
            ))
        return out

    async def create_session(self, *, guild_id: int | None, channel_id: int | None, mode: str,
                             home_team: dict[str, Any], away_team: dict[str, Any], home_squad: list[dict[str, Any]], away_squad: list[dict[str, Any]],
                             home_lineup: Lineup | None = None, away_lineup: Lineup | None = None,
                             home_tactics: dict[str, Any] | None = None, away_tactics: dict[str, Any] | None = None,
                             home_manager_id: int | None = None, away_manager_id: int | None = None,
                             home_custom_players: list[dict[str, Any]] | None = None, away_custom_players: list[dict[str, Any]] | None = None,
                             season_id: str | None = None, fixture_id: int | None = None, league_key: str | None = None, competition_type: str = "friendly") -> ActiveMatchSession:
        import logging
        _log = logging.getLogger(__name__)
        rng = random.Random()
        ht = Team(api_id=int(home_team["api_id"]), name=home_team.get("name", "Home"), league_id=home_team.get("league_id"), league_name=home_team.get("league_name"), country=home_team.get("country"), logo=home_team.get("logo"), short_name=home_team.get("short_name") or home_team.get("code"))
        at = Team(api_id=int(away_team["api_id"]), name=away_team.get("name", "Away"), league_id=away_team.get("league_id"), league_name=away_team.get("league_name"), country=away_team.get("country"), logo=away_team.get("logo"), short_name=away_team.get("short_name") or away_team.get("code"))
        from services.lineup_service import auto_build_lineup

        home_needs_human = home_manager_id is not None and home_lineup is None
        away_needs_human = away_manager_id is not None and away_lineup is None

        # For human managers, build a temporary internal placeholder ONLY for engine init.
        # It is never exposed and the match cannot start until the manager submits a real lineup.
        if home_lineup is not None:
            hl = home_lineup
        elif home_needs_human:
            hl = auto_build_lineup(ht.api_id, ht.name, home_squad, "4-3-3")
        else:
            hl = generate_ai_lineup(ht.api_id, ht.name, home_squad, rng)
        if away_lineup is not None:
            al = away_lineup
        elif away_needs_human:
            al = auto_build_lineup(at.api_id, at.name, away_squad, "4-3-3")
        else:
            al = generate_ai_lineup(at.api_id, at.name, away_squad, rng)

        def _lu_diag(label: str, lu: Lineup):
            try:
                ids = [int(e.player_api_id) for e in (lu.starters or [])]
                names = [str(e.name).strip().lower() for e in (lu.starters or [])]
                dup_ids = [x for i, x in enumerate(ids) if x in ids[:i]]
                dup_names = [x for i, x in enumerate(names) if x and x in names[:i]]
                _log.info("SESSION_LINEUP %s formation=%s starters=%s bench=%s dup_ids=%s dup_names=%s", label, lu.formation, len(lu.starters or []), len(lu.bench or []), dup_ids[:5], dup_names[:5])
            except Exception:
                _log.exception("SESSION_LINEUP diag failed %s", label)
        _lu_diag(f"home:{ht.name}", hl)
        _lu_diag(f"away:{at.name}", al)

        htac = home_tactics or (generate_ai_tactics(rng) if home_manager_id is None else default_tactics())
        atac = away_tactics or (generate_ai_tactics(rng) if away_manager_id is None else default_tactics())
        home_players = self._players_from_squad(home_squad, ht.api_id) + self._players_from_squad(home_custom_players or [], ht.api_id)
        away_players = self._players_from_squad(away_squad, at.api_id) + self._players_from_squad(away_custom_players or [], at.api_id)
        engine = MatchEngine.initialize(
            match_id=new_match_id(), guild_id=guild_id, channel_id=channel_id, mode=mode,
            home_team=ht, away_team=at, home_lineup=hl, away_lineup=al,
            home_tactics=htac, away_tactics=atac,
            home_players=home_players, away_players=away_players,
        )
        session = ActiveMatchSession(state=engine.state, engine=engine, db=self.db, home_manager_id=home_manager_id, away_manager_id=away_manager_id)
        # Season/competition linkage
        session.state.season_id = season_id
        session.state.fixture_id = fixture_id
        session.state.league_key = league_key
        session.state.competition_type = competition_type or "friendly"
        # Submission / setup tracking
        sub = session.state.submission
        sub.setdefault("competition", "League" if competition_type == "league" else "Friendly")
        sub.setdefault("match_type", "League" if competition_type == "league" else "Friendly")
        sub.setdefault("stadium", home_team.get("venue_name") or home_team.get("venue") or f"{ht.name} Stadium")
        sub.setdefault("timeout_policy", "cancel")  # configurable: cancel | forfeit | admin
        sub.setdefault("home_lineup_submitted", not home_needs_human)
        sub.setdefault("away_lineup_submitted", not away_needs_human)
        sub.setdefault("home_tactics_submitted", home_manager_id is None)
        sub.setdefault("away_tactics_submitted", away_manager_id is None)
        sub.setdefault("home_submitted_at", None)
        sub.setdefault("away_submitted_at", None)
        sub.setdefault("deadline_at", None)
        sub.setdefault("initial_deadline_seconds", 20 * 60)
        sub.setdefault("extension_seconds", 10 * 60)
        sub.setdefault("stats_persisted", False)
        self.sessions[engine.state.id] = session
        await session.save()
        return session

    def get(self, match_id: str) -> ActiveMatchSession | None:
        return self.sessions.get(match_id)

    async def load_from_db(self, match_id: str) -> ActiveMatchSession | None:
        if match_id in self.sessions:
            return self.sessions[match_id]
        row = await self.db.get_match(match_id)
        if not row:
            return None
        state = MatchState.from_dict(self.db.loads(row["state_json"]))
        engine = MatchEngine(state)
        home_manager_id = None
        away_manager_id = None
        try:
            participants = await self.db.get_match_participants(match_id)
            for p in participants:
                pd = dict(p)
                side = pd.get("side")
                uid = pd.get("manager_user_id")
                is_bot = bool(pd.get("is_bot"))
                if side == "home":
                    home_manager_id = int(uid) if uid is not None and not is_bot else None
                elif side == "away":
                    away_manager_id = int(uid) if uid is not None and not is_bot else None
        except Exception:
            logger.exception("MATCH_SERVICE failed to load participants match_id=%s", match_id)
        session = ActiveMatchSession(state=state, engine=engine, db=self.db, home_manager_id=home_manager_id, away_manager_id=away_manager_id)
        self.sessions[match_id] = session
        return session
