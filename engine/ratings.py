from __future__ import annotations

from engine.models import MatchState, Side


def _clamp(r: float) -> float:
    return max(0.0, min(10.0, r))


def _ps(state: MatchState, side: Side, player_id: int):
    return state.player_states.get(state.player_key(side, player_id))


def _add_contrib(ps, text: str):
    if text and text not in ps.contributions:
        ps.contributions.append(text)
    if len(ps.contributions) > 8:
        ps.contributions = ps.contributions[-8:]


def adjust(state: MatchState, side: Side, player_id: int, delta: float, contrib: str | None = None):
    ps = _ps(state, side, player_id)
    if not ps:
        return
    ps.rating_points = max(1.0, min(10.5, ps.rating_points + delta))
    ps.rating = _clamp(round(ps.rating_points, 1))
    if contrib:
        _add_contrib(ps, contrib)


def record_goal(state: MatchState, side: Side, scorer: int, assist: int | None = None):
    adjust(state, side, scorer, 0.9, "Goal")
    if assist and assist != scorer:
        adjust(state, side, assist, 0.5, "Assist")


def record_shot(state: MatchState, side: Side, player_id: int, on_target: bool, big_chance: bool = False):
    delta = 0.05 if on_target else -0.02
    if big_chance and not on_target:
        delta -= 0.12
    elif big_chance and on_target:
        delta += 0.08
    adjust(state, side, player_id, delta, "Shot on target" if on_target else None)


def record_key_pass(state: MatchState, side: Side, player_id: int):
    adjust(state, side, player_id, 0.18, "Key pass")


def record_save(state: MatchState, side: Side, gk_id: int, big: bool = False):
    adjust(state, side, gk_id, 0.25 if big else 0.12, "Important save" if big else "Save")


def record_tackle(state: MatchState, side: Side, player_id: int):
    adjust(state, side, player_id, 0.08, "Tackle")


def record_interception(state: MatchState, side: Side, player_id: int):
    adjust(state, side, player_id, 0.07, "Interception")


def record_def_error(state: MatchState, side: Side, player_id: int, severe: bool = False):
    adjust(state, side, player_id, -0.35 if severe else -0.18, "Defensive error")


def record_missed_big(state: MatchState, side: Side, player_id: int):
    adjust(state, side, player_id, -0.25, "Missed chance")


def record_card(state: MatchState, side: Side, player_id: int, card: str):
    if card == "yellow":
        adjust(state, side, player_id, -0.20, "Booked")
    elif card == "red":
        adjust(state, side, player_id, -0.80, "Sent off")


def record_pass_quality(state: MatchState, side: Side, player_id: int, good: bool):
    adjust(state, side, player_id, 0.01 if good else -0.01, None)


def finalize_all(state: MatchState):
    for ps in state.player_states.values():
        if ps.minutes_played > 0 or ps.on_pitch or ps.substituted_out_minute is not None:
            ps.rating = _clamp(round(ps.rating_points, 1))
        else:
            ps.rating = 0.0
            ps.stamina = min(ps.stamina, 100.0)
