from __future__ import annotations

import copy
import random
from dataclasses import dataclass
from typing import Any

from engine import events as E
from engine.commentary import generate as gen_commentary
from engine.models import Lineup, MatchEvent, MatchState, Player, PlayerMatchState, Side, Team, TeamMatchState, default_tactics
from engine.ratings import adjust as rating_adjust, finalize_all, record_card as rating_card, record_goal as rating_goal, record_interception, record_key_pass as rating_key, record_missed_big, record_save as rating_save, record_shot as rating_shot, record_tackle
from engine.statistics import card as stat_card, corner as stat_corner, foul as stat_foul, goal as stat_goal, interception as stat_interception, key_pass as stat_key, offside as stat_offside, passes as stat_passes, save as stat_save, shot as stat_shot, tackle as stat_tackle
from engine.stamina import apply_halftime_recovery, apply_minute_stamina
from engine.tactics_engine import team_strength


@dataclass
class StepResult:
    events: list[dict[str, Any]]
    break_reached: str | None = None
    finished: bool = False


class MatchEngine:
    def __init__(self, state: MatchState):
        self.state = state
        self.rng = random.Random(state.rng_seed or 1)

    @classmethod
    def initialize(cls, *, match_id: str, guild_id: int | None, channel_id: int | None, mode: str,
                   home_team: Team, away_team: Team, home_lineup: Lineup, away_lineup: Lineup,
                   home_tactics: dict[str, Any] | None, away_tactics: dict[str, Any] | None,
                   home_players: list[Player], away_players: list[Player], seed: int | None = None) -> "MatchEngine":
        rng_seed = seed if seed is not None else random.randint(1, 2**31 - 1)
        st = MatchState(
            id=match_id, guild_id=guild_id, channel_id=channel_id, mode=mode,  # type: ignore
            home=TeamMatchState("home", home_team, home_lineup, copy.deepcopy(home_tactics or default_tactics())),
            away=TeamMatchState("away", away_team, away_lineup, copy.deepcopy(away_tactics or default_tactics())),
            status="setup", minute=0, phase="pre_kickoff", rng_seed=rng_seed,
        )
        eng = cls(st)
        for p in home_players: st.players_index[st.player_key("home", p.api_id)] = p
        for p in away_players: st.players_index[st.player_key("away", p.api_id)] = p
        eng._init_ps("home", home_lineup)
        eng._init_ps("away", away_lineup)
        st.possession_side = "home" if eng.rng.random() < 0.53 else "away"
        st.stoppage_time_1 = eng.rng.randint(1, 3)
        st.stoppage_time_2 = eng.rng.randint(2, 6)
        return eng

    def _init_ps(self, side: Side, lineup: Lineup):
        for e in lineup.starters + lineup.bench:
            is_starter = any(s.player_api_id == e.player_api_id for s in lineup.starters)
            key = self.state.player_key(side, e.player_api_id)
            player = self.state.players_index.get(key)
            stamina = 100.0 if is_starter else 100.0
            if player and is_starter:
                stamina = max(90.0, min(100.0, 95.0 + (player.attributes.stamina - 70) * 0.1))
            self.state.player_states[key] = PlayerMatchState(
                player_api_id=e.player_api_id, name=e.name, team_side=side, position=e.position or (player.position if player else "MID"),
                slot=e.slot, on_pitch=is_starter, is_starter=is_starter,
                is_goalkeeper=(e.slot == "GK" or e.player_api_id == lineup.goalkeeper_player_id or (player and player.pos_group == "GK")),
                is_captain=(e.player_api_id == lineup.captain_player_id), stamina=stamina, rating=6.5, rating_points=6.5,
            )

    # --- emit ---
    def _emit(self, et: str, side: Side | None = None, pid: int | None = None, payload: dict[str, Any] | None = None, commentary: str | None = None) -> dict[str, Any]:
        payload = payload or {}
        ev = {"seq": self.state.next_event_seq, "minute": self.state.minute, "extra_minute": self.state.extra_minute,
              "phase": self.state.phase, "event_type": et, "team_side": side, "player_api_id": pid,
              "payload": payload, "public_payload": payload}
        if commentary is None:
            commentary = gen_commentary(self.state, ev, self.rng)
        ev["commentary"] = commentary
        self.state.events.append(MatchEvent.from_dict(ev))
        self.state.next_event_seq += 1
        return ev

    # --- queries ---
    def on_pitch(self, side: Side, pos: str | None = None):
        out = []
        for ps in self.state.player_states.values():
            if ps.team_side == side and ps.on_pitch and not ps.red_card and not ps.injured:
                if pos and ps.position != pos:
                    continue
                out.append(ps)
        return out

    def bench(self, side: Side):
        return [ps for ps in self.state.player_states.values() if ps.team_side==side and not ps.on_pitch and not ps.red_card and not ps.injured and ps.substituted_out_minute is None]

    def gk(self, side: Side):
        for ps in self.on_pitch(side):
            if ps.is_goalkeeper or ps.position == "GK":
                return ps
        gks = self.on_pitch(side, "GK")
        return gks[0] if gks else None

    def pick(self, side: Side, role: str = "any", attr: str | None = None):
        pool = self.on_pitch(side)
        if role == "gk": return self.gk(side)
        if role == "def": pool = [p for p in pool if p.position=="DEF"] or pool
        elif role == "mid": pool = [p for p in pool if p.position=="MID"] or pool
        elif role == "fwd": pool = [p for p in pool if p.position=="FWD"] or pool
        elif role == "att": pool = [p for p in pool if p.position in {"FWD","MID"}] or pool
        elif role == "out": pool = [p for p in pool if p.position!="GK"] or pool
        pool = [p for p in pool if not p.is_goalkeeper] if role not in {"any","gk"} and any(not p.is_goalkeeper for p in pool) else pool
        if not pool: return None
        if attr:
            ws = []
            for ps in pool:
                pl = self.state.players_index.get(self.state.player_key(side, ps.player_api_id))
                w = float(getattr(pl.attributes, attr, 50)) if pl else 50.0
                w *= 0.5 + 0.5*(ps.stamina/100.0)
                ws.append(max(1.0,w))
            return self.rng.choices(pool, weights=ws, k=1)[0]
        return self.rng.choice(pool)

    # --- control ---
    def is_waiting_for_break_decisions(self) -> bool:
        return self.state.pending_break is not None and self.state.status in {"break","halftime"}

    def mark_break_decision(self, side: Side, done: bool = True):
        if side == "home": self.state.home_break_decision_done = done
        else: self.state.away_break_decision_done = done

    def start(self) -> list[dict[str, Any]]:
        self.state.status = "running"
        self.state.phase = "first_half"
        self.state.minute = 0
        return [self._emit(E.KICKOFF, self.state.possession_side, payload={"detail": f"Kick-off! {self.state.home.team.name} get us underway against {self.state.away.team.name}."})]

    def resume_after_break(self) -> list[dict[str, Any]]:
        br = self.state.pending_break
        if not br: return []
        self.state.pending_break = None
        self.state.home_break_decision_done = False
        self.state.away_break_decision_done = False
        self.state.status = "running"
        evs = []
        if br == "halftime":
            apply_halftime_recovery(self.state)
            self.state.minute = 46; self.state.extra_minute = 0; self.state.phase = "second_half"
            self.state.possession_side = "away" if self.state.possession_side == "home" else "home"
            evs.append(self._emit(E.SECOND_HALF_KICKOFF, self.state.possession_side))
        elif br == "break_60":
            self.state.minute = 61; self.state.extra_minute = 0; self.state.phase = "second_half"
            evs.append(self._emit(E.BREAK_RESUME, payload={"detail":"Play resumes after the 60th-minute tactical break."}))
        elif br == "break_75":
            self.state.minute = 76; self.state.extra_minute = 0; self.state.phase = "second_half"
            evs.append(self._emit(E.BREAK_RESUME, payload={"detail":"Play resumes after the 75th-minute tactical break."}))
        return evs

    # --- changes ---
    def apply_substitution(self, side: Side, off_id: int, on_id: int, reason: str | None = None) -> dict[str, Any]:
        team = self.state.team(side)
        off = self.state.player_states.get(self.state.player_key(side, off_id))
        on = self.state.player_states.get(self.state.player_key(side, on_id))
        if not off or not on: raise ValueError("Substitution failed: player not found")
        if not off.on_pitch: raise ValueError(f"{off.name} is not on the pitch")
        if on.on_pitch: raise ValueError(f"{on.name} is already on the pitch")
        if off.red_card or on.red_card or on.injured: raise ValueError("Substitution failed: unavailable player")
        if team.substitutions_made >= 5: raise ValueError("Substitution limit reached (5)")
        off.on_pitch = False; off.substituted_out_minute = self.state.minute
        on.on_pitch = True; on.substituted_in_minute = self.state.minute; on.stamina = max(on.stamina, 92.0); on.slot = off.slot
        if off.is_goalkeeper: on.is_goalkeeper = True; on.position = "GK"
        else: on.is_goalkeeper = False
        if off.is_captain:
            off.is_captain = False
            nc = self.pick(side, "mid") or self.pick(side, "def") or self.pick(side, "out")
            if nc: nc.is_captain = True; team.lineup.captain_player_id = nc.player_api_id
        for i,e in enumerate(team.lineup.starters):
            if e.player_api_id == off_id:
                team.lineup.starters[i] = type(e)(on_id, on.name, on.position, off.slot); break
        team.lineup.bench = [b for b in team.lineup.bench if b.player_api_id != on_id]
        team.substitutions_made += 1
        rec = {"minute": self.state.minute, "off_player_id": off_id, "off_name": off.name, "on_player_id": on_id, "on_name": on.name, "reason": reason}
        team.substitution_events.append(rec)
        return self._emit(E.SUBSTITUTION, side, on_id, {"off_name": off.name, "on_name": on.name, "reason": reason, **rec})

    def apply_tactical_change(self, side: Side, updates: dict[str, Any], summary: str | None = None) -> dict[str, Any]:
        team = self.state.team(side)
        def merge(b,d):
            o = copy.deepcopy(b)
            for k,v in (d or {}).items():
                if isinstance(v,dict) and isinstance(o.get(k),dict): o[k]=merge(o[k],v)
                else: o[k]=v
            return o
        team.tactics = merge(team.tactics or default_tactics(), updates or {})
        new_form = updates.get("formation") or (updates.get("lineup") or {}).get("formation")
        if new_form and isinstance(new_form,str):
            self._apply_formation(side, new_form)
        team.tactical_changes.append({"minute": self.state.minute, "summary": summary or "tactical adjustment", "updates": updates})
        detail = f"{team.team.name} make a tactical adjustment: {summary}." if summary else f"{team.team.name} adjust their approach as play resumes."
        return self._emit(E.TACTICAL_CHANGE, side, payload={"summary": summary or "tactical adjustment", "detail": detail})

    def _apply_formation(self, side: Side, formation: str):
        from engine.formations import SUPPORTED_FORMATIONS, get_formation_slots
        if formation not in SUPPORTED_FORMATIONS: return
        team = self.state.team(side)
        slots = get_formation_slots(formation)
        onp = self.on_pitch(side)
        gk = self.gk(side)
        assigned = {}
        used = set()
        for s in slots:
            if s.group=="GK" and gk and gk.player_api_id not in used:
                assigned[gk.player_api_id]=s.code; used.add(gk.player_api_id); gk.slot=s.code; break
        for s in slots:
            if s.code in assigned.values(): continue
            cands = [p for p in onp if p.player_api_id not in used]
            pick = None
            for pref in [s.group,"MID","DEF","FWD"]:
                for p in cands:
                    if p.position==pref or (s.group=="DEF" and p.position=="MID") or (s.group=="MID" and p.position in {"DEF","FWD"}) or (s.group=="FWD" and p.position=="MID"):
                        pick=p; break
                if pick: break
            if not pick and cands: pick=cands[0]
            if pick: assigned[pick.player_api_id]=s.code; used.add(pick.player_api_id); pick.slot=s.code
        starters=[]
        for s in slots:
            ps = next((p for p in onp if assigned.get(p.player_api_id)==s.code), None)
            if ps: starters.append(type(team.lineup.starters[0])(ps.player_api_id, ps.name, ps.position, s.code))
        if len(starters)==11:
            team.lineup.starters=starters; team.lineup.formation=formation

    # --- simulation ---
    def simulate_until_break_or_end(self, max_minutes: int = 200) -> StepResult:
        produced: list[dict[str, Any]] = []
        for _ in range(max_minutes):
            if self.state.status == "finished": return StepResult(produced, finished=True)
            if self.is_waiting_for_break_decisions(): return StepResult(produced, break_reached=self.state.pending_break)
            if self.state.status == "setup": produced.extend(self.start()); continue
            if self.state.phase == "second_half_stoppage":
                self.state.extra_minute += 1
                produced.extend(self._minute())
                if self.state.extra_minute >= self.state.stoppage_time_2:
                    self.state.status="finished"; self.state.phase="finished"; finalize_all(self.state)
                    produced.append(self._emit(E.FULL_TIME, payload={"detail": f"Full-time! {self.state.scoreline()}"}))
                    return StepResult(produced, finished=True)
                continue
            if self.state.minute >= 90 and self.state.phase == "second_half":
                self.state.phase = "second_half_stoppage"; self.state.extra_minute = 0
                produced.append(self._emit(E.STOPPAGE_TIME, payload={"minutes": self.state.stoppage_time_2, "half":"the second half", "detail": f"The referee indicates {self.state.stoppage_time_2} minute(s) of stoppage time at the end of the second half."}))
                continue
            self.state.minute += 1; self.state.extra_minute = 0
            produced.extend(self._minute())
            if self.state.phase == "first_half" and self.state.minute == 45:
                produced.append(self._emit(E.HALF_TIME, payload={"detail": f"Half-time. {self.state.scoreline()}"}))
                self.state.pending_break="halftime"; self.state.status="halftime"; self.state.phase="halftime"
                self.state.home_break_decision_done=False; self.state.away_break_decision_done=False
                produced.append(self._emit(E.TACTICAL_BREAK, payload={"label":"Half-time","break":"halftime","detail":"Half-time. The match is paused for tactical decisions."}))
                return StepResult(produced, break_reached="halftime")
            if self.state.phase == "second_half" and self.state.minute == 60:
                self.state.pending_break="break_60"; self.state.status="break"; self.state.phase="break_60"
                self.state.home_break_decision_done=False; self.state.away_break_decision_done=False
                produced.append(self._emit(E.TACTICAL_BREAK, payload={"label":"60th-minute tactical break","break":"break_60","detail":"60th-minute tactical break. The match is paused for tactical decisions."}))
                return StepResult(produced, break_reached="break_60")
            if self.state.phase == "second_half" and self.state.minute == 75:
                self.state.pending_break="break_75"; self.state.status="break"; self.state.phase="break_75"
                self.state.home_break_decision_done=False; self.state.away_break_decision_done=False
                produced.append(self._emit(E.TACTICAL_BREAK, payload={"label":"75th-minute tactical break","break":"break_75","detail":"75th-minute tactical break. The match is paused for tactical decisions."}))
                return StepResult(produced, break_reached="break_75")
        return StepResult(produced, finished=self.state.status=="finished")

    def _minute(self) -> list[dict[str, Any]]:
        evs=[]
        hs = team_strength(self.state, "home"); as_ = team_strength(self.state, "away")
        apply_minute_stamina(self.state, hs.get("stamina_drain_mult",1.0), as_.get("stamina_drain_mult",1.0))
        att = self._pick_att(hs, as_)
        deff = self.state.opp_side(att)
        self.state.possession_side = att
        seq = self._possession(att, deff, hs, as_)
        evs.extend(seq)
        if not seq:
            evs.append(self._emit(E.PASS_SEQUENCE, att, payload={"detail": self._quiet_detail(att, deff, hs, as_)}))
        inj = self._maybe_injury(att,deff)
        if inj: evs.append(inj)
        self._update_poss()
        return evs

    def _avg_stam(self, side):
        xs=[p.stamina for p in self.on_pitch(side)]
        return sum(xs)/len(xs) if xs else 70.0

    def _update_poss(self):
        h=self.state.home.passes_attempted; a=self.state.away.passes_attempted; t=h+a
        if t>0:
            self.state.home.possession_share = round(h/t*100,1)
            self.state.away.possession_share = round(100-self.state.home.possession_share,1)

    def _pick_att(self, hs, as_):
        hc = hs["midfield"]*1.02 + hs["attack"]*0.2
        ac = as_["midfield"] + as_["attack"]*0.2
        diff = self.state.home.score - self.state.away.score
        mf = min(1.0, self.state.minute/90.0)
        if diff>0: ac*=1+0.08*mf
        elif diff<0: hc*=1+0.08*mf
        p_home = hc/(hc+ac) if (hc+ac)>0 else 0.5
        p_home += 0.04 if self.state.possession_side=="home" else -0.04
        return "home" if self.rng.random() < max(0.25,min(0.75,p_home)) else "away"

    def _quiet_detail(self, att, deff, hs, as_):
        an = self.state.team(att).team.name
        dn = self.state.team(deff).team.name
        minute = self.state.minute
        score = self.state.scoreline()
        diff = self.state.team(att).score - self.state.team(deff).score
        phase = "early on" if minute < 20 else "as the half develops" if minute < 45 else "after the restart" if minute < 65 else "in a tense spell" if minute < 80 else "in the closing stages"
        opts = [
            f"{an} circulate possession patiently through midfield, drawing {dn} across before looking to switch the point of attack.",
            f"{an} build from the back with measured passing, while {dn} keep a compact block and wait for a trigger to press.",
            f"Play slows {phase} as {an} recycle the ball across the defensive line, searching for a runner between {dn}'s lines.",
            f"{an} probe down the right channel, but {dn} shuffle across quickly and force play back into less dangerous areas.",
            f"{an} test the left flank with a combination pass, yet {dn}'s full-back holds position and the move breaks down safely.",
            f"A midfield battle unfolds: {an} try to play through pressure while {dn} snap into challenges to disrupt the rhythm.",
            f"{an} knock the ball around confidently {phase}, though {dn}'s spacing denies any clear passing lane into the box.",
            f"{dn} drop into an organized shape and {an} are made to settle for sideways possession near the halfway line.",
            f"{an} attempt to quicken the tempo with a forward pass, but {dn} read it early and regain territorial control.",
            f"{an} keep the ball moving under light pressure; {dn} stay disciplined, refusing to overcommit at {score}.",
            f"Neat link-up play from {an} in midfield, but the final ball toward the forwards is cut out by {dn}.",
            f"{an} work an overload on one side, switch play, and then recycle again as {dn} recover their defensive structure.",
            f"{phase.capitalize()}, {an} look to dictate possession while {dn} focus on staying compact and springing forward when possible.",
            f"{an} try to draw {dn} out with short passes around the center circle, waiting for a gap to exploit.",
            f"{dn} apply a brief press, but {an} play out calmly and re-establish control in their own half.",
        ]
        if diff > 0:
            opts += [
                f"Leading the match, {an} manage the tempo carefully, keeping possession and forcing {dn} to chase shadows.",
                f"{an} show composure with the advantage, circulating the ball to drain momentum from {dn}'s response.",
            ]
        elif diff < 0:
            opts += [
                f"Chasing the game, {an} push numbers higher and move the ball quicker, but {dn} absorb the pressure for now.",
                f"{an} take a few more risks in possession as they look for a route back, while {dn} remain watchful.",
            ]
        else:
            opts += [
                f"With the score level at {score}, neither side wants to overcommit as {an} and {dn} trade careful spells of possession.",
            ]
        # subtle tactical flavor
        att_s = hs if att == "home" else as_
        def_s = as_ if att == "home" else hs
        if def_s.get("press", 1.0) > att_s.get("midfield", 1.0) * 0.95:
            opts.append(f"{dn}'s press asks questions, but {an} stay composed on the ball and avoid a costly turnover.")
        if def_s.get("compactness", 1.0) > 1.05:
            opts.append(f"{dn} remain very compact between the lines, making it difficult for {an} to thread passes into advanced areas.")
        if att_s.get("width", 1.0) > 1.05:
            opts.append(f"{an} stretch the pitch and try to use the full width, but {dn} track the runners and protect central spaces.")
        return self.rng.choice(opts)

    def _possession(self, att, deff, hs, as_):
        evs=[]
        att_s = hs if att=="home" else as_; def_s = as_ if att=="home" else hs
        pa = self.rng.randint(3,8)
        pq = max(0.55,min(0.95,0.72 + (att_s["midfield"]/max(30,def_s["press"]*0.9+def_s["midfield"]*0.2)-1.0)*0.12))
        pq -= max(0,(60-self._avg_stam(att))*0.002)
        pc = int(round(pa*max(0.45,min(0.97,pq + self.rng.uniform(-0.05,0.05)))))
        stat_passes(self.state, att, pa, pc)
        midp = self.pick(att,"mid")
        if midp: from engine.ratings import record_pass_quality as rpq; rpq(self.state,att,midp.player_api_id,pc>=pa-1)
        turnover = 0.10 + (def_s["press"]-att_s["midfield"])*0.003 + (100-self._avg_stam(att))*0.0008
        if self.rng.random() < max(0.03,min(0.35,turnover)):
            evs.extend(self._turnover(deff, att, def_s, att_s)); return evs
        prog = (att_s["attack"]*0.55 + att_s["midfield"]*0.45)/max(25, def_s["defense"]*0.7 + def_s.get("compactness",1.0)*20)
        chance_p = max(0.03,min(0.42,0.08 + (prog-1.0)*0.12))
        if (att=="home" and self.state.home.score<self.state.away.score) or (att=="away" and self.state.away.score<self.state.home.score):
            if self.state.minute>=70: chance_p*=1.08
        if self.rng.random() < chance_p:
            evs.extend(self._attack(att,deff,att_s,def_s))
        return evs

    def _turnover(self, winner_side, loser_side, w_s, l_s):
        evs = []
        wn = self.state.team(winner_side).team.name
        ln = self.state.team(loser_side).team.name
        winp = self.pick(winner_side, "mid", "interceptions") or self.pick(winner_side, "def", "tackling")
        if winp:
            if self.rng.random() < 0.55:
                stat_interception(self.state, winner_side, winp.player_api_id)
                record_interception(self.state, winner_side, winp.player_api_id)
                detail = self.rng.choice([
                    f"{winp.name} reads the pass superbly and steps in to intercept for {wn}, cutting out {ln}'s move.",
                    f"Smart anticipation from {winp.name}, who picks off the ball in midfield and gives {wn} a chance to break.",
                    f"{wn} regain possession as {winp.name} jumps the passing lane and intercepts cleanly.",
                    f"{winp.name} spots the danger early, nicks possession away, and {wn} can transition forward.",
                ])
                evs.append(self._emit(E.PASS_SEQUENCE, winner_side, winp.player_api_id, {"detail": detail}))
            else:
                stat_tackle(self.state, winner_side, winp.player_api_id)
                record_tackle(self.state, winner_side, winp.player_api_id)
                detail = self.rng.choice([
                    f"{winp.name} times the challenge perfectly, dispossessing the opponent and winning it back for {wn}.",
                    f"Strong tackle from {winp.name}. {wn} come away with the ball as {ln}'s attack breaks down.",
                    f"{winp.name} bites into the duel and comes out on top, allowing {wn} to recover possession.",
                    f"A crunching but fair challenge by {winp.name} halts {ln}'s progress and turns possession over to {wn}.",
                ])
                evs.append(self._emit(E.PASS_SEQUENCE, winner_side, winp.player_api_id, {"detail": detail}))
        vuln = l_s.get("counter_vulnerability", 1.0) * (w_s["transition_attack"] / max(30, l_s["transition_defense"]))
        if self.rng.random() < max(0.05, min(0.45, 0.12 * vuln)):
            evs.extend(self._attack(winner_side, loser_side, w_s, l_s, is_counter=True))
        return evs

    def _attack(self, att, deff, att_s, def_s, is_counter=False):
        evs=[]
        an=self.state.team(att).team.name; dn=self.state.team(deff).team.name
        creator=self.pick(att,"mid","creativity") or self.pick(att,"att","passing")
        finisher=self.pick(att,"fwd","finishing") or self.pick(att,"att","shooting")
        defender=self.pick(deff,"def","defensive_awareness") or self.pick(deff,"def")
        gk=self.gk(deff)
        off_r = 0.04 + def_s.get("offside_trap_risk",0.02) + (def_s.get("line_height",1.0)-1.0)*0.05
        if is_counter: off_r*=0.8
        if self.rng.random() < max(0.01,min(0.18,off_r)) and finisher:
            stat_offside(self.state,att)
            evs.append(self._emit(E.OFFSIDE,att,finisher.player_api_id,{"detail":f"{an} try to release {finisher.name} in behind, but the assistant's flag goes up for offside."}))
            return evs
        foul_ch = 0.06*def_s.get("foul_risk",1.0)+0.01
        if self.rng.random() < min(0.18,foul_ch):
            evs.extend(self._foul_situation(att,deff,in_box=self.rng.random()<0.10)); return evs
        if not finisher: return evs
        buildup = self._buildup(att, deff, creator, finisher, is_counter)
        evs.append(self._emit(E.CHANCE, att, finisher.player_api_id, {"player_name": finisher.name, "detail": buildup}))
        if creator and self.rng.random() < 0.55:
            stat_key(self.state, att, creator.player_api_id)
            rating_key(self.state, att, creator.player_api_id)
        xg = max(0.02,min(0.85,0.08 + (((att_s["attack"]*(1.05 if is_counter else 1.0))/max(25,def_s["defense"]*0.85 + self._gk_str(deff)*0.15))-1.0)*0.18 + (0.06 if is_counter else 0.0)))
        if is_counter and def_s.get("line_height",1.0)>1.1: xg+=0.04
        block_p = 0.18 + def_s["defense"]*0.0015
        if self.rng.random() < block_p:
            blocker = defender or self.pick(deff,"def")
            stat_shot(self.state,att,finisher.player_api_id,False,xg*0.3); rating_shot(self.state,att,finisher.player_api_id,False,big_chance=xg>0.22)
            if blocker: stat_tackle(self.state,deff,blocker.player_api_id); record_tackle(self.state,deff,blocker.player_api_id)
            evs.append(self._emit(E.BLOCK,deff,blocker.player_api_id if blocker else None,{"detail": self.rng.choice([
                f"{finisher.name} gets a shot away for {an}, but {blocker.name if blocker else 'a defender'} throws himself into the line of fire to block.",
                f"A powerful effort from {finisher.name} is bravely charged down by {blocker.name if blocker else 'a defender'}.",
                f"{blocker.name if blocker else 'A defender'} makes a crucial block inside the box as {finisher.name} looks to score for {an}.",
                f"{finisher.name} pulls the trigger, only for {blocker.name if blocker else 'a recovering defender'} to intervene with a timely block.",
            ])}))
            if self.rng.random()<0.35:
                stat_corner(self.state,att); evs.append(self._emit(E.CORNER,att,{"detail": self.rng.choice([
                    f"The deflection spins behind and {an} will have a corner.",
                    f"Blocked and out for a corner to {an}.",
                    f"It ricochets off the defender and behind — corner kick for {an}.",
                ])}))
            return evs
        on_target_p = max(0.18,min(0.65,0.34 + xg*0.5))
        on_target = self.rng.random() < on_target_p
        stat_shot(self.state,att,finisher.player_api_id,on_target,xg); rating_shot(self.state,att,finisher.player_api_id,on_target,big_chance=xg>0.22)
        if not on_target:
            evs.append(self._emit(E.SHOT,att,finisher.player_api_id,{"outcome":"off_target","detail":self._miss_detail(att,finisher),"xg":round(xg,3)}))
            if xg>0.28 and self.rng.random()<0.6: record_missed_big(self.state,att,finisher.player_api_id)
            return evs
        gprob = self._goal_prob(att,deff,finisher,gk,xg,att_s,def_s,is_counter)
        if self.rng.random() < gprob:
            evs.extend(self._score_goal(att,deff,finisher,creator,xg,is_counter=is_counter))
        else:
            evs.extend(self._save_event(att,deff,finisher,gk,xg))
        return evs

    def _gk_str(self, side):
        g=self.gk(side)
        if not g: return 45.0
        p=self.state.players_index.get(self.state.player_key(side,g.player_api_id))
        if not p: return 55.0
        return (p.attributes.goalkeeping*0.7 + p.attributes.positioning*0.15 + p.attributes.composure*0.15)*(0.6+0.4*g.stamina/100.0)

    def _goal_prob(self, att,deff,finisher,gk,xg,att_s,def_s,is_counter):
        fp=self.state.players_index.get(self.state.player_key(att,finisher.player_api_id))
        fs=60.0
        if fp: fs=(fp.attributes.finishing*0.5+fp.attributes.composure*0.3+fp.attributes.shooting*0.2)*(0.55+0.45*finisher.stamina/100.0)
        gks=self._gk_str(deff)
        base=xg*(0.55+fs/160.0)*(1.15-gks/180.0)
        if is_counter: base*=1.05
        men_a=len(self.on_pitch(att)); men_d=len(self.on_pitch(deff))
        if men_a>men_d: base*=1.0+0.05*(men_a-men_d)
        return max(0.03,min(0.80,base))

    def _buildup(self, att, deff, creator, finisher, is_counter):
        an = self.state.team(att).team.name
        dn = self.state.team(deff).team.name
        c = creator.name if creator else "a midfielder"
        f = finisher.name if finisher else "an attacker"
        minute = self.state.minute
        late = minute >= 75
        if is_counter:
            return self.rng.choice([
                f"Dangerous transition! {an} win the ball and break at pace, {c} surging forward before releasing {f} into space behind {dn}'s recovering line.",
                f"{an} spring a ruthless counterattack. {c} carries through the middle and slides {f} in on goal as {dn} are caught upfield.",
                f"Lightning break from {an}. A quick turnover becomes a direct run forward, and {c}'s pass sends {f} racing clear of {dn}'s defense.",
                f"{an} turn defense into attack in seconds: {c} drives into open grass and feeds {f}, who suddenly has a sight of goal.",
                f"Explosive counter from {an} {('late in the game' if late else 'on the break')}. {c} exploits the gap and finds {f} with a perfectly weighted through ball.",
            ])
        return self.rng.choice([
            f"{an} build patiently through midfield. {c} receives between the lines, turns, and threads a pass to {f} in a promising position.",
            f"Quick combination play from {an} opens {dn}'s shape as {c} links neatly with runners before setting up {f} near the box.",
            f"{an} work the ball into the final third, switching play to stretch {dn} before {c} picks out {f} with an incisive pass.",
            f"Intricate passing from {an} draws defenders toward the ball, and {c} slips {f} into space at the edge of the area.",
            f"{an} advance down the flank and cut the ball back inside, where {c} tees up {f} for a shooting opportunity.",
            f"A clever one-two around the box unlocks {dn}'s defense, with {c} providing the final touch to release {f}.",
            f"{an} increase the tempo {('with time running short' if late else 'in this spell')}, moving the ball sharply until {c} finds {f} in a dangerous pocket.",
            f"{an} probe patiently, recycle once, then accelerate: {c} spots {f}'s run and delivers a measured pass into the channel.",
        ])

    def _miss_detail(self, att, finisher):
        an = self.state.team(att).team.name
        return self.rng.choice([
            f"{finisher.name} strikes for {an}, but the effort curls just wide of the far post.",
            f"{finisher.name} gets a clean look at goal, yet drags the shot inches past the upright.",
            f"{finisher.name} shoots from a promising position but cannot keep it down, sending it over the bar.",
            f"A decent opening for {an}, but {finisher.name} leans back and lifts the attempt harmlessly over.",
            f"{finisher.name} snaps at the chance and pulls the effort across goal and wide.",
            f"{finisher.name} connects well enough for {an}, though the strike flashes the wrong side of the post.",
            f"Space opens up for {finisher.name}, but the finish lacks precision and skids wide.",
            f"{finisher.name} aims for the corner but miscues slightly, and the ball rolls out for a goal kick.",
        ])

    def _score_goal(self, att,deff,scorer,creator,xg,is_counter=False,is_penalty=False):
        evs=[]
        assist = creator if (creator and creator.player_api_id!=scorer.player_api_id and self.rng.random()<0.75) else None
        # VAR
        var_ch = 0.04 + (0.03 if xg<0.12 else 0.0)
        if self.rng.random() < var_ch:
            reasons=[("a possible offside in the build-up","offside"),("a potential foul before the goal","foul"),("possible handball in the attacking phase","handball")]
            reason,_ = self.rng.choice(reasons)
            evs.append(self._emit(E.VAR_REVIEW,att,scorer.player_api_id,{"reason":reason,"detail":f"VAR review: the goal is being checked for {reason}."}))
            if self.rng.random() < 0.35:
                evs.append(self._emit(E.VAR_OVERTURN,att,scorer.player_api_id,{"reason":f"for {reason}","outcome":"No goal","detail":f"VAR decision: the goal is ruled out for {reason}. Score remains {self.state.home.team.name} {self.state.home.score}–{self.state.away.score} {self.state.away.team.name}."}))
                evs.append(self._emit(E.DISALLOWED_GOAL,att,scorer.player_api_id,{"reason":reason,"assist_name":assist.name if assist else None,"detail":f"Goal disallowed for {self.state.team(att).team.name}. {scorer.name}'s finish is chalked off for {reason}."}))
                return evs
            else:
                evs.append(self._emit(E.VAR_OVERTURN,att,scorer.player_api_id,{"reason":f"after checking {reason}","outcome":"Goal confirmed","detail":f"VAR decision: the goal stands after checking {reason}."}))
        stat_goal(self.state,att,scorer.player_api_id,assist.player_api_id if assist else None)
        rating_goal(self.state,att,scorer.player_api_id,assist.player_api_id if assist else None)
        detail = self._goal_detail(att,scorer,assist,is_counter,is_penalty)
        evs.append(self._emit(E.GOAL,att,scorer.player_api_id,{"assist_name":assist.name if assist else None,"assist_player_id":assist.player_api_id if assist else None,"xg":round(xg,3),"detail":detail,"score":{"home":self.state.home.score,"away":self.state.away.score}}))
        return evs

    def _goal_detail(self, att, scorer, assist, is_counter, is_penalty):
        an = self.state.team(att).team.name
        score = f"{self.state.home.team.name} {self.state.home.score}–{self.state.away.score} {self.state.away.team.name}"
        a = f" after a measured pass from {assist.name}" if assist else ""
        if is_penalty:
            return self.rng.choice([
                f"GOAL! {scorer.name} stays ice-cool from the spot and rolls the penalty into the net for {an}. {score}.",
                f"GOAL! {scorer.name} converts the penalty with conviction, sending {an} level or ahead. {score}.",
            ])
        if is_counter:
            return self.rng.choice([
                f"GOAL! {an} punish the space on the break as {scorer.name} finishes clinically{a}. {score}.",
                f"GOAL! A devastating counterattack ends with {scorer.name} sliding the ball home for {an}{a}. {score}.",
                f"GOAL! Swift, direct, ruthless — {scorer.name} completes the counter for {an}{a}. {score}.",
                f"GOAL! {scorer.name} keeps his composure at the end of a rapid transition and scores for {an}{a}. {score}.",
            ])
        return self.rng.choice([
            f"GOAL! {scorer.name} finds the net for {an}{a} after a flowing attacking move. {score}.",
            f"GOAL! A composed finish from {scorer.name}{a} gives {an} their reward. {score}.",
            f"GOAL! {scorer.name} arrives right on cue and steers the ball home for {an}{a}. {score}.",
            f"GOAL! Brilliant execution in the final third — {scorer.name} finishes confidently for {an}{a}. {score}.",
            f"GOAL! {scorer.name} makes no mistake inside the area, putting {an} on the scoresheet{a}. {score}.",
        ])

    def _save_event(self, att, deff, shooter, gk, xg):
        evs = []
        gk = self.gk(deff) or gk
        an = self.state.team(att).team.name
        if gk:
            stat_save(self.state, deff, gk.player_api_id)
            rating_save(self.state, deff, gk.player_api_id, big=xg > 0.25)
            detail = self.rng.choice([
                f"What a save! {shooter.name} strikes firmly for {an}, but {gk.name} reacts brilliantly to push it away.",
                f"Big stop from {gk.name}, who gets down quickly to deny {shooter.name}'s well-hit effort for {an}.",
                f"{shooter.name} looks certain to score for {an}, yet {gk.name} spreads himself and makes a crucial save.",
                f"Excellent reflexes from {gk.name}, tipping {shooter.name}'s shot out of danger as {an} threaten.",
                f"{gk.name} holds his nerve, reading {shooter.name}'s attempt and smothering the ball safely.",
                f"Superb goalkeeping. {shooter.name} finds the target for {an}, but {gk.name} produces a strong hand to keep it out.",
            ])
            evs.append(self._emit(E.SAVE, deff, gk.player_api_id, {"shooter_name": shooter.name, "xg": round(xg, 3), "detail": detail}))
            if self.rng.random() < (0.35 if xg > 0.18 else 0.22):
                stat_corner(self.state, att)
                evs.append(self._emit(E.CORNER, att, {"detail": self.rng.choice([
                    f"{an} earn a corner as {gk.name} can only parry the effort behind.",
                    f"The save spills loose territory and {an} win a corner kick.",
                    f"{gk.name} turns it behind at full stretch — corner to {an}.",
                ])}))
        else:
            evs.append(self._emit(E.SHOT, att, shooter.player_api_id, {"outcome": "on_target", "detail": f"{shooter.name} forces a save with a firm effort on target for {an}."}))
        return evs

    def _foul_situation(self, att,deff,in_box=False):
        evs=[]
        fouler=self.pick(deff,"def","tackling") or self.pick(deff,"mid") or self.pick(deff,"out")
        fouled=self.pick(att,"att","dribbling") or self.pick(att,"mid")
        if not fouler or not fouled: return evs
        reason=self.rng.choice(["a late challenge","a mistimed tackle","a trip as the attacker advanced"]) if not in_box else self.rng.choice(["a clumsy challenge inside the area","a foul as the attacker shaped to shoot"])
        stat_foul(self.state,deff,fouler.player_api_id,att,fouled.player_api_id)
        if in_box and self.rng.random() < 0.45:
            evs.append(self._emit(E.FOUL, deff, fouler.player_api_id, {"fouled_name": fouled.name, "reason": reason, "detail": self.rng.choice([
                f"Drama in the box! {fouler.name} catches {fouled.name} with {reason}, and the referee has a big decision to make.",
                f"{fouled.name} goes down under pressure from {fouler.name} inside the area ({reason}).",
                f"Contact in the penalty area as {fouler.name} challenges {fouled.name} with {reason}.",
            ])}))
            evs.extend(self._penalty(att, deff, fouled, fouler))
            return evs
        sev = self.rng.random()
        card = None
        if sev < 0.06:
            card = "red"
        elif sev < 0.28:
            card = "yellow"
        evs.append(self._emit(E.FOUL, deff, fouler.player_api_id, {"fouled_name": fouled.name, "reason": reason, "detail": self.rng.choice([
            f"Free kick to {self.state.team(att).team.name}. {fouler.name} is penalised for {reason} on {fouled.name}.",
            f"{fouler.name} halts {fouled.name}'s progress with {reason}, and {self.state.team(att).team.name} earn a free kick.",
            f"The referee's whistle sounds as {fouler.name} commits {reason} on {fouled.name}. Free kick for {self.state.team(att).team.name}.",
            f"{fouled.name} is brought down by {fouler.name} ({reason}), stopping a promising move by {self.state.team(att).team.name}.",
        ])}))
        if card: evs.extend(self._issue_card(deff,fouler,card,reason))
        if self.rng.random()<0.18 and not fouler.red_card:
            taker=self.pick(att,"att","shooting") or fouled
            evs.append(self._emit(E.FREE_KICK, att, taker.player_api_id, {"detail": self.rng.choice([
                f"{self.state.team(att).team.name} have a free kick in a promising position. {taker.name} stands over it, eyeing the target.",
                f"Set-piece opportunity for {self.state.team(att).team.name}. {taker.name} measures the distance as the wall is set.",
                f"{taker.name} places the ball carefully for {self.state.team(att).team.name}; this is a dangerous shooting range.",
            ])}))
            if self.rng.random() < 0.22:
                gk = self.gk(deff)
                xg = 0.07 + self.rng.random() * 0.08
                on_t = self.rng.random() < 0.5
                stat_shot(self.state, att, taker.player_api_id, on_t, xg)
                rating_shot(self.state, att, taker.player_api_id, on_t)
                if on_t and self.rng.random() < self._goal_prob(att, deff, taker, gk, xg, team_strength(self.state, att), team_strength(self.state, deff), False):
                    evs.extend(self._score_goal(att, deff, taker, None, xg))
                elif on_t and gk:
                    stat_save(self.state, deff, gk.player_api_id)
                    rating_save(self.state, deff, gk.player_api_id, big=True)
                    evs.append(self._emit(E.SAVE, deff, gk.player_api_id, {"shooter_name": taker.name, "detail": self.rng.choice([
                        f"{taker.name} curls the free kick towards goal, but {gk.name} reads it well and makes the save.",
                        f"A dipping free kick from {taker.name} is on target, yet {gk.name} gets across to keep it out.",
                        f"{gk.name} springs to his side and parries {taker.name}'s set-piece effort away from danger.",
                    ])}))
                else:
                    evs.append(self._emit(E.SHOT, att, taker.player_api_id, {"outcome": "off_target", "detail": self.rng.choice([
                        f"{taker.name}'s free kick clears the wall but drifts beyond the post.",
                        f"{taker.name} goes for goal from the set piece, but the effort sails over the bar.",
                        f"Decent strike from {taker.name}, yet it bends just wide of the target.",
                    ])}))
        return evs

    def _penalty(self, att,deff,fouled,fouler):
        evs=[]
        taker=self.pick(att,"fwd","finishing") or fouled; gk=self.gk(deff)
        evs.append(self._emit(E.PENALTY_AWARDED, att, taker.player_api_id, {"reason": f"a foul by {fouler.name} on {fouled.name}", "detail": self.rng.choice([
            f"Penalty to {self.state.team(att).team.name}! {fouler.name} is adjudged to have fouled {fouled.name}, and the referee points straight to the spot.",
            f"The referee has no hesitation — penalty for {self.state.team(att).team.name} after {fouler.name}'s challenge on {fouled.name} in the area.",
            f"Spot kick awarded. {fouled.name} goes down under contact from {fouler.name}, and {self.state.team(att).team.name} have a huge chance.",
        ])}))
        if self.rng.random()<0.25 and not fouler.red_card:
            evs.extend(self._issue_card(deff,fouler,"yellow","denying a goalscoring opportunity"))
        fp=self.state.players_index.get(self.state.player_key(att,taker.player_api_id))
        ts=65.0
        if fp: ts=fp.attributes.finishing*0.6 + fp.attributes.composure*0.4
        gks=self._gk_str(deff)
        score_p=max(0.55,min(0.92,0.76 + (ts-gks)*0.003)); save_p=max(0.05,min(0.30,1.0-score_p-0.08))
        r=self.rng.random()
        if r < score_p:
            stat_shot(self.state,att,taker.player_api_id,True,0.79); rating_shot(self.state,att,taker.player_api_id,True,big_chance=True)
            evs.extend(self._score_goal(att,deff,taker,None,0.79,is_penalty=True))
            evs.append(self._emit(E.PENALTY_SCORED, att, taker.player_api_id, {"detail": self.rng.choice([
                f"Penalty converted by {taker.name}, who keeps his nerve under pressure.",
                f"{taker.name} dispatches the penalty confidently into the net.",
                f"Cool finish from the spot as {taker.name} beats the goalkeeper.",
            ]), "score": {"home": self.state.home.score, "away": self.state.away.score}}))
        elif r < score_p + save_p and gk:
            stat_shot(self.state, att, taker.player_api_id, True, 0.79)
            rating_shot(self.state, att, taker.player_api_id, True, big_chance=True)
            record_missed_big(self.state, att, taker.player_api_id)
            stat_save(self.state, deff, gk.player_api_id)
            rating_save(self.state, deff, gk.player_api_id, big=True)
            evs.append(self._emit(E.PENALTY_SAVED, deff, gk.player_api_id, {"taker_name": taker.name, "detail": self.rng.choice([
                f"Penalty saved! {gk.name} guesses correctly and produces a brilliant stop to deny {taker.name}.",
                f"{gk.name} is the hero! He dives the right way and keeps out {taker.name}'s penalty.",
                f"Superb save from {gk.name}, who reads {taker.name}'s intentions and palms the penalty away.",
            ])}))
        else:
            stat_shot(self.state, att, taker.player_api_id, False, 0.79)
            rating_shot(self.state, att, taker.player_api_id, False, big_chance=True)
            record_missed_big(self.state, att, taker.player_api_id)
            evs.append(self._emit(E.PENALTY_MISSED, att, taker.player_api_id, {"detail": self.rng.choice([
                f"Penalty missed! {taker.name} fails to hit the target from the spot.",
                f"Off target from {taker.name} — the penalty is squandered.",
                f"{taker.name} puts too much on it and sends the penalty wide of the goal.",
            ])}))
        return evs

    def _issue_card(self, side, player, ctype, reason):
        evs=[]
        tn = self.state.team(side).team.name
        if ctype == "red":
            stat_card(self.state, side, player.player_api_id, red=True)
            rating_card(self.state, side, player.player_api_id, "red")
            player.red_card = True
            player.on_pitch = False
            evs.append(self._emit(E.RED_CARD, side, player.player_api_id, {"reason": reason, "detail": self.rng.choice([
                f"Red card! {player.name} ({tn}) is shown a straight red for {reason}. {tn} are reduced in numbers.",
                f"Straight dismissal for {player.name}. The referee deems {reason} worthy of a red card for {tn}.",
                f"{player.name} ({tn}) is sent off for {reason}. A huge setback for his side.",
            ])}))
        else:
            prev = player.yellow_cards
            stat_card(self.state, side, player.player_api_id, yellow=True)
            rating_card(self.state, side, player.player_api_id, "yellow")
            if prev >= 1:
                stat_card(self.state, side, player.player_api_id, red=True)
                rating_card(self.state, side, player.player_api_id, "red")
                player.red_card = True
                player.on_pitch = False
                evs.append(self._emit(E.SECOND_YELLOW, side, player.player_api_id, {"reason": reason, "detail": self.rng.choice([
                    f"Second yellow for {player.name} ({tn}) after {reason} — that's a red card. He has to go.",
                    f"{player.name} is booked again for {reason} and receives a second yellow. {tn} are down a player.",
                    f"Two cautions for {player.name} ({tn}) and the referee sends him off following {reason}.",
                ])}))
            else:
                evs.append(self._emit(E.YELLOW_CARD, side, player.player_api_id, {"reason": reason, "detail": self.rng.choice([
                    f"Yellow card for {player.name} ({tn}) for {reason}. He'll need to be careful now.",
                    f"The referee cautions {player.name} of {tn} after {reason}.",
                    f"Booking for {player.name} ({tn}) — a yellow card shown for {reason}.",
                    f"{player.name} goes into the book for {tn} following {reason}.",
                ])}))
        return evs

    def _maybe_injury(self, att, deff):
        base = 0.003
        side = att if self.rng.random() < 0.5 else deff
        prob = base + max(0, (50 - self._avg_stam(side)) * 0.0002)
        if self.rng.random() >= prob:
            return None
        cands = self.on_pitch(side)
        if not cands:
            return None
        player = self.rng.choice(cands)
        sr = self.rng.random()
        if sr < 0.15:
            severity = "serious injury"; can_continue = False
        elif sr < 0.45:
            severity = "muscle problem"; can_continue = self.rng.random() < 0.35
        else:
            severity = "knock"; can_continue = self.rng.random() < 0.85
        player.injury_minute = self.state.minute
        player.injury_severity = severity
        player.injured = True
        tn = self.state.team(side).team.name
        subbed = False
        continued = False
        if can_continue:
            continued = True
            player.injury_continued = True
            detail = self.rng.choice([
                f"Concern for {tn} as {player.name} goes down with a {severity}, but after treatment he is able to continue.",
                f"{player.name} needs medical attention for a {severity}. Fortunately for {tn}, he shakes it off and returns to play.",
                f"A worrying moment for {tn}: {player.name} takes a {severity}, though he jogs back on after a quick assessment.",
            ])
        else:
            bench = self.bench(side)
            rep = next((b for b in bench if b.position == player.position), bench[0] if bench else None)
            if rep and self.state.team(side).substitutions_made < 5:
                try:
                    self.apply_substitution(side, player.player_api_id, rep.player_api_id, reason=f"injury ({severity})")
                    subbed = True
                    player.injury_substituted = True
                    detail = self.rng.choice([
                        f"Injury blow for {tn}. {player.name} cannot continue after a {severity} and is replaced by {rep.name}.",
                        f"{player.name} pulls up with a {severity} and is unable to carry on. {tn} send on {rep.name} as a replacement.",
                        f"Forced change for {tn}: {player.name} is helped off with a {severity}, making way for {rep.name}.",
                    ])
                except Exception:
                    player.on_pitch = False
                    detail = self.rng.choice([
                        f"Injury setback for {tn}. {player.name} cannot continue after a {severity}.",
                        f"{player.name} is unable to shake off a {severity} and {tn} are forced to cope without him.",
                    ])
            else:
                player.on_pitch = False
                detail = self.rng.choice([
                    f"Injury setback for {tn}. {player.name} cannot continue after a {severity}.",
                    f"{player.name} goes off with a {severity} and {tn} must continue without him for now.",
                ])
        return self._emit(E.INJURY, side, player.player_api_id, {"player_name": player.name, "severity": severity, "substituted": subbed, "continued": continued, "detail": detail})
