from __future__ import annotations

import random
from typing import Any

from engine.formations import SUPPORTED_FORMATIONS
from engine.models import MatchState, Side, default_tactics
from services.lineup_service import auto_build_lineup

PREFERRED = ["4-3-3", "4-2-3-1", "4-4-2", "4-1-4-1", "3-5-2", "3-4-3", "4-3-1-2", "4-2-2-2"]


def choose_ai_formation(rng: random.Random) -> str:
    return rng.choice(PREFERRED)


def generate_ai_lineup(team_api_id: int, team_name: str, squad: list[dict[str, Any]], rng: random.Random, formation: str | None = None):
    formation = formation or choose_ai_formation(rng)
    for f in [formation] + [x for x in PREFERRED if x != formation] + SUPPORTED_FORMATIONS:
        try:
            return auto_build_lineup(team_api_id, team_name, squad, formation=f)
        except Exception:
            continue
    raise RuntimeError(f"Could not build lineup for {team_name}")


def generate_ai_tactics(rng: random.Random, style: str | None = None) -> dict[str, Any]:
    t = default_tactics()
    style = style or rng.choice(["balanced", "possession", "press", "counter", "defensive", "attacking"])
    if style == "possession":
        t["attacking"].update({"mentality": "balanced", "build_up": "short", "passing_directness": "short", "possession_style": "patient"})
        t["defensive"].update({"pressing_intensity": "medium", "compactness": "high"})
    elif style == "press":
        t["attacking"].update({"mentality": "positive", "attacking_transitions": "fast"})
        t["defensive"].update({"defensive_line": "high", "pressing_intensity": "high", "counterpressing": True})
    elif style == "counter":
        t["attacking"].update({"mentality": "balanced", "build_up": "direct", "counterattacking": True, "attacking_transitions": "fast"})
        t["defensive"].update({"defensive_line": "deep", "pressing_intensity": "low", "compactness": "high", "defensive_block": "low"})
    elif style == "defensive":
        t["attacking"].update({"mentality": "cautious", "counterattacking": True})
        t["defensive"].update({"defensive_line": "deep", "compactness": "high", "defensive_block": "low"})
    elif style == "attacking":
        t["attacking"].update({"mentality": "attacking", "attacking_width": "wide", "attacking_transitions": "fast"})
        t["defensive"].update({"defensive_line": "high", "pressing_intensity": "medium"})
    return t


def _player_overall(state: MatchState, side: Side, ps) -> float:
    p = state.players_index.get(state.player_key(side, ps.player_api_id))
    if not p:
        return 60.0
    try:
        return float(getattr(p.attributes, "overall", 60) or 60)
    except Exception:
        return 60.0


def _player_pace(state: MatchState, side: Side, ps) -> float:
    p = state.players_index.get(state.player_key(side, ps.player_api_id))
    if not p:
        return 60.0
    try:
        return float(getattr(p.attributes, "pace", 60) or 60)
    except Exception:
        return 60.0


