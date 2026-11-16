from __future__ import annotations

import random
from typing import Any
from engine import events as E
from engine.models import MatchState


def _team(state: MatchState, side):
    if side == "home": return state.home.team.name
    if side == "away": return state.away.team.name
    return "The team"


def _player(state: MatchState, side, pid, payload):
    if payload.get("player_name"): return payload["player_name"]
    if side and pid:
        ps = state.player_states.get(state.player_key(side, pid))
        if ps: return ps.name
    return "the player"


def generate(state: MatchState, event: dict[str, Any], rng: random.Random) -> str:
    et = event.get("event_type")
    side = event.get("team_side")
    payload = event.get("payload") or {}
    m = int(event.get("minute", state.minute))
    ex = int(event.get("extra_minute", state.extra_minute))
    pfx = f"{m}+{ex}'" if ex else f"{m}'"
    team = _team(state, side)
    opp = _team(state, state.opp_side(side)) if side in ("home","away") else ""
    player = _player(state, side, event.get("player_api_id"), payload)
    if payload.get("detail"):
        d = payload["detail"]
        # append score on goal events if detail already includes it we keep as-is
        return f"{pfx} — {d}"
    if et == E.KICKOFF: return f"{pfx} — Kick-off! {state.home.team.name} get us underway against {state.away.team.name}."
    if et == E.SECOND_HALF_KICKOFF: return f"{pfx} — Second half underway. {state.scoreline()}."
    if et == E.HALF_TIME: return f"{pfx} — Half-time. {state.scoreline()}."
    if et == E.FULL_TIME: return f"{pfx} — Full-time! {state.scoreline()}."
    if et == E.STOPPAGE_TIME: return f"{pfx} — The referee indicates {payload.get('minutes',1)} minute(s) of stoppage time at the end of {payload.get('half','this half')}."
    if et == E.TACTICAL_BREAK: return f"{pfx} — {payload.get('label','Tactical break')}. Play pauses for tactical decisions."
    if et == E.BREAK_RESUME: return f"{pfx} — Play resumes as both sides look to implement their adjustments."
    if et == E.PASS_SEQUENCE: return f"{pfx} — {team} build possession through midfield, probing {opp}'s defensive shape for an opening."
    if et == E.CHANCE: return f"{pfx} — {team} create a promising opening as {player} finds space in a dangerous area."
    if et == E.SHOT:
        o = payload.get("outcome")
        if o=="on_target": return f"{pfx} — {player} tests the goalkeeper with a shot for {team}."
        if o=="blocked": return f"{pfx} — {player}'s effort for {team} is blocked inside the area."
        return f"{pfx} — {player} shoots for {team}, but the attempt misses the target."
    if et == E.SAVE: return f"{pfx} — Important save! {payload.get('shooter_name','the attacker')} forces {player} into action."
    if et == E.BLOCK: return f"{pfx} — {player} makes a crucial block to deny {opp}."
    if et == E.CORNER: return f"{pfx} — Corner to {team} after pressure in the final third."
    if et == E.FOUL: return f"{pfx} — Foul by {player} on {payload.get('fouled_name','an opponent')}. Free kick to {opp}."
    if et == E.FREE_KICK: return f"{pfx} — {team} have a free kick in a promising position. {player} stands over it."
    if et == E.PENALTY_AWARDED: return f"{pfx} — Penalty to {team}! The referee points to the spot after {payload.get('reason','a foul in the area')}."
    if et == E.GOAL:
        a = f" (assist: {payload.get('assist_name')})" if payload.get("assist_name") else ""
        return f"{pfx} — GOAL! {player} scores for {team}{a}. Score: {state.home.team.name} {state.home.score}–{state.away.score} {state.away.team.name}."
    if et == E.PENALTY_SCORED: return f"{pfx} — GOAL! {player} converts the penalty for {team}. Score: {state.home.team.name} {state.home.score}–{state.away.score} {state.away.team.name}."
    if et == E.PENALTY_SAVED: return f"{pfx} — Penalty saved! {player} denies {payload.get('taker_name','the taker')} from the spot."
    if et == E.PENALTY_MISSED: return f"{pfx} — Penalty missed! {player} fails to convert for {team}."
    if et == E.DISALLOWED_GOAL: return f"{pfx} — Goal disallowed for {team} ({payload.get('reason','infringement')}). Score remains {state.home.team.name} {state.home.score}–{state.away.score} {state.away.team.name}."
    if et == E.OFFSIDE: return f"{pfx} — {player} is flagged offside for {team}."
    if et == E.YELLOW_CARD: return f"{pfx} — Yellow card for {player} ({team}) for {payload.get('reason','a foul')}."
    if et == E.SECOND_YELLOW: return f"{pfx} — Second yellow! {player} ({team}) is sent off."
    if et == E.RED_CARD: return f"{pfx} — Red card! {player} ({team}) is dismissed for {payload.get('reason','serious foul play')}."
    if et == E.INJURY: return f"{pfx} — Injury concern for {team}: {player} ({payload.get('severity','knock')})."
    if et == E.SUBSTITUTION: return f"{pfx} — Substitution, {team}: {payload.get('on_name','substitute')} replaces {payload.get('off_name',player)}."
    if et == E.TACTICAL_CHANGE: return f"{pfx} — {team} make a tactical adjustment: {payload.get('summary','changes applied')}."
    if et == E.VAR_REVIEW: return f"{pfx} — VAR review: checking for {payload.get('reason','a possible infringement')}."
    if et == E.VAR_OVERTURN: return f"{pfx} — VAR decision: {payload.get('outcome','decision changed')} {payload.get('reason','after review')}."
    return f"{pfx} — Play continues."
