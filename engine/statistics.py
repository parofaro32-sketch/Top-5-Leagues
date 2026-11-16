from __future__ import annotations

from engine.models import MatchState, Side


def _t(state: MatchState, side: Side):
    return state.team(side)


def _p(state: MatchState, side: Side, pid: int):
    return state.player_states.get(state.player_key(side, pid))


def passes(state: MatchState, side: Side, attempted: int, completed: int):
    t = _t(state, side)
    t.passes_attempted += max(0, attempted)
    t.passes_completed += max(0, min(completed, attempted))


def shot(state: MatchState, side: Side, pid: int, on_target: bool, xg: float = 0.0):
    t = _t(state, side)
    t.shots += 1
    t.xg += max(0.0, xg)
    if on_target:
        t.shots_on_target += 1
    p = _p(state, side, pid)
    if p:
        p.shots += 1
        if on_target:
            p.shots_on_target += 1


def goal(state: MatchState, side: Side, scorer: int, assist: int | None = None):
    _t(state, side).score += 1
    sp = _p(state, side, scorer)
    if sp:
        sp.goals += 1
    if assist is not None:
        ap = _p(state, side, assist)
        if ap:
            ap.assists += 1


def corner(state: MatchState, side: Side):
    _t(state, side).corners += 1


def foul(state: MatchState, side: Side, fouler: int | None = None, fouled_side: Side | None = None, fouled: int | None = None):
    _t(state, side).fouls += 1
    if fouler is not None:
        p = _p(state, side, fouler)
        if p:
            p.fouls_committed += 1
    if fouled_side and fouled is not None:
        p = _p(state, fouled_side, fouled)
        if p:
            p.fouls_won += 1


def card(state: MatchState, side: Side, pid: int, yellow=False, red=False):
    t = _t(state, side)
    p = _p(state, side, pid)
    if yellow:
        t.yellow_cards += 1
        if p:
            p.yellow_cards += 1
    if red:
        t.red_cards += 1
        if p:
            p.red_card = True


def offside(state: MatchState, side: Side):
    _t(state, side).offsides += 1


def save(state: MatchState, side: Side, gk_id: int):
    _t(state, side).saves += 1
    p = _p(state, side, gk_id)
    if p:
        p.saves += 1


def key_pass(state: MatchState, side: Side, pid: int):
    p = _p(state, side, pid)
    if p:
        p.key_passes += 1


def tackle(state: MatchState, side: Side, pid: int):
    p = _p(state, side, pid)
    if p:
        p.tackles += 1


def interception(state: MatchState, side: Side, pid: int):
    p = _p(state, side, pid)
    if p:
        p.interceptions += 1
