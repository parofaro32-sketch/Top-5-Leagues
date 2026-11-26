from __future__ import annotations

import random
from typing import Any
from engine.formations import SUPPORTED_FORMATIONS
from engine.models import MatchState, Side, default_tactics
from services.lineup_service import auto_build_lineup

PREFERRED = ["4-3-3","4-2-3-1","4-4-2","4-1-4-1","3-5-2","3-4-3","4-3-1-2","4-2-2-2"]


def choose_ai_formation(rng: random.Random) -> str:
    return rng.choice(PREFERRED)


def generate_ai_lineup(team_api_id: int, team_name: str, squad: list[dict[str, Any]], rng: random.Random, formation: str | None = None):
    formation = formation or choose_ai_formation(rng)
    for f in [formation] + [x for x in PREFERRED if x!=formation] + SUPPORTED_FORMATIONS:
        try:
            return auto_build_lineup(team_api_id, team_name, squad, formation=f)
        except Exception:
            continue
    raise RuntimeError(f"Could not build lineup for {team_name}")


def generate_ai_tactics(rng: random.Random, style: str | None = None) -> dict[str, Any]:
    t = default_tactics()
    style = style or rng.choice(["balanced","possession","press","counter","defensive","attacking"])
    if style=="possession":
        t["attacking"].update({"mentality":"balanced","build_up":"short","passing_directness":"short","possession_style":"patient"})
        t["defensive"].update({"pressing_intensity":"medium","compactness":"high"})
    elif style=="press":
        t["attacking"].update({"mentality":"positive","attacking_transitions":"fast"})
        t["defensive"].update({"defensive_line":"high","pressing_intensity":"high","counterpressing":True})
    elif style=="counter":
        t["attacking"].update({"mentality":"balanced","build_up":"direct","counterattacking":True,"attacking_transitions":"fast"})
        t["defensive"].update({"defensive_line":"deep","pressing_intensity":"low","compactness":"high","defensive_block":"low"})
    elif style=="defensive":
        t["attacking"].update({"mentality":"cautious","counterattacking":True})
        t["defensive"].update({"defensive_line":"deep","compactness":"high","defensive_block":"low"})
    elif style=="attacking":
        t["attacking"].update({"mentality":"attacking","attacking_width":"wide","attacking_transitions":"fast"})
        t["defensive"].update({"defensive_line":"high","pressing_intensity":"medium"})
    return t


