from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.views import BreakControlView, SimpleChoiceView, TeamSelect

logger = logging.getLogger(__name__)


def _clamp(text: str, limit: int = 1900) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…(truncated)"


def _slot_group(slot: str | None, position: str | None) -> str:
    s = (slot or "").upper()
    p = (position or "MID").upper()
    if s == "GK" or p == "GK":
        return "GK"
    if s in {"LB", "RB", "LWB", "RWB", "CB", "LCB", "RCB"} or p == "DEF":
        return "DEF"
    if s in {"LW", "RW", "ST", "CF", "LS", "RS"} or p == "FWD":
        return "FWD"
    return "MID"


def _format_lineup(state, side: str) -> str:
    team = state.team(side)
    lu = team.lineup
    starters = list(lu.starters)
    bench = list(lu.bench)
    pos_order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    group_labels = {"GK": "Goalkeeper", "DEF": "Defence", "MID": "Midfield", "FWD": "Attack"}

    def sort_key(e):
        grp = _slot_group(e.slot, e.position)
        slot = (e.slot or "").upper()
        # stable-ish order within groups by common slot names
        slot_rank = {
            "GK": 0,
            "LB": 0, "LWB": 0, "LCB": 1, "CB": 2, "RCB": 3, "RB": 4, "RWB": 4,
            "CDM": 0, "LDM": 0, "RDM": 1, "LM": 0, "LCM": 1, "CM": 2, "RCM": 3, "RM": 4, "CAM": 5, "LAM": 4, "RAM": 6,
            "LW": 0, "LS": 0, "CF": 1, "ST": 2, "RS": 3, "RW": 4,
        }.get(slot, 9)
        return (pos_order.get(grp, 2), slot_rank, slot, e.name)

    # Defensive dedupe by player id/name to avoid displaying repeated players
    seen_ids = set()
    seen_names = set()
    unique_starters = []
    for e in starters:
        pid = int(getattr(e, "player_api_id", 0) or 0)
        nkey = str(getattr(e, "name", "") or "").strip().lower()
        if pid and pid in seen_ids:
            continue
        if nkey and nkey in seen_names:
            continue
        if pid:
            seen_ids.add(pid)
        if nkey:
            seen_names.add(nkey)
        unique_starters.append(e)
    starters_sorted = sorted(unique_starters, key=sort_key)
    lines = [f"**{team.team.name} — Starting Lineup**", f"Formation: **{lu.formation}**", "", "**Starting XI (Position):**"]

    current_group = None
    for e in starters_sorted:
        grp = _slot_group(e.slot, e.position)
        if grp != current_group:
            if current_group is not None:
                lines.append("")
            lines.append(f"__{group_labels.get(grp, grp)}__")
            current_group = grp
        pos_label = e.slot or (e.position or "POS")
        tags = []
        if e.player_api_id == lu.captain_player_id:
            tags.append("C")
        if e.player_api_id == lu.goalkeeper_player_id or pos_label.upper() == "GK" or (e.position or "").upper() == "GK":
            if "GK" not in tags:
                tags.append("GK")
        tag_txt = f" ({', '.join(tags)})" if tags else ""
        lines.append(f"- **{pos_label}**: {e.name}{tag_txt}")

    lines.append("")
    if bench:
        bench_txt = ", ".join([f"{b.name} ({b.slot or b.position or 'SUB'})" for b in bench])
        lines.append(f"**Bench:** {bench_txt}")
    else:
        lines.append("**Bench:** none")

    # Tactics summary
    t = team.tactics or {}
    atk = t.get("attacking", {})
    dfn = t.get("defensive", {})
    tparts = []
    if atk.get("mentality"):
        tparts.append(f"mentality: {atk.get('mentality')}")
    if atk.get("build_up"):
        tparts.append(f"build-up: {atk.get('build_up')}")
    if atk.get("attacking_width"):
        tparts.append(f"width: {atk.get('attacking_width')}")
    if atk.get("attacking_transitions"):
        tparts.append(f"transitions: {atk.get('attacking_transitions')}")
    if atk.get("counterattacking"):
        tparts.append("counter-attacking: on")
    if dfn.get("pressing_intensity"):
        tparts.append(f"press: {dfn.get('pressing_intensity')}")
    if dfn.get("defensive_line"):
        tparts.append(f"line: {dfn.get('defensive_line')}")
    if dfn.get("compactness"):
        tparts.append(f"compactness: {dfn.get('compactness')}")
    if dfn.get("counterpressing"):
        tparts.append("counter-press: on")
    if tparts:
        lines.append("**Tactical Setup:** " + ", ".join(tparts))

    return _clamp("\n".join(lines))


def _format_break_status(state: dict[str, Any]) -> str:
    score = state.get("score", {})
    stats = state.get("statistics", {})
    h = stats.get("home", {})
    a = stats.get("away", {})
    lines = [
        f"**Tactical Break — {score.get('scoreline','Match')}**",
        f"Minute: {state.get('minute',0)}' | Phase: {state.get('phase','break')}",
        "",
        f"Possession: {h.get('possession',0)}% - {a.get('possession',0)}%",
        f"Shots (on target): {h.get('shots',0)} ({h.get('shots_on_target',0)}) - {a.get('shots',0)} ({a.get('shots_on_target',0)})",
        f"xG: {h.get('xg',0)} - {a.get('xg',0)}",
        f"Corners: {h.get('corners',0)} - {a.get('corners',0)}",
        f"Fouls: {h.get('fouls',0)} - {a.get('fouls',0)}",
        f"Cards (Y/R): {h.get('yellow_cards',0)}/{h.get('red_cards',0)} - {a.get('yellow_cards',0)}/{a.get('red_cards',0)}",
    ]
    return _clamp("\n".join(lines))


