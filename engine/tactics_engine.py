from __future__ import annotations

from typing import Any
from engine.models import MatchState, Side


class Profile:
    __slots__ = ("attack","defense","midfield","press","transition_attack","transition_defense","width","compactness","line_height","chance_creation","chance_prevention","stamina_drain_mult","foul_risk","offside_trap_risk","counter_vulnerability","set_piece_attack","set_piece_defense")
    def __init__(self):
        self.attack=self.defense=self.midfield=self.press=1.0
        self.transition_attack=self.transition_defense=1.0
        self.width=self.compactness=self.line_height=1.0
        self.chance_creation=self.chance_prevention=1.0
        self.stamina_drain_mult=self.foul_risk=1.0
        self.offside_trap_risk=0.02
        self.counter_vulnerability=1.0
        self.set_piece_attack=self.set_piece_defense=1.0


def _g(t, s, k, d=None):
    return (t.get(s) or {}).get(k, d)


def build_profile(tactics: dict[str, Any]) -> Profile:
    p = Profile()
    ment = str(_g(tactics,"attacking","mentality","balanced")).lower()
    build = str(_g(tactics,"attacking","build_up","mixed")).lower()
    width = str(_g(tactics,"attacking","attacking_width","balanced")).lower()
    counter = bool(_g(tactics,"attacking","counterattacking",False))
    trans = str(_g(tactics,"attacking","attacking_transitions","balanced")).lower()
    overlaps = bool(_g(tactics,"attacking","overlapping_fullbacks",True))
    line = str(_g(tactics,"defensive","defensive_line","standard")).lower()
    press = str(_g(tactics,"defensive","pressing_intensity","medium")).lower()
    block = str(_g(tactics,"defensive","defensive_block","mid")).lower()
    compact = str(_g(tactics,"defensive","compactness","balanced")).lower()
    counterpress = bool(_g(tactics,"defensive","counterpressing",False))
    trap = bool(_g(tactics,"defensive","offside_trap",False))

    if ment == "cautious": p.attack*=0.9; p.defense*=1.08
    elif ment == "positive": p.attack*=1.06; p.defense*=0.97
    elif ment in {"attacking","very_attacking"}: p.attack*=1.14; p.defense*=0.9; p.counter_vulnerability*=1.15
    if build in {"direct","long"}: p.transition_attack*=1.08; p.midfield*=0.98
    elif build == "short": p.midfield*=1.05
    if width == "wide": p.width*=1.2; p.compactness*=0.95; p.set_piece_attack*=1.03
    elif width == "narrow": p.width*=0.85; p.compactness*=1.05
    if counter: p.transition_attack*=1.12
    if trans == "fast": p.transition_attack*=1.06
    if overlaps: p.attack*=1.02; p.counter_vulnerability*=1.04
    if line == "deep": p.line_height*=0.78; p.chance_prevention*=1.05; p.counter_vulnerability*=0.88
    elif line == "high": p.line_height*=1.18; p.counter_vulnerability*=1.18; p.offside_trap_risk+=0.04
    elif line == "very_high": p.line_height*=1.3; p.counter_vulnerability*=1.3; p.offside_trap_risk+=0.07
    if block == "low": p.chance_prevention*=1.06; p.compactness*=1.06
    if press == "low": p.press*=0.82; p.stamina_drain_mult*=0.92
    elif press == "high": p.press*=1.15; p.stamina_drain_mult*=1.1; p.foul_risk*=1.05
    elif press == "very_high": p.press*=1.28; p.stamina_drain_mult*=1.18; p.foul_risk*=1.1
    if compact == "high": p.compactness*=1.12; p.chance_prevention*=1.03
    elif compact == "low": p.compactness*=0.9
    if counterpress: p.press*=1.05; p.stamina_drain_mult*=1.05
    if trap: p.offside_trap_risk+=0.05; p.counter_vulnerability*=1.04

    for a in ("attack","defense","midfield","press","transition_attack","transition_defense","width","compactness","line_height","chance_creation","chance_prevention","stamina_drain_mult","foul_risk","counter_vulnerability","set_piece_attack","set_piece_defense"):
        setattr(p,a,max(0.6,min(1.6,getattr(p,a))))
    p.offside_trap_risk = max(0.0, min(0.25, p.offside_trap_risk))
    return p


def team_strength(state: MatchState, side: Side) -> dict[str, Any]:
    team = state.team(side)
    prof = build_profile(team.tactics)
    on_pitch = [ps for ps in state.player_states.values() if ps.team_side==side and ps.on_pitch and not ps.red_card]
    if not on_pitch:
        return {"attack":20,"defense":20,"midfield":20,"press":20,"transition_attack":20,"transition_defense":20,"goalkeeping":20,"stamina_drain_mult":prof.stamina_drain_mult,"foul_risk":prof.foul_risk,"counter_vulnerability":prof.counter_vulnerability,"offside_trap_risk":prof.offside_trap_risk,"line_height":prof.line_height,"width":prof.width,"compactness":prof.compactness,"profile":prof}
    atk=[]; dfn=[]; mid=[]; gk=[]; pace=[]; stams=[]
    for ps in on_pitch:
        player = state.players_index.get(state.player_key(side, ps.player_api_id))
        if not player: continue
        stams.append(ps.stamina)
        eff = player.attributes.overall(ps.position) * (0.55 + 0.45*(ps.stamina/100.0))
        if ps.stamina < 50: eff -= (50-ps.stamina)*0.12
        pace.append(player.attributes.pace*(0.6+0.4*ps.stamina/100.0))
        pg = (ps.position or "MID").upper()
        if pg=="GK": gk.append(eff)
        elif pg=="DEF": dfn.append(eff); mid.append(eff*0.4)
        elif pg=="MID": mid.append(eff); atk.append(eff*0.5); dfn.append(eff*0.5)
        else: atk.append(eff); mid.append(eff*0.4)
    avg = lambda xs,d=55: (sum(xs)/len(xs)) if xs else d
    men = len(on_pitch)
    factor = max(0.55, 1.0 - 0.07*max(0,11-men))
    fatigue_pen = max(0.0, (55 - avg(stams,80)) * 0.07)
    attack = max(20, avg(atk)*prof.attack*prof.chance_creation*factor - fatigue_pen)
    defense = max(20, avg(dfn)*prof.defense*prof.chance_prevention*factor - fatigue_pen*0.8)
    midfield = max(20, avg(mid)*prof.midfield*factor - fatigue_pen)
    return {
        "attack": attack, "defense": defense, "midfield": midfield,
        "press": max(20, (midfield*0.5 + avg(pace,60)*0.5)*prof.press),
        "transition_attack": max(20, (attack*0.55 + avg(pace,60)*0.45)*prof.transition_attack),
        "transition_defense": max(20, (defense*0.55 + avg(pace,60)*0.45)*prof.transition_defense),
        "goalkeeping": max(20, avg(gk,55)),
        "stamina_drain_mult": prof.stamina_drain_mult, "foul_risk": prof.foul_risk,
        "counter_vulnerability": prof.counter_vulnerability, "offside_trap_risk": prof.offside_trap_risk,
        "line_height": prof.line_height, "width": prof.width, "compactness": prof.compactness,
        "set_piece_attack": prof.set_piece_attack, "set_piece_defense": prof.set_piece_defense,
        "profile": prof,
    }
