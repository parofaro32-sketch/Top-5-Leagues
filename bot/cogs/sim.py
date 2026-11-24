from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.modals import LineupSubmitModal, SubsTextModal, TacticsTextModal
from bot.ui.views import BreakControlView, LeagueSelect, SimpleChoiceView, TeamSelect
from services.lineup_service import LineupValidationError, format_squad_for_manager, parse_break_subs_text, parse_lineup_submission, resolve_player_ref

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

    # Tactics are private - never expose in public lineup messages.
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
        f"Saves: {h.get('saves',0)} - {a.get('saves',0)}",
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
    disc = report.get('discipline') or []
    if disc:
        extra_lines.append("**Discipline**")
        for x in disc[:15]:
            extra_lines.append(f"- {x.get('minute')}' {x.get('player')} ({x.get('team')}) — {str(x.get('type','')).replace('_',' ')}")
    inj = report.get('injuries') or []
    if inj:
        if extra_lines:
            extra_lines.append("")
        extra_lines.append("**Injuries**")
        for x in inj[:12]:
            status = 'continued' if x.get('continued') else 'substituted off' if x.get('substituted') else 'forced off'
            extra_lines.append(f"- {x.get('minute')}' {x.get('player')} ({x.get('team')}) — {x.get('severity')} ({status})")
    subs = report.get('substitutions') or []
    if subs:
        if extra_lines:
            extra_lines.append("")
        extra_lines.append("**Substitutions**")
        for x in subs[:20]:
            reason = f" ({x.get('reason')})" if x.get('reason') else ''
            extra_lines.append(f"- {x.get('minute')}' {x.get('team')}: {x.get('on_name')} for {x.get('off_name')}{reason}")
    if extra_lines:
        msgs.append(_clamp("\n".join(extra_lines)))

    return [m for m in msgs if m.strip()]


def _competition_and_type(home: dict[str, Any], away: dict[str, Any]) -> tuple[str, str]:
    # /sim creates an ad-hoc match with no scheduled fixture; label as Friendly.
    # If fixture/competition data is wired in later, this should use it.
    home_league = (home.get("league_name") or "").strip()
    away_league = (away.get("league_name") or "").strip()
    if home_league and away_league and home_league == away_league:
        return "Friendly", "Friendly"
    if home_league or away_league:
        return "Friendly", "Friendly"
    return "Friendly", "Friendly"


def build_match_setup_embed(session, *, competition: str | None = None, match_type: str | None = None, stadium: str | None = None) -> discord.Embed:
    st = session.state
    sub = st.submission or {}
    comp = competition or sub.get("competition") or "Friendly"
    mtype = match_type or sub.get("match_type") or ("Friendly" if comp == "Friendly" else "Competitive")
    stad = stadium or sub.get("stadium") or st.home.team.logo or f"{st.home.team.name} Stadium"
    # if stadium accidentally got logo, fix
    if isinstance(stad, str) and stad.startswith("http"):
        stad = sub.get("stadium") or f"{st.home.team.name} Stadium"
    embed = discord.Embed(
        title=f"{st.home.team.name} vs {st.away.team.name}",
        description="Match setup is ready. Submit your lineup and tactics before kickoff.",
        color=discord.Color.blue(),
    )
    if st.home.team.logo:
        embed.set_thumbnail(url=st.home.team.logo)
    embed.add_field(name="Competition", value=str(comp), inline=True)
    embed.add_field(name="Match Type", value=str(mtype), inline=True)
    embed.add_field(name="Stadium", value=str(stad), inline=True)
    home_mgr = f"<@{session.home_manager_id}>" if session.home_manager_id else "Bot (AI)"
    away_mgr = f"<@{session.away_manager_id}>" if session.away_manager_id else "Bot (AI)"
    embed.add_field(name="Home Manager", value=home_mgr, inline=True)
    embed.add_field(name="Away Manager", value=away_mgr, inline=True)
    embed.add_field(name="Mode", value=st.mode.replace("_", " ").title(), inline=True)
    # Submission status
    def _status(side: str, mgr_id):
        if mgr_id is None:
            return "✅ Bot ready"
        lu = "✅" if sub.get(f"{side}_lineup_submitted") else "⏳"
        tac = "✅" if sub.get(f"{side}_tactics_submitted") else "⏳"
        return f"Lineup {lu} | Tactics {tac}"
    embed.add_field(name=f"{st.home.team.name} Submissions", value=_status("home", session.home_manager_id), inline=False)
    embed.add_field(name=f"{st.away.team.name} Submissions", value=_status("away", session.away_manager_id), inline=False)
    deadline_at = sub.get("deadline_at")
    if deadline_at:
        try:
            ts = int(datetime.fromisoformat(str(deadline_at).replace("Z", "+00:00")).timestamp())
            embed.add_field(name="Submission Deadline", value=f"<t:{ts}:R> (<t:{ts}:T>)", inline=False)
        except Exception:
            embed.add_field(name="Submission Deadline", value=str(deadline_at), inline=False)
    embed.set_footer(text=f"Match ID: {st.id} • Tactics are private")
    return embed