def _find_player_in_report(report: dict[str, Any], name: str | None, team_name: str | None = None) -> dict[str, Any] | None:
    if not name:
        return None
    want = str(name).strip().lower()
    ratings = report.get("player_ratings") or {}
    pools = []
    if team_name:
        fs = report.get("final_score", {})
        if str(fs.get("home_team", "")).strip().lower() == str(team_name).strip().lower():
            pools = [ratings.get("home", [])]
        elif str(fs.get("away_team", "")).strip().lower() == str(team_name).strip().lower():
            pools = [ratings.get("away", [])]
    if not pools:
        pools = [ratings.get("home", []), ratings.get("away", [])]
    for pool in pools:
        for p in pool or []:
            if str(p.get("name", "")).strip().lower() == want:
                return p
    return None


def _motm_reason(report: dict[str, Any], motm: dict[str, Any] | None) -> str:
    if not motm:
        return "Delivered a standout performance across the match"
    name = motm.get("name")
    team = motm.get("team")
    p = _find_player_in_report(report, name, team) or {}
    goals = int(p.get("goals") or motm.get("goals") or 0)
    assists = int(p.get("assists") or motm.get("assists") or 0)
    saves = int(p.get("saves") or 0)
    key_passes = int(p.get("key_passes") or 0)
    tackles = int(p.get("tackles") or 0)
    interceptions = int(p.get("interceptions") or 0)
    shots_on_target = int(p.get("shots_on_target") or 0)
    pos = str(p.get("position") or "").upper()
    rating = motm.get("rating", p.get("rating"))

    parts = []
    if goals >= 2:
        parts.append("scored a decisive brace")
    elif goals == 1:
        parts.append("scored a crucial goal")
    if assists >= 2:
        parts.append("provided multiple assists")
    elif assists == 1:
        parts.append("supplied a key assist")
    if pos == "GK":
        if saves >= 5:
            parts.append("made several outstanding saves")
        elif saves >= 3:
            parts.append("produced important saves to keep his side in control")
    else:
        if key_passes >= 3:
            parts.append("consistently unlocked the opposition with creative passing")
        elif key_passes >= 1 and not assists:
            parts.append("created dangerous opportunities in the final third")
        if tackles + interceptions >= 6:
            parts.append("dominated defensive duels and broke up play effectively")
        elif tackles + interceptions >= 3:
            parts.append("worked tirelessly out of possession")
        if shots_on_target >= 3 and goals == 0:
            parts.append("tested the goalkeeper repeatedly with quality efforts")

    if not parts:
        try:
            r = float(rating)
        except Exception:
            r = 7.0
        if pos == "GK":
            parts.append("commanded his area with confidence and composure")
        elif r >= 8.0:
            parts.append("controlled key moments and influenced the match at both ends")
        elif r >= 7.2:
            parts.append("delivered a composed and influential all-round display")
        else:
            parts.append("made a telling impact in important phases of the game")

    if len(parts) == 1:
        reason = parts[0]
    elif len(parts) == 2:
        reason = f"{parts[0]} and {parts[1]}"
    else:
        reason = f"{parts[0]}, {parts[1]}, and {parts[2]}"
    reason = reason.strip()
    if reason:
        reason = reason[0].upper() + reason[1:]
    if not reason.endswith(('.', '!', '?')):
        reason += '.'
    return reason


def _interval_text_for_state(state) -> str | None:
    # Determine which interval is about to be simulated based on current state
    if state.status == "setup":
        return "⚡ *Simulating Interval 1 (0' - 45' Half-Time) with Match Intelligence Engine...*"
    return None


def _next_interval_text(pending_break: str | None, state) -> str | None:
    if pending_break == "halftime":
        return "⚡ *Simulating Interval 2 (46' - 60') with Match Intelligence Engine...*"
    if pending_break == "break_60":
        return "⚡ *Simulating Interval 3 (61' - 75') with Match Intelligence Engine...*"
    if pending_break == "break_75":
        st2 = getattr(state, "stoppage_time_2", 0) or 0
        end = f"90'+{st2}" if st2 else "90'+"
        return f"⚡ *Simulating Interval 4 (76' - {end} Full Time) with Match Intelligence Engine...*"
    return None


def _player_brief_note(p: dict[str, Any], goals_conceded: int | None = None, is_live: bool = True) -> str:
    notes: list[str] = []
    pos = str(p.get("position") or "").upper()
    goals = int(p.get("goals") or 0)
    assists = int(p.get("assists") or 0)
    shots = int(p.get("shots") or 0)
    sot = int(p.get("shots_on_target") or 0)
    key_passes = int(p.get("key_passes") or 0)
    tackles = int(p.get("tackles") or 0)
    interceptions = int(p.get("interceptions") or 0)
    saves = int(p.get("saves") or 0)
    yc = int(p.get("yellow_cards") or 0)
    rc = bool(p.get("red_card"))
    mins = int(p.get("minutes_played") or 0)
    injured = bool(p.get("injured"))

    if goals > 0:
        notes.append(f"Goals: {goals}")
    if assists > 0:
        notes.append(f"Assists: {assists}")
    if key_passes > 0:
        notes.append(f"Key Passes: {key_passes}")
    if saves > 0:
        notes.append(f"Saves: {saves}")
    if tackles >= 2:
        notes.append(f"Crucial tackles won: {tackles}")
    elif tackles == 1:
        notes.append("Crucial tackles won: 1")
    if interceptions >= 2:
        notes.append(f"Interceptions: {interceptions}")
    if sot >= 2 and goals == 0:
        notes.append(f"Shots on target: {sot}")
    elif shots >= 3 and goals == 0 and sot == 0 and pos in {"FWD", "MID"}:
        notes.append(f"Attempts: {shots}")
    if yc > 0:
        notes.append(f"Yellow Card: {yc}")
    if rc:
        notes.append("Red Card: 1")
    if injured:
        sev = p.get("injury_severity") or "injury"
        notes.append(f"Injured ({sev})")
    if goals_conceded is not None and pos == "GK":
        if goals_conceded == 0:
            notes.append("Clean sheet so far" if is_live else "Clean sheet")
        else:
            notes.append(f"Goals Conceded: {goals_conceded}")

    # Qualitative fallback from rating/contributions
    try:
        rating = float(p.get("rating") or 6.0)
    except Exception:
        rating = 6.0
    contribs = [str(c).lower() for c in (p.get("contributions") or [])]
    if not notes:
        if "important save" in contribs or "save" in contribs:
            notes.append("Made key interventions when called upon")
        elif "key pass" in contribs and assists == 0:
            notes.append("Created openings in the final third")
        elif "tackle" in contribs or "interception" in contribs:
            notes.append("Worked hard defensively throughout")
        elif rating >= 7.5:
            notes.append("Influential performance with strong all-round impact")
        elif rating >= 6.8:
            notes.append("Solid and composed contribution")
        elif rating >= 6.2:
            notes.append("Steady shift for the team")
        elif mins > 0:
            notes.append("Found it difficult to make a decisive impact")
        else:
            notes.append("Limited involvement")

    # Ensure at least one note
    if not notes:
        notes.append("Contributed to the team effort")

    # Keep concise (2-4 items)
    return ", ".join(notes[:4])


