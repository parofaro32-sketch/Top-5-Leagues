from __future__ import annotations

from typing import Any
from engine.models import MatchState, Side
from engine.ratings import finalize_all


def _fmt(m, ex=0):
    return f"{m}+{ex}" if ex else str(m)


def team_stats(state: MatchState, side: Side) -> dict[str, Any]:
    t = state.team(side)
    return {"team": t.team.name, "score": t.score, "possession": round(t.possession_share,1), "shots": t.shots, "shots_on_target": t.shots_on_target,
            "xg": round(t.xg,2), "corners": t.corners, "fouls": t.fouls, "yellow_cards": t.yellow_cards, "red_cards": t.red_cards,
            "offsides": t.offsides, "saves": t.saves, "pass_accuracy": round(t.pass_accuracy,1), "passes_completed": t.passes_completed, "passes_attempted": t.passes_attempted}


def match_stats(state: MatchState) -> dict[str, Any]:
    return {"scoreline": state.scoreline(), "minute": state.minute, "phase": state.phase, "home": team_stats(state,"home"), "away": team_stats(state,"away")}


def participating_players(state: MatchState, side: Side | None = None) -> list[dict[str, Any]]:
    out=[]
    for ps in state.player_states.values():
        if side and ps.team_side!=side: continue
        if ps.minutes_played<=0 and not ps.on_pitch and ps.substituted_in_minute is None and not ps.is_starter: continue
        if not (ps.on_pitch or ps.minutes_played>0 or ps.substituted_out_minute is not None or ps.substituted_in_minute is not None): continue
        out.append({"player_api_id":ps.player_api_id,"name":ps.name,"team_side":ps.team_side,"position":ps.position,"slot":ps.slot,"on_pitch":ps.on_pitch,
                    "rating":round(ps.rating,1),"stamina":round(max(0,min(100,ps.stamina)),1),"goals":ps.goals,"assists":ps.assists,
                    "shots":ps.shots,"shots_on_target":ps.shots_on_target,"key_passes":ps.key_passes,"tackles":ps.tackles,"interceptions":ps.interceptions,
                    "saves":ps.saves,"yellow_cards":ps.yellow_cards,"red_card":ps.red_card,"injured":ps.injured,"injury_severity":ps.injury_severity,
                    "injury_minute":ps.injury_minute,"continued":ps.injury_continued,"substituted":ps.injury_substituted,
                    "minutes_played":ps.minutes_played,"substituted_in_minute":ps.substituted_in_minute,"substituted_out_minute":ps.substituted_out_minute,
                    "contributions":list(ps.contributions)})
    out.sort(key=lambda x:(0 if x["team_side"]=="home" else 1, 0 if x["on_pitch"] else 1, -x["minutes_played"], x["name"]))
    return out


def goalscorers(state: MatchState):
    out=[]
    for ev in state.events:
        if ev.event_type=="goal":
            name = (ev.payload or {}).get("player_name")
            if not name and ev.player_api_id and ev.team_side:
                ps=state.player_states.get(state.player_key(ev.team_side,ev.player_api_id)); name=ps.name if ps else "Unknown"
            out.append({"minute":ev.minute,"extra_minute":ev.extra_minute,"team":state.team(ev.team_side).team.name if ev.team_side else None,"scorer":name,"assist":(ev.payload or {}).get("assist_name")})
    return out


def discipline(state: MatchState):
    out=[]
    for ev in state.events:
        if ev.event_type in {"yellow_card","second_yellow","red_card"}:
            name=(ev.payload or {}).get("player_name")
            if not name and ev.player_api_id and ev.team_side:
                ps=state.player_states.get(state.player_key(ev.team_side,ev.player_api_id)); name=ps.name if ps else "Unknown"
            out.append({"minute":ev.minute,"team":state.team(ev.team_side).team.name if ev.team_side else None,"player":name,"type":ev.event_type,"reason":(ev.payload or {}).get("reason")})
    return out


def injuries(state: MatchState):
    out=[]; seen=set()
    for ev in state.events:
        if ev.event_type=="injury":
            p=ev.payload or {}; name=p.get("player_name")
            if not name and ev.player_api_id and ev.team_side:
                ps=state.player_states.get(state.player_key(ev.team_side,ev.player_api_id)); name=ps.name if ps else "Unknown"
            key=(ev.team_side,ev.player_api_id,ev.minute)
            if key in seen: continue
            seen.add(key)
            out.append({"minute":ev.minute,"team":state.team(ev.team_side).team.name if ev.team_side else None,"player":name,"severity":p.get("severity"),"continued":bool(p.get("continued")),"substituted":bool(p.get("substituted"))})
    return out


