from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

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
        })
        await self.db.upsert_match_participant(match_id=self.state.id, side="home", team_api_id=self.state.home.team.api_id, manager_user_id=self.home_manager_id, is_bot=self.home_manager_id is None)
        await self.db.upsert_match_participant(match_id=self.state.id, side="away", team_api_id=self.state.away.team.api_id, manager_user_id=self.away_manager_id, is_bot=self.away_manager_id is None)

    async def append_events(self, events: list[dict[str, Any]]) -> None:
        if not self.persist:
            return
        for ev in events:
            await self.db.append_match_event(
                match_id=self.state.id, seq=ev.get("seq"), minute=ev.get("minute", 0), extra_minute=ev.get("extra_minute", 0), phase=ev.get("phase"),
                event_type=ev.get("event_type"), team_side=ev.get("team_side"), player_api_id=ev.get("player_api_id"),
                payload=ev.get("payload") or {}, commentary=ev.get("commentary"), public_payload=ev.get("public_payload") or ev.get("payload") or {},
            )

    async def start_and_simulate_until_break(self, tick_callback: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None) -> list[dict[str, Any]]:
        async with self.lock:
            out = self.engine.start() if self.state.status == "setup" else []
            await self.append_events(out)
            if tick_callback and out:
                await tick_callback(out)
            step = self.engine.simulate_until_break_or_end()
            out.extend(step.events)
            await self.append_events(step.events)
            if tick_callback and step.events:
                await tick_callback(step.events)
            await self.save()
            return out

    async def continue_after_break(self, tick_callback: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None) -> list[dict[str, Any]]:
        async with self.lock:
            evs = self.engine.resume_after_break()
            await self.append_events(evs)
            if tick_callback and evs:
                await tick_callback(evs)
            step = self.engine.simulate_until_break_or_end()
            evs.extend(step.events)
            await self.append_events(step.events)
            if tick_callback and step.events:
                await tick_callback(step.events)
            await self.save()
            return evs

    async def apply_break_actions(self, side: str, substitutions: list[dict[str, Any]] | None = None, tactics_updates: dict[str, Any] | None = None, summary: str | None = None) -> list[dict[str, Any]]:
        async with self.lock:
            evs: list[dict[str, Any]] = []
            for sub in (substitutions or []):
                evs.append(self.engine.apply_substitution(side, int(sub["off_player_id"]), int(sub["on_player_id"]), reason=sub.get("reason")))
            if tactics_updates or summary:
                evs.append(self.engine.apply_tactical_change(side, tactics_updates or {}, summary=summary))
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
                    evs.append(self.engine.apply_substitution("home", int(sub["off_player_id"]), int(sub["on_player_id"]), reason=sub.get("reason")))
                if actions.get("tactics_updates"):
                    evs.append(self.engine.apply_tactical_change("home", actions["tactics_updates"], summary="AI tactical adjustment"))
                self.engine.mark_break_decision("home", True)
            if self.away_manager_id is None and not self.state.away_break_decision_done and self.state.pending_break:
                actions = decide_ai_break_actions(self.state, "away")
                for sub in actions.get("substitutions", []):
                    evs.append(self.engine.apply_substitution("away", int(sub["off_player_id"]), int(sub["on_player_id"]), reason=sub.get("reason")))
                if actions.get("tactics_updates"):
                    evs.append(self.engine.apply_tactical_change("away", actions["tactics_updates"], summary="AI tactical adjustment"))
                self.engine.mark_break_decision("away", True)
            await self.append_events(evs)
            await self.save()
            return evs

    def is_ready_to_resume(self) -> bool:
        if self.state.pending_break is None:
            return False
        home_ok = self.state.home_break_decision_done or self.home_manager_id is None
        away_ok = self.state.away_break_decision_done or self.away_manager_id is None
        return home_ok and away_ok

    def break_status(self) -> dict[str, Any]:
        return break_status(self.state)

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
                             home_manager_id: int | None = None, away_manager_id: int | None = None) -> ActiveMatchSession:
        rng = random.Random()
        ht = Team(api_id=int(home_team["api_id"]), name=home_team.get("name", "Home"), league_id=home_team.get("league_id"), league_name=home_team.get("league_name"), country=home_team.get("country"), logo=home_team.get("logo"), short_name=home_team.get("short_name") or home_team.get("code"))
        at = Team(api_id=int(away_team["api_id"]), name=away_team.get("name", "Away"), league_id=away_team.get("league_id"), league_name=away_team.get("league_name"), country=away_team.get("country"), logo=away_team.get("logo"), short_name=away_team.get("short_name") or away_team.get("code"))
        hl = home_lineup or generate_ai_lineup(ht.api_id, ht.name, home_squad, rng)
        al = away_lineup or generate_ai_lineup(at.api_id, at.name, away_squad, rng)
        htac = home_tactics or (generate_ai_tactics(rng) if home_manager_id is None else default_tactics())
        atac = away_tactics or (generate_ai_tactics(rng) if away_manager_id is None else default_tactics())
        engine = MatchEngine.initialize(
            match_id=new_match_id(), guild_id=guild_id, channel_id=channel_id, mode=mode,
            home_team=ht, away_team=at, home_lineup=hl, away_lineup=al,
            home_tactics=htac, away_tactics=atac,
            home_players=self._players_from_squad(home_squad, ht.api_id), away_players=self._players_from_squad(away_squad, at.api_id),
        )
        session = ActiveMatchSession(state=engine.state, engine=engine, db=self.db, home_manager_id=home_manager_id, away_manager_id=away_manager_id)
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
        session = ActiveMatchSession(state=state, engine=engine, db=self.db)
        self.sessions[match_id] = session
        return session
