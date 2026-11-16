from __future__ import annotations

from engine.models import MatchState

BASE_DRAIN = {"GK": 0.18, "DEF": 0.42, "MID": 0.52, "FWD": 0.48}


def halftime_recovery(stamina: float) -> float:
    return min(100.0, stamina + 8.0 + (100.0 - stamina) * 0.08)


def apply_minute_stamina(state: MatchState, home_press_mult: float = 1.0, away_press_mult: float = 1.0) -> None:
    for ps in state.player_states.values():
        if not ps.on_pitch or ps.red_card:
            continue
        press = home_press_mult if ps.team_side == "home" else away_press_mult
        drain = BASE_DRAIN.get((ps.position or "MID").upper(), 0.48) * max(0.85, min(1.35, press))
        if state.possession_side == ps.team_side:
            drain *= 1.02
        # late fatigue accelerates
        if state.minute > 70:
            drain *= 1.05
        ps.stamina = max(0.0, ps.stamina - drain)
        ps.minutes_played += 1
        if ps.stamina < 30:
            ps.rating_points = max(3.0, ps.rating_points - 0.002)


def apply_halftime_recovery(state: MatchState) -> None:
    for ps in state.player_states.values():
        if ps.on_pitch and not ps.red_card:
            ps.stamina = halftime_recovery(ps.stamina)
        elif not ps.on_pitch and not ps.red_card and not ps.injured and ps.substituted_out_minute is None:
            ps.stamina = min(100.0, ps.stamina + 1.5)