def decide_ai_break_actions(state: MatchState, side: Side, rng: random.Random | None = None) -> dict[str, Any]:
    rng = rng or random.Random((state.rng_seed or 0) + state.minute + (101 if side == "home" else 203))
    team = state.team(side)
    opp = state.team(state.opp_side(side))
    diff = team.score - opp.score
    minute = state.minute
    actions = {"substitutions": [], "tactics_updates": {}, "notes": []}
    on_pitch = [ps for ps in state.player_states.values() if ps.team_side == side and ps.on_pitch and not ps.red_card]
    bench = [ps for ps in state.player_states.values() if ps.team_side == side and not ps.on_pitch and not ps.red_card and not ps.injured and ps.substituted_out_minute is None]

    # Substitutions: freshness, performance, tactical balance, game state (fair logic for both sides)
    cands = []
    for ps in on_pitch:
        s = 0.0
        if getattr(ps, "injured", False):
            s += 6.0
        if ps.stamina < 48:
            s += (48 - ps.stamina) * 0.07
        if ps.stamina < 32:
            s += 1.4
        if ps.stamina < 25:
            s += 1.0
        if ps.rating < 5.9 and ps.minutes_played >= 20:
            s += (5.9 - ps.rating) * 0.55
        if ps.yellow_cards >= 1 and minute >= 65 and ps.position != "GK":
            s += 0.45
        if ps.yellow_cards >= 1 and diff > 0 and minute >= 75 and ps.position != "GK":
            s += 0.25
        if ps.position == "GK":
            s *= 0.15
        cands.append((s, ps))
    cands.sort(key=lambda x: x[0], reverse=True)

    desired = 0
    if state.pending_break == "halftime":
        desired = 1 if any(s > 0.9 for s, _ in cands[:3]) else (1 if rng.random() < 0.25 else 0)
    elif state.pending_break == "break_60":
        desired = 1 if any(s > 0.8 for s, _ in cands[:3]) or diff != 0 else (1 if rng.random() < 0.45 else 0)
    elif state.pending_break == "break_75":
        if diff < 0:
            desired = 2 if any(s > 0.6 for s, _ in cands[:3]) else 1
        elif diff == 0:
            desired = 1 if any(s > 0.8 for s, _ in cands[:3]) or rng.random() < 0.6 else 0
        else:
            desired = 1 if any(s > 1.0 for s, _ in cands[:3]) else (1 if rng.random() < 0.4 else 0)

    forced = sum(1 for sc, ps in cands if getattr(ps, "injured", False))
    desired = max(desired, forced)
    desired = min(desired, max(0, 5 - team.substitutions_made), len(bench), 3)
    used_off = set()
    used_on = set()
    for s, off in cands:
        if len(actions["substitutions"]) >= desired:
            break
        if off.player_api_id in used_off or s < 0.35:
            continue
        opts = []
        for b in bench:
            if b.player_api_id in used_on:
                continue
            fit = 2.2 if b.position == off.position else (1.0 if b.position in {"MID", "DEF", "FWD"} else 0.0)
            fit += b.stamina * 0.018
            # Tactical intent by game state (symmetric rules for both teams)
            if diff < 0 and minute >= 60 and b.position in {"FWD", "MID"}:
                fit += 0.7
            if diff > 0 and minute >= 70 and b.position in {"DEF", "MID"}:
                fit += 0.45
            if diff == 0 and minute >= 75 and b.position in {"FWD", "MID"}:
                fit += 0.35
            opts.append((fit, b))
        opts.sort(key=lambda x: x[0], reverse=True)
        if not opts:
            continue
        on = opts[0][1]
        actions["substitutions"].append({
            "off_player_id": off.player_api_id, "off_name": off.name,
            "on_player_id": on.player_api_id, "on_name": on.name,
            "reason": "fresh legs and tactical balance" if s >= 0.8 else "tactical adjustment",
        })
        used_off.add(off.player_api_id)
        used_on.add(on.player_api_id)

    # Tactical updates based primarily on match state and team needs (balanced/fair)
    tu = {"attacking": {}, "defensive": {}}
    if diff < 0:
        if minute >= 75:
            tu["attacking"].update({"mentality": "attacking", "attacking_transitions": "fast", "attacking_width": "wide", "counterattacking": False})
            tu["defensive"].update({"defensive_line": "high", "pressing_intensity": "high", "counterpressing": True})
            actions["notes"].append("chasing the game")
        elif minute >= 60:
            tu["attacking"].update({"mentality": "positive", "attacking_transitions": "balanced"})
            tu["defensive"].update({"pressing_intensity": "medium"})
            actions["notes"].append("pushing for an equaliser")
        else:
            tu["attacking"].update({"mentality": "balanced"})
    elif diff > 0:
        if minute >= 75:
            tu["attacking"].update({"mentality": "cautious", "counterattacking": True, "attacking_transitions": "fast"})
            tu["defensive"].update({"defensive_line": "standard", "compactness": "high", "defensive_block": "mid", "pressing_intensity": "medium"})
            actions["notes"].append("protecting the lead")
        elif minute >= 60:
            tu["attacking"].update({"mentality": "balanced", "counterattacking": True})
            tu["defensive"].update({"compactness": "high"})
            actions["notes"].append("managing the advantage")
    else:
        if minute >= 75:
            tu["attacking"].update({"mentality": "positive", "attacking_transitions": "fast"})
            tu["defensive"].update({"pressing_intensity": "medium"})
            actions["notes"].append("seeking a winner")
        elif minute >= 60:
            tu["attacking"].update({"mentality": "balanced"})
            actions["notes"].append("keeping control")

    my_on = len(on_pitch)
    opp_on = len([ps for ps in state.player_states.values() if ps.team_side == state.opp_side(side) and ps.on_pitch and not ps.red_card])
    if my_on < opp_on:
        tu["defensive"].update({"compactness": "high", "defensive_block": "low", "pressing_intensity": "low"})
        tu["attacking"].update({"mentality": "cautious", "counterattacking": True})
        actions["notes"].append("adapting to numerical disadvantage")
    elif my_on > opp_on and diff <= 0:
        tu["attacking"].update({"mentality": "positive", "attacking_width": "wide"})
        actions["notes"].append("using extra player advantage")

    tu = {k: v for k, v in tu.items() if v}
    actions["tactics_updates"] = tu
    return actions
