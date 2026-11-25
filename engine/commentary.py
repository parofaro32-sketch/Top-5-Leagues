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


def _short_score(state: MatchState) -> str:
    return f"{state.home.score}-{state.away.score}"


def _tref(state: MatchState, side: str | None, rng: random.Random) -> str:
    if side == "home":
        return _pick(rng, [state.home.team.name, state.home.team.name, "the hosts", state.home.team.name])
    if side == "away":
        return _pick(rng, [state.away.team.name, state.away.team.name, "the visitors", state.away.team.name])
    return "the side"


def _oref(state: MatchState, side: str | None, rng: random.Random) -> str:
    if side in {"home", "away"}:
        opp = state.team(state.opp_side(side)).team.name
        label = "the visitors" if side == "home" else "the hosts"
        return _pick(rng, [opp, opp, opp, label])
    return "the opposition"


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
            "during the first tentative minutes, each side feeling the other out",
        ])
    if m < 30:
        return _pick(rng, [
            "as the first half develops and tactical shapes become clearer",
            "with the game settling into a defined rhythm",
            "midway through the first half as both teams probe for control",
            "as possession and territory start to ebb and flow",
            "with the contest taking shape and midfield duels intensifying",
        ])
    if m < 45:
        return _pick(rng, [
            "approaching the interval with intensity rising",
            "late in the first half as both sides look for an edge",
            "as half-time nears and concentration becomes crucial",
            "with the first period moving toward its conclusion",
            "in the final minutes before the break, where focus is vital",
        ])
    if m < 60:
        return _pick(rng, [
            "after the restart, with renewed energy and adjusted instructions",
            "early in the second half as the tactical battle evolves",
            "as the second period unfolds and spaces begin to open",
            "following the break, with both teams pushing their game plan",
            "with fresh impetus after the interval and new patterns emerging",
        ])
    if m < 75:
        return _pick(rng, [
            "with time ticking on and the match entering a decisive phase",
            "as the contest tightens in a crucial spell",
            "in the middle of the second half where control and transitions matter most",
            "as fatigue and tactical tweaks start to influence the flow",
            "with the game finely poised and every sequence carrying extra weight",
        ])
    if m < 90:
        return _pick(rng, [
            "in the closing stages, where every decision carries extra weight",
            "late on as the pressure builds around every duel",
            "as the clock winds down and urgency grows",
            "with the finish line in sight and margins razor thin",
            "in the dying minutes of normal time, tension at its peak",
        ])
    return _pick(rng, [
        "deep into stoppage time with nerves on edge",
        "in added time as one more moment could decide everything",
        "with seconds draining away in a tense finale",
        "in the final breaths of stoppage time, every action magnified",
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
                f" The scoreboard reads {_short_score(state)}, and the momentum swings sharply.",
                f" At {_short_score(state)}, that moment carries real tactical weight.",
                f" You can feel the emotional shift ripple across the pitch.",
                f" A defining flashpoint at {_short_score(state)} with consequences for both benches.",
                f" The stadium atmosphere changes instantly as {_short_score(state)} frames the contest.",
                "",
            ])
            if extra and extra.strip() not in txt:
                txt += extra
        elif et in {E.CHANCE, E.SAVE, E.BLOCK} and rng.random() < 0.5:
            extra = _pick(rng, [
                " The movement and execution in that sequence stood out.",
                " Fine margins in both boxes are proving decisive.",
                " Both benches react immediately to the danger.",
                " A sharp reminder of how quickly the balance can tilt.",
                " The technical quality in that phase was clear to see.",
                " That sequence exposed the defensive spacing for a split second.",
                " The tempo of the attack caught the back line off guard.",
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
    tref = _tref(state, side, rng)
    oref = _oref(state, side, rng)
    player = _player(state, side, event.get("player_api_id"), payload)
    pfx = _prefix(state, event)
    flav = _phase_flavor(state, rng)
    score = _score(state)
    ss = _short_score(state)
    t_hint = _tactic_hint(state, side, rng)
    s_hint = _state_hint(state, side, rng)

    if et == E.KICKOFF:
        return _pick(rng, [
            f"{pfx} — Kick-off! {team} get us underway against {opp}. Early shape and tempo will set the tone.",
            f"{pfx} — We're underway at {ss}. {tref.capitalize()} take the first touch, looking to impose their plan on {oref}.",
            f"{pfx} — The referee blows, and {team} start the match against {opp}. The opening minutes will reveal each side's intent.",
        ])
    if et == E.SECOND_HALF_KICKOFF:
        return _pick(rng, [
            f"{pfx} — Second half begins at {ss}. {tref.capitalize()} restart, and any interval adjustments are now under pressure.",
            f"{pfx} — Back out for the second period. {team} kick off with the score {ss}; the next phase should reveal who adapted better.",
            f"{pfx} — The second 45 is underway. At {ss}, tactical changes from the break could reshape the contest.",
        ])
    if et == E.HALF_TIME:
        return _pick(rng, [
            f"{pfx} — Half-time: {score}. A chance for both managers to reassess pressing cues, spacing, and transitions.",
            f"{pfx} — The whistle goes for the break at {ss}. Tactical tweaks and fresh legs could define the second half.",
            f"{pfx} — Half-time arrives with the score {ss}. The first period offered clues; the second will demand answers.",
        ])
    if et == E.FULL_TIME:
        return _pick(rng, [
            f"{pfx} — Full-time: {score}. Execution in key moments and tactical discipline settle the contest.",
            f"{pfx} — It's over at {ss}. {state.home.team.name} and {state.away.team.name} leave everything on the pitch after a hard-fought battle.",
            f"{pfx} — The final whistle confirms {score}. A match defined by key duels, transitions, and decisive moments in both boxes.",
        ])
    if et == E.STOPPAGE_TIME:
        mins = payload.get("minutes", 1)
        half = payload.get("half", "this half")
        return _pick(rng, [
            f"{pfx} — {mins} minute(s) of added time at the end of {half}. At {ss}, every touch matters.",
            f"{pfx} — The board goes up: {mins} additional minute(s). Still time for a late twist with the score {ss}.",
            f"{pfx} — {mins} minute(s) indicated. With {ss} on the board, the closing moments carry huge tension.",
        ])
    if et == E.TACTICAL_BREAK:
        label = payload.get("label", "Tactical break")
        return _pick(rng, [
            f"{pfx} — {label}. A brief pause for detailed instructions on shape, pressing triggers, and energy management.",
            f"{pfx} — {label}. Both benches use the stoppage to fine-tune matchups and attacking patterns.",
            f"{pfx} — {label}. Managers gather their players, adjusting structure and targeting weaknesses before play resumes.",
        ])
    if et == E.BREAK_RESUME:
        return _pick(rng, [
            f"{pfx} — Play resumes {flav}. The tweaks just made will be tested immediately.{s_hint}",
            f"{pfx} — Back underway at {ss}. Expect subtle changes in pressing height and buildup as play continues.{s_hint}",
            f"{pfx} — The ball is rolling again at {ss}. Fresh instructions meet on-field reality {flav}.{s_hint}",
        ])

    if et == E.PASS_SEQUENCE:
        return _pick(rng, [
            f"{pfx} — {tref.capitalize()} circulate possession patiently, probing for a gap between the lines.{t_hint}{s_hint}",
            f"{pfx} — {flav.capitalize()}, {tref} build methodically while {oref} hold their shape and wait for a pressing trigger.{t_hint}",
            f"{pfx} — Controlled spell: {tref} recycle through midfield, testing {oref}'s compactness without forcing the final ball.{t_hint}",
            f"{pfx} — {tref.capitalize()} switch the point of attack, pulling {oref} across before looking to progress.{s_hint}",
            f"{pfx} — {oref.capitalize()} press in phases, but {tref} stay composed and reset the attack with calm passing.{t_hint}",
            f"{pfx} — A patient buildup: support angles, quick touches, and careful tempo as {tref} search for an opening.",
            f"{pfx} — {tref.capitalize()} work it wide, then back inside, keeping {oref} moving at {ss}.",
            f"{pfx} — {tref.capitalize()} string passes together in midfield, looking for a runner beyond {oref}'s defensive line.{t_hint}",
            f"{pfx} — Neat possession from {tref}, who move the ball with purpose and wait for {oref} to overcommit.{s_hint}",
            f"{pfx} — {tref.capitalize()} probe down the flank, combining neatly before {oref} close the passing lane.{t_hint}",
            f"{pfx} — Measured buildup from {tref}: one-touch combinations draw {oref} out, then the ball is recycled to keep control.{s_hint}",
            f"{pfx} — {tref.capitalize()} keep the ball moving, switching tempo to unsettle {oref}'s midfield screen.{t_hint}",
        ])

    if et == E.CHANCE:
        return _pick(rng, [
            f"{pfx} — CHANCE! {player} finds space in a dangerous area for {tref}, and {oref} scramble to recover.{s_hint}",
            f"{pfx} — CHANCE! A sharp sequence opens up {oref}, and {player} suddenly has a promising sight of goal.{s_hint}",
            f"{pfx} — CHANCE! {tref.capitalize()} carve through with incisive movement — {player} attacks the gap and the crowd senses danger.",
            f"{pfx} — Big opening! {player} receives between the lines and {oref} are backpedaling toward their own box.{s_hint}",
            f"{pfx} — CHANCE! Quick interplay releases {player} in a threatening pocket. The defensive line is stretched and vulnerable.",
            f"{pfx} — A golden opening for {team}! {player} bursts into space after clever movement, putting {oref} under immediate pressure.",
            f"{pfx} — CHANCE! {player} times the run perfectly, collecting possession in a high-value area with defenders scrambling.",
            f"{pfx} — {tref.capitalize()} unlock the defense — {player} has a real opportunity as the back line loses its shape.{s_hint}",
        ])

    if et == E.SHOT:
        outcome = payload.get("outcome", "off_target")
        if outcome == "on_target":
            return _pick(rng, [
                f"{pfx} — {player} strikes cleanly for {tref}, forcing the goalkeeper into a sharp reaction.",
                f"{pfx} — Well-hit effort by {player}! On target, with real intent behind it — {oref} are briefly opened up.",
                f"{pfx} — {player} lets fly and tests the keeper. Good technique, firm contact, and a moment of concern for {oref}.",
                f"{pfx} — Shot on target from {player}. The goalkeeper has to be alert to deal with it.",
                f"{pfx} — {player} drives one goalward with purpose, making the keeper work for {oref}.",
                f"{pfx} — Crisp strike by {player} — on frame and asking questions of the goalkeeper at {ss}.",
            ])
        if outcome == "blocked":
            return _pick(rng, [
                f"{pfx} — {player} gets a shot away, but a defender throws himself into the line for a vital block.",
                f"{pfx} — Blocked! {player}'s effort is charged down inside a crowded area — brave defending by {oref}.",
                f"{pfx} — {player} shoots through bodies, but {oref} get a crucial block in to snuff out the danger.",
                f"{pfx} — A deflection off a defender takes the sting out of {player}'s attempt. Important intervention.",
                f"{pfx} — {player} pulls the trigger, only for a sliding block to deny the shot path.",
            ])
        return _pick(rng, [
            f"{pfx} — {player} finds a shooting angle {flav}, but the effort drifts off target.",
            f"{pfx} — A glimpse of goal for {player}, but under pressure the finish misses the frame.",
            f"{pfx} — {tref.capitalize()} work an opening, yet {player}'s attempt lacks precision and {oref} survive.",
            f"{pfx} — {player} goes for goal from a decent position but can't keep it down. A let-off for {oref}.",
            f"{pfx} — {player} tries his luck, sending the attempt wide of the post as the chance goes begging.",
            f"{pfx} — The shot from {player} rises over the bar — promising buildup, but the finish doesn't match.",
        ])

    if et == E.SAVE:
        shooter = payload.get("shooter_name", "the attacker")
        return _pick(rng, [
            f"{pfx} — Outstanding save! {player} shows sharp reflexes to deny {shooter} and keep {tref} in it.",
            f"{pfx} — Crucial stop by {player}, reading {shooter}'s effort early and producing a composed save under pressure.",
            f"{pfx} — Big save! {player} gets down quickly to parry {shooter}'s strike away from danger.",
            f"{pfx} — {player} comes up huge for {tref}, reacting brilliantly to keep {shooter}'s on-target effort out.",
            f"{pfx} — Brilliant goalkeeping. {player} stretches to tip {shooter}'s attempt away, preserving the {ss} scoreline.",
            f"{pfx} — {player} reads it all the way, smothering {shooter}'s shot with confident handling.",
        ])

    if et == E.BLOCK:
        return _pick(rng, [
            f"{pfx} — Heroic block by {player}! Body on the line to prevent a clear scoring chance for {oref}.",
            f"{pfx} — Last-ditch defending from {player}, who anticipates the strike and intervenes decisively.",
            f"{pfx} — Crucial block! {player} throws himself in front of the shot, sacrificing his body for {tref}.",
            f"{pfx} — {player} makes a brave intervention, blocking the effort at close range to keep {oref} out.",
        ])

    if et == E.CORNER:
        return _pick(rng, [
            f"{pfx} — Corner to {tref} after sustained pressure forces {oref} to turn it behind. Set-piece chance incoming.",
            f"{pfx} — {tref.capitalize()} win a corner {flav}. Delivery and movement in the box could be decisive.",
            f"{pfx} — Corner kick for {tref}. {oref.capitalize()} clear for now, but the set piece loads the box again.",
            f"{pfx} — {team} earn a corner — a chance to test {oref}'s aerial organization and marking.",
            f"{pfx} — The attack ends with a corner for {tref}. Runners gather in the box as {oref} set up to defend.",
        ])

    if et == E.FOUL:
        fouled = payload.get("fouled_name", "an opponent")
        reason = payload.get("reason", "a mistimed challenge")
        return _pick(rng, [
            f"{pfx} — Foul by {player} on {fouled} ({reason}). {oref.capitalize()} get the free kick.",
            f"{pfx} — {player} mistimes the challenge on {fouled} and the referee whistles for {reason}.",
            f"{pfx} — Free kick to {oref} after {player} catches {fouled} — {reason}.",
            f"{pfx} — The referee stops play: {player} is penalized for {reason} on {fouled}.",
        ])

    if et == E.FREE_KICK:
        return _pick(rng, [
            f"{pfx} — Free kick in a promising area for {tref}. {player} stands over it — delivery or direct effort?",
            f"{pfx} — Set-piece opportunity for {team} {flav}. {oref.capitalize()} organize the wall as {player} prepares.",
            f"{pfx} — Dangerous free-kick position. {player} has options in the box and a shooting angle to consider.",
            f"{pfx} — {tref.capitalize()} have a free kick in a useful spot. {player} eyes up the delivery as targets move into position.",
        ])

    if et == E.PENALTY_AWARDED:
        reason = payload.get("reason", "a foul in the box")
        return _pick(rng, [
            f"{pfx} — PENALTY to {team}! After {reason}, the referee points to the spot. Huge moment at {ss}.",
            f"{pfx} — Spot kick for {tref}! {reason.capitalize() if reason else 'A foul in the box'} and the pressure is immense at {ss}.",
            f"{pfx} — The referee has no hesitation — penalty for {team}! {reason.capitalize() if reason else 'Foul in the area'} gives them a golden chance.",
        ])

    if et == E.GOAL:
        assist = payload.get("assist_name")
        a = f", assisted by {assist}" if assist else ""
        return _pick(rng, [
            f"{pfx} — GOAL! {player} finishes with conviction for {team}{a}. {score}.",
            f"{pfx} — GOAL for {tref}! {player} delivers the decisive touch{a}, and the match shifts to {ss}.",
            f"{pfx} — It's in! {player} scores for {team}{a} — a clinical finish that changes the picture at {ss}.",
            f"{pfx} — GOAL! {player} finds the net for {team}{a}. Composed finish, and the scoreline moves to {score}.",
            f"{pfx} — What a moment! {player} converts for {tref}{a}, sparking celebrations as the board shows {ss}.",
        ])

    if et == E.PENALTY_SCORED:
        return _pick(rng, [
            f"{pfx} — GOAL! {player} holds his nerve from the spot and converts for {team}. {score}.",
            f"{pfx} — Cool as you like. {player} buries the penalty for {tref}. {ss}.",
            f"{pfx} — Penalty dispatched! {player} sends the keeper the wrong way and makes it {ss}.",
        ])

    if et == E.PENALTY_SAVED:
        taker = payload.get("taker_name", "the penalty taker")
        return _pick(rng, [
            f"{pfx} — Penalty saved! {player} reads {taker} brilliantly and produces a decisive stop.",
            f"{pfx} — Huge save from {player}! {taker}'s penalty is kept out — a massive moment at {ss}.",
            f"{pfx} — Denied! {player} guesses right and keeps out {taker}'s spot kick with a strong hand.",
        ])

    if et == E.PENALTY_MISSED:
        return _pick(rng, [
            f"{pfx} — Penalty missed! {player} can't convert for {tref}, and a golden chance slips away.",
            f"{pfx} — Agony for {team} — {player}'s spot kick fails to find the target at {ss}.",
            f"{pfx} — Off target from the spot! {player} wastes the penalty and {oref} breathe a huge sigh of relief.",
        ])

    if et == E.DISALLOWED_GOAL:
        reason = payload.get("reason", "an infringement")
        return _pick(rng, [
            f"{pfx} — Goal disallowed for {team} due to {reason}. The score remains {ss}.",
            f"{pfx} — Ruled out! {player}'s effort for {tref} is chalked off ({reason}). Fine margins at {ss}.",
            f"{pfx} — The celebrations are cut short — {team} have a goal ruled out for {reason}. Still {ss}.",
        ])

    if et == E.OFFSIDE:
        return _pick(rng, [
            f"{pfx} — Offside against {player}. {tref.capitalize()}'s move had promise, but the run was just early.",
            f"{pfx} — Flag up — {player} is caught offside as {tref} try to release him behind the line.",
            f"{pfx} — The assistant's flag halts the attack: {player} timed the run a fraction too soon.",
        ])

    if et == E.YELLOW_CARD:
        reason = payload.get("reason", "a foul")
        return _pick(rng, [
            f"{pfx} — Yellow card for {player} ({team}) for {reason}. He'll need to be careful now.",
            f"{pfx} — Booking. {player} goes into the book after {reason} — a warning {flav}.",
            f"{pfx} — The referee shows yellow to {player} for {reason}. {tref.capitalize()} must manage that risk.",
            f"{pfx} — Caution for {player} ({team}). A mistimed moment leads to a booking for {reason}.",
        ])

    if et == E.SECOND_YELLOW:
        reason = payload.get("reason", "another foul")
        return _pick(rng, [
            f"{pfx} — Second yellow for {player} ({team}) after {reason} — and that's a red! A major blow at {ss}.",
            f"{pfx} — Sent off! {player} picks up a second booking and {tref} are down to ten.",
            f"{pfx} — Two yellows, one red. {player} is dismissed for {team}, leaving them short-handed at {ss}.",
        ])

    if et == E.RED_CARD:
        reason = payload.get("reason", "serious foul play")
        return _pick(rng, [
            f"{pfx} — Straight red! {player} ({team}) is dismissed for {reason}. The match is reshaped at {ss}.",
            f"{pfx} — Red card for {player}. {tref.capitalize()} must reorganize after that dismissal for {reason}.",
            f"{pfx} — The referee reaches for red. {player} is sent off for {reason}, a pivotal moment at {ss}.",
        ])

    if et == E.INJURY:
        severity = payload.get("severity", "knock")
        if payload.get("continued"):
            return _pick(rng, [
                f"{pfx} — Concern as {player} goes down with a {severity}. After treatment, he's able to continue for {tref}.",
                f"{pfx} — {player} needs medical attention ({severity}), but thankfully returns to the pitch.",
                f"{pfx} — A brief stoppage as {player} receives treatment for a {severity}. He's fit to carry on.",
            ])
        if payload.get("substituted"):
            return _pick(rng, [
                f"{pfx} — Injury blow for {team}: {player} can't shake off a {severity} and is replaced.",
                f"{pfx} — {player} is unable to continue after a {severity}. Forced change for {tref}.",
                f"{pfx} — {team} are forced into a change — {player} comes off with a {severity} and can't continue.",
            ])
        return _pick(rng, [
            f"{pfx} — {player} ({team}) is forced off with a {severity}, leaving {tref} to reorganize at {ss}.",
            f"{pfx} — Injury setback: {player} can't continue after a {severity}, a tough blow for {team}.",
        ])

    if et == E.SUBSTITUTION:
        on_name = payload.get("on_name", "a substitute")
        off_name = payload.get("off_name", player)
        reason = payload.get("reason")
        reason_txt = f" to provide {reason}" if reason else ""
        return _pick(rng, [
            f"{pfx} — Substitution for {team}: {on_name} replaces {off_name}{reason_txt}. Fresh legs and new instructions.",
            f"{pfx} — Change for {tref}: {on_name} comes on for {off_name}{reason_txt}, {flav}.",
            f"{pfx} — {team} make a switch — {off_name} off, {on_name} on{reason_txt}. A tactical tweak in personnel.",
            f"{pfx} — {tref.capitalize()} turn to the bench: {on_name} enters for {off_name}{reason_txt} at {ss}.",
        ])

    if et == E.TACTICAL_CHANGE:
        return _pick(rng, [
            f"{pfx} — {tref.capitalize()} make a tactical adjustment on the touchline {flav}.",
            f"{pfx} — A tactical tweak from {team} as they look to influence the next phase at {ss}.",
            f"{pfx} — {tref.capitalize()} adjust their approach as the match situation evolves.",
            f"{pfx} — Instructions from the bench: {team} reshape their setup to respond to the current game state.",
        ])

    if et == E.VAR_REVIEW:
        reason = payload.get("reason", "a possible infringement")
        return _pick(rng, [
            f"{pfx} — VAR review in progress for {reason}. Play is paused while officials examine the incident.",
            f"{pfx} — The referee is sent to check a potential incident ({reason}). A consequential decision could follow at {ss}.",
            f"{pfx} — VAR is checking {reason}. Tension builds as the decision is reviewed at {ss}.",
        ])

    if et == E.VAR_OVERTURN:
        outcome = payload.get("outcome", "Decision updated")
        reason = payload.get("reason", "after review")
        return _pick(rng, [
            f"{pfx} — VAR decision: {outcome} {reason}. The match resumes with clarity at {ss}.",
            f"{pfx} — After review, the official confirms: {outcome} {reason}. A big call in this contest.",
            f"{pfx} — The verdict is in — {outcome} {reason}. Play continues at {ss} with the decision settled.",
        ])

    return _pick(rng, [
        f"{pfx} — {tref.capitalize()} and {oref} contest every inch {flav}, with transitions shaping this phase at {ss}.{t_hint}",
        f"{pfx} — The battle for control remains balanced. {tref.capitalize()} probe while {oref} stay organized.{s_hint}",
        f"{pfx} — Play flows on at {ss}. Spacing and decision-making define the rhythm between the two sides.",
        f"{pfx} — {tref.capitalize()} keep probing, but {oref} hold their shape well {flav}, waiting for a break.{s_hint}",
        f"{pfx} — A cagey spell at {ss}: possession against a compact block, both sides hunting the next mistake.{t_hint}",
        f"{pfx} — {team} look to build momentum {flav}, but {opp} are disciplined in their defensive structure.",
        f"{pfx} — The tempo dips briefly as both sides reset. {tref.capitalize()} look for a cleaner route forward at {ss}.{s_hint}",
        f"{pfx} — Midfield becomes the battleground {flav}, with neither side giving an inch in the duel for control.",
        f"{pfx} — {tref.capitalize()} try to raise the tempo, but {oref} match their intensity and keep the lanes closed.{s_hint}",
        f"{pfx} — A tactical chess match at {ss}: {tref} circulate, {oref} shuffle, and the next pass could unlock everything.{t_hint}",
        f"{pfx} — {flav.capitalize()}, {tref} push bodies forward while {oref} stay compact and look ready to spring.{s_hint}",
        f"{pfx} — The crowd senses a shift as {tref} increase the pressure, forcing {oref} into a deeper defensive posture.",
    ])
