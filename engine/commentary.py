from __future__ import annotations

import random
from typing import Any

from engine import events as E
from engine.models import MatchState


def _team(state: MatchState, side: str | None) -> str:
    if side == "home":
        return state.home.team.name
    if side == "away":
        return state.away.team.name
    return "The side"


def _opp(state: MatchState, side: str | None) -> str:
    if side in {"home", "away"}:
        return state.team(state.opp_side(side)).team.name
    return "the opposition"


def _player(state: MatchState, side: str | None, pid: int | None, payload: dict[str, Any]) -> str:
    for k in ("player_name", "name", "scorer_name", "taker_name"):
        if payload.get(k):
            return str(payload[k])
    if side and pid:
        ps = state.player_states.get(state.player_key(side, int(pid)))
        if ps:
            return ps.name
    return "the player"


def _prefix(state: MatchState, event: dict[str, Any]) -> str:
    m = int(event.get("minute", state.minute))
    ex = int(event.get("extra_minute", state.extra_minute))
    return f"{m}+{ex}'" if ex else f"{m}'"


def _score(state: MatchState) -> str:
    return f"{state.home.team.name} {state.home.score}–{state.away.score} {state.away.team.name}"


def _pick(rng: random.Random, options: list[str]) -> str:
    return rng.choice(options) if options else ""


def _phase_flavor(state: MatchState, rng: random.Random) -> str:
    m = state.minute
    if m <= 10:
        return _pick(rng, [
            "in the opening exchanges, with both sides testing distances and pressing triggers",
            "early in the contest as patterns begin to emerge",
            "in the opening spell, with the tempo still settling",
            "as the match finds its early rhythm",
        ])
    if m < 30:
        return _pick(rng, [
            "as the first half develops and tactical shapes become clearer",
            "with the game settling into a defined rhythm",
            "midway through the first half as both teams probe for control",
            "as possession and territory start to ebb and flow",
        ])
    if m < 45:
        return _pick(rng, [
            "approaching the interval with intensity rising",
            "late in the first half as both sides look for an edge",
            "as half-time nears and concentration becomes crucial",
            "with the first period moving toward its conclusion",
        ])
    if m < 60:
        return _pick(rng, [
            "after the restart, with renewed energy and adjusted instructions",
            "early in the second half as the tactical battle evolves",
            "as the second period unfolds and spaces begin to open",
            "following the break, with both teams pushing their game plan",
        ])
    if m < 75:
        return _pick(rng, [
            "with time ticking on and the match entering a decisive phase",
            "as the contest tightens in a crucial spell",
            "in the middle of the second half where control and transitions matter most",
            "as fatigue and tactical tweaks start to influence the flow",
        ])
    if m < 90:
        return _pick(rng, [
            "in the closing stages, where every decision carries extra weight",
            "late on as the pressure builds around every duel",
            "as the clock winds down and urgency grows",
            "with the finish line in sight and margins razor thin",
        ])
    return _pick(rng, [
        "deep into stoppage time with nerves on edge",
        "in added time as one more moment could decide everything",
        "with seconds draining away in a tense finale",
    ])


def _tactic_hint(state: MatchState, side: str | None, rng: random.Random) -> str:
    if side not in {"home", "away"}:
        return ""
    try:
        tac = state.team(side).tactics or {}
        atk = (tac.get("attacking") or {})
        dfn = (tac.get("defensive") or {})
        ment = str(atk.get("mentality") or "").lower()
        press = str(dfn.get("pressing_intensity") or "").lower()
        line = str(dfn.get("defensive_line") or "").lower()
        width = str(atk.get("attacking_width") or "").lower()
        buildup = str(atk.get("build_up") or "").lower()
        opts = []
        if ment in {"attacking", "positive"}:
            opts.append("committing numbers forward with an assertive attacking intent")
        elif ment == "cautious":
            opts.append("prioritizing structure and control before committing bodies forward")
        if press in {"high", "very_high"}:
            opts.append("pressing aggressively to win the ball high up the pitch")
        elif press == "low":
            opts.append("sitting in a compact shape and inviting pressure")
        if line in {"high", "very_high"}:
            opts.append("holding a high defensive line to compress space")
        elif line == "deep":
            opts.append("protecting depth with a deeper defensive line")
        if width == "wide":
            opts.append("stretching play to use the full width of the pitch")
        elif width == "narrow":
            opts.append("narrowing their attacking shape to overload central lanes")
        if buildup == "short":
            opts.append("building patiently through short passing combinations")
        elif buildup in {"direct", "long"}:
            opts.append("looking to progress quickly with more direct passes")
        if not opts:
            return ""
        return _pick(rng, [" " + opts[0].capitalize() + ".", " " + opts[0].capitalize() + ".", ""])
    except Exception:
        return ""