def substitutions(state: MatchState):
    out=[]
    for side in ("home","away"):
        t=state.team(side)
        for s in t.substitution_events:
            out.append({"minute":s.get("minute"),"team":t.team.name,"off_name":s.get("off_name"),"on_name":s.get("on_name"),"reason":s.get("reason")})
    out.sort(key=lambda x:x.get("minute") or 0)
    return out


def man_of_the_match(state: MatchState):
    finalize_all(state); best=None
    for ps in state.player_states.values():
        if ps.minutes_played<=0 and not ps.on_pitch: continue
        sc=ps.rating + ps.goals*0.4 + ps.assists*0.25 + ps.saves*0.06 + ps.key_passes*0.05 - (1.5 if ps.red_card else 0)
        cand={"name":ps.name,"team":state.team(ps.team_side).team.name,"rating":round(ps.rating,1),"goals":ps.goals,"assists":ps.assists,"_s":sc}
        if best is None or sc>best["_s"]: best=cand
    if best: best.pop("_s",None)
    return best


def tactical_analysis(state: MatchState) -> str:
    h,a=state.home,state.away
    lines=[]
    def perf(side):
        t=state.team(side); o=state.team(state.opp_side(side)); parts=[]
        if t.xg>o.xg+0.35: parts.append("created the clearer chances")
        elif o.xg>t.xg+0.35: parts.append("found it harder to generate high-quality chances")
        else: parts.append("were closely matched in chance quality")
        if t.possession_share>=55: parts.append("controlled more of the ball")
        elif t.possession_share<=45: parts.append("had longer spells without possession")
        return f"{t.team.name} {', '.join(parts)}."
    lines.append(perf("home")); lines.append(perf("away"))
    motm=man_of_the_match(state)
    if motm: lines.append(f"Key performer: {motm['name']} ({motm['team']}) delivered a {motm['rating']} rating in influential moments.")
    changes=[]
    for side in ("home","away"):
        t=state.team(side)
        for ch in t.tactical_changes: changes.append((ch.get("minute",0), t.team.name, ch.get("summary") or "adjustment"))
        for s in t.substitution_events[:3]: changes.append((s.get("minute",0), t.team.name, f"introduced {s.get('on_name')}"))
    changes.sort()
    if changes:
        lines.append("Observable adjustments: " + "; ".join([f"{m}' {tm} {sm}" for m,tm,sm in changes[:5]]) + ". These shifts coincided with changes in pressure, control, or attacking threat.")
    if h.score>a.score: lines.append(f"Why the result occurred: {h.team.name} managed the decisive moments more effectively, while {a.team.name} could not fully overturn key phases.")
    elif a.score>h.score: lines.append(f"Why the result occurred: {a.team.name} managed the decisive moments more effectively, while {h.team.name} could not fully overturn key phases.")
    else: lines.append("Why the result occurred: neither side established a decisive edge in the key moments and the match remained balanced.")
    return "\n".join(f"- {l}" for l in lines)


def full_time_report(state: MatchState) -> dict[str, Any]:
    finalize_all(state)
    return {"final_score":{"home_team":state.home.team.name,"away_team":state.away.team.name,"home_score":state.home.score,"away_score":state.away.score,"scoreline":state.scoreline()},
            "goalscorers":goalscorers(state), "statistics":match_stats(state), "discipline":discipline(state), "injuries":injuries(state),
            "substitutions":substitutions(state), "player_ratings":{"home":participating_players(state,"home"),"away":participating_players(state,"away")},
            "man_of_the_match":man_of_the_match(state), "tactical_analysis":tactical_analysis(state)}


def break_status(state: MatchState) -> dict[str, Any]:
    finalize_all(state)
    return {"score":{"home":state.home.score,"away":state.away.score,"scoreline":state.scoreline()}, "statistics":match_stats(state),
            "players":{"home":participating_players(state,"home"),"away":participating_players(state,"away")}, "minute":state.minute, "phase":state.phase}