class ManagerSetupView(discord.ui.View):
    def __init__(self, cog: "SimCog", session, *, timeout: float | None = None):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.session = session
        self.message: discord.Message | None = None
        # Stable custom IDs for persistent dispatch / restart safety
        mid = str(getattr(session.state, "id", "") or "")
        try:
            self.view_squad.custom_id = f"ms:{mid}:squad"
            self.submit_lineup.custom_id = f"ms:{mid}:lineup"
            self.submit_tactics.custom_id = f"ms:{mid}:tactics"
            self.cancel_match.custom_id = f"ms:{mid}:cancel"
        except Exception:
            pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    def _resolve_side(self, interaction: discord.Interaction) -> str | None:
        uid = interaction.user.id if interaction.user else None
        if uid is not None and uid == self.session.home_manager_id:
            return "home"
        if uid is not None and uid == self.session.away_manager_id:
            return "away"
        return None

    async def _refresh_message(self, interaction: discord.Interaction | None = None):
        embed = build_match_setup_embed(self.session)
        try:
            if interaction and not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            elif self.message:
                await self.message.edit(embed=embed, view=self)
        except Exception:
            logger.exception("Failed to refresh setup message")

    @discord.ui.button(label="View Squad", style=discord.ButtonStyle.secondary)
    async def view_squad(self, interaction: discord.Interaction, button: discord.ui.Button):
        cid = (interaction.data or {}).get("custom_id", "")
        if isinstance(cid, str) and cid.startswith("ms:"):
            return  # handled by SimCog.on_interaction persistent dispatcher
        side = self._resolve_side(interaction)
        if not side:
            await interaction.response.send_message("Only assigned managers can view their squad privately here.", ephemeral=True)
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        try:
            team = self.session.state.team(side)
            squad = await self.cog.bot.football_data.get_squad(team.team.api_id)
            text = format_squad_for_manager(squad)
            header = f"**{team.team.name} — Available Squad**\nFormation: `{team.lineup.formation}` (current)\n\n"
            await interaction.followup.send(_clamp(header + "```\n" + text + "\n```", 1900), ephemeral=True)
        except Exception:
            logger.exception("Failed to show squad")
            try:
                await interaction.followup.send("Failed to load squad. Please try again.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Submit Lineup", style=discord.ButtonStyle.primary)
    async def submit_lineup(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            cid = (interaction.data or {}).get("custom_id", "")
            if isinstance(cid, str) and cid.startswith("ms:"):
                return  # handled by SimCog.on_interaction persistent dispatcher
            side = self._resolve_side(interaction)
            if not side:
                await interaction.response.send_message("Only assigned managers can submit a lineup for their side.", ephemeral=True)
                return
            if self.session.state.status not in {"setup"}:
                await interaction.response.send_message("Lineup submissions are closed for this match.", ephemeral=True)
                return

            async def on_submit(inter: discord.Interaction, payload: dict[str, Any]):
                await self.cog._handle_lineup_submission(inter, self.session, side, payload, self)

            await interaction.response.send_modal(LineupSubmitModal(on_submit))
        except Exception:
            logger.exception("Failed to open lineup modal")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Failed to open lineup form. Please try again.", ephemeral=True)
                else:
                    await interaction.followup.send("Failed to open lineup form. Please try again.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Submit Tactics", style=discord.ButtonStyle.success)
    async def submit_tactics(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            cid = (interaction.data or {}).get("custom_id", "")
            if isinstance(cid, str) and cid.startswith("ms:"):
                return  # handled by SimCog.on_interaction persistent dispatcher
            side = self._resolve_side(interaction)
            if not side:
                await interaction.response.send_message("Only assigned managers can submit tactics for their side.", ephemeral=True)
                return
            if self.session.state.status not in {"setup"}:
                await interaction.response.send_message("Tactics submissions are closed for this match.", ephemeral=True)
                return

            async def on_submit(inter: discord.Interaction, updates: dict[str, Any], summary: str | None):
                await self.cog._handle_tactics_submission(inter, self.session, side, updates, summary, self)

            await interaction.response.send_modal(TacticsTextModal(on_submit))
        except Exception:
            logger.exception("Failed to open tactics modal (setup)")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Failed to open tactics form. Please try again.", ephemeral=True)
                else:
                    await interaction.followup.send("Failed to open tactics form. Please try again.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Cancel Match (Admin)", style=discord.ButtonStyle.danger)
    async def cancel_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        cid = (interaction.data or {}).get("custom_id", "")
        if isinstance(cid, str) and cid.startswith("ms:"):
            return  # handled by SimCog.on_interaction persistent dispatcher
        perms = getattr(interaction.user, "guild_permissions", None)
        is_admin = bool(perms and (perms.administrator or perms.manage_guild))
        is_manager = interaction.user.id in {self.session.home_manager_id, self.session.away_manager_id}
        if not (is_admin or is_manager):
            await interaction.response.send_message("Only a manager or server admin can cancel this match.", ephemeral=True)
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass
        try:
            self.session.state.status = "cancelled"
            self.session.state.phase = "cancelled"
            await self.session.save()
            for child in self.children:
                child.disabled = True
            embed = build_match_setup_embed(self.session)
            try:
                await interaction.edit_original_response(content="Match cancelled.", embed=embed, view=self)
            except Exception:
                if self.message:
                    await self.message.edit(content="Match cancelled.", embed=embed, view=self)
        except Exception:
            logger.exception("Failed to cancel match")
            try:
                await interaction.followup.send("Failed to cancel match. Please try again.", ephemeral=True)
            except Exception:
                pass


class SimCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._recovery_done = False
        self._deadline_tasks: dict[str, asyncio.Task] = {}

    def _side_manager_id(self, session, side: str) -> int | None:
        return session.home_manager_id if side == "home" else session.away_manager_id

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Persistent fallback handler for setup/break buttons.
        Handles custom IDs even when the original View object is no longer in cache
        (after restart, timeout, or cache eviction).
        """
        cid = ""
        try:
            if interaction.type != discord.InteractionType.component:
                return
            data = interaction.data or {}
            raw_cid = data.get("custom_id")
            if not isinstance(raw_cid, str):
                return
            cid = raw_cid
            if cid.startswith("ms:"):
                await self._handle_ms_component(interaction, cid)
            elif cid.startswith("br:"):
                await self._handle_br_component(interaction, cid)
        except Exception:
            logger.exception("on_interaction persistent handler failed cid=%s", cid)
            if isinstance(cid, str) and (cid.startswith("ms:") or cid.startswith("br:")):
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(
                            "Something went wrong handling that button. Please try again, or use /tactics as a fallback.",
                            ephemeral=True,
                        )
                    else:
                        await interaction.followup.send(
                            "Something went wrong handling that button. Please try again, or use /tactics as a fallback.",
                            ephemeral=True,
                        )
                except Exception:
                    pass

    async def _get_session_for_interaction(self, match_id: str):
        session = self.bot.match_service.sessions.get(match_id)
        if session is None:
            try:
                session = await self.bot.match_service.load_from_db(match_id)
            except Exception:
                logger.exception("Failed to load session for interaction match_id=%s", match_id)
                session = None
        return session

    async def _safe_send(self, interaction: discord.Interaction, content: str, *, ephemeral: bool = True):
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(content, ephemeral=ephemeral)
            else:
                await interaction.followup.send(content, ephemeral=ephemeral)
        except Exception:
            pass

    @staticmethod
    def _resolve_side_for_session(session, user_id: int | None) -> str | None:
        if user_id is None:
            return None
        if user_id == session.home_manager_id:
            return "home"
        if user_id == session.away_manager_id:
            return "away"
        return None

    async def _handle_ms_component(self, interaction: discord.Interaction, cid: str):
        # Format: ms:<match_id>:<action>
        parts = cid.split(":")
        if len(parts) != 3:
            await self._safe_send(interaction, "Invalid button. Please try again.", ephemeral=True)
            return
        _, match_id, action = parts
        uid = interaction.user.id if interaction.user else None

        # Fast in-memory lookup; avoid DB load before modal responses.
        session = self.bot.match_service.sessions.get(match_id)

        if action in {"lineup", "tactics"}:
            # Modal must be the initial response. Validate quickly from memory when possible;
            # always revalidate in modal submit callback.
            if session is not None:
                side = self._resolve_side_for_session(session, uid)
                if not side:
                    await self._safe_send(interaction, "Only assigned managers can submit for their side.", ephemeral=True)
                    return
                if session.state.status != "setup":
                    await self._safe_send(interaction, "Submissions are closed for this match.", ephemeral=True)
                    return

            if action == "lineup":
                async def on_submit_lineup(inter: discord.Interaction, payload: dict[str, Any]):
                    try:
                        if not inter.response.is_done():
                            await inter.response.defer(ephemeral=True)
                    except Exception:
                        pass
                    try:
                        sess = await self._get_session_for_interaction(match_id)
                        if sess is None:
                            await self._safe_send(inter, "This match session is no longer available.", ephemeral=True)
                            return
                        s = self._resolve_side_for_session(sess, inter.user.id if inter.user else None)
                        if not s:
                            await self._safe_send(inter, "Only assigned managers can submit this lineup.", ephemeral=True)
                            return
                        await self._handle_lineup_submission(inter, sess, s, payload, None)
                    except Exception:
                        logger.exception("ms:lineup modal submit failed match_id=%s", match_id)
                        await self._safe_send(inter, "Failed to submit lineup. Please try again.", ephemeral=True)
                try:
                    await interaction.response.send_modal(LineupSubmitModal(on_submit_lineup))
                except Exception:
                    logger.exception("ms:lineup send_modal failed match_id=%s", match_id)
                    await self._safe_send(interaction, "Failed to open lineup form. Please retry, or use /lineup.", ephemeral=True)
                return

            # tactics
            async def on_submit_tactics(inter: discord.Interaction, updates: dict[str, Any], summary: str | None):
                try:
                    if not inter.response.is_done():
                        await inter.response.defer(ephemeral=True)
                except Exception:
                    pass
                try:
                    sess = await self._get_session_for_interaction(match_id)
                    if sess is None:
                        await self._safe_send(inter, "This match session is no longer available.", ephemeral=True)
                        return
                    s = self._resolve_side_for_session(sess, inter.user.id if inter.user else None)
                    if not s:
                        await self._safe_send(inter, "Only assigned managers can submit tactics for this side.", ephemeral=True)
                        return
                    await self._handle_tactics_submission(inter, sess, s, updates, summary, None)
                except Exception:
                    logger.exception("ms:tactics modal submit failed match_id=%s", match_id)
                    await self._safe_send(inter, "Failed to save tactics. Please try again, or use /tactics.", ephemeral=True)
            try:
                await interaction.response.send_modal(TacticsTextModal(on_submit_tactics))
            except Exception:
                logger.exception("ms:tactics send_modal failed match_id=%s", match_id)
                await self._safe_send(interaction, "Failed to open tactics form. Please retry, or use /tactics.", ephemeral=True)
            return

        # Non-modal actions: defer immediately to avoid 3s timeout
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        if session is None:
            session = await self._get_session_for_interaction(match_id)
        if session is None:
            await self._safe_send(interaction, "This match session is no longer available. Please start a new /sim.", ephemeral=True)
            return

        side = self._resolve_side_for_session(session, uid)

        if action == "squad":
            if not side:
                await self._safe_send(interaction, "Only assigned managers can view their squad privately here.", ephemeral=True)
                return
            try:
                team = session.state.team(side)
                squad = await self.bot.football_data.get_squad(team.team.api_id)
                text = format_squad_for_manager(squad)
                header = f"**{team.team.name} — Available Squad**\nFormation: `{team.lineup.formation}` (current)\n\n"
                await interaction.followup.send(_clamp(header + "```\n" + text + "\n```", 1900), ephemeral=True)
            except Exception:
                logger.exception("ms:squad failed match_id=%s", match_id)
                await self._safe_send(interaction, "Failed to load squad. Please try again.", ephemeral=True)
            return

        if action == "cancel":
            perms = getattr(interaction.user, "guild_permissions", None)
            is_admin = bool(perms and (perms.administrator or perms.manage_guild))
            is_manager = uid in {session.home_manager_id, session.away_manager_id}
            if not (is_admin or is_manager):
                await self._safe_send(interaction, "Only a manager or server admin can cancel this match.", ephemeral=True)
                return
            try:
                session.state.status = "cancelled"
                session.state.phase = "cancelled"
                await session.save()
                try:
                    ch = self.bot.get_channel(session.state.channel_id) if session.state.channel_id else None
                    if ch is None and session.state.channel_id:
                        ch = await self.bot.fetch_channel(session.state.channel_id)
                    if ch and session.state.message_id:
                        msg = await ch.fetch_message(int(session.state.message_id))
                        await msg.edit(content="Match cancelled.", embed=build_match_setup_embed(session), view=None)
                except Exception:
                    pass
                await self._safe_send(interaction, "Match cancelled.", ephemeral=True)
            except Exception:
                logger.exception("ms:cancel failed match_id=%s", match_id)
                await self._safe_send(interaction, "Failed to cancel match. Please try again.", ephemeral=True)
            return

        await self._safe_send(interaction, "Unknown setup action. Please try again.", ephemeral=True)

    async def _handle_br_component(self, interaction: discord.Interaction, cid: str):
        # Format: br:<match_id>:<action>
        parts = cid.split(":")
        if len(parts) != 3:
            await self._safe_send(interaction, "Invalid button. Please try again.", ephemeral=True)
            return
        _, match_id, action = parts
        uid = interaction.user.id if interaction.user else None

        session = self.bot.match_service.sessions.get(match_id)

        if action == "submit":
            # Modal must be the initial response. Fast in-memory validation when possible.
            if session is not None:
                side = self._resolve_side_for_session(session, uid)
                if not side:
                    await self._safe_send(interaction, "Only assigned human managers can submit tactics for their side.", ephemeral=True)
                    return
                if session.state.status in {"finished", "cancelled"} or not session.state.pending_break:
                    await self._safe_send(interaction, "No active tactical break for this match.", ephemeral=True)
                    return

            async def on_submit(inter: discord.Interaction, updates: dict[str, Any], summary: str | None):
                try:
                    if not inter.response.is_done():
                        await inter.response.defer(ephemeral=True)
                except Exception:
                    pass
                try:
                    sess = await self._get_session_for_interaction(match_id)
                    if sess is None:
                        await inter.followup.send("This match session is no longer available.", ephemeral=True)
                        return
                    s = self._resolve_side_for_session(sess, inter.user.id if inter.user else None)
                    if not s:
                        await inter.followup.send("Only assigned human managers can submit tactics for their side.", ephemeral=True)
                        return
                    if sess.state.status in {"finished", "cancelled"} or not sess.state.pending_break:
                        await inter.followup.send("No active tactical break for this match.", ephemeral=True)
                        return
                    await sess.apply_break_actions(s, substitutions=[], tactics_updates=updates, summary=summary or "Manager submitted tactics")
                    await inter.followup.send(f"{s.title()} tactics submitted privately and side marked ready.", ephemeral=True)
                except Exception:
                    logger.exception("br:submit modal save failed match_id=%s", match_id)
                    try:
                        await inter.followup.send("Failed to save tactics. Please try again, or use /tactics.", ephemeral=True)
                    except Exception:
                        pass

            try:
                await interaction.response.send_modal(TacticsTextModal(on_submit))
            except Exception:
                logger.exception("br:submit send_modal failed match_id=%s", match_id)
                await self._safe_send(interaction, "Failed to open tactics form. Please use /tactics as a fallback.", ephemeral=True)
            return

        if action == "subs":
            if session is not None:
                side = self._resolve_side_for_session(session, uid)
                if not side:
                    await self._safe_send(interaction, "Only assigned human managers can submit subs for their side.", ephemeral=True)
                    return
                if session.state.status in {"finished", "cancelled"} or not session.state.pending_break:
                    await self._safe_send(interaction, "No active tactical break for this match.", ephemeral=True)
                    return

            async def on_submit_subs(inter: discord.Interaction, raw: str):
                try:
                    if not inter.response.is_done():
                        await inter.response.defer(ephemeral=True)
                except Exception:
                    pass
                try:
                    sess = await self._get_session_for_interaction(match_id)
                    if sess is None:
                        await inter.followup.send("This match session is no longer available.", ephemeral=True)
                        return
                    s = self._resolve_side_for_session(sess, inter.user.id if inter.user else None)
                    if not s:
                        await inter.followup.send("Only assigned human managers can submit subs for their side.", ephemeral=True)
                        return
                    if sess.state.status in {"finished", "cancelled"} or not sess.state.pending_break:
                        await inter.followup.send("No active tactical break for this match.", ephemeral=True)
                        return
                    subs = parse_break_subs_text(raw, sess.state, s)
                    evs = await sess.apply_break_actions(s, substitutions=subs, tactics_updates={}, summary=None, mark_done=False)
                    sub_txt = ", ".join([f"{x['off_name']}→{x['on_name']}" for x in subs])
                    await inter.followup.send(f"{s.title()} substitutions applied: {sub_txt}. You can still submit tactics or keep current tactics.".strip()[:1900], ephemeral=True)
                    # Broadcast to match channel so everyone sees the subs
                    try:
                        ch = self.bot.get_channel(sess.state.channel_id) if sess.state.channel_id else None
                        if ch is None and sess.state.channel_id:
                            ch = await self.bot.fetch_channel(sess.state.channel_id)
                        if ch and evs:
                            await self._send_events_live(ch, evs, delay=0.5, state=sess.state)
                    except Exception:
                        logger.exception("Failed to broadcast substitution events match_id=%s", match_id)
                except (LineupValidationError, ValueError) as e:
                    try:
                        await inter.followup.send(f"Invalid substitutions: {e}", ephemeral=True)
                    except Exception:
                        pass
                except Exception:
                    logger.exception("br:subs modal save failed match_id=%s", match_id)
                    try:
                        await inter.followup.send("Failed to apply substitutions. Please try again.", ephemeral=True)
                    except Exception:
                        pass

            try:
                await interaction.response.send_modal(SubsTextModal(on_submit_subs))
            except Exception:
                logger.exception("br:subs send_modal failed match_id=%s", match_id)
                await self._safe_send(interaction, "Failed to open subs form. Please try again.", ephemeral=True)
            return

        # Non-modal actions: defer immediately
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        if session is None:
            session = await self._get_session_for_interaction(match_id)
        if session is None:
            await self._safe_send(interaction, "This match session is no longer available.", ephemeral=True)
            return

        side = self._resolve_side_for_session(session, uid)

        if action == "keep":
            if not side:
                await self._safe_send(interaction, "Only assigned human managers can confirm for their side in manager mode.", ephemeral=True)
                return
            if session.state.status in {"finished", "cancelled"} or not session.state.pending_break:
                await self._safe_send(interaction, "No active tactical break for this match.", ephemeral=True)
                return
            try:
                await session.apply_break_actions(side, substitutions=[], tactics_updates={}, summary=None)
                await self._safe_send(interaction, f"{side.title()} side: current tactics kept and marked ready.", ephemeral=True)
            except Exception:
                logger.exception("br:keep failed match_id=%s side=%s", match_id, side)
                await self._safe_send(interaction, "Failed to keep current tactics. Please try again.", ephemeral=True)
            return

        if action == "ai":
            if not side:
                await self._safe_send(interaction, "Only assigned human managers can use this for their side.", ephemeral=True)
                return
            if session.state.status in {"finished", "cancelled"} or not session.state.pending_break:
                await self._safe_send(interaction, "No active tactical break for this match.", ephemeral=True)
                return
            try:
                from engine.ai_manager import decide_ai_break_actions
                actions = decide_ai_break_actions(session.state, side)
                evs = await session.apply_break_actions(side, substitutions=actions.get("substitutions", []), tactics_updates=actions.get("tactics_updates", {}), summary="Manager accepted AI adjustment")
                subs = actions.get("substitutions", [])
                txt = "AI adjustments applied and side marked ready."
                if subs:
                    txt += " Subs: " + ", ".join([f"{s.get('on_name')} for {s.get('off_name')}" for s in subs])
                await self._safe_send(interaction, txt[:1900], ephemeral=True)
                # Broadcast substitution/tactical events to match channel
                try:
                    ch = self.bot.get_channel(session.state.channel_id) if session.state.channel_id else None
                    if ch is None and session.state.channel_id:
                        ch = await self.bot.fetch_channel(session.state.channel_id)
                    if ch and evs:
                        await self._send_events_live(ch, evs, delay=0.5, state=session.state)
                except Exception:
                    logger.exception("Failed to broadcast AI break events match_id=%s", match_id)
            except Exception:
                logger.exception("br:ai failed match_id=%s side=%s", match_id, side)
                await self._safe_send(interaction, "Failed to apply AI adjustments. Please try again.", ephemeral=True)
            return

        await self._safe_send(interaction, "Unknown break action. Please try again.", ephemeral=True)

    async def _handle_lineup_submission(self, interaction: discord.Interaction, session, side: str, payload: dict[str, Any], view: ManagerSetupView | None = None):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            logger.exception("Failed to defer lineup submission interaction")
        try:
            if self._side_manager_id(session, side) != interaction.user.id:
                await interaction.followup.send("Only the assigned manager can submit this lineup.", ephemeral=True)
                return
            if session.state.status != "setup":
                await interaction.followup.send("Lineup submissions are closed for this match.", ephemeral=True)
                return
            sub = session.state.submission or {}
            if sub.get(f"{side}_lineup_submitted") and sub.get(f"{side}_tactics_submitted"):
                await interaction.followup.send("Your lineup and tactics are already submitted. Use setup controls before kickoff only.", ephemeral=True)
                return
            team = session.state.team(side)
            squad = await self.bot.football_data.get_squad(team.team.api_id)
            try:
                lu = parse_lineup_submission(payload.get("formation", ""), payload.get("starters_raw", ""), payload.get("bench_raw", ""), squad, team.team.api_id, team.team.name)
            except LineupValidationError as e:
                await interaction.followup.send(f"Lineup invalid: {e}", ephemeral=True)
                return
            except Exception:
                logger.exception("Lineup submission parse failed")
                await interaction.followup.send("Could not validate lineup. Please check names/numbers and try again.", ephemeral=True)
                return

            # Check availability (injured/unavailable)
            squad_by_id = {int(p.get("api_id") or 0): p for p in squad}
            unavailable = []
            for e in list(lu.starters) + list(lu.bench):
                p = squad_by_id.get(int(e.player_api_id))
                if not p:
                    continue
                status = str(p.get("status") or "active").lower()
                if p.get("injured") or status in {"injured", "unavailable", "suspended"}:
                    unavailable.append(f"{e.name} ({status if status != 'active' else 'injured'})")
            if unavailable:
                await interaction.followup.send(
                    "Lineup invalid: unavailable players selected: " + ", ".join(unavailable[:10]),
                    ephemeral=True,
                )
                return

            # Apply lineup to state (replace placeholder). Keep tactics.
            team.lineup = lu
            # Rebuild player states for this side to match submitted lineup
            session.engine._init_ps(side, lu)
            # Ensure players_index has all squad players
            from engine.models import Player, PlayerAttributes
            for p in squad:
                key = session.state.player_key(side, int(p.get("api_id") or 0))
                if key not in session.state.players_index:
                    attrs = PlayerAttributes.from_dict(p.get("attributes") or {})
                    session.state.players_index[key] = Player(
                        api_id=int(p.get("api_id") or 0), name=p.get("name") or "Unknown", team_api_id=team.team.api_id,
                        position=(p.get("position") or "MID").upper(), detailed_position=p.get("detailed_position"), number=p.get("number"),
                        nationality=p.get("nationality"), age=p.get("age"), photo=p.get("photo"), injured=bool(p.get("injured", False)),
                        attributes=attrs,
                    )
            sub[f"{side}_lineup_submitted"] = True
            sub[f"{side}_lineup_submitted_at"] = datetime.now(timezone.utc).isoformat()
            await session.save()

            await interaction.followup.send(
                f"Lineup submitted privately for **{team.team.name}** ({lu.formation}).", ephemeral=True
            )
            await self._after_submission_update(session, side, view)
        except Exception:
            logger.exception("Failed to handle lineup submission")
            try:
                await interaction.followup.send("Failed to submit lineup. Please try again.", ephemeral=True)
            except Exception:
                pass

    async def _handle_tactics_submission(self, interaction: discord.Interaction, session, side: str, updates: dict[str, Any], summary: str | None, view: ManagerSetupView | None = None):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            logger.exception("Failed to defer tactics submission interaction")
        try:
            if self._side_manager_id(session, side) != interaction.user.id:
                await interaction.followup.send("Only the assigned manager can submit tactics for this side.", ephemeral=True)
                return
            if session.state.status != "setup":
                await interaction.followup.send("Tactics submissions are closed for this match setup.", ephemeral=True)
                return
            sub = session.state.submission or {}
            team = session.state.team(side)
            # Apply privately
            from services.tactics_service import normalize_tactics as _norm
            team.tactics = _norm({**(team.tactics or {}), **(updates or {})})
            sub[f"{side}_tactics_submitted"] = True
            sub[f"{side}_tactics_submitted_at"] = datetime.now(timezone.utc).isoformat()
            await session.save()

            await interaction.followup.send(
                f"Tactics submitted privately for **{team.team.name}**. Confirmation: received.", ephemeral=True
            )
            await self._after_submission_update(session, side, view)
        except Exception:
            logger.exception("Failed to handle tactics submission")
            try:
                await interaction.followup.send("Failed to save tactics. Please try again.", ephemeral=True)
            except Exception:
                pass

    async def _after_submission_update(self, session, side: str, view: ManagerSetupView | None = None):
        sub = session.state.submission or {}
        # If this side is fully submitted and the other isn't, extend deadline to 10 min from now
        if session.side_submission_complete(side):
            sub[f"{side}_submitted_at"] = datetime.now(timezone.utc).isoformat()
            other = "away" if side == "home" else "home"
            other_mgr = self._side_manager_id(session, other)
            if other_mgr is not None and not session.side_submission_complete(other):
                now = datetime.now(timezone.utc)
                ext = int(sub.get("extension_seconds") or 600)
                new_deadline = now + timedelta(seconds=ext)
                # Always set to 10 min from now per spec
                sub["deadline_at"] = new_deadline.isoformat()
                try:
                    ch = self.bot.get_channel(session.state.channel_id) if session.state.channel_id else None
                    if ch:
                        ts = int(new_deadline.timestamp())
                        await ch.send(_clamp(f"⏳ <@{other_mgr}> your opponent has submitted their lineup and tactics. You have 10 minutes to submit yours (deadline <t:{ts}:R>).", 1900))
                except Exception:
                    logger.exception("Failed to send extension notice")
        await session.save()
        # Refresh setup message
        try:
            embed = build_match_setup_embed(session)
            if view and view.message:
                await view.message.edit(embed=embed, view=view)
            elif session.state.channel_id and session.state.message_id:
                ch = self.bot.get_channel(session.state.channel_id) if session.state.channel_id else None
                if ch is None:
                    try:
                        ch = await self.bot.fetch_channel(session.state.channel_id)
                    except Exception:
                        ch = None
                if ch:
                    try:
                        msg = await ch.fetch_message(int(session.state.message_id))
                        if view:
                            view.message = msg
                            await msg.edit(embed=embed, view=view)
                        else:
                            await msg.edit(embed=embed)
                    except Exception:
                        pass
        except Exception:
            logger.exception("Failed to refresh setup embed")
        # Start if ready
        if session.is_setup_complete() and session.state.status == "setup":
            await self._start_manager_match(session, view)

    async def _start_manager_match(self, session, view: ManagerSetupView | None = None):
        # prevent duplicate starts
        sub = session.state.submission or {}
        if sub.get("match_started"):
            return
        if not session.is_setup_complete():
            return
        sub["match_started"] = True
        await session.save()
        try:
            if view:
                for child in view.children:
                    child.disabled = True
                if view.message:
                    await view.message.edit(embed=build_match_setup_embed(session), view=view)
                elif session.state.channel_id and session.state.message_id:
                    ch = self.bot.get_channel(session.state.channel_id) if session.state.channel_id else None
                    if ch is None:
                        try:
                            ch = await self.bot.fetch_channel(session.state.channel_id)
                        except Exception:
                            ch = None
                    if ch:
                        try:
                            msg = await ch.fetch_message(int(session.state.message_id))
                            await msg.edit(embed=build_match_setup_embed(session), view=None)
                        except Exception:
                            pass
            elif session.state.channel_id and session.state.message_id:
                ch = self.bot.get_channel(session.state.channel_id) if session.state.channel_id else None
                if ch is None:
                    try:
                        ch = await self.bot.fetch_channel(session.state.channel_id)
                    except Exception:
                        ch = None
                if ch:
                    try:
                        msg = await ch.fetch_message(int(session.state.message_id))
                        await msg.edit(embed=build_match_setup_embed(session), view=None)
                    except Exception:
                        pass
        except Exception:
            logger.exception("Failed to disable setup view on match start")
        try:
            ch = self.bot.get_channel(session.state.channel_id) if session.state.channel_id else None
            if ch:
                await ch.send(_clamp(f"✅ Both managers are ready. Starting match: **{session.state.home.team.name}** vs **{session.state.away.team.name}**", 1900))
        except Exception:
            pass
        # Launch the match flow as a task. We need a fake interaction-like channel context; _run_match_flow takes (interaction, session) but only uses interaction.channel
        # Create a small shim
        class _Shim:
            channel = self.bot.get_channel(session.state.channel_id) if session.state.channel_id else None
        shim = _Shim()
        if shim.channel is None:
            # try fetching
            try:
                shim.channel = await self.bot.fetch_channel(session.state.channel_id) if session.state.channel_id else None
            except Exception:
                shim.channel = None
        if shim.channel is not None:
            asyncio.create_task(self._run_match_flow(shim, session))

    def _start_deadline_watcher(self, session, view: ManagerSetupView | None = None):
        mid = getattr(session.state, "id", None)
        if not mid:
            return None
        existing = self._deadline_tasks.get(mid)
        if existing and not existing.done():
            return existing
        task = asyncio.create_task(self._watch_submission_deadline(session, view))
        self._deadline_tasks[mid] = task
        return task

    async def _watch_submission_deadline(self, session, view: ManagerSetupView | None = None):
        """Watch manager submission deadline. Never auto-picks lineups for humans."""
        try:
            while session.state.status == "setup":
                sub = session.state.submission or {}
                if sub.get("match_started"):
                    return
                if session.is_setup_complete():
                    await self._start_manager_match(session, view)
                    return
                deadline_at = sub.get("deadline_at")
                if not deadline_at:
                    await asyncio.sleep(5)
                    continue
                try:
                    dl = datetime.fromisoformat(str(deadline_at).replace("Z", "+00:00"))
                except Exception:
                    await asyncio.sleep(5)
                    continue
                now = datetime.now(timezone.utc)
                remaining = (dl - now).total_seconds()
                if remaining > 0:
                    await asyncio.sleep(min(remaining, 15))
                    continue
                # Deadline reached
                if sub.get("match_started") or session.state.status != "setup":
                    return
                missing = session.missing_submissions()
                if not missing:
                    await self._start_manager_match(session, view)
                    return
                policy = str(sub.get("timeout_policy") or "cancel").lower()
                ch = self.bot.get_channel(session.state.channel_id) if session.state.channel_id else None
                if ch is None and session.state.channel_id:
                    try:
                        ch = await self.bot.fetch_channel(session.state.channel_id)
                    except Exception:
                        ch = None
                if policy == "forfeit" and len(missing) == 1:
                    # Forfeit: cancel with explicit result note; do not auto-pick lineup
                    forfeiter = missing[0]
                    winner = "away" if forfeiter == "home" else "home"
                    session.state.status = "cancelled"
                    session.state.phase = "forfeited"
                    sub["forfeit_side"] = forfeiter
                    sub["winner_side"] = winner
                    await session.save()
                    if view:
                        for child in view.children:
                            child.disabled = True
                        try:
                            if view.message:
                                await view.message.edit(embed=build_match_setup_embed(session), view=view)
                        except Exception:
                            pass
                    if ch:
                        mgr = self._side_manager_id(session, forfeiter)
                        who = f"<@{mgr}>" if mgr else forfeiter.title()
                        await ch.send(_clamp(f"⏱️ Submission deadline reached. {who} did not submit required lineup/tactics. Match forfeited by timeout policy. No lineup was auto-selected.", 1900))
                    return
                # Default: cancel / admin intervention. Never auto-pick.
                session.state.status = "cancelled"
                session.state.phase = "cancelled_timeout"
                sub["timeout_missing_sides"] = missing
                await session.save()
                if view:
                    for child in view.children:
                        child.disabled = True
                    try:
                        if view.message:
                            await view.message.edit(embed=build_match_setup_embed(session), view=view)
                    except Exception:
                        pass
                if ch:
                    missing_mentions = []
                    for s in missing:
                        mgr = self._side_manager_id(session, s)
                        missing_mentions.append(f"<@{mgr}>" if mgr else s.title())
                    await ch.send(_clamp(
                        "⏱️ Submission deadline reached. Missing submissions: " + ", ".join(missing_mentions) +
                        ". Match cancelled by timeout policy. No lineup was auto-selected. An administrator can create a new match or reset as needed.",
                        1900
                    ))
                return
        except Exception:
            logger.exception("Submission deadline watcher failed match_id=%s", getattr(session.state, "id", None))
        finally:
            mid = getattr(session.state, "id", None)
            if mid:
                self._deadline_tasks.pop(mid, None)

    @commands.Cog.listener()
    async def on_ready(self):
        if self._recovery_done:
            return
        self._recovery_done = True
        asyncio.create_task(self._recover_pending_setups())

    async def _recover_pending_setups(self):
        try:
            rows = await self.bot.db.list_recoverable_matches()
        except Exception:
            logger.exception("Failed to list recoverable matches")
            return
        for row in rows:
            try:
                state_raw = row["state_json"] if isinstance(row, dict) else row["state_json"]
                state = self.bot.db.loads(state_raw)
                if not isinstance(state, dict):
                    continue
                if state.get("status") != "setup":
                    continue
                if state.get("mode") != "manager_vs_manager":
                    continue
                match_id = state.get("id") or (row["id"] if "id" in row.keys() else None)
                if not match_id:
                    continue
                session = await self.bot.match_service.load_from_db(str(match_id))
                if not session:
                    continue
                if session.state.status != "setup":
                    continue
                sub = session.state.submission or {}
                if sub.get("match_started"):
                    continue
                # Rebuild view
                view = ManagerSetupView(self, session, timeout=None)
                ch = self.bot.get_channel(session.state.channel_id) if session.state.channel_id else None
                if ch is None and session.state.channel_id:
                    try:
                        ch = await self.bot.fetch_channel(session.state.channel_id)
                    except Exception:
                        ch = None
                if ch and session.state.message_id:
                    try:
                        msg = await ch.fetch_message(int(session.state.message_id))
                        view.message = msg
                        await msg.edit(embed=build_match_setup_embed(session), view=view)
                    except Exception:
                        pass
                if session.is_setup_complete():
                    asyncio.create_task(self._start_manager_match(session, view))
                    logger.info("Recovered ready manager setup match_id=%s; starting", session.state.id)
                    continue
                # Re-attach deadline watcher
                self._start_deadline_watcher(session, view)
                logger.info("Recovered pending manager setup match_id=%s", session.state.id)
            except Exception:
                logger.exception("Failed to recover pending setup row")

    async def _send_events_live(self, channel: discord.abc.Messageable, events: list[dict[str, Any]], delay: float = 3.0, state=None):
        import re as _re
        events = list(events or [])
        match_id = getattr(state, "id", None) if state is not None else None
        if not events:
            logger.warning("MATCH_FLOW send_events_live: empty events list match_id=%s", match_id)
            return

        # Presentation fast-forward: skip low-importance moments (no simulation speed change).
        CRITICAL = {
            "goal", "penalty_scored", "penalty_missed", "penalty_saved", "own_goal",
            "second_yellow", "red_card", "injury", "substitution", "tactical_change",
            "var_review", "var_overturn", "disallowed_goal",
            "half_time", "full_time", "tactical_break", "break_resume",
            "kickoff", "second_half_kickoff", "stoppage_time",
        }
        NOTABLE = {
            "shot", "save", "block", "chance", "penalty_awarded", "yellow_card",
            "corner", "free_kick",
        }
        # Routine events are fast-forwarded: no message, no pacing delay.
        ROUTINE_SKIP = {"pass_sequence", "note"}

        sent = 0
        silent = 0
        skipped = 0
        for ev in events:
            et = ev.get("event_type")
            text = ev.get("commentary")
            payload = ev.get("payload") or {}

            # Fast-forward: skip routine non-important events entirely (presentation only).
            if et in ROUTINE_SKIP:
                skipped += 1
                continue

            if not text:
                # CHANCE events are intentionally paired with rich outcome narratives; avoid duplicate display
                if et != "chance":
                    text = payload.get("detail") or payload.get("summary") or ""
            if not text:
                # silent/internal event: skip without adding pacing delay
                silent += 1
                continue

            # Fast-forward: hide low-signal routine fouls/offside unless notable; keep pacing light.
            if et in {"foul", "offside"} and et not in CRITICAL:
                # show occasionally? skip to keep match moving; still counted as skipped
                skipped += 1
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
                    sent += 1
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
                    sent += 1
                elif et == "tactical_change" and state is not None:
                    minute = int(ev.get("minute", getattr(state, "minute", 0)) or 0)
                    extra = int(ev.get("extra_minute", getattr(state, "extra_minute", 0)) or 0)
                    mtxt = f"{minute}+{extra}'" if extra else f"{minute}'"
                    side = ev.get("team_side")
                    team_name = state.team(side).team.name if side else "Team"
                    detail = (payload.get("detail") or payload.get("summary") or "tactical adjustments made").strip()
                    msg = f"- **{mtxt}** — 🧠 **Tactical Change ({team_name}):** {detail}"
                    await channel.send(_clamp(msg, 1900))
                    sent += 1
                else:
                    await channel.send(_clamp(str(text), 1900))
                    sent += 1
            except Exception:
                logger.exception("Failed to send match event message")

            # Presentation pacing: keep normal feel for important moments, faster for routine.
            if delay > 0:
                if et in CRITICAL:
                    await asyncio.sleep(delay)
                elif et in NOTABLE:
                    await asyncio.sleep(min(delay, 1.2))
                else:
                    # other non-critical events: minimal pacing
                    await asyncio.sleep(min(delay, 0.4))
        logger.info("MATCH_FLOW send_events_live done match_id=%s total=%s sent=%s silent=%s skipped=%s", match_id, len(events), sent, silent, skipped)

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
                # Timeout: for human managers, keep current tactics (no AI auto-changes).
                for side in ("home", "away"):
                    mgr = session.home_manager_id if side == "home" else session.away_manager_id
                    done = session.state.home_break_decision_done if side == "home" else session.state.away_break_decision_done
                    if mgr is not None and not done:
                        try:
                            timeout_evs = await session.apply_break_actions(
                                side, substitutions=[], tactics_updates={}, summary="Kept current tactics (timeout)"
                            )
                            if timeout_evs:
                                action_evs.extend(timeout_evs)
                                await self._send_events_live(channel, timeout_evs, delay=1.0, state=session.state)
                        except Exception:
                            logger.exception("Failed to mark break timeout as keep-current for %s", side)
                await session.save()
                break
        return action_evs

    async def _run_match_flow(self, interaction: discord.Interaction, session):
        channel = interaction.channel
        if channel is None:
            return

        if not session.is_setup_complete() and session.state.status == "setup":
            logger.info("MATCH_FLOW blocked: setup incomplete match_id=%s", session.state.id)
            return

        st = session.state
        logger.info(
            "MATCH_FLOW start match_id=%s status=%s phase=%s minute=%s mode=%s home=%s away=%s home_mgr=%s away_mgr=%s",
            st.id, st.status, st.phase, st.minute, st.mode, st.home.team.name, st.away.team.name,
            session.home_manager_id, session.away_manager_id,
        )

        def _log_state(prefix: str) -> None:
            s = session.state
            logger.info(
                "MATCH_FLOW %s match_id=%s status=%s phase=%s minute=%s extra=%s pending_break=%s home_ready=%s away_ready=%s score=%s st1=%s st2=%s",
                prefix, s.id, s.status, s.phase, s.minute, getattr(s, "extra_minute", 0),
                s.pending_break, s.home_break_decision_done, s.away_break_decision_done,
                s.scoreline(), getattr(s, "stoppage_time_1", None), getattr(s, "stoppage_time_2", None),
            )

        async def _lineup_diag(side: str) -> None:
            try:
                lu = session.state.team(side).lineup
                ids = [int(e.player_api_id) for e in lu.starters]
                names = [str(e.name).strip().lower() for e in lu.starters]
                dup_ids = [x for i, x in enumerate(ids) if x in ids[:i]]
                dup_names = [x for i, x in enumerate(names) if x and x in names[:i]]
                logger.info(
                    "MATCH_LINEUP match_id=%s side=%s formation=%s starters=%s unique_ids=%s unique_names=%s dup_ids=%s dup_names=%s",
                    session.state.id, side, lu.formation, len(ids), len(set(ids)), len(set(names)), dup_ids[:5], dup_names[:5],
                )
            except Exception:
                logger.exception("MATCH_LINEUP diag failed match_id=%s side=%s", getattr(session.state, "id", None), side)

        try:
            await channel.send(_clamp(
                f"**Match Starting**\n{st.home.team.name} vs {st.away.team.name}\n"
                f"Match ID: `{st.id}` | Mode: {st.mode.replace('_', ' ').title()}"
            ))

            # Starting lineups
            try:
                await _lineup_diag("home")
                await _lineup_diag("away")
                await channel.send(_format_lineup(st, "home"))
                await channel.send(_format_lineup(st, "away"))
            except Exception:
                logger.exception("Failed to send starting lineups")

            # Main loop: always drive simulation forward until full time
            loop_guard = 0
            while session.state.status != "finished":
                loop_guard += 1
                if loop_guard % 5 == 1 or session.state.pending_break or session.state.status == "setup":
                    _log_state(f"loop#{loop_guard}")
                if loop_guard > 5000:
                    logger.error("MATCH_FLOW loop guard tripped match_id=%s; aborting to avoid infinite loop", session.state.id)
                    try:
                        await channel.send(_clamp(f"⚠️ Match flow stopped unexpectedly (safety guard). Match ID: `{session.state.id}`. Please check logs."))
                    except Exception:
                        pass
                    return
                try:
                    # If we're in a pending break, handle it and resume to next segment
                    if session.state.pending_break and session.engine.is_waiting_for_break_decisions():
                        pending = session.state.pending_break
                        _log_state(f"handling_break:{pending}")
                        await self._handle_break(channel, session)
                        _log_state(f"after_handle_break:{pending}")
                        # Ensure decisions are ready; force AI resolution if still stuck
                        if session.state.pending_break and not session.is_ready_to_resume():
                            try:
                                await session.auto_resolve_ai_sides_if_ready()
                                await session.save()
                            except Exception:
                                logger.exception("Failed to force-resolve break decisions")
                            _log_state(f"after_force_resolve:{pending}")
                        if session.state.pending_break and not session.is_ready_to_resume():
                            logger.info("MATCH_FLOW waiting for break decisions match_id=%s break=%s", session.state.id, session.state.pending_break)
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
                        logger.info(
                            "MATCH_FLOW resumed_after_break match_id=%s from=%s events=%s new_status=%s new_phase=%s minute=%s pending=%s",
                            session.state.id, pending, len(evs or []), session.state.status, session.state.phase, session.state.minute, session.state.pending_break,
                        )
                        await self._send_events_live(channel, evs, delay=3.0, state=session.state)
                        continue

                    # Start or simulate next segment (setup/normal)
                    if session.state.status == "setup":
                        kickoff_evs = await session.start_if_setup()
                        if kickoff_evs:
                            try:
                                await self._send_events_live(channel, kickoff_evs, delay=0.5, state=session.state)
                            except Exception:
                                logger.exception("Failed to send kickoff events match_id=%s", session.state.id)
                        interval_msg = _interval_text_for_state(session.state)
                        if interval_msg:
                            try:
                                await channel.send(interval_msg)
                            except Exception:
                                logger.exception("Failed to send interval announcement")
                        # Simulate rest of segment (engine already started)
                        async with session.lock:
                            try:
                                loop = asyncio.get_running_loop()
                                step = await loop.run_in_executor(None, session.engine.simulate_until_break_or_end)
                                sim_evs = step.events
                                await session.append_events(sim_evs)
                            except Exception:
                                logger.exception("MATCH_SERVICE simulate_until_break_or_end failed match_id=%s", session.state.id)
                                sim_evs = []
                            await session.save()
                        evs = sim_evs
                        logger.info(
                            "MATCH_FLOW started match_id=%s kickoff=%s events=%s status=%s phase=%s minute=%s pending=%s",
                            session.state.id, len(kickoff_evs or []), len(evs or []), session.state.status, session.state.phase, session.state.minute, session.state.pending_break,
                        )
                    elif session.state.status == "finished":
                        break
                    else:
                        interval_msg = _interval_text_for_state(session.state)
                        if interval_msg:
                            try:
                                await channel.send(interval_msg)
                            except Exception:
                                logger.exception("Failed to send interval announcement")
                        # Not in break and not finished: continue simulation to break/end
                        try:
                            evs = await session.continue_after_break()
                        except Exception:
                            logger.exception("MATCH_FLOW continue_after_break failed in running state match_id=%s", session.state.id)
                            evs = []
                        if not evs and session.state.status != "finished":
                            async with session.lock:
                                loop = asyncio.get_running_loop()
                                step = await loop.run_in_executor(None, session.engine.simulate_until_break_or_end)
                                evs = step.events
                                await session.append_events(evs)
                                await session.save()
                        logger.info(
                            "MATCH_FLOW progressed match_id=%s events=%s status=%s phase=%s minute=%s pending=%s",
                            session.state.id, len(evs or []), session.state.status, session.state.phase, session.state.minute, session.state.pending_break,
                        )

                    if evs:
                        await self._send_events_live(channel, evs, delay=3.0, state=session.state)
                    elif session.state.status != "finished" and not session.state.pending_break:
                        # Safety: avoid tight loop if nothing produced unexpectedly
                        logger.warning("MATCH_FLOW no events produced and not finished/break match_id=%s; sleeping", session.state.id)
                        await asyncio.sleep(0.5)
                    else:
                        # Empty events but state advanced to break/finished - likely engine bug or send issue.
                        logger.warning(
                            "MATCH_FLOW empty events with advanced state match_id=%s status=%s phase=%s minute=%s pending=%s",
                            session.state.id, session.state.status, session.state.phase, session.state.minute, session.state.pending_break,
                        )
                        try:
                            await channel.send(_clamp(
                                f"⚠️ No play-by-play events were produced for that interval (minute {session.state.minute}, phase {session.state.phase}). "
                                "Attempting to recover simulation..."
                            ))
                        except Exception:
                            pass
                        # Recovery: if at a break with no events, force-resolve and try to continue
                        if session.state.pending_break and session.engine.is_waiting_for_break_decisions():
                            try:
                                await session.auto_resolve_ai_sides_if_ready()
                                session.state.home_break_decision_done = True
                                session.state.away_break_decision_done = True
                                await session.save()
                            except Exception:
                                logger.exception("MATCH_FLOW recovery force-resolve failed match_id=%s", session.state.id)
                except Exception:
                    logger.exception("Match flow iteration failed; attempting recovery match_id=%s", session.state.id)
                    _log_state("recovery")
                    try:
                        if session.state.status != "finished" and not session.state.pending_break:
                            async with session.lock:
                                loop = asyncio.get_running_loop()
                                step = await loop.run_in_executor(None, session.engine.simulate_until_break_or_end)
                                recovery_evs = list(step.events or [])
                                if recovery_evs:
                                    try:
                                        await session.append_events(recovery_evs)
                                        await session.save()
                                    except Exception:
                                        logger.exception("MATCH_FLOW recovery persist failed match_id=%s", session.state.id)
                            if recovery_evs:
                                await self._send_events_live(channel, recovery_evs, delay=3.0, state=session.state)
                        await asyncio.sleep(0.5)
                    except Exception:
                        logger.exception("Match flow recovery failed match_id=%s", session.state.id)
                        await asyncio.sleep(1)

            # Full-time interval marker + report (stat sheet, injuries, physical condition, then summary/details/ratings/analysis)
            _log_state("finished_loop_exit")
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
                logger.info("MATCH_FLOW full-time report sent match_id=%s score=%s", session.state.id, session.state.scoreline())
            except Exception:
                logger.exception("Failed to send full-time report match_id=%s", session.state.id)
                try:
                    await channel.send(_clamp(f"Full-time: {session.state.scoreline()}"))
                except Exception:
                    pass
            try:
                persisted = await session.persist_player_stats()
                if persisted:
                    await channel.send(_clamp("📊 Player match and career statistics updated.", 1900))
            except Exception:
                logger.exception("Failed to persist player stats match_id=%s", session.state.id)
        except Exception:
            logger.exception("MATCH_FLOW fatal unhandled error match_id=%s", getattr(getattr(session, 'state', None), 'id', None))
            try:
                _log_state("fatal")
            except Exception:
                pass
            try:
                await channel.send(_clamp(
                    f"⚠️ Match simulation encountered a fatal error and stopped (Match ID: `{getattr(getattr(session,'state',None),'id','?')}`). "
                    "Please check Railway logs for `MATCH_FLOW fatal`."
                ))
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
            # Try league filter first (if provided), then fall back to all leagues for cross-league support
            matches = await fd.list_teams(query=q, league=league, limit=5)
            if not matches and league:
                matches = await fd.list_teams(query=q, league=None, limit=5)
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

        # If teams not provided or not found, fall back to interactive selection
        if home is None or away is None or (home and away and home.get("api_id") == away.get("api_id")):
            leagues = fd.list_leagues()
            from data.bundled import resolve_league_key
            default_league_key = resolve_league_key(league) if league else None

            selected: dict[str, Any] = {"home": home, "away": away}

            async def _send(inter: discord.Interaction, *, content: str, view: discord.ui.View | None = None):
                if inter.response.is_done():
                    await inter.edit_original_response(content=content, view=view)
                else:
                    await inter.response.edit_message(content=content, view=view)

            async def prompt_away_league(inter: discord.Interaction):
                async def on_away_league_pick(i2: discord.Interaction, lk: str):
                    await prompt_away_team(i2, lk)

                v = SimpleChoiceView(interaction.user.id)
                v.add_item(LeagueSelect(leagues, on_away_league_pick))
                await _send(inter, content=(
                    f"**Match Setup**\nHome: **{selected['home'].get('name')}** ({selected['home'].get('league_name','')})\n"
                    "Select the AWAY team's league (cross-league matches allowed)."
                ), view=v)

            async def prompt_away_team(inter: discord.Interaction, lk: str):
                teams = await fd.list_teams(league=lk, limit=25)
                if not teams:
                    await inter.response.send_message("Could not load teams for that league.", ephemeral=True)
                    return
                league_name = next((l['name'] for l in leagues if l['key'] == lk), lk)
                away_opts = [x for x in teams if not (selected.get("home") and int(x.get("api_id", 0)) == int(selected["home"].get("api_id", 0)))]

                async def on_away_pick(i2: discord.Interaction, team_id: int):
                    t = await fd.get_team(team_id)
                    if not t:
                        await i2.response.send_message("Team not found.", ephemeral=True)
                        return
                    if selected.get("home") and int(t.get("api_id", 0)) == int(selected["home"].get("api_id", 0)):
                        await i2.response.send_message("Away team must be different from home team.", ephemeral=True)
                        return
                    selected["away"] = t
                    await i2.response.edit_message(content=f"Match setup: **{selected['home'].get('name')}** vs **{t.get('name')}**\nStarting simulation...", view=None)
                    await self._launch_with_selection(interaction, mode.value, selected["home"], selected["away"], home_manager, away_manager)

                v = SimpleChoiceView(interaction.user.id)
                v.add_item(TeamSelect(away_opts, f"Select AWAY team ({league_name})", on_away_pick))
                # Allow changing league if needed
                async def change_away_league(i2: discord.Interaction):
                    await prompt_away_league(i2)
                # No extra button component needed; user can rerun /sim to restart. Keep simple.
                await _send(inter, content=(
                    f"**Match Setup**\nHome: **{selected['home'].get('name')}** ({selected['home'].get('league_name','')})\n"
                    f"Away league: **{league_name}**\nSelect the away team."
                ), view=v)

            async def prompt_home_team(inter: discord.Interaction, lk: str):
                teams = await fd.list_teams(league=lk, limit=25)
                if not teams:
                    await inter.response.send_message("Could not load teams for that league.", ephemeral=True)
                    return
                league_name = next((l['name'] for l in leagues if l['key'] == lk), lk)

                async def on_home_pick(i2: discord.Interaction, team_id: int):
                    t = await fd.get_team(team_id)
                    if not t:
                        await i2.response.send_message("Team not found.", ephemeral=True)
                        return
                    selected["home"] = t
                    # After home selected, pick away league (cross-league allowed)
                    await prompt_away_league(i2)

                v = SimpleChoiceView(interaction.user.id)
                v.add_item(TeamSelect(teams, f"Select HOME team ({league_name})", on_home_pick))
                await _send(inter, content=(
                    f"**Match Setup**\nHome league: **{league_name}**\nSelect the home team."
                ), view=v)

            async def prompt_home_league(inter: discord.Interaction):
                async def on_home_league_pick(i2: discord.Interaction, lk: str):
                    await prompt_home_team(i2, lk)

                v = SimpleChoiceView(interaction.user.id)
                v.add_item(LeagueSelect(leagues, on_home_league_pick))
                await _send(inter, content="**Match Setup**\nSelect the HOME team's league (cross-league matches allowed).", view=v)

            # Entry: if league arg provided, default to that for home but still allow cross-league for away
            if (not home) and default_league_key:
                # send an initial message then go to home team in that league
                view0 = SimpleChoiceView(interaction.user.id)
                view0.add_item(LeagueSelect(leagues, lambda i2, lk: prompt_home_team(i2, lk)))
                # If league provided, jump directly to home team selection
                teams = await fd.list_teams(league=default_league_key, limit=25)
                if teams:
                    league_name = next((l['name'] for l in leagues if l['key'] == default_league_key), default_league_key)
                    async def on_home_pick_direct(i2: discord.Interaction, team_id: int):
                        t = await fd.get_team(team_id)
                        if not t:
                            await i2.response.send_message("Team not found.", ephemeral=True)
                            return
                        selected["home"] = t
                        await prompt_away_league(i2)
                    v = SimpleChoiceView(interaction.user.id)
                    v.add_item(TeamSelect(teams, f"Select HOME team ({league_name})", on_home_pick_direct))
                    await interaction.followup.send(f"**Match Setup**\nHome league: **{league_name}** (from command)\nSelect the home team. Away team can be from any league.", view=v, ephemeral=True)
                    return
                # fallback to league pick
                await interaction.followup.send("**Match Setup**\nSelect the HOME team's league (cross-league matches allowed).", view=view0, ephemeral=True)
                return

            if home and not away:
                # We have home, need away - send prompt
                v = SimpleChoiceView(interaction.user.id)
                async def on_away_league_pick(i2: discord.Interaction, lk: str):
                    await prompt_away_team(i2, lk)
                v.add_item(LeagueSelect(leagues, on_away_league_pick))
                await interaction.followup.send(
                    f"**Match Setup**\nHome: **{home.get('name')}** ({home.get('league_name','')})\nSelect the AWAY team's league (cross-league matches allowed).",
                    view=v,
                    ephemeral=True,
                )
                return

            # Neither team resolved
            v = SimpleChoiceView(interaction.user.id)
            async def on_home_league_pick(i2: discord.Interaction, lk: str):
                await prompt_home_team(i2, lk)
            v.add_item(LeagueSelect(leagues, on_home_league_pick))
            await interaction.followup.send("**Match Setup**\nSelect the HOME team's league (cross-league matches allowed).", view=v, ephemeral=True)
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

        # Determine competition / match type / stadium
        competition, match_type = _competition_and_type(home, away)
        stadium = home.get("venue_name") or home.get("venue") or f"{home.get('name','Home')} Stadium"
        session.state.submission["competition"] = competition
        session.state.submission["match_type"] = match_type
        session.state.submission["stadium"] = stadium
        await session.save()

        setup_embed = build_match_setup_embed(session, competition=competition, match_type=match_type, stadium=stadium)

        if mode == "manager_vs_manager":
            # 20-minute initial deadline for both managers
            now = datetime.now(timezone.utc)
            deadline = now + timedelta(seconds=int(session.state.submission.get("initial_deadline_seconds") or 1200))
            session.state.submission["deadline_at"] = deadline.isoformat()
            session.state.submission["match_started"] = False
            await session.save()

            view = ManagerSetupView(self, session, timeout=None)
            try:
                setup_msg = await channel.send(embed=build_match_setup_embed(session), view=view)
                view.message = setup_msg
                session.state.message_id = setup_msg.id
                await session.save()
            except Exception:
                logger.exception("Failed to send manager setup embed")
                try:
                    await interaction.followup.send("Failed to post match setup embed in channel.", ephemeral=True)
                except Exception:
                    pass
                return

            # Notify managers (private DM best-effort + channel mention)
            deadline_ts = int(deadline.timestamp())
            notice = (
                f"**Manager vs Manager — Submissions Open**\n"
                f"{home.get('name')} vs {away.get('name')}\n"
                f"Both managers have 20 minutes to submit their starting lineup and tactics.\n"
                f"Deadline: <t:{deadline_ts}:R> (<t:{deadline_ts}:T>)\n"
                f"Please select your starting XI, choose your bench and submit your tactics before the deadline."
            )
            for uid in (hm_id, am_id):
                try:
                    user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                    await user.send(notice + f"\nMatch ID: `{session.state.id}`")
                except Exception:
                    pass
            try:
                await channel.send(_clamp(f"<@{hm_id}> <@{am_id}> " + notice, 1900))
            except Exception:
                pass

            try:
                await interaction.followup.send(_clamp(f"Match setup created: `{session.state.id}`. Waiting for manager submissions."), ephemeral=True)
            except Exception:
                pass

            self._start_deadline_watcher(session, view)
            return

        # Bot vs Bot: show setup embed then start
        try:
            msg = await channel.send(embed=setup_embed)
            session.state.message_id = msg.id
            await session.save()
        except Exception:
            logger.exception("Failed to send bot vs bot setup embed")

        try:
            await interaction.followup.send(_clamp(f"Match created: **{home.get('name')}** vs **{away.get('name')}**\nMatch ID: `{session.state.id}`"), ephemeral=True)
        except Exception:
            pass

        asyncio.create_task(self._run_match_flow(interaction, session))


async def setup(bot):
    await bot.add_cog(SimCog(bot))