def _state_hint(state: MatchState, side: str | None, rng: random.Random) -> str:
    diff = state.home.score - state.away.score
    if side == "away":
        diff = -diff
    m = state.minute
    if diff == 0:
        if m >= 80:
            return _pick(rng, [
                " With the score level, neither side wants to make the costly mistake.",
                " The deadlock raises the tension as both teams search for a decisive moment.",
            ])
        return ""
    if diff > 0:
        if m >= 75:
            return _pick(rng, [
                " Protecting the lead, game management and compactness become vital now.",
                " With an advantage in hand, composure and control are the priority.",
            ])
        return _pick(rng, [
            " The lead gives them confidence, but concentration remains essential.",
            "",
        ])
    # trailing
    if m >= 70:
        return _pick(rng, [
            " Chasing the game, urgency and risk-taking naturally increase.",
            " The need for a response adds intensity to every attack.",
        ])
    if m >= 50:
        return _pick(rng, [
            " There is a growing push to find a way back into the contest.",
            "",
        ])
    return ""


def generate(state: MatchState, event: dict[str, Any], rng: random.Random) -> str:
    payload = event.get("payload") or {}
    # Prefer engine-provided detailed text when available, but enrich it for depth
    detail = payload.get("detail")
    if detail:
        txt = str(detail).strip()
        if not txt.startswith(("0'", "1'", "2'", "3'", "4'", "5'", "6'", "7'", "8'", "9'")):
            txt = f"{_prefix(state, event)} — {txt}"
        et = event.get("event_type")
        side = event.get("team_side")
        if et in {E.GOAL, E.PENALTY_SCORED, E.PENALTY_SAVED, E.PENALTY_MISSED, E.RED_CARD, E.SECOND_YELLOW, E.DISALLOWED_GOAL}:
            extra = _pick(rng, [
                f" The scoreboard now reads {_score(state)}, and the momentum of the match shifts with it.",
                f" A defining moment at {_score(state)} that could shape the tactical decisions to come.",
                f" You can feel the emotional swing ripple through both teams at {_score(state)}.",
                "",
            ])
            if extra and extra.strip() not in txt:
                txt += extra
        elif et in {E.CHANCE, E.SAVE, E.BLOCK} and rng.random() < 0.45:
            extra = _pick(rng, [
                " The quality of the movement and execution in that sequence stands out.",
                " It is the kind of passage that reflects the tactical plan taking shape on the pitch.",
                " Fine margins in both boxes are proving decisive in this spell.",
                " Both benches respond immediately, aware of how quickly the balance can tilt.",
            ])
            if extra and extra.strip() not in txt:
                txt += extra
        elif et in {E.PASS_SEQUENCE} and rng.random() < 0.25:
            extra = _tactic_hint(state, side, rng)
            if extra and extra.strip() not in txt:
                txt += extra
        return txt

    et = event.get("event_type")
    side = event.get("team_side")
    team = _team(state, side)
    opp = _opp(state, side)
    player = _player(state, side, event.get("player_api_id"), payload)
    pfx = _prefix(state, event)
    flav = _phase_flavor(state, rng)
    score = _score(state)
    t_hint = _tactic_hint(state, side, rng)
    s_hint = _state_hint(state, side, rng)

    if et == E.KICKOFF:
        return f"{pfx} — Kick-off and we are underway. {team} take the first touches and look to impose their game plan against {opp}, with early shape and tempo already under the microscope."
    if et == E.SECOND_HALF_KICKOFF:
        return f"{pfx} — The second half begins with the score at {score}. {team} restart play, and any tactical adjustments from the interval are about to be tested under match pressure."
    if et == E.HALF_TIME:
        return f"{pfx} — Half-time whistle. {score}. A valuable pause for both managers to reassess pressing structure, spacing between the lines, and how to manage the next phase of the contest."
    if et == E.FULL_TIME:
        return f"{pfx} — Full-time. {score}. The result is settled by execution in key moments, tactical discipline, and the ability to convert pressure into clear outcomes."
    if et == E.STOPPAGE_TIME:
        mins = payload.get("minutes", 1)
        half = payload.get("half", "this half")
        return _pick(rng, [
            f"{pfx} — The fourth official indicates {mins} additional minute(s) at the end of {half}. There is still time for a decisive twist if either side can find precision in the final moments.",
            f"{pfx} — {mins} minute(s) of stoppage time to be played. With the score at {score}, every clearance, pass, and duel carries added weight.",
        ])
    if et == E.TACTICAL_BREAK:
        label = payload.get("label", "Tactical break")
        return _pick(rng, [
            f"{pfx} — {label}. Play pauses briefly, giving both benches a chance to deliver detailed tactical instructions on pressing cues, compactness, and attacking transitions.",
            f"{pfx} — {label}. A short stoppage, but an important one: managers use the moment to refine shape, manage energy levels, and target weaknesses in the opposition setup.",
        ])
    if et == E.BREAK_RESUME:
        return _pick(rng, [
            f"{pfx} — Play resumes {flav}. The tactical tweaks made moments ago will be tested immediately as both sides compete for control of space and tempo.{s_hint}",
            f"{pfx} — Back underway at {score}. You can expect adjustments in pressing height, buildup patterns, and transition play as the match continues.{s_hint}",
        ])

    if et == E.PASS_SEQUENCE:
        return _pick(rng, [
            f"{pfx} — {team} circulate possession with patience, switching the point of attack to pull {opp} out of their block before probing for a progressive pass between the lines.{t_hint}{s_hint}",
            f"{pfx} — {flav.capitalize()}, {team} build methodically from the back, using short combinations and positional rotations while {opp} stay organized and look for the right moment to press.{t_hint}",
            f"{pfx} — Controlled spell for {team}: measured passing, support angles, and careful tempo as they try to unbalance {opp}'s defensive structure without forcing the final ball.{t_hint}",
            f"{pfx} — {team} work the ball across midfield and into wider channels, testing {opp}'s compactness and recovery runs as the tactical chess match continues at {score}.{s_hint}",
            f"{pfx} — {opp} apply pressure in phases, but {team} remain composed in possession, recycling play and resetting their attacking shape to find a cleaner route forward.{t_hint}",
        ])

    if et == E.CHANCE:
        return _pick(rng, [
            f"{pfx} — CHANCE! {team} unlock space with incisive movement and a well-timed pass, allowing {player} to attack a dangerous area as {opp} scramble to recover their defensive positions.{s_hint}",
            f"{pfx} — CHANCE! A sharp attacking sequence from {team} {flav} cuts through the pressure, and {player} suddenly finds a promising opening in and around the box.{s_hint}",
            f"{pfx} — CHANCE! {team}'s buildup pays off as {player} receives in a threatening pocket, forcing {opp}'s back line into a desperate defensive reaction.",
        ])

    if et == E.SHOT:
        outcome = payload.get("outcome", "off_target")
        if outcome == "on_target":
            return _pick(rng, [
                f"{pfx} — {player} connects cleanly for {team}, driving an effort on target that tests the goalkeeper's positioning and reactions in a high-pressure moment.",
                f"{pfx} — Well-struck attempt by {player}. The shot carries intent and accuracy, forcing a response as {opp} are briefly opened up.",
            ])
        if outcome == "blocked":
            return _pick(rng, [
                f"{pfx} — {player} gets a shot away for {team}, but a defender reads the danger and throws himself into the line to produce a vital block.",
                f"{pfx} — Determined defending from {opp} as {player}'s effort is charged down inside a crowded area, denying what looked a promising strike.",
            ])
        return _pick(rng, [
            f"{pfx} — {player} finds a shooting opportunity for {team} {flav}, but the execution falls short and the effort misses the target.",
            f"{pfx} — A glimpse of goal for {player}, who tries to pick his spot under pressure yet cannot keep the attempt on frame.",
            f"{pfx} — {team} work an opening, but {player}'s finish lacks the required precision and {opp} survive the moment.",
        ])

    if et == E.SAVE:
        shooter = payload.get("shooter_name", "the attacker")
        return _pick(rng, [
            f"{pfx} — Outstanding save. {shooter} strikes with conviction for {opp}, but {player} shows sharp reflexes and strong technique to keep {team} in the contest.",
            f"{pfx} — Crucial intervention from {player}, who reads {shooter}'s effort early and produces a composed save under pressure.",
            f"{pfx} — {player} comes up big for {team}, parrying {shooter}'s on-target attempt away from danger at a key moment.",
        ])

    if et == E.BLOCK:
        return _pick(rng, [
            f"{pfx} — Heroic defending. {player} puts his body on the line to block the shot, absorbing the impact and preventing a clear scoring chance for {opp}.",
            f"{pfx} — Last-ditch block from {player}, who anticipates the strike and intervenes decisively to protect {team}.",
        ])

    if et == E.CORNER:
        return _pick(rng, [
            f"{pfx} — Corner to {team} after sustained pressure in the final third forces {opp} to turn the ball behind. A set-piece chance to test organization and aerial strength.",
            f"{pfx} — {team} win a corner {flav}. Deliveries and movement in the box could be decisive here as {opp} prepare to defend.",
        ])

    if et == E.FOUL:
        fouled = payload.get("fouled_name", "an opponent")
        reason = payload.get("reason", "a mistimed challenge")
        return _pick(rng, [
            f"{pfx} — Foul by {player} on {fouled} ({reason}). The referee stops play and awards {opp} a free kick, breaking up the momentum of the move.",
            f"{pfx} — {player} mistimes the challenge on {fouled} and concedes a foul for {reason}. {opp} have an opportunity to regroup or threaten from the set piece.",
        ])

    if et == E.FREE_KICK:
        return _pick(rng, [
            f"{pfx} — Free kick in a promising area for {team}. {player} stands over the ball as options are discussed — a delivery into the box or a direct effort are both on.",
            f"{pfx} — Set-piece opportunity for {team} {flav}. {player} prepares the strike/delivery while {opp} organize their wall and marking assignments.",
        ])

    if et == E.PENALTY_AWARDED:
        reason = payload.get("reason", "a foul in the box")
        return _pick(rng, [
            f"{pfx} — Penalty to {team}! After {reason}, the referee points to the spot. A huge moment with the score at {score} and pressure at its peak.",
            f"{pfx} — Spot kick awarded for {team} following {reason}. Composure and technique will decide this pivotal opportunity.",
        ])

    if et == E.GOAL:
        assist = payload.get("assist_name")
        a = f", set up by a well-judged contribution from {assist}" if assist else ""
        return _pick(rng, [
            f"{pfx} — GOAL! {player} finishes with conviction for {team}{a}. A high-quality moment of execution and the score moves to {score}.",
            f"{pfx} — GOAL for {team}! {player} delivers the decisive touch{a}, rewarding the buildup and changing the complexion of the match at {score}.",
        ])

    if et == E.PENALTY_SCORED:
        return _pick(rng, [
            f"{pfx} — GOAL! {player} holds his nerve from the penalty spot and converts clinically for {team}. {score}.",
            f"{pfx} — Composed penalty from {player}, who sends the goalkeeper the wrong way and scores for {team}. {score}.",
        ])

    if et == E.PENALTY_SAVED:
        taker = payload.get("taker_name", "the penalty taker")
        return _pick(rng, [
            f"{pfx} — Penalty saved! {player} reads {taker}'s intentions brilliantly and produces a decisive stop to deny what looked a certain goal.",
            f"{pfx} — Huge moment for {team}: {player} dives the right way and keeps out {taker}'s penalty with excellent reflexes and timing.",
        ])

    if et == E.PENALTY_MISSED:
        return _pick(rng, [
            f"{pfx} — Penalty missed. {player} cannot convert for {team}, and a golden opportunity slips away in a crucial phase of the match.",
            f"{pfx} — Agony for {team} as {player}'s penalty fails to find the target, letting the pressure moment pass.",
        ])

    if et == E.DISALLOWED_GOAL:
        reason = payload.get("reason", "an infringement")
        return _pick(rng, [
            f"{pfx} — Goal disallowed for {team} due to {reason}. The celebrations are cut short and the score remains {score}.",
            f"{pfx} — After review/decision, {player}'s effort is ruled out for {team} ({reason}). Fine margins again at {score}.",
        ])

    if et == E.OFFSIDE:
        return _pick(rng, [
            f"{pfx} — Offside flag raised against {player}. {team}'s move had promise, but the timing of the run was just a fraction too early.",
            f"{pfx} — {player} is caught offside as {team} attempt to release him behind the line. {opp} regain possession with the free kick.",
        ])

    if et == E.YELLOW_CARD:
        reason = payload.get("reason", "a foul")
        return _pick(rng, [
            f"{pfx} — Yellow card for {player} ({team}) after {reason}. The referee makes a disciplinary statement as intensity rises {flav}.",
            f"{pfx} — Booking for {player}. Cautioned for {reason}, and he will need to manage his challenges carefully for the remainder of the match.",
        ])

    if et == E.SECOND_YELLOW:
        reason = payload.get("reason", "another foul")
        return _pick(rng, [
            f"{pfx} — Second yellow card for {player} ({team}) after {reason}, and that means a red card. A major disciplinary and tactical blow at {score}.",
            f"{pfx} — Dismissal. {player} picks up a second booking for {team} and is sent off, forcing an immediate reshuffle in shape and responsibilities.",
        ])

    if et == E.RED_CARD:
        reason = payload.get("reason", "serious foul play")
        return _pick(rng, [
            f"{pfx} — Straight red card! {player} ({team}) is dismissed for {reason}. The numerical disadvantage will significantly affect tactics and game state.",
            f"{pfx} — Red card shown to {player} of {team} for {reason}. A pivotal disciplinary moment that reshapes the contest at {score}.",
        ])

    if et == E.INJURY:
        severity = payload.get("severity", "knock")
        if payload.get("continued"):
            return _pick(rng, [
                f"{pfx} — Concern for {team} as {player} goes down with a {severity}. After assessment and treatment, he is able to continue, though it is something to monitor.",
                f"{pfx} — {player} requires medical attention for a {severity}. Thankfully for {team}, he returns to his feet and resumes, but the interruption breaks the rhythm.",
            ])
        if payload.get("substituted"):
            return _pick(rng, [
                f"{pfx} — Injury setback for {team}. {player} cannot shake off a {severity} and is replaced, prompting a forced adjustment to personnel and possibly tactics.",
                f"{pfx} — {player} is unable to continue after a {severity}. {team} must adapt quickly with an enforced substitution in this phase of the match.",
            ])
        return _pick(rng, [
            f"{pfx} — {player} ({team}) is forced off with a {severity}, leaving his side to reorganize under pressure at {score}.",
            f"{pfx} — Blow for {team}: {player} cannot continue due to a {severity}, creating both a fitness and tactical concern.",
        ])

    if et == E.SUBSTITUTION:
        on_name = payload.get("on_name", "a substitute")
        off_name = payload.get("off_name", player)
        reason = payload.get("reason")
        reason_txt = f" to provide {reason}" if reason else ""
        return _pick(rng, [
            f"{pfx} — Substitution for {team}: {on_name} comes on for {off_name}{reason_txt}. A change that may influence energy levels, matchups, and tactical balance.",
            f"{pfx} — {team} make a change {flav}. {off_name} makes way and {on_name} enters the action{reason_txt}, with fresh legs and new instructions.",
            f"{pfx} — Personnel adjustment by {team}: {on_name} replaces {off_name}{reason_txt}, potentially altering pressing, width, or attacking threat.",
        ])

    if et == E.TACTICAL_CHANGE:
        summary = payload.get("summary", "a tactical adjustment")
        return _pick(rng, [
            f"{pfx} — {team} adjust their approach: {summary}. The change in shape, pressing intensity, or transition plan could alter how the next phase unfolds against {opp}.",
            f"{pfx} — Tactical tweak from {team} — {summary}. It signals a clear response to the current game state at {score} and the patterns seen so far.",
            f"{pfx} — {team} modify their setup ({summary}), aiming to gain better control of key zones and create more favorable matchups.",
        ])

    if et == E.VAR_REVIEW:
        reason = payload.get("reason", "a possible infringement")
        return _pick(rng, [
            f"{pfx} — VAR review in progress for {reason}. Play is paused while the officials examine the incident in detail, with both sets of players waiting anxiously.",
            f"{pfx} — The referee is asked to review a potential incident ({reason}). A careful check now, as the decision could have significant impact at {score}.",
        ])

    if et == E.VAR_OVERTURN:
        outcome = payload.get("outcome", "Decision updated")
        reason = payload.get("reason", "after review")
        return _pick(rng, [
            f"{pfx} — VAR decision confirmed: {outcome} {reason}. The match resumes with clarity, but the implications of that call may linger.",
            f"{pfx} — After review, the official communicates the outcome: {outcome} {reason}. A consequential decision in the context of this game.",
        ])

    return _pick(rng, [
        f"{pfx} — {team} and {opp} continue to contest every inch {flav}, with tactical discipline and execution in transitions shaping this phase at {score}.{t_hint}",
        f"{pfx} — The battle for control remains finely balanced. {team} look to execute their plan while {opp} respond with organization and selective pressure.{s_hint}",
        f"{pfx} — Play flows on at {score}. Spacing, timing, and decision-making in possession are defining the rhythm of this spell between {team} and {opp}.",
    ])
