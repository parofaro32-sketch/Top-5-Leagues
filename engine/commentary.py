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
        return _pick(rng, ["in the opening exchanges", "early in the contest", "in the early stages", "as the match settles"])
    if m < 30:
        return _pick(rng, ["as the first half develops", "with the game finding its rhythm", "midway through the first half"])
    if m < 45:
        return _pick(rng, ["approaching the interval", "late in the first half", "as half-time nears"])
    if m < 60:
        return _pick(rng, ["after the restart", "early in the second half", "as the second period unfolds"])
    if m < 75:
        return _pick(rng, ["with time ticking on", "as the match enters a decisive phase", "in the middle of the second half"])
    if m < 90:
        return _pick(rng, ["in the closing stages", "late on", "as the clock winds down", "with the finish line in sight"])
    return _pick(rng, ["deep into stoppage time", "in added time", "with seconds draining away"])


def generate(state: MatchState, event: dict[str, Any], rng: random.Random) -> str:
    payload = event.get("payload") or {}
    # Prefer engine-provided detailed text when available
    detail = payload.get("detail")
    if detail:
        txt = str(detail)
        if not txt.startswith(("0'", "1'", "2'", "3'", "4'", "5'", "6'", "7'", "8'", "9'")):
            return f"{_prefix(state, event)} — {txt}"
        return txt

    et = event.get("event_type")
    side = event.get("team_side")
    team = _team(state, side)
    opp = _opp(state, side)
    player = _player(state, side, event.get("player_api_id"), payload)
    pfx = _prefix(state, event)
    flav = _phase_flavor(state, rng)
    score = _score(state)

    if et == E.KICKOFF:
        return f"{pfx} — Kick-off at a lively tempo. {team} take the first touch and look to set the tone against {opp}."
    if et == E.SECOND_HALF_KICKOFF:
        return f"{pfx} — The second half is underway. {team} restart play with the score at {score}, and both benches will be watching closely."
    if et == E.HALF_TIME:
        return f"{pfx} — Half-time whistle. {score}. A chance now for both managers to assess shape, energy, and matchups."
    if et == E.FULL_TIME:
        return f"{pfx} — Full-time. {score}. A result shaped by key moments, discipline, and execution in both boxes."
    if et == E.STOPPAGE_TIME:
        mins = payload.get("minutes", 1)
        half = payload.get("half", "this half")
        return _pick(rng, [
            f"{pfx} — The fourth official signals {mins} additional minute(s) at the end of {half}.",
            f"{pfx} — {mins} minute(s) of stoppage time indicated. There is still time for a twist at the end of {half}.",
        ])
    if et == E.TACTICAL_BREAK:
        label = payload.get("label", "Tactical break")
        return _pick(rng, [
            f"{pfx} — {label}. Play is paused for drinks and tactical instructions on both touchlines.",
            f"{pfx} — {label}. The tempo drops briefly as managers gather their players for adjustments.",
            f"{pfx} — {label}. A crucial window for {team if side else 'both sides'} to reset shape and instructions.",
        ])
    if et == E.BREAK_RESUME:
        return _pick(rng, [
            f"{pfx} — Play resumes. The tactical tweaks will be tested immediately as intensity picks up again.",
            f"{pfx} — Back underway {flav}. Both teams look to impose the adjustments made during the break.",
            f"{pfx} — The match restarts with {score} on the board and fresh instructions in play.",
        ])

    if et == E.PASS_SEQUENCE:
        return _pick(rng, [
            f"{pfx} — {team} circulate possession through midfield, switching angles as {opp} hold their block.",
            f"{pfx} — Patient build-up from {team} {flav}; they work the ball side to side, probing for a gap in {opp}'s shape.",
            f"{pfx} — {team} keep the ball moving with short combinations, trying to draw {opp} out before playing forward.",
            f"{pfx} — A spell of controlled possession for {team}, but {opp} stay compact and deny space between the lines.",
            f"{pfx} — {team} recycle play from back to front, looking for a runner while {opp} track diligently.",
            f"{pfx} — {team} vary the tempo in midfield, mixing quick one-touch passes with a slower, deliberate rhythm.",
            f"{pfx} — {opp} press in bursts, yet {team} manage to retain possession and reset the attack.",
            f"{pfx} — {team} probe down the flank and then cut back inside, but {opp}'s defensive spacing remains disciplined.",
        ])

    if et == E.CHANCE:
        return _pick(rng, [
            f"{pfx} — Promising move from {team}. {player} finds room in a dangerous pocket and the attack suddenly looks alive.",
            f"{pfx} — {team} carve out an opening {flav} as {player} drifts into space between defenders.",
            f"{pfx} — A sharp sequence from {team} releases {player} in an advanced area, forcing {opp} to scramble.",
            f"{pfx} — {team} unlock space with a clever pass and {player} is suddenly in position to threaten.",
        ])

    if et == E.SHOT:
        outcome = payload.get("outcome", "off_target")
        if outcome == "on_target":
            return _pick(rng, [
                f"{pfx} — {player} gets a clean strike away for {team}, directing it on target and asking a real question of the goalkeeper.",
                f"{pfx} — Effort on goal by {player}. It stays low and true, testing the keeper's handling.",
                f"{pfx} — {player} shoots through traffic for {team}; it's on target and carries enough pace to be dangerous.",
            ])
        if outcome == "blocked":
            return _pick(rng, [
                f"{pfx} — {player} pulls the trigger for {team}, but a defender throws themselves in the way to make a block.",
                f"{pfx} — Shot from {player} is charged down inside the area as {opp} defend desperately.",
                f"{pfx} — {player} finds a shooting lane, yet the effort is deflected behind by a committed block.",
            ])
        return _pick(rng, [
            f"{pfx} — {player} tries his luck for {team} {flav}, but the attempt drifts wide of the post.",
            f"{pfx} — Sight of goal for {player}, who shoots from a decent position but cannot keep it down.",
            f"{pfx} — {player} snatches at the chance and the effort sails over the bar for {team}.",
            f"{pfx} — A half-opening for {team}, but {player}'s finish lacks precision and misses the target.",
        ])

    if et == E.SAVE:
        shooter = payload.get("shooter_name", "the attacker")
        return _pick(rng, [
            f"{pfx} — Big save. {shooter} connects well for {opp}, but {player} reacts sharply to keep {team} level in the moment.",
            f"{pfx} — {player} is equal to it, getting down quickly to deny {shooter}'s on-target effort.",
            f"{pfx} — Strong hands from {player}, who parries {shooter}'s strike away from danger.",
            f"{pfx} — Excellent goalkeeping. {player} reads the shot from {shooter} and makes an important stop for {team}.",
        ])

    if et == E.BLOCK:
        return _pick(rng, [
            f"{pfx} — Crucial intervention. {player} throws a body in front of the shot to deny {opp} at close range.",
            f"{pfx} — {player} makes a last-ditch block for {team}, absorbing the power and snuffing out the danger.",
            f"{pfx} — Brave defending from {player}, who blocks the effort and preserves the shape for {team}.",
        ])

    if et == E.CORNER:
        return _pick(rng, [
            f"{pfx} — Corner to {team} as pressure in the final third forces {opp} to concede territory.",
            f"{pfx} — {team} win a corner {flav}; bodies gather in the box for the delivery.",
            f"{pfx} — The attack is turned behind and {team} will have a chance to load the penalty area from the corner.",
        ])

    if et == E.FOUL:
        fouled = payload.get("fouled_name", "an opponent")
        reason = payload.get("reason", "a mistimed challenge")
        return _pick(rng, [
            f"{pfx} — Foul by {player} on {fouled} ({reason}). Free kick to {opp} in a useful position.",
            f"{pfx} — The referee blows for a foul after {player} catches {fouled}. {opp} can reset from the set piece.",
            f"{pfx} — {player} stops the advance illegally with {reason}, and {opp} are awarded a free kick.",
        ])

    if et == E.FREE_KICK:
        return _pick(rng, [
            f"{pfx} — Free kick for {team} in a promising area. {player} stands over it, weighing up delivery or shot.",
            f"{pfx} — {team} have a set-piece opportunity {flav}; {player} and teammates discuss options as the wall forms.",
        ])

    if et == E.PENALTY_AWARDED:
        reason = payload.get("reason", "a foul in the box")
        return _pick(rng, [
            f"{pfx} — Penalty to {team}! The referee points to the spot after {reason}. Huge moment in this match.",
            f"{pfx} — Spot kick awarded for {team}. Contact in the area is punished for {reason}, and the pressure is on.",
        ])

    if et == E.GOAL:
        assist = payload.get("assist_name")
        a = f" after a smart pass from {assist}" if assist else ""
        return _pick(rng, [
            f"{pfx} — GOAL! {player} finds the net for {team}{a}. {score}.",
            f"{pfx} — GOAL for {team}! {player} finishes clinically{a} and the score moves to {score}.",
            f"{pfx} — It's in! {player} scores for {team}{a} {flav}. {score}.",
        ])

    if et == E.PENALTY_SCORED:
        return _pick(rng, [
            f"{pfx} — GOAL! {player} sends the goalkeeper the wrong way and converts the penalty for {team}. {score}.",
            f"{pfx} — Cool from the spot. {player} buries the penalty for {team}. {score}.",
        ])

    if et == E.PENALTY_SAVED:
        taker = payload.get("taker_name", "the penalty taker")
        return _pick(rng, [
            f"{pfx} — Penalty saved! {player} dives the right way and denies {taker} with a superb stop.",
            f"{pfx} — Brilliant goalkeeping from {player}, who keeps out {taker}'s penalty and ignites a reaction.",
        ])

    if et == E.PENALTY_MISSED:
        return _pick(rng, [
            f"{pfx} — Penalty missed. {player} cannot convert for {team} and the chance goes begging.",
            f"{pfx} — Agony for {team} as {player}'s penalty fails to find the target.",
        ])

    if et == E.DISALLOWED_GOAL:
        reason = payload.get("reason", "an infringement")
        return _pick(rng, [
            f"{pfx} — Goal disallowed for {team} due to {reason}. The score remains {score}.",
            f"{pfx} — The celebrations are cut short. {player}'s effort is ruled out for {team} ({reason}). {score}.",
        ])

    if et == E.OFFSIDE:
        return _pick(rng, [
            f"{pfx} — The flag goes up. {player} is caught offside just as {team} looked to break in behind.",
            f"{pfx} — Offside against {team}. {player}'s run is mistimed and {opp} get the free kick.",
        ])

    if et == E.YELLOW_CARD:
        reason = payload.get("reason", "a foul")
        return _pick(rng, [
            f"{pfx} — Yellow card for {player} ({team}) after {reason}. The referee reaches for the notebook.",
            f"{pfx} — Booking for {player}. A caution for {reason} as {team} are warned to stay disciplined.",
        ])

    if et == E.SECOND_YELLOW:
        reason = payload.get("reason", "another foul")
        return _pick(rng, [
            f"{pfx} — Second yellow card for {player} ({team}) after {reason}. He is sent off and {team} are down to fewer players.",
            f"{pfx} — Dismissal! {player} picks up a second booking for {team} and must leave the pitch.",
        ])

    if et == E.RED_CARD:
        reason = payload.get("reason", "serious foul play")
        return _pick(rng, [
            f"{pfx} — Straight red card! {player} ({team}) is dismissed for {reason}. A major turning point.",
            f"{pfx} — Red card shown to {player} of {team} for {reason}. {team} face an uphill task now.",
        ])

    if et == E.INJURY:
        severity = payload.get("severity", "knock")
        if payload.get("continued"):
            return _pick(rng, [
                f"{pfx} — Concern for {team} as {player} goes down with a {severity}, but after treatment he is fit to continue.",
                f"{pfx} — {player} receives attention for a {severity}. Thankfully for {team}, he gets back to his feet and carries on.",
            ])
        if payload.get("substituted"):
            return _pick(rng, [
                f"{pfx} — Injury setback for {team}. {player} cannot shake off a {severity} and has to be replaced.",
                f"{pfx} — {player} is unable to continue after a {severity}, forcing {team} into an enforced change.",
            ])
        return _pick(rng, [
            f"{pfx} — {player} ({team}) is forced off with a {severity}, leaving his side to reshuffle.",
            f"{pfx} — Blow for {team}: {player} cannot continue due to a {severity}.",
        ])

    if et == E.SUBSTITUTION:
        on_name = payload.get("on_name", "a substitute")
        off_name = payload.get("off_name", player)
        reason = payload.get("reason")
        reason_txt = f" ({reason})" if reason else ""
        return _pick(rng, [
            f"{pfx} — Substitution for {team}: {on_name} comes on for {off_name}{reason_txt}.",
            f"{pfx} — Change for {team} {flav}. {off_name} makes way and {on_name} enters the action{reason_txt}.",
            f"{pfx} — {team} refresh their side: {on_name} replaces {off_name}{reason_txt}.",
        ])

    if et == E.TACTICAL_CHANGE:
        summary = payload.get("summary", "a tactical adjustment")
        return _pick(rng, [
            f"{pfx} — {team} adjust their setup: {summary}. The shape and pressing cues look different now.",
            f"{pfx} — Tactical tweak from {team} — {summary}. It will be interesting to see how {opp} respond.",
            f"{pfx} — {team} make a change in approach ({summary}), aiming to shift control of key zones.",
        ])

    if et == E.VAR_REVIEW:
        reason = payload.get("reason", "a possible infringement")
        return _pick(rng, [
            f"{pfx} — VAR review in progress for {reason}. The referee awaits confirmation as players hold their breath.",
            f"{pfx} — Play is paused for a VAR check regarding {reason}. Tension around the ground.",
        ])

    if et == E.VAR_OVERTURN:
        outcome = payload.get("outcome", "Decision updated")
        reason = payload.get("reason", "after review")
        return _pick(rng, [
            f"{pfx} — VAR decision: {outcome} {reason}.",
            f"{pfx} — After review, the official confirms: {outcome} {reason}.",
        ])

    return _pick(rng, [
        f"{pfx} — {team} and {opp} continue to battle for control {flav}, with little separating the sides in this phase.",
        f"{pfx} — The intensity remains high as {team} and {opp} contest every second ball.",
        f"{pfx} — Play flows on at {score}, with both teams alert to the next mistake or opening.",
    ])