def decide_ai_break_actions(state: MatchState, side: Side, rng: random.Random | None = None) -> dict[str, Any]:
    """Context-aware AI decisions at breaks: reacts to scoreline, minute, stamina, cards, injuries, red cards, tactics."""
    rng = rng or random.Random((state.rng_seed or 0) + int(state.minute or 0) * 17 + (101 if side == "home" else 203))
    team = state.team(side)
    opp = state.team(state.opp_side(side))
    diff = int(team.score) - int(opp.score)
    minute = int(state.minute or 0)
    actions: dict[str, Any] = {"substitutions": [], "tactics_updates": {}, "notes": []}

    on_pitch = [ps for ps in state.player_states.values() if ps.team_side == side and ps.on_pitch and not ps.red_card]
    bench = [ps for ps in state.player_states.values() if ps.team_side == side and not ps.on_pitch and not ps.red_card and not ps.injured and ps.substituted_out_minute is None]
    opp_on = [ps for ps in state.player_states.values() if ps.team_side == state.opp_side(side) and ps.on_pitch and not ps.red_card]

    my_on = len(on_pitch)
    opp_on_n = len(opp_on)
    men_diff = my_on - opp_on_n  # negative means we have fewer players

    # --- Substitution scoring ---
    cands: list[tuple[float, Any]] = []
    for ps in on_pitch:
        s = 0.0
        injured = bool(getattr(ps, "injured", False))
        stamina = float(getattr(ps, "stamina", 100) or 100)
        rating = float(getattr(ps, "rating", 6.5) or 6.5)
        mins_played = int(getattr(ps, "minutes_played", 0) or 0)
        yellows = int(getattr(ps, "yellow_cards", 0) or 0)
        pos = str(getattr(ps, "position", "MID") or "MID").upper()

        if injured:
            s += 8.0
        # stamina/fatigue
        if stamina < 55:
            s += (55 - stamina) * 0.08
        if stamina < 38:
            s += 1.6
        if stamina < 28:
            s += 1.2
        # poor performance
        if mins_played >= 20 and rating < 6.0:
            s += (6.0 - rating) * 0.7
        if mins_played >= 35 and rating < 5.6:
            s += 0.6
        # card risk
        if yellows >= 1 and pos != "GK":
            if minute >= 60:
                s += 0.35
            if minute >= 75:
                s += 0.35
            if diff > 0 and minute >= 70:
                s += 0.25
            # if opponent attacking hard, booked defender is risky
            if diff <= 0 and minute >= 70 and pos == "DEF":
                s += 0.15
        # protect GK
        if pos == "GK":
            s *= 0.12
        # if we're down a man, don't sub off defenders lightly unless exhausted/injured
        if men_diff < 0 and pos == "DEF" and not injured and stamina >= 35:
            s *= 0.7
        cands.append((s, ps))
    cands.sort(key=lambda x: x[0], reverse=True)

    # --- Desired number of subs ---
    desired = 0
    high_need = sum(1 for sc, _ in cands[:4] if sc > 1.0)
    med_need = sum(1 for sc, _ in cands[:4] if sc > 0.55)
    forced = sum(1 for sc, ps in cands if bool(getattr(ps, "injured", False)))

    if state.pending_break == "halftime":
        desired = 1 if high_need >= 1 or forced else (1 if med_need >= 2 and rng.random() < 0.35 else 0)
    elif state.pending_break == "break_60":
        if diff < 0:
            desired = 2 if (high_need >= 1 or med_need >= 2) else 1
        elif diff > 0:
            desired = 1 if (high_need >= 1 or med_need >= 2) else (1 if rng.random() < 0.35 else 0)
        else:
            desired = 1 if (high_need >= 1 or med_need >= 1) else (1 if rng.random() < 0.45 else 0)
    elif state.pending_break == "break_75":
        if diff < 0:
            desired = 2 if (high_need >= 1 or med_need >= 2) else 1
            if diff <= -2:
                desired = max(desired, 2)
        elif diff > 0:
            desired = 1 if (high_need >= 1 or forced) else (1 if rng.random() < 0.30 else 0)
        else:
            desired = 1 if (high_need >= 1 or med_need >= 1 or rng.random() < 0.55) else 0
    else:
        # generic break
        desired = 1 if high_need >= 1 else 0

    # red card / men disadvantage: be careful with subs count and shape
    if men_diff < 0:
        desired = min(desired, 2)
    if men_diff > 0 and diff <= 0:
        desired = max(desired, 1)

    desired = max(desired, forced)
    desired = min(desired, max(0, 5 - int(getattr(team, "substitutions_made", 0) or 0)), len(bench), 3)

    used_off: set[int] = set()
    used_on: set[int] = set()

    def _bench_fit(off_ps, b_ps) -> float:
        fit = 0.0
        off_pos = str(getattr(off_ps, "position", "MID") or "MID").upper()
        b_pos = str(getattr(b_ps, "position", "MID") or "MID").upper()
        # position fit
        if b_pos == off_pos:
            fit += 3.0
        elif off_pos == "GK":
            fit += 3.0 if b_pos == "GK" else -5.0
        elif b_pos == "GK":
            fit -= 5.0
        elif {off_pos, b_pos} <= {"DEF", "MID"} or {off_pos, b_pos} <= {"MID", "FWD"}:
            fit += 1.4
        else:
            fit += 0.3
        # freshness and quality
        fit += float(getattr(b_ps, "stamina", 100) or 100) * 0.02
        fit += _player_overall(state, side, b_ps) * 0.03
        # game state intent
        pace = _player_pace(state, side, b_ps)
        if diff < 0 and minute >= 60:
            if b_pos in {"FWD", "MID"}:
                fit += 1.2
            if pace >= 75:
                fit += 0.4
            if diff <= -2 and b_pos == "FWD":
                fit += 0.6
        if diff > 0 and minute >= 70:
            if b_pos in {"DEF", "MID"}:
                fit += 0.9
            if b_pos == "FWD" and pace >= 78:
                fit += 0.2  # outlet on counter
        if diff == 0 and minute >= 75:
            if b_pos in {"FWD", "MID"}:
                fit += 0.6
        # men disadvantage - prioritize defensive shape / ball retention
        if men_diff < 0:
            if b_pos in {"DEF", "MID"}:
                fit += 0.7
            if b_pos == "FWD":
                fit -= 0.4
        # men advantage - attack
        if men_diff > 0 and diff <= 0:
            if b_pos in {"FWD", "MID"}:
                fit += 0.6
        return fit

    for s, off in cands:
        if len(actions["substitutions"]) >= desired:
            break
        off_id = int(getattr(off, "player_api_id", 0) or 0)
        if off_id in used_off or s < 0.30:
            continue
        opts: list[tuple[float, Any]] = []
        for b in bench:
            b_id = int(getattr(b, "player_api_id", 0) or 0)
            if b_id in used_on:
                continue
            fit = _bench_fit(off, b)
            opts.append((fit, b))
        opts.sort(key=lambda x: x[0], reverse=True)
        if not opts:
            continue
        on = opts[0][1]
        on_id = int(getattr(on, "player_api_id", 0) or 0)
        if on_id <= 0 or off_id <= 0 or on_id == off_id:
            continue
        reason = "tactical adjustment"
        if bool(getattr(off, "injured", False)):
            reason = "injury replacement"
        elif float(getattr(off, "stamina", 100) or 100) < 45:
            reason = "fresh legs"
        elif float(getattr(off, "rating", 6.5) or 6.5) < 5.8 and int(getattr(off, "minutes_played", 0) or 0) >= 20:
            reason = "change in performance"
        elif int(getattr(off, "yellow_cards", 0) or 0) >= 1 and minute >= 70:
            reason = "card risk management"
        elif diff < 0 and minute >= 70:
            reason = "attacking change"
        elif diff > 0 and minute >= 75:
            reason = "game management"

        actions["substitutions"].append({
            "off_player_id": off_id,
            "off_name": str(getattr(off, "name", off_id) or off_id),
            "on_player_id": on_id,
            "on_name": str(getattr(on, "name", on_id) or on_id),
            "reason": reason,
        })
        used_off.add(off_id)
        used_on.add(on_id)

    # --- Tactical updates ---
    tu: dict[str, dict[str, Any]] = {"attacking": {}, "defensive": {}}

    # base by game state
    if diff < 0:
        if minute >= 80:
            tu["attacking"].update({"mentality": "very_attacking", "attacking_transitions": "fast", "attacking_width": "wide", "counterattacking": False, "possession_style": "fast"})
            tu["defensive"].update({"defensive_line": "high", "pressing_intensity": "high", "counterpressing": True, "compactness": "balanced"})
            actions["notes"].append("all-out push for an equaliser")
        elif minute >= 70:
            tu["attacking"].update({"mentality": "attacking", "attacking_transitions": "fast", "attacking_width": "wide"})
            tu["defensive"].update({"defensive_line": "high", "pressing_intensity": "high", "counterpressing": True})
            actions["notes"].append("chasing the game")
        elif minute >= 55:
            tu["attacking"].update({"mentality": "positive", "attacking_transitions": "balanced"})
            tu["defensive"].update({"pressing_intensity": "medium", "defensive_line": "standard"})
            actions["notes"].append("pushing for an equaliser")
        else:
            tu["attacking"].update({"mentality": "balanced"})
    elif diff > 0:
        if minute >= 80:
            tu["attacking"].update({"mentality": "cautious", "counterattacking": True, "attacking_transitions": "slow", "possession_style": "patient"})
            tu["defensive"].update({"defensive_line": "deep", "compactness": "high", "defensive_block": "low", "pressing_intensity": "low"})
            actions["notes"].append("protecting the lead late")
        elif minute >= 70:
            tu["attacking"].update({"mentality": "cautious", "counterattacking": True, "attacking_transitions": "fast"})
            tu["defensive"].update({"defensive_line": "standard", "compactness": "high", "defensive_block": "mid", "pressing_intensity": "medium"})
            actions["notes"].append("protecting the lead")
        elif minute >= 55:
            tu["attacking"].update({"mentality": "balanced", "counterattacking": True})
            tu["defensive"].update({"compactness": "high"})
            actions["notes"].append("managing the advantage")
    else:
        if minute >= 78:
            tu["attacking"].update({"mentality": "attacking", "attacking_transitions": "fast"})
            tu["defensive"].update({"pressing_intensity": "high", "defensive_line": "high"})
            actions["notes"].append("seeking a late winner")
        elif minute >= 60:
            tu["attacking"].update({"mentality": "positive"})
            tu["defensive"].update({"pressing_intensity": "medium"})
            actions["notes"].append("looking for control and a winner")
        else:
            tu["attacking"].update({"mentality": "balanced"})

    # numerical disadvantage/advantage overrides
    if men_diff < 0:
        tu["defensive"].update({"compactness": "high", "defensive_block": "low", "pressing_intensity": "low", "defensive_line": "deep"})
        tu["attacking"].update({"mentality": "cautious" if diff >= 0 else "balanced", "counterattacking": True, "attacking_transitions": "fast"})
        actions["notes"].append("adapting to red card/numerical disadvantage")
    elif men_diff > 0:
        if diff <= 0:
            tu["attacking"].update({"mentality": "attacking" if minute >= 65 else "positive", "attacking_width": "wide"})
            tu["defensive"].update({"pressing_intensity": "high", "defensive_line": "high"})
            actions["notes"].append("using extra player advantage")
        else:
            tu["attacking"].update({"counterattacking": True, "possession_style": "patient"})
            tu["defensive"].update({"compactness": "high"})
            actions["notes"].append("controlling game with extra player")

    # fatigue-based adjustment
    avg_stam = 0.0
    if on_pitch:
        avg_stam = sum(float(getattr(ps, "stamina", 100) or 100) for ps in on_pitch) / len(on_pitch)
    if avg_stam < 55 and diff >= 0:
        tu["defensive"].setdefault("compactness", "high")
        tu["defensive"].setdefault("pressing_intensity", "low")
        tu["attacking"].setdefault("possession_style", "patient")
        actions["notes"].append("managing fatigue")
    if avg_stam < 50 and diff < 0 and minute < 80:
        # still need to push, but be smarter
        tu["attacking"].setdefault("attacking_transitions", "fast")

    # clean empty sections
    tu = {k: v for k, v in tu.items() if v}
    actions["tactics_updates"] = tu
    # dedupe notes
    seen_n = set()
    notes = []
    for n in actions["notes"]:
        if n not in seen_n:
            seen_n.add(n)
            notes.append(n)
    actions["notes"] = notes
    return actions
