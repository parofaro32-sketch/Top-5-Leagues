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
    rng = rng or random.Random((state.rng_seed or 0) + state.minute + (1 if side=="home" else 2))
    team = state.team(side)
    diff = team.score - state.team(state.opp_side(side)).score
    minute = state.minute
    actions = {"substitutions": [], "tactics_updates": {}, "notes": []}
    on_pitch = [ps for ps in state.player_states.values() if ps.team_side==side and ps.on_pitch and not ps.red_card]
    bench = [ps for ps in state.player_states.values() if ps.team_side==side and not ps.on_pitch and not ps.red_card and not ps.injured and ps.substituted_out_minute is None]
    # subs
    cands=[]
    for ps in on_pitch:
        s=0.0
        if ps.stamina<45: s += (45-ps.stamina)*0.08
        if ps.stamina<30: s += 1.5
        if ps.rating<5.8 and ps.minutes_played>=20: s += (5.8-ps.rating)*0.6
        if ps.yellow_cards>=1 and minute>=65: s += 0.4
        if ps.position=="GK": s *= 0.2
        cands.append((s,ps))
    cands.sort(key=lambda x:x[0], reverse=True)
    desired=0
    if state.pending_break=="halftime": desired = 1 if any(s>1.0 for s,_ in cands[:2]) or rng.random()<0.3 else 0
    elif state.pending_break=="break_60": desired = 1 if any(s>0.8 for s,_ in cands[:2]) or diff!=0 else (1 if rng.random()<0.4 else 0)
    elif state.pending_break=="break_75": desired = 2 if diff<0 else (1 if any(s>1.0 for s,_ in cands[:2]) or rng.random()<0.6 else 0)
    desired = min(desired, max(0,5-team.substitutions_made), len(bench))
    used_off=set(); used_on=set()
    for s,off in cands:
        if len(actions["substitutions"])>=desired: break
        if off.player_api_id in used_off or s<0.4: continue
        opts=[]
        for b in bench:
            if b.player_api_id in used_on: continue
            fit = 2.0 if b.position==off.position else (1.0 if b.position in {"MID","DEF","FWD"} else 0.0)
            fit += b.stamina*0.02
            if diff<0 and minute>=60 and b.position in {"FWD","MID"}: fit += 0.8
            if diff>0 and minute>=70 and b.position in {"DEF","MID"}: fit += 0.4
            opts.append((fit,b))
        opts.sort(key=lambda x:x[0], reverse=True)
        if not opts: continue
        on=opts[0][1]
        actions["substitutions"].append({"off_player_id":off.player_api_id,"off_name":off.name,"on_player_id":on.player_api_id,"on_name":on.name,"reason":"tactical/freshness"})
        used_off.add(off.player_api_id); used_on.add(on.player_api_id)
    # tactics
    tu={"attacking":{},"defensive":{}}
    if diff<0 and minute>=70: tu["attacking"].update({"mentality":"attacking","attacking_transitions":"fast"}); tu["defensive"].update({"defensive_line":"high","pressing_intensity":"high"})
    elif diff<0 and minute>=60: tu["attacking"].update({"mentality":"positive"})
    elif diff>0 and minute>=70: tu["attacking"].update({"mentality":"cautious","counterattacking":True}); tu["defensive"].update({"defensive_line":"deep","compactness":"high"})
    elif diff>0 and minute>=60: tu["defensive"].update({"compactness":"high"})
    my_on=len(on_pitch); opp_on=len([ps for ps in state.player_states.values() if ps.team_side==state.opp_side(side) and ps.on_pitch and not ps.red_card])
    if my_on<opp_on: tu["defensive"].update({"compactness":"high","defensive_block":"low"}); tu["attacking"].update({"mentality":"cautious"})
    elif my_on>opp_on and diff<=0: tu["attacking"].update({"mentality":"positive","attacking_width":"wide"})
    tu={k:v for k,v in tu.items() if v}
    actions["tactics_updates"]=tu
    return actions