def _format_live_player_ratings_block(home_name: str, away_name: str, home_players: list[dict[str, Any]], away_players: list[dict[str, Any]], home_score: int, away_score: int, is_live: bool = True) -> list[str]:
    # home conceded = away_score, away conceded = home_score
    header = "⭐️Live Player Ratings"

    def sort_players(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ps = [p for p in (players or []) if (p.get("minutes_played", 0) > 0 or p.get("on_pitch") or p.get("substituted_in_minute") is not None)]
        if not ps:
            ps = list(players or [])
        def key(p):
            return (0 if p.get("on_pitch") else 1, -int(p.get("minutes_played") or 0), str(p.get("name") or ""))
        return sorted(ps, key=key)

    def line(team_name: str, p: dict[str, Any], conceded: int) -> str:
        pos = p.get("slot") or p.get("position") or "POS"
        rating = p.get("rating")
        try:
            rating_txt = f"{float(rating):.1f}" if rating is not None else "-"
        except Exception:
            rating_txt = str(rating)
        note = _player_brief_note(p, goals_conceded=conceded, is_live=is_live)
        name = p.get("name") or "Unknown"
        return f"**{team_name}:** {name} ({pos}): {rating_txt} — Brief Note: {note}"

    lines = [header, ""]
    for p in sort_players(home_players):
        lines.append(line(home_name, p, away_score))
    for p in sort_players(away_players):
        lines.append(line(away_name, p, home_score))

    msgs: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for ln in lines:
        add = len(ln) + 1
        if cur and cur_len + add > 1850:
            msgs.append("\n".join(cur))
            cur = [ln]
            cur_len = len(ln) + 1
        else:
            cur.append(ln)
            cur_len += add
    if cur:
        msgs.append("\n".join(cur))
    return msgs


def _format_minute_label(minute: int, phase: str | None = None, extra: int = 0, is_full_time: bool = False) -> str:
    if is_full_time:
        if extra:
            return f"Minute {minute}+{extra} (Full Time)"
        return f"Minute {minute} (Full Time)"
    if phase == "halftime" or minute == 45:
        return "Minute 45 (Half-Time)"
    return f"Minute {minute}"


def _format_match_stat_sheet(home_name: str, away_name: str, home_stats: dict[str, Any], away_stats: dict[str, Any], home_score: int, away_score: int, minute_label: str) -> str:
    def pct(v):
        try:
            return f"{int(round(float(v)))}%"
        except Exception:
            return f"{v}%"
    def num(v):
        try:
            fv = float(v)
            if abs(fv - round(fv)) < 1e-9:
                return str(int(round(fv)))
            return f"{fv:.2f}".rstrip('0').rstrip('.')
        except Exception:
            return str(v)
    def xg(v):
        try:
            return f"{float(v):.2f}"
        except Exception:
            return str(v)

    h_pos = pct(home_stats.get("possession", 0))
    a_pos = pct(away_stats.get("possession", 0))
    h_pass = home_stats.get("passes_attempted", home_stats.get("passes_completed", 0))
    a_pass = away_stats.get("passes_attempted", away_stats.get("passes_completed", 0))
    h_cards = int(home_stats.get("yellow_cards", 0) or 0) + int(home_stats.get("red_cards", 0) or 0)
    a_cards = int(away_stats.get("yellow_cards", 0) or 0) + int(away_stats.get("red_cards", 0) or 0)

    rows = [
        ("**Possession %**", h_pos, a_pos),
        ("**Shots**", num(home_stats.get("shots", 0)), num(away_stats.get("shots", 0))),
        ("**Shots on Target**", num(home_stats.get("shots_on_target", 0)), num(away_stats.get("shots_on_target", 0))),
        ("**xG**", xg(home_stats.get("xg", 0)), xg(away_stats.get("xg", 0))),
        ("**Passes**", num(h_pass), num(a_pass)),
        ("**Pass Accuracy %**", pct(home_stats.get("pass_accuracy", 0)), pct(away_stats.get("pass_accuracy", 0))),
        ("**Fouls**", num(home_stats.get("fouls", 0)), num(away_stats.get("fouls", 0))),
        ("**Cards**", str(h_cards), str(a_cards)),
        ("**Goals**", str(home_score), str(away_score)),
        ("**Corners**", num(home_stats.get("corners", 0)), num(away_stats.get("corners", 0))),
        ("**Offsides**", num(home_stats.get("offsides", 0)), num(away_stats.get("offsides", 0))),
    ]
    lines = [
        f"📊 Match Stat Sheet ({minute_label})",
        f"| Statistic | {home_name} | {away_name} |",
        "|---|---|---|",
    ]
    for label, hv, av in rows:
        lines.append(f"| {label} | {hv} | {av} |")
    return "\n".join(lines)


def _format_injury_consistency(injuries: list[dict[str, Any]]) -> str:
    lines = ["", "### 🚑 INJURY CONSISTENCY (MANDATORY)"]
    if not injuries:
        lines.append("- No injuries occurred.")
        return "\n".join(lines)
    # de-duplicate roughly by player+minute
    seen = set()
    for inj in injuries:
        minute = inj.get("minute", 0)
        ex = inj.get("extra_minute", 0)
        mtxt = f"{minute}+{ex}'" if ex else f"{minute}'"
        player = inj.get("player") or inj.get("player_name") or "Unknown"
        team = inj.get("team") or ""
        sev = inj.get("severity") or "knock"
        continued = inj.get("continued")
        substituted = inj.get("substituted")
        key = (mtxt, player, team, sev)
        if key in seen:
            continue
        seen.add(key)
        if continued:
            status = "continued after treatment"
        elif substituted:
            status = "substituted off"
        else:
            status = "unable to continue"
        team_txt = f" ({team})" if team else ""
        lines.append(f"- {mtxt} {player}{team_txt} — {sev}, {status}.")
    if len(lines) == 2:
        lines.append("- No injuries occurred.")
    return "\n".join(lines)


def _format_physical_condition_sheet(home_name: str, away_name: str, home_players: list[dict[str, Any]], away_players: list[dict[str, Any]], minute_label: str) -> list[str]:
    header = [f"### 🔋 Physical Condition Sheet ({minute_label})"]

    def sort_players(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ps = [p for p in (players or []) if (p.get("minutes_played", 0) > 0 or p.get("on_pitch") or p.get("substituted_in_minute") is not None or p.get("is_starter"))]
        if not ps:
            ps = list(players or [])
        pos_order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
        def key(p):
            pos = str(p.get("position") or "MID").upper()
            return (0 if p.get("on_pitch") else 1, pos_order.get(pos, 2), -int(p.get("minutes_played") or 0), str(p.get("name") or ""))
        return sorted(ps, key=key)

    def line(team_name: str, p: dict[str, Any]) -> str:
        pos = p.get("slot") or p.get("position") or "POS"
        name = p.get("name") or "Unknown"
        try:
            stamina = int(round(max(0.0, min(100.0, float(p.get("stamina", 100))))))
        except Exception:
            stamina = 100
        injured = bool(p.get("injured"))
        continued = bool(p.get("continued") or p.get("injury_continued"))
        if injured and not continued and not p.get("on_pitch", False):
            fitness = max(40, min(85, stamina))
        elif injured:
            fitness = max(70, min(95, stamina + 5))
        else:
            fitness = 100
        return f"**{team_name}:** {name} ({pos}) — Stamina: {stamina}% | Fitness: {fitness}%"

    lines = list(header)
    for p in sort_players(home_players):
        lines.append(line(home_name, p))
    for p in sort_players(away_players):
        lines.append(line(away_name, p))

    # chunk
    msgs: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for ln in lines:
        add = len(ln) + 1
        if cur and cur_len + add > 1850:
            msgs.append("\n".join(cur))
            cur = [ln]
            cur_len = len(ln) + 1
        else:
            cur.append(ln)
            cur_len += add
    if cur:
        msgs.append("\n".join(cur))
    return msgs


def _format_report(report: dict[str, Any]) -> list[str]:
    fs = report.get("final_score", {})
    stats = report.get("statistics", {})
    h = stats.get("home", {})
    a = stats.get("away", {})
    msgs: list[str] = []

    scoreline = fs.get('scoreline') or f"{fs.get('home_team','Home')} {fs.get('home_score',0)} - {fs.get('away_score',0)} {fs.get('away_team','Away')}"
    goals = report.get("goalscorers", []) or []

    summary_lines = [
        "🏆 MATCH SUMMARY (Full-Time Only)",
        scoreline,
    ]
    if goals:
        summary_lines.append("")
        for g in goals:
            m = f"{g.get('minute',0)}{('+'+str(g.get('extra_minute'))) if g.get('extra_minute') else ''}'"
            who = g.get('scorer') or 'Unknown'
            assist = g.get('assist') or 'None'
            summary_lines.append(f"⚽  {m}: {who} (Assist: {assist})")
    else:
        summary_lines.append("")
        summary_lines.append("⚽  No goals")

    motm = report.get("man_of_the_match")
    if motm:
        reason = _motm_reason(report, motm)
        motm_name = motm.get('name') or 'Unknown'
        motm_rating = motm.get('rating', '-')
        summary_lines.append("")
        summary_lines.append(f"MOTM: {motm_name} - {motm_rating} *({reason})*")

    summary_block = "🏆 **Match Summary & Post-Match Wrap-Up**\n```\n" + "\n".join(summary_lines) + "\n```"
    msgs.append(_clamp(summary_block, 1900))

    detail_lines = [
        f"**Full-Time Details: {scoreline}**",
        "",
        "**Match Statistics**",
        f"Possession: {h.get('possession',0)}% - {a.get('possession',0)}%",
        f"Shots (on target): {h.get('shots',0)} ({h.get('shots_on_target',0)}) - {a.get('shots',0)} ({a.get('shots_on_target',0)})",
        f"xG: {h.get('xg',0)} - {a.get('xg',0)}",
        f"Corners: {h.get('corners',0)} - {a.get('corners',0)}",
        f"Fouls: {h.get('fouls',0)} - {a.get('fouls',0)}",
        f"Cards (Y/R): {h.get('yellow_cards',0)}/{h.get('red_cards',0)} - {a.get('yellow_cards',0)}/{a.get('red_cards',0)}",
        f"Pass accuracy: {h.get('pass_accuracy',0)}% - {a.get('pass_accuracy',0)}%",
    ]
    msgs.append(_clamp("\n".join(detail_lines)))

    # Live player ratings format (requested for full time as well)
    ratings = report.get("player_ratings") or {}
    msgs.extend(_format_live_player_ratings_block(
        fs.get('home_team', 'Home'), fs.get('away_team', 'Away'),
        ratings.get('home', []), ratings.get('away', []),
        int(fs.get('home_score', 0) or 0), int(fs.get('away_score', 0) or 0),
        is_live=False,
    ))

    extra_lines = []
    ta = report.get('tactical_analysis')
    if ta:
        extra_lines.append("**Tactical Analysis**")
        extra_lines.append(str(ta))
    disc = report.get('discipline') or []
    if disc:
        extra_lines.append("")
        extra_lines.append("**Discipline**")
        for x in disc[:15]:
            extra_lines.append(f"- {x.get('minute')}' {x.get('player')} ({x.get('team')}) — {str(x.get('type','')).replace('_',' ')}")
    inj = report.get('injuries') or []
    if inj:
        extra_lines.append("")
        extra_lines.append("**Injuries**")
        for x in inj[:12]:
            status = 'continued' if x.get('continued') else 'substituted off' if x.get('substituted') else 'forced off'
            extra_lines.append(f"- {x.get('minute')}' {x.get('player')} ({x.get('team')}) — {x.get('severity')} ({status})")
    subs = report.get('substitutions') or []
    if subs:
        extra_lines.append("")
        extra_lines.append("**Substitutions**")
        for x in subs[:20]:
            reason = f" ({x.get('reason')})" if x.get('reason') else ''
            extra_lines.append(f"- {x.get('minute')}' {x.get('team')}: {x.get('on_name')} for {x.get('off_name')}{reason}")
    if extra_lines:
        msgs.append(_clamp("\n".join(extra_lines)))

    return [m for m in msgs if m.strip()]


class SimCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _send_events_live(self, channel: discord.abc.Messageable, events: list[dict[str, Any]], delay: float = 3.0, state=None):
        import re as _re
        for ev in events:
            text = ev.get("commentary")
            payload = ev.get("payload") or {}
            et = ev.get("event_type")
            if not text:
                # CHANCE events are intentionally paired with rich outcome narratives; avoid duplicate display
                if et != "chance":
                    text = payload.get("detail") or payload.get("summary") or ""
            if not text:
                # fallback: skip silent/internal events but keep pacing sane
                if delay > 0:
                    await asyncio.sleep(delay)
                continue
            try:
                et = ev.get("event_type")
                if et in {"goal", "penalty_scored"} and state is not None:
                    minute = int(ev.get("minute", getattr(state, "minute", 0)) or 0)
                    extra = int(ev.get("extra_minute", getattr(state, "extra_minute", 0)) or 0)
                    mtxt = f"{minute}+{extra}'" if extra else f"{minute}'"
                    side = ev.get("team_side")
                    scorer = payload.get("player_name") or payload.get("scorer_name")
                    if not scorer and side and ev.get("player_api_id"):
                        try:
                            ps = state.player_states.get(state.player_key(side, int(ev.get("player_api_id"))))
                            scorer = ps.name if ps else None
                        except Exception:
                            scorer = None
                    scorer = scorer or "Unknown"
                    assist = payload.get("assist_name")
                    assist_txt = assist if assist else "None"
                    sc = payload.get("score") or {}
                    hs = sc.get("home", getattr(state.home, "score", 0))
                    as_ = sc.get("away", getattr(state.away, "score", 0))
                    home_name = state.home.team.name
                    away_name = state.away.team.name
                    scoreline = f"{home_name} {hs} - {as_} {away_name}"
                    narrative = str(text).strip()
                    narrative = _re.sub(r"^(?:-?\s*\*\*)?\d+(?:\+\d+)?'\*?\*?\s*[—\-:]+\s*", "", narrative)
                    narrative = _re.sub(r"^\*\*?⚽?\s*GOAL!?\*\*?\s*", "", narrative, flags=_re.IGNORECASE)
                    narrative = _re.sub(r"^⚽\s*GOAL!?\s*", "", narrative, flags=_re.IGNORECASE)
                    if narrative:
                        narrative = f"**⚽ GOAL!** {narrative.strip()}"
                    else:
                        narrative = f"**⚽ GOAL!** {scorer} finds the net"
                    if f"{home_name} {hs}" not in narrative and f"{hs} - {as_}" not in narrative and f"{hs}-{as_}" not in narrative:
                        narrative = narrative.rstrip()
                        if narrative.endswith("!"):
                            narrative = narrative[:-1].rstrip() + f"! **({scoreline})**"
                        elif narrative.endswith("."):
                            narrative = narrative[:-1].rstrip() + f". **({scoreline})**"
                        else:
                            narrative = narrative + f" **({scoreline})**"
                    msg = f"- **{mtxt}** — {narrative}\n  * *(Goal: {scorer} | Assist: {assist_txt})*"
                    await channel.send(_clamp(msg, 1900))
                    if et == "goal":
                        banner = f"# ⚽ {home_name.upper()} {hs}-{as_} {away_name.upper()}"
                        await channel.send(_clamp(banner, 1900))
                elif et == "substitution" and state is not None:
                    minute = int(ev.get("minute", getattr(state, "minute", 0)) or 0)
                    extra = int(ev.get("extra_minute", getattr(state, "extra_minute", 0)) or 0)
                    mtxt = f"{minute}+{extra}'" if extra else f"{minute}'"
                    side = ev.get("team_side")
                    team_name = state.team(side).team.name if side else "Team"
                    on_name = payload.get("on_name") or "Substitute"
                    off_name = payload.get("off_name") or "Player"
                    reason = payload.get("reason")
                    reason_txt = f" — {reason}" if reason else ""
                    msg = f"- **{mtxt}** — 🔁 **Substitution ({team_name}):** {on_name} comes on for {off_name}{reason_txt}."
                    await channel.send(_clamp(msg, 1900))
                elif et == "tactical_change" and state is not None:
                    minute = int(ev.get("minute", getattr(state, "minute", 0)) or 0)
                    extra = int(ev.get("extra_minute", getattr(state, "extra_minute", 0)) or 0)
                    mtxt = f"{minute}+{extra}'" if extra else f"{minute}'"
                    side = ev.get("team_side")
                    team_name = state.team(side).team.name if side else "Team"
                    summary = payload.get("summary") or payload.get("detail") or "tactical adjustment"
                    msg = f"- **{mtxt}** — 🧠 **Tactical Change ({team_name}):** {summary}"
                    await channel.send(_clamp(msg, 1900))
                else:
                    await channel.send(_clamp(str(text), 1900))
            except Exception:
                logger.exception("Failed to send match event message")
            if delay > 0:
                await asyncio.sleep(delay)

    async def _send_live_ratings(self, channel: discord.abc.Messageable, session) -> None:
        try:
            bs = session.break_status() if session.state.status != "finished" else session.report()
            if "players" in bs:
                home_players = (bs.get("players") or {}).get("home", [])
                away_players = (bs.get("players") or {}).get("away", [])
                score = bs.get("score") or {}
                home_name = session.state.home.team.name
                away_name = session.state.away.team.name
                home_score = int(score.get("home", session.state.home.score))
                away_score = int(score.get("away", session.state.away.score))
            else:
                fs = bs.get("final_score", {})
                ratings = bs.get("player_ratings") or {}
                home_players = ratings.get("home", [])
                away_players = ratings.get("away", [])
                home_name = fs.get("home_team", session.state.home.team.name)
                away_name = fs.get("away_team", session.state.away.team.name)
                home_score = int(fs.get("home_score", session.state.home.score) or 0)
                away_score = int(fs.get("away_score", session.state.away.score) or 0)
            msgs = _format_live_player_ratings_block(home_name, away_name, home_players, away_players, home_score, away_score, is_live=(session.state.status != "finished"))
            for msg in msgs:
                await channel.send(_clamp(msg, 1900))
        except Exception:
            logger.exception("Failed to send live player ratings")

    async def _send_tactical_break_sheets(self, channel: discord.abc.Messageable, session, *, is_full_time: bool = False) -> None:
        try:
            st = session.state
            if is_full_time or st.status == "finished":
                rep = session.report()
                fs = rep.get("final_score", {})
                mstats = rep.get("statistics") or {}
                home_stats = mstats.get("home", {})
                away_stats = mstats.get("away", {})
                ratings = rep.get("player_ratings") or {}
                home_players = ratings.get("home", [])
                away_players = ratings.get("away", [])
                home_name = fs.get("home_team", st.home.team.name)
                away_name = fs.get("away_team", st.away.team.name)
                home_score = int(fs.get("home_score", st.home.score) or 0)
                away_score = int(fs.get("away_score", st.away.score) or 0)
                minute = int(mstats.get("minute", st.minute) or st.minute)
                extra = int(st.extra_minute or 0)
                injuries = rep.get("injuries") or []
                minute_label = _format_minute_label(minute, st.phase, extra, is_full_time=True)
            else:
                bs = session.break_status()
                score = bs.get("score") or {}
                mstats = bs.get("statistics") or {}
                home_stats = mstats.get("home", {})
                away_stats = mstats.get("away", {})
                players = bs.get("players") or {}
                home_players = players.get("home", [])
                away_players = players.get("away", [])
                home_name = st.home.team.name
                away_name = st.away.team.name
                home_score = int(score.get("home", st.home.score) or 0)
                away_score = int(score.get("away", st.away.score) or 0)
                minute = int(bs.get("minute", st.minute) or st.minute)
                injuries = []
                from engine.report import injuries as _inj
                try:
                    injuries = _inj(st)
                except Exception:
                    injuries = []
                minute_label = _format_minute_label(minute, st.phase, is_full_time=False)

            sheet = _format_match_stat_sheet(home_name, away_name, home_stats, away_stats, home_score, away_score, minute_label)
            injury_block = _format_injury_consistency(injuries)
            await channel.send(_clamp(sheet + "\n" + injury_block, 1900))
            for msg in _format_physical_condition_sheet(home_name, away_name, home_players, away_players, minute_label):
                await channel.send(_clamp(msg, 1900))
        except Exception:
            logger.exception("Failed to send tactical break/full-time sheets")

    def _interval_break_label(self, session) -> str | None:
        st = session.state
        h = st.home.team.name
        a = st.away.team.name
        br = st.pending_break
        if br == "halftime":
            return f"⏸️ **INTERVAL {h} vs {a} — Interval 1 (Minutes 0' to 45')**"
        if br == "break_60":
            return f"⏸️ **INTERVAL {h} vs {a} — Interval 2 (Minutes 46' to 60')**"
        if br == "break_75":
            return f"⏸️ **INTERVAL {h} vs {a} — Interval 3 (Minutes 61' to 75')**"
        return None

    async def _handle_break(self, channel: discord.abc.Messageable, session) -> list[dict[str, Any]]:
        # Interval marker for completed segment
        label = self._interval_break_label(session)
        if label:
            try:
                await channel.send(label)
            except Exception:
                logger.exception("Failed to send interval break label")

        # Tactical break reports (stat sheet, injuries, physical condition, live ratings)
        await self._send_tactical_break_sheets(channel, session, is_full_time=False)
        await self._send_live_ratings(channel, session)

        # Auto-resolve AI sides immediately (subs/tactics) and capture events to show
        action_evs = await session.auto_resolve_ai_sides_if_ready()
        await session.save()

        has_human = (session.home_manager_id is not None) or (session.away_manager_id is not None)

        if not has_human:
            # Show bot-vs-bot subs/tactical adjustments clearly
            if action_evs:
                await self._send_events_live(channel, action_evs, delay=1.0, state=session.state)
            try:
                await channel.send("🤖 **Both AI Managers have locked in tactical adjustments!** Resuming match...")
            except Exception:
                pass
            return action_evs

        # Manager mode: show AI-side actions now (if any), then controls for humans
        if action_evs:
            await self._send_events_live(channel, action_evs, delay=1.0, state=session.state)

        view = BreakControlView(session.state.id, session)
        try:
            await channel.send(_clamp("Managers: confirm ready or apply AI-suggested adjustments for your side."), view=view)
        except Exception:
            try:
                await channel.send(_format_break_status(session.break_status()))
            except Exception:
                pass

        waited = 0
        while session.state.pending_break and session.engine.is_waiting_for_break_decisions():
            # apply AI again just in case (safe/idempotent per side)
            more = await session.auto_resolve_ai_sides_if_ready()
            if more:
                action_evs.extend(more)
                await self._send_events_live(channel, more, delay=1.0, state=session.state)
            await session.save()
            if session.is_ready_to_resume():
                break
            await asyncio.sleep(2)
            waited += 2
            if waited >= 300:
                # timeout: auto-apply for remaining human sides
                for side in ("home", "away"):
                    mgr = session.home_manager_id if side == "home" else session.away_manager_id
                    done = session.state.home_break_decision_done if side == "home" else session.state.away_break_decision_done
                    if mgr is not None and not done:
                        try:
                            from engine.ai_manager import decide_ai_break_actions
                            actions = decide_ai_break_actions(session.state, side)
                            timeout_evs = await session.apply_break_actions(side, substitutions=actions.get("substitutions", []), tactics_updates=actions.get("tactics_updates", {}), summary="Auto-applied (timeout)")
                            if timeout_evs:
                                action_evs.extend(timeout_evs)
                                await self._send_events_live(channel, timeout_evs, delay=1.0, state=session.state)
                        except Exception:
                            logger.exception("Failed to auto-apply break actions on timeout for %s", side)
                await session.save()
                break
        return action_evs

    async def _run_match_flow(self, interaction: discord.Interaction, session):
        channel = interaction.channel
        if channel is None:
            return

        st = session.state
        await channel.send(_clamp(
            f"**Match Starting**\n{st.home.team.name} vs {st.away.team.name}\n"
            f"Match ID: `{st.id}` | Mode: {st.mode.replace('_', ' ').title()}"
        ))

        # Starting lineups
        try:
            await channel.send(_format_lineup(st, "home"))
            await channel.send(_format_lineup(st, "away"))
        except Exception:
            logger.exception("Failed to send starting lineups")

        # Main loop: always drive simulation forward until full time
        while session.state.status != "finished":
            try:
                # If we're in a pending break, handle it and resume to next segment
                if session.state.pending_break and session.engine.is_waiting_for_break_decisions():
                    pending = session.state.pending_break
                    await self._handle_break(channel, session)
                    # Ensure decisions are ready; force AI resolution if still stuck
                    if session.state.pending_break and not session.is_ready_to_resume():
                        try:
                            await session.auto_resolve_ai_sides_if_ready()
                            await session.save()
                        except Exception:
                            logger.exception("Failed to force-resolve break decisions")
                    if session.state.pending_break and not session.is_ready_to_resume():
                        await asyncio.sleep(1)
                        continue
                    # Resume into next interval segment
                    next_msg = _next_interval_text(pending, session.state)
                    if next_msg:
                        try:
                            await channel.send(next_msg)
                        except Exception:
                            logger.exception("Failed to send next interval announcement")
                    evs = await session.continue_after_break()
                    await self._send_events_live(channel, evs, delay=3.0, state=session.state)
                    continue

                # Start or simulate next segment (setup/normal)
                interval_msg = _interval_text_for_state(session.state)
                if interval_msg:
                    try:
                        await channel.send(interval_msg)
                    except Exception:
                        logger.exception("Failed to send interval announcement")

                if session.state.status == "setup":
                    evs = await session.start_and_simulate_until_break()
                elif session.state.status == "finished":
                    break
                else:
                    # Not in break and not finished: continue simulation to break/end
                    try:
                        evs = await session.continue_after_break()
                    except Exception:
                        evs = []
                    if not evs and session.state.status != "finished":
                        step = session.engine.simulate_until_break_or_end()
                        evs = step.events
                        await session.append_events(evs)
                        await session.save()

                if evs:
                    await self._send_events_live(channel, evs, delay=3.0, state=session.state)
                elif session.state.status != "finished" and not session.state.pending_break:
                    # Safety: avoid tight loop if nothing produced unexpectedly
                    await asyncio.sleep(0.5)
            except Exception:
                logger.exception("Match flow iteration failed; attempting recovery")
                try:
                    if session.state.status != "finished" and not session.state.pending_break:
                        step = session.engine.simulate_until_break_or_end()
                        if step.events:
                            await session.append_events(step.events)
                            await session.save()
                            await self._send_events_live(channel, step.events, delay=3.0, state=session.state)
                    await asyncio.sleep(0.5)
                except Exception:
                    logger.exception("Match flow recovery failed")
                    await asyncio.sleep(1)

        # Full-time interval marker + report (stat sheet, injuries, physical condition, then summary/details/ratings/analysis)
        try:
            st = session.state
            h = st.home.team.name
            a = st.away.team.name
            end = f"90'+{st.stoppage_time_2}" if st.stoppage_time_2 else "90'"
            await channel.send(_clamp(f"⏸️ **INTERVAL {h} vs {a} — Interval 4 (Minutes 76' to {end})**"))
        except Exception:
            pass
        try:
            await self._send_tactical_break_sheets(channel, session, is_full_time=True)
            rep = session.report()
            for msg in _format_report(rep):
                await channel.send(_clamp(msg, 1900))
                await asyncio.sleep(0.5)
        except Exception:
            logger.exception("Failed to send full-time report")
            try:
                await channel.send(_clamp(f"Full-time: {session.state.scoreline()}"))
            except Exception:
                pass

    @app_commands.command(name="sim", description="Start a football match simulation (Bot vs Bot or Manager vs Manager)")
    @app_commands.describe(
        mode="Match mode",
        league="League to choose teams from",
        home_team="Home team name (search)",
        away_team="Away team name (search)",
        home_manager="Home human manager (Manager vs Manager)",
        away_manager="Away human manager (Manager vs Manager)",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Bot vs Bot", value="bot_vs_bot"),
        app_commands.Choice(name="Manager vs Manager", value="manager_vs_manager"),
    ])
    async def sim(self, interaction: discord.Interaction,
                  mode: app_commands.Choice[str],
                  league: str | None = None,
                  home_team: str | None = None,
                  away_team: str | None = None,
                  home_manager: discord.User | None = None,
                  away_manager: discord.User | None = None):
        await interaction.response.defer(thinking=True)
        fd = self.bot.football_data
        ms = self.bot.match_service

        # Resolve teams
        async def resolve_team(q: str | None, label: str) -> dict[str, Any] | None:
            if not q:
                return None
            matches = await fd.list_teams(query=q, league=league, limit=5)
            if not matches:
                return None
            # exact-ish first
            ql = q.lower().strip()
            for m in matches:
                if (m.get("name") or "").lower() == ql or (m.get("short_name") or "").lower() == ql:
                    return m
            return matches[0]

        home = await resolve_team(home_team, "home")
        away = await resolve_team(away_team, "away")

        # If teams not provided or not found, fall back to interactive selection for the given/first league
        if home is None or away is None or (home and away and home.get("api_id") == away.get("api_id")):
            leagues = fd.list_leagues()
            league_key = None
            from data.bundled import resolve_league_key
            if league:
                league_key = resolve_league_key(league)
            if not league_key:
                league_key = leagues[0]["key"] if leagues else "premier_league"
            teams = await fd.list_teams(league=league_key, limit=25)
            if len(teams) < 2:
                await interaction.followup.send("Could not load enough teams for selection.", ephemeral=True)
                return

            selected: dict[str, Any] = {"home": home, "away": away}

            async def on_away_pick(inter: discord.Interaction, team_id: int):
                t = await fd.get_team(team_id)
                if selected.get("home") and int(t.get("api_id", 0)) == int(selected["home"].get("api_id", 0)):
                    await inter.response.send_message("Away team must be different from home team.", ephemeral=True)
                    return
                selected["away"] = t
                await inter.response.edit_message(content=f"Match setup: **{selected['home'].get('name')}** vs **{t.get('name')}**\nStarting simulation...", view=None)
                await self._launch_with_selection(interaction, mode.value, selected["home"], selected["away"], home_manager, away_manager)

            async def on_home_pick_chain(inter: discord.Interaction, team_id: int):
                t = await fd.get_team(team_id)
                selected["home"] = t
                away_opts = [x for x in teams if int(x["api_id"]) != team_id]
                view2_local = SimpleChoiceView(interaction.user.id)
                view2_local.add_item(TeamSelect(away_opts, "Select AWAY team", on_away_pick))
                await inter.response.edit_message(content=f"Home team: **{t.get('name')}**\nSelect away team.", view=view2_local)

            view = SimpleChoiceView(interaction.user.id)
            view.add_item(TeamSelect(teams, "Select HOME team", on_home_pick_chain))
            league_name = next((l['name'] for l in fd.list_leagues() if l['key'] == league_key), league_key)
            await interaction.followup.send(f"**Match Setup** — {league_name}\nSelect the home team, then away team.", view=view, ephemeral=True)
            return

        await self._launch_with_selection(interaction, mode.value, home, away, home_manager, away_manager)

    async def _launch_with_selection(self, interaction: discord.Interaction, mode: str, home: dict[str, Any], away: dict[str, Any], home_manager: discord.User | None, away_manager: discord.User | None):
        fd = self.bot.football_data
        ms = self.bot.match_service
        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("No channel available to run the match.", ephemeral=True)
            return

        if int(home["api_id"]) == int(away["api_id"]):
            await interaction.followup.send("Home and away teams must be different.", ephemeral=True)
            return

        hm_id = None
        am_id = None
        if mode == "manager_vs_manager":
            hm_id = home_manager.id if home_manager else interaction.user.id
            am_id = away_manager.id if away_manager else None
            if am_id is None:
                await interaction.followup.send("Manager vs Manager mode requires `away_manager` (and optionally `home_manager`).", ephemeral=True)
                return
            if hm_id == am_id:
                await interaction.followup.send("Home and away managers must be different users.", ephemeral=True)
                return

        try:
            home_squad = await fd.get_squad(int(home["api_id"]))
            away_squad = await fd.get_squad(int(away["api_id"]))
        except Exception as e:
            logger.exception("Failed to load squads")
            await interaction.followup.send(f"Failed to load team squads: {e}", ephemeral=True)
            return

        if len(home_squad) < 11 or len(away_squad) < 11:
            await interaction.followup.send("One or both teams do not have enough players in bundled data.", ephemeral=True)
            return

        try:
            session = await ms.create_session(
                guild_id=interaction.guild_id,
                channel_id=channel.id if hasattr(channel, 'id') else None,
                mode=mode,
                home_team=home, away_team=away,
                home_squad=home_squad, away_squad=away_squad,
                home_manager_id=hm_id, away_manager_id=am_id,
            )
        except Exception as e:
            logger.exception("Failed to create match session")
            await interaction.followup.send(f"Failed to create match: {e}", ephemeral=True)
            return

        msg = f"Match created: **{home.get('name')}** vs **{away.get('name')}**\nMatch ID: `{session.state.id}`\nMode: {mode.replace('_',' ').title()}"
        if mode == "manager_vs_manager":
            msg += f"\nHome manager: <@{hm_id}>\nAway manager: <@{am_id}>"
        try:
            await interaction.followup.send(_clamp(msg), ephemeral=True)
        except Exception:
            pass

        # Run full flow in background task
        asyncio.create_task(self._run_match_flow(interaction, session))


async def setup(bot):
    await bot.add_cog(SimCog(bot))
